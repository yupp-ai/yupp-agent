"""MCP tools for GCP and Vercel log searching.

Provides tools for searching Google Cloud Logging and Vercel logs,
plus fetching GCP Monitoring alert metadata.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

from google.api_core.exceptions import ResourceExhausted, TooManyRequests
from google.cloud import logging as google_logging

from ypl.backend.config import settings
from ypl.backend.utils.parsing_utils import parse_rfc3339_timestamp
from ypl.backend.utils.redis_utils import RedisRateLimitWatcher
from ypl.mcp_common.shared_tool import shared_tool
from ypl.mcp_server.tools.gcp_alert_search import get_gcp_alert_metadata
from ypl.structured_logger import get_logger

logger = get_logger()

# Global: prevent hammering GCP logging read quota across MCP servers.
_GCP_LOGS_RATE_LIMIT_TTL_SECONDS = 60
_GCP_LOGS_RATE_LIMIT_TOPIC = "gcp_logs_read_requests"
_GCP_LOGS_RATE_LIMIT_IDENTIFIER = "global"
_GCP_LOGS_RATE_LIMIT_WATCHER = RedisRateLimitWatcher(
    ttl_seconds=_GCP_LOGS_RATE_LIMIT_TTL_SECONDS,
    topic=_GCP_LOGS_RATE_LIMIT_TOPIC,
)

# Hard timeout for the synchronous GCP Logging API call (run in a thread).
# Prevents the event loop from blocking indefinitely on slow or hung queries.
_GCP_LOGS_TIMEOUT_S = 30


_TimestampComparison = Literal["<=", ">=", "<", ">", "="]
_TIMESTAMP_COMPARISON_PATTERNS: dict[_TimestampComparison, tuple[re.Pattern[str], ...]] = {
    comparison: (
        re.compile(rf'\btimestamp\b\s*{re.escape(comparison)}\s*"([^"]+)"', flags=re.IGNORECASE),
        re.compile(rf"\btimestamp\b\s*{re.escape(comparison)}\s*'([^']+)'", flags=re.IGNORECASE),
    )
    for comparison in get_args(_TimestampComparison)
}


def _is_gcp_logging_rate_limited_error(error: Exception) -> bool:
    if isinstance(error, ResourceExhausted | TooManyRequests):
        return True

    return False


async def _is_gcp_logs_search_rate_limited() -> bool:
    try:
        return await _GCP_LOGS_RATE_LIMIT_WATCHER.is_rate_limited(_GCP_LOGS_RATE_LIMIT_IDENTIFIER)
    except Exception as e:
        logger.warning("GCP logs rate limit check failed", error=str(e))
        return False


async def _mark_gcp_logs_search_rate_limited() -> None:
    try:
        await _GCP_LOGS_RATE_LIMIT_WATCHER.set_rate_limited(_GCP_LOGS_RATE_LIMIT_IDENTIFIER)
    except Exception as e:
        logger.warning("Failed to set GCP logs rate limit in Redis", error=str(e))


def _extract_timestamp_comparison_value(query: str, comparison: _TimestampComparison) -> str | None:
    """Extract the raw `timestamp {comparison} ...` value from a Logging filter query.

    Returns the last matching value if multiple are present.
    """
    if not query:
        return None

    extracted: str | None = None
    for pattern in _TIMESTAMP_COMPARISON_PATTERNS[comparison]:
        matches = pattern.findall(query)
        if matches:
            extracted = matches[-1]

    if extracted is None:
        return None

    raw = extracted.strip()
    return raw or None


def _parse_query_timestamp_comparison(query: str, comparison: _TimestampComparison) -> datetime | None:
    """Extract a `timestamp {comparison} ...` bound from a GCP Logging filter query.

    Cloud Logging filters typically express timestamps as RFC3339 strings, e.g.:
    `timestamp <= "2026-01-25T12:34:56Z"`.
    """
    extracted = _extract_timestamp_comparison_value(query, comparison)
    if extracted is None:
        return None

    return parse_rfc3339_timestamp(extracted)


def _fetch_gcp_log_entries(
    client: google_logging.Client,
    filter_: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fetch GCP log entries synchronously.

    Intended to run inside asyncio.to_thread() so it does not block the event loop.
    """
    entries = client.list_entries(  # type: ignore[no-untyped-call]
        filter_=filter_,
        order_by="timestamp desc",
        page_size=max_results,
    )
    results = []
    for entry in entries:
        resource_data = None
        if entry.resource:
            resource_data = {
                "type": entry.resource.type,
                "labels": dict(entry.resource.labels) if entry.resource.labels else None,
            }
        results.append(
            {
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                "severity": entry.severity,
                "message": entry.payload,
                "trace": entry.trace,
                "labels": entry.labels,
                "resource": resource_data,
            }
        )
        if len(results) >= max_results:
            break
    return results


@shared_tool(
    requires_settings=("GCP_PROJECT_ID",),
    name="search_gcp_logs",
    description=(
        "Search Google Cloud Logging for Yupp MIND production logs. Use this to debug errors, "
        "trace requests, or investigate issues. Supports GCP logging filter syntax. "
        "By default, `hours_back` sets the lookback window. "
        'To use an explicit time range, include both `timestamp >= "..."` and `timestamp <= "..."` '
        "in the query (then `hours_back` is ignored)."
    ),
)
async def search_gcp_logs(
    query: str,
    hours_back: int = 2,
    max_results: int = 100,
) -> dict[str, Any]:
    """Search Google Cloud Logging for Yupp MIND production logs.

    By default, this tool injects an explicit time window filter:
    `timestamp >= start_time AND timestamp <= end_time`, and combines it with the provided
    filter as `time_filter AND (query)` (if `query` is non-empty).

    If the provided `query` includes a `timestamp <= "..."` (or single-quoted) clause, the
    tool will parse that RFC3339 timestamp and use it as `end_time` (in UTC). Otherwise it
    uses the current time as `end_time`. `start_time` is then computed as
    `end_time - timedelta(hours=hours_back)`.

    If the provided `query` includes BOTH `timestamp >= ...` and `timestamp <= ...`, the tool
    will not inject any additional time window (so `hours_back` will not further restrict results).

    Args:
        query: GCP Logging filter query (e.g. `severity=ERROR`, `labels.user_id=123`).
        hours_back: How many hours back to search when the query does not specify an explicit time range.
        max_results: Maximum number of results to return (default: 100)

    Returns:
        Dictionary containing matching log entries and the resolved time range.
    """
    try:
        if await _is_gcp_logs_search_rate_limited():
            return {
                "success": False,
                "error": "RATE_LIMITED",
                "retry_after_seconds": _GCP_LOGS_RATE_LIMIT_TTL_SECONDS,
                "query": query,
            }

        client = google_logging.Client(project=settings.GCP_PROJECT_ID)  # type: ignore[no-untyped-call]

        start_raw = _extract_timestamp_comparison_value(query, ">=")
        end_raw = _extract_timestamp_comparison_value(query, "<=")
        extracted_start_time = _parse_query_timestamp_comparison(query, ">=")
        extracted_end_time = _parse_query_timestamp_comparison(query, "<=")

        if start_raw is not None and end_raw is not None:
            full_filter = query
            time_range: dict[str, str | None] = {
                "source": "query",
                "start": extracted_start_time.isoformat() if extracted_start_time else None,
                "end": extracted_end_time.isoformat() if extracted_end_time else None,
                "start_raw": start_raw,
                "end_raw": end_raw,
            }
            logger.info("Using explicit timestamp bounds from query", time_range=time_range)
        else:
            # Calculate time range
            now_time = datetime.now(UTC)
            end_time = extracted_end_time or now_time
            if extracted_end_time is not None:
                logger.info("Using end_time extracted from query", end_time=end_time.isoformat())
            start_time = end_time - timedelta(hours=hours_back)

            # Build filter
            time_filter = f'timestamp>="{start_time.isoformat()}" AND timestamp<="{end_time.isoformat()}"'
            full_filter = f"{time_filter} AND ({query})" if query else time_filter
            time_range = {"source": "hours_back", "start": start_time.isoformat(), "end": end_time.isoformat()}

        logger.info("Searching GCP logs", filter=full_filter, max_results=max_results)

        # Execute search in a thread — the google-cloud-logging client is synchronous
        # and would block the event loop. A hard timeout prevents hung queries from
        # stalling the agent indefinitely.
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_fetch_gcp_log_entries, client, full_filter, max_results),
                timeout=_GCP_LOGS_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "GCP logs search timed out",
                filter=full_filter,
                timeout_s=_GCP_LOGS_TIMEOUT_S,
                query=query,
            )
            return {
                "success": False,
                "error": f"GCP logs search timed out after {_GCP_LOGS_TIMEOUT_S}s",
                "query": query,
            }

        logger.info("GCP logs search completed", results_count=len(results))

        return {
            "success": True,
            "count": len(results),
            "query": query,
            "time_range": time_range,
            "results": results,
        }

    except Exception as e:
        if _is_gcp_logging_rate_limited_error(e):
            logger.warning("GCP logs search rate limited; entering cooldown", error=str(e), query=query)
            await _mark_gcp_logs_search_rate_limited()
            return {
                "success": False,
                "error": "RATE_LIMITED",
                "retry_after_seconds": _GCP_LOGS_RATE_LIMIT_TTL_SECONDS,
                "query": query,
            }

        logger.warning("Error searching GCP logs", error=str(e), query=query)
        return {"success": False, "error": str(e), "query": query}


@shared_tool(
    requires_settings=("GCP_PROJECT_ID",),
    name="search_vercel_logs",
    description=(
        "Search Vercel logs imported via vercel-log-drain to GCP logging. Use this to debug frontend issues, "
        "investigate deployment problems, or trace Vercel-specific errors. "
        "By default, `hours_back` sets the lookback window. "
        'To use an explicit time range, include both `timestamp >= "..."` and `timestamp <= "..."` '
        "in the query (then `hours_back` is ignored)."
    ),
)
async def search_vercel_logs(
    query: str = "",
    hours_back: int = 2,
    max_results: int = 100,
) -> dict[str, Any]:
    """Search Vercel deployment logs via GCP Logging.

    Vercel logs are ingested into GCP Logging via `vercel-log-drain`. This tool prepends a
    service filter (`resource.labels.service_name="vercel-log-drain"`) and then delegates
    to `search_gcp_logs`, so it inherits the same time window injection behavior (including
    optional `timestamp <= ...` end-time extraction).

    Args:
        query: Additional GCP Logging filter query to AND with the Vercel service filter.
        hours_back: How many hours back to search when the query does not specify an explicit time range.
        max_results: Maximum number of results to return (default: 100)

    Returns:
        Dictionary containing matching Vercel log entries.
    """
    try:
        # Vercel logs are in GCP logging with this resource label
        vercel_filter = 'resource.labels.service_name="vercel-log-drain"'

        # Combine with user query if provided
        full_query = f"{vercel_filter} AND ({query})" if query else vercel_filter

        logger.info("Searching Vercel logs via GCP", query=full_query, hours_back=hours_back)

        # Use the GCP logs search function (access original fn since search_gcp_logs is decorated)
        result: dict[str, Any] = await search_gcp_logs.fn(
            query=full_query,
            hours_back=hours_back,
            max_results=max_results,
        )

        # Add context that these are Vercel logs
        if result.get("success"):
            result["source"] = "vercel"

        return result

    except Exception as e:
        logger.warning("Error searching Vercel logs", error=str(e), query=query)
        return {"success": False, "error": str(e), "query": query}


@shared_tool(
    requires_settings=("GCP_PROJECT_ID",),
    name="get_gcp_alert_details",
    description=(
        "Fetch GCP Monitoring alert (incident) metadata. "
        "Accepts either a full GCP alert console URL (e.g., "
        "https://console.cloud.google.com/monitoring/alerting/alerts/ALERT_ID) "
        "or just an alert ID (e.g., '0.o3gwwfx7rf2c'). "
        "Returns the raw alert metadata from the GCP Monitoring API including state, timing, "
        "resource info, policy details, and other alert properties."
    ),
)
async def get_gcp_alert_details(alert_input: str) -> dict[str, Any]:
    """Fetch GCP Monitoring alert metadata.

    Args:
        alert_input: Either a full GCP alert console URL (e.g.,
            https://console.cloud.google.com/monitoring/alerting/alerts/ALERT_ID)
            or just an alert ID (e.g., "0.o3gwwfx7rf2c"). Always uses settings.GCP_PROJECT_ID as the project.

    Returns:
        Dictionary containing:
        - success: Whether the request succeeded
        - alert_id: The extracted alert ID
        - project_id: The project ID used (from settings.GCP_PROJECT_ID)
        - metadata: The raw alert metadata from GCP API
        - error: Error message if success is False
    """
    return await get_gcp_alert_metadata(alert_input)
