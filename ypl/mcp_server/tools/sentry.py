"""Sentry Proxy Tools.

Proxy tools for the Sentry REST API, enabling headless environments
(CI, automated agents, CLI) to query Sentry issues via a shared org-level auth token.
"""

import asyncio
import re
from typing import Any

import aiohttp

from ypl.backend.config import settings
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

_SENTRY_ORG = "bsl-ai"
_SENTRY_BASE_URL = "https://us.sentry.io/api/0/"

# Lazy-initialized singleton session (guarded by _session_lock)
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

# Short IDs contain a dash and at least one letter, e.g. "YUPP-HEAD-QKE"
_SHORT_ID_PATTERN = re.compile(r"^[A-Z][\w-]*-[A-Z0-9]+$", re.IGNORECASE)

# Validation patterns for user-supplied path segments
_HEX32_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_TAG_KEY_PATTERN = re.compile(r"^[\w.-]+$")

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-vercel-proxy-signature",
        "x-vercel-oidc-token",
        "x-vercel-sc-headers",
        "x-kpsdk-ct",
        "x-kpsdk-h",
        "x-kpsdk-cd",
        "x-is-human",
    }
)


def _get_auth_token() -> str:
    token = settings.SENTRY_AUTH_TOKEN
    if not token:
        raise ValueError("SENTRY_AUTH_TOKEN is not configured")
    return token


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                base_url=_SENTRY_BASE_URL,
                headers={
                    "Authorization": f"Bearer {_get_auth_token()}",
                    "Content-Type": "application/json",
                },
            )
        return _session


async def close_sentry_session() -> None:
    """Close the shared aiohttp session. Call from the application shutdown hook."""
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None


async def _sentry_get(path: str) -> Any:
    """Make an authenticated GET request to the Sentry API."""
    session = await _get_session()
    async with session.get(path.lstrip("/")) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise aiohttp.ClientResponseError(
                request_info=resp.request_info,
                history=resp.history,
                status=resp.status,
                message=f"Sentry API error: {resp.status} - {body}",
            )
        return await resp.json()


# ---------------------------------------------------------------------------
# Markdown formatters
#
# These extract a subset of the Sentry API response to keep MCP tool output
# token-efficient. The raw responses are very large (10k+ tokens as JSON).
# Callers needing data beyond what's formatted here should use a more specific
# tool (e.g. get_sentry_issue_tag_values for tag distributions) or request a
# specific event_id for full event detail.
# ---------------------------------------------------------------------------


def _fmt_issue(data: dict[str, Any]) -> str:
    """Format a Sentry issue response as markdown.

    Kept: core triage fields (status, priority, assignee, culprit, counts, timestamps),
    metadata filename/function, and tag key names.

    Dropped (for token efficiency):
    - stats: per-interval event count histograms (large arrays)
    - activity: issue state-change log (assigned, resolved, etc.)
    - participants/seenBy: lists of users who viewed the issue
    - pluginActions/pluginContexts/pluginIssues: integration-specific data
    - annotations: external links from integrations (e.g. Linear, Jira)
    - statusDetails/subscriptionDetails: subscription/snooze metadata
    - shareId: public sharing identifier
    - isBookmarked/isSubscribed/hasSeen: per-user UI state
    """
    assigned = data.get("assignedTo")
    assigned_str = "Unassigned"
    if isinstance(assigned, dict):
        assigned_str = f"{assigned.get('name', '?')} ({assigned.get('email', '?')})"

    lines = [
        f"# {data.get('shortId')} — {data.get('title')}",
        "",
        f"**URL**: {data.get('permalink')}",
        f"**Status**: {data.get('status')} ({data.get('substatus')})",
        f"**Level**: {data.get('level')} | **Priority**: {data.get('priority')}",
        f"**Platform**: {data.get('platform')} | **Project**: {data.get('project', {}).get('slug')}",
        f"**Assigned To**: {assigned_str}",
        f"**Culprit**: {data.get('culprit')}",
        f"**Occurrences**: {data.get('count')} | **Users**: {data.get('userCount')}",
        f"**First Seen**: {data.get('firstSeen')} | **Last Seen**: {data.get('lastSeen')}",
    ]

    meta = data.get("metadata", {})
    if meta.get("filename"):
        lines.append(f"**File**: {meta['filename']} ({meta.get('function', '?')})")

    tags = data.get("tags", [])
    if tags:
        lines.append("")
        lines.append("## Tags")
        lines.append(", ".join(t["key"] for t in tags))

    return "\n".join(lines)


def _fmt_frame(frame: dict[str, Any]) -> str:
    """Format a single stacktrace frame."""
    fn = frame.get("function") or "?"
    filename = frame.get("filename", "?")
    line = frame.get("lineNo", "?")
    col = frame.get("colNo", "?")
    return f"  {filename}:{line}:{col} ({fn})"


def _fmt_event(data: dict[str, Any]) -> str:
    """Format a Sentry event response as markdown.

    Kept: event ID/title/date/group, exception entries (type, value, mechanism,
    stacktrace with source context for inApp frames), request method/URL/headers
    (sensitive ones stripped), all tags with values, and all contexts.

    Dropped (for token efficiency):
    - rawStacktrace: unprocessed version of each stacktrace (redundant with stacktrace)
    - non-inApp frame details: trimmed to filename/lineNo/function only (library frames)
    - request body/data/cookies/env: potentially large and often contain PII
    - sdk: SDK name/version/integrations metadata
    - fingerprint/groupingConfig: issue grouping internals
    - _meta: field-level metadata annotations from Sentry
    - message entry: redundant with exception title
    - sensitive headers: authorization, cookie, set-cookie, vercel proxy signatures,
      kasada bot-detection tokens (see _SENSITIVE_HEADERS)
    """
    lines = [
        f"# Event {data.get('eventID')}",
        "",
        f"**Title**: {data.get('title')}",
        f"**Date**: {data.get('dateCreated')}",
        f"**Group**: {data.get('groupID')}",
    ]

    # Exception entries
    for entry in data.get("entries", []):
        if entry.get("type") == "exception":
            for exc in entry.get("data", {}).get("values", []):
                lines.append("")
                lines.append(f"## {exc.get('type', 'Error')}: {exc.get('value', '')}")
                lines.append(
                    f"Mechanism: {exc.get('mechanism', {}).get('type', '?')} "
                    f"(handled={exc.get('mechanism', {}).get('handled', '?')})"
                )

                stacktrace = exc.get("stacktrace", {})
                frames = stacktrace.get("frames", [])
                if frames:
                    lines.append("")
                    lines.append("### Stacktrace")
                    for frame in frames:
                        prefix = "→" if frame.get("inApp") else " "
                        lines.append(f"{prefix} {_fmt_frame(frame)}")
                        # Include source context for inApp frames
                        if frame.get("inApp") and frame.get("context"):
                            for ctx_line_no, ctx_text in frame["context"]:
                                marker = ">>>" if ctx_line_no == frame.get("lineNo") else "   "
                                lines.append(f"    {marker} {ctx_line_no} | {ctx_text}")

        elif entry.get("type") == "request":
            req = entry.get("data", {})
            lines.append("")
            lines.append(f"## Request: {req.get('method')} {req.get('url')}")
            headers = req.get("headers", [])
            kept = [[k, v] for k, v in headers if k.lower() not in _SENSITIVE_HEADERS]
            if kept:
                lines.append("")
                for k, v in kept:
                    lines.append(f"  {k}: {v}")

    # Tags
    tags = data.get("tags", [])
    if tags:
        lines.append("")
        lines.append("## Tags")
        lines.extend(f"**{t.get('key')}**: {t.get('value')}" for t in tags)

    # Contexts
    contexts = data.get("contexts", {})
    if contexts:
        lines.append("")
        lines.append("## Contexts")
        for ctx_name, ctx_data in contexts.items():
            ctx_items = {k: v for k, v in ctx_data.items() if k != "type" and v is not None}
            if ctx_items:
                lines.append(f"**{ctx_name}**: {', '.join(f'{k}={v}' for k, v in ctx_items.items())}")

    return "\n".join(lines)


def _fmt_tag_values(issue_id: str, tag_key: str, data: list[dict[str, Any]]) -> str:
    """Format tag values as markdown.

    Kept: value, count, firstSeen, lastSeen for each tag value.

    Dropped: key (redundant — same for all entries), id, name (same as value),
    query parameters, and any internal Sentry metadata.
    """
    lines = [f"# Tag `{tag_key}` for issue {issue_id}", ""]
    lines.extend(
        f"- **{v.get('value')}**: {v.get('count')} (first={v.get('firstSeen')}, last={v.get('lastSeen')})" for v in data
    )
    return "\n".join(lines)


def _fmt_trace(trace_id: str, data: dict[str, Any]) -> str:
    """Format trace details as markdown.

    Kept: transaction name, project slug, and span ID for each transaction;
    title, project slug, and event ID for each orphan error.

    Dropped: individual span timing (start_timestamp, timestamp), trace parent/child
    relationships, span data/tags, performance measurements, and full event payloads
    within transactions. Use get_sentry_issue_details with a specific event_id for
    full event detail.
    """
    lines = [f"# Trace {trace_id}", ""]
    txns = data.get("transactions", [])
    if txns:
        lines.append(f"## Transactions ({len(txns)})")
        lines.extend(
            f"- {t.get('transaction', '?')} [{t.get('project_slug', '?')}] span={t.get('span_id', '?')}" for t in txns
        )
    errors = data.get("orphan_errors", [])
    if errors:
        lines.append(f"## Orphan Errors ({len(errors)})")
        lines.extend(
            f"- {e.get('title', '?')} [{e.get('project_slug', '?')}] event={e.get('event_id', '?')}" for e in errors
        )
    if not txns and not errors:
        lines.append("No transactions or errors found for this trace.")
    return "\n".join(lines)


def _fmt_breadcrumbs(issue_id: str, event_id: str | None, crumbs: list[dict[str, Any]]) -> str:
    """Format breadcrumbs as markdown.

    Kept: timestamp, category, level, message for each breadcrumb.

    Dropped: breadcrumb data dict (structured payload — can be large for HTTP
    breadcrumbs with full request/response info), event_id within each breadcrumb,
    and type field (redundant with category in most cases).
    """
    lines = [f"# Breadcrumbs for {issue_id} (event={event_id})", f"Count: {len(crumbs)}", ""]
    for b in crumbs:
        ts = b.get("timestamp", "?")
        cat = b.get("category", "?")
        level = b.get("level", "?")
        msg = b.get("message") or ""
        lines.append(f"[{ts}] **{cat}** ({level}): {msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


async def _resolve_issue_id(issue_id: str) -> str:
    """Resolve a Sentry short ID (e.g. YUPP-HEAD-QKE) to a numerical group ID.

    Passes through numerical IDs as-is.
    """
    if _SHORT_ID_PATTERN.match(issue_id):
        data = await _sentry_get(f"/organizations/{_SENTRY_ORG}/shortids/{issue_id}/")
        resolved = str(data["groupId"])
        logger.info("Resolved Sentry short ID", short_id=issue_id, group_id=resolved)
        return resolved
    return issue_id


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


async def get_sentry_issue_details(
    issue_id: str,
    event_id: str | None = None,
    project_slug: str = "",
) -> dict[str, Any]:
    """Get Sentry issue details, optionally for a specific event.

    Args:
        issue_id: Numerical issue ID or short ID (e.g. YUPP-HEAD-QKE).
        event_id: Optional event ID to fetch a specific event instead of the issue summary.
        project_slug: Sentry project slug. Not currently used — all endpoints here are
            issue-scoped (/issues/{id}/...) so Sentry resolves the project internally.
            Kept in the signature so the MCP schema exposes it for future project-scoped
            endpoints (e.g. issue search).

    Returns:
        Dictionary with success flag and markdown-formatted data.
    """
    try:
        resolved_id = await _resolve_issue_id(issue_id)
        if event_id:
            data = await _sentry_get(f"/issues/{resolved_id}/events/{event_id}/")
            return {"success": True, "issue_id": resolved_id, "data": _fmt_event(data)}
        data = await _sentry_get(f"/issues/{resolved_id}/")
        return {"success": True, "issue_id": resolved_id, "data": _fmt_issue(data)}
    except Exception as e:
        logger.warning("Error fetching Sentry issue details", issue_id=issue_id, error=str(e))
        return {"success": False, "error": str(e), "issue_id": issue_id}


async def get_sentry_issue_tag_values(
    issue_id: str,
    tag_key: str,
) -> dict[str, Any]:
    """Get tag value distribution for a Sentry issue.

    Args:
        issue_id: Numerical issue ID or short ID.
        tag_key: Tag key to inspect (e.g. "browser", "os", "environment").

    Returns:
        Dictionary with success flag and markdown-formatted tag values.
    """
    if not _TAG_KEY_PATTERN.match(tag_key):
        return {"success": False, "error": "Invalid tag key format", "issue_id": issue_id, "tag_key": tag_key}
    try:
        resolved_id = await _resolve_issue_id(issue_id)
        data = await _sentry_get(f"/issues/{resolved_id}/tags/{tag_key}/values/")
        return {
            "success": True,
            "issue_id": resolved_id,
            "tag_key": tag_key,
            "data": _fmt_tag_values(resolved_id, tag_key, data),
        }
    except Exception as e:
        logger.warning("Error fetching Sentry tag values", issue_id=issue_id, tag_key=tag_key, error=str(e))
        return {"success": False, "error": str(e), "issue_id": issue_id, "tag_key": tag_key}


async def get_sentry_trace_details(
    trace_id: str,
) -> dict[str, Any]:
    """Get trace details by 32-char hex trace ID.

    Args:
        trace_id: The 32-character hex trace ID.

    Returns:
        Dictionary with success flag and markdown-formatted trace data.
    """
    if not _HEX32_PATTERN.match(trace_id):
        return {"success": False, "error": "Invalid trace ID format (expected 32-char hex)", "trace_id": trace_id}
    try:
        data = await _sentry_get(f"/organizations/{_SENTRY_ORG}/events-trace/{trace_id}/")
        return {"success": True, "trace_id": trace_id, "data": _fmt_trace(trace_id, data)}
    except Exception as e:
        logger.warning("Error fetching Sentry trace details", trace_id=trace_id, error=str(e))
        return {"success": False, "error": str(e), "trace_id": trace_id}


async def get_sentry_breadcrumbs(
    issue_id: str,
    event_id: str | None = None,
    project_slug: str = "",
) -> dict[str, Any]:
    """Get breadcrumbs for an issue's latest or specific event.

    Fetches the event and extracts breadcrumb entries. This is an augmented tool
    not available in the Sentry MCP server.

    Args:
        issue_id: Numerical issue ID or short ID.
        event_id: Optional event ID. If not provided, fetches the latest event.
        project_slug: Sentry project slug. Not currently used — all endpoints here are
            issue-scoped (/issues/{id}/...) so Sentry resolves the project internally.
            Kept in the signature so the MCP schema exposes it for future project-scoped
            endpoints (e.g. issue search).

    Returns:
        Dictionary with success flag and markdown-formatted breadcrumbs.
    """
    try:
        resolved_id = await _resolve_issue_id(issue_id)

        if event_id:
            event = await _sentry_get(f"/issues/{resolved_id}/events/{event_id}/")
        else:
            event = await _sentry_get(f"/issues/{resolved_id}/events/latest/")

        breadcrumbs: list[dict[str, Any]] = []
        for entry in event.get("entries", []):
            if entry.get("type") == "breadcrumbs":
                breadcrumbs.extend(entry.get("data", {}).get("values", []))

        actual_event_id = event.get("eventID", event_id)
        return {
            "success": True,
            "issue_id": resolved_id,
            "event_id": actual_event_id,
            "data": _fmt_breadcrumbs(resolved_id, actual_event_id, breadcrumbs),
        }
    except Exception as e:
        logger.warning("Error fetching Sentry breadcrumbs", issue_id=issue_id, error=str(e))
        return {"success": False, "error": str(e), "issue_id": issue_id}


# ---------------------------------------------------------------------------
# MCP tool registrations
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="get_sentry_issue_details",
    description=(
        "Get Sentry issue details with optional specific event. "
        "Accepts numerical issue IDs or short IDs (e.g. YUPP-HEAD-QKE). "
        "Returns issue metadata, latest event stacktrace, and tags."
    ),
)
async def mcp_get_sentry_issue_details(
    issue_id: str,
    event_id: str | None = None,
    project_slug: str = "",
) -> dict[str, Any]:
    """Get Sentry issue details.

    Args:
        issue_id: Numerical issue ID or short ID (e.g. YUPP-HEAD-QKE).
        event_id: Optional event ID for a specific event.
        project_slug: Sentry project slug (currently unused — see get_sentry_issue_details).
    """
    return await get_sentry_issue_details(issue_id, event_id, project_slug)


@mcp_server.tool(
    name="get_sentry_issue_tag_values",
    description=(
        "Get tag value distribution for a Sentry issue. "
        "Returns tag values with counts for the specified tag key "
        "(e.g. browser, os, environment, server_name)."
    ),
)
async def mcp_get_sentry_issue_tag_values(
    issue_id: str,
    tag_key: str,
) -> dict[str, Any]:
    """Get tag value distribution for a Sentry issue.

    Args:
        issue_id: Numerical issue ID or short ID.
        tag_key: Tag key to inspect (e.g. browser, os, environment).
    """
    return await get_sentry_issue_tag_values(issue_id, tag_key)


@mcp_server.tool(
    name="get_sentry_trace_details",
    description=(
        "Get trace details by 32-char hex trace ID. "
        "Returns trace spans and transactions for distributed tracing analysis."
    ),
)
async def mcp_get_sentry_trace_details(
    trace_id: str,
) -> dict[str, Any]:
    """Get trace details by trace ID.

    Args:
        trace_id: The 32-character hex trace ID.
    """
    return await get_sentry_trace_details(trace_id)


@mcp_server.tool(
    name="get_sentry_breadcrumbs",
    description=(
        "Get breadcrumbs for a Sentry issue's latest or specific event. "
        "Returns timestamped breadcrumb entries with categories and messages. "
        "This is an augmented tool not available in the native Sentry MCP server."
    ),
)
async def mcp_get_sentry_breadcrumbs(
    issue_id: str,
    event_id: str | None = None,
    project_slug: str = "",
) -> dict[str, Any]:
    """Get breadcrumbs for a Sentry issue event.

    Args:
        issue_id: Numerical issue ID or short ID.
        event_id: Optional event ID. If not provided, uses the latest event.
        project_slug: Sentry project slug (currently unused — see get_sentry_breadcrumbs).
    """
    return await get_sentry_breadcrumbs(issue_id, event_id, project_slug)
