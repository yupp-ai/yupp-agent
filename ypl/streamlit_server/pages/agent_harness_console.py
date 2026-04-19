"""Agent Harness Console — browse agents, sessions, messages, and feedbacks."""

from __future__ import annotations
import html
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.llm.constants import LINEAR_TO_SLACK_ID
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import (
    Agent,
    AgentArtifact,
    AgentArtifactType,
    AgentExecutorType,
    AgentFeedback,
    AgentSession,
    AgentSessionMessage,
    AgentSessionTrigger,
)
from ypl.streamlit_server.auth import is_auth_configured, require_auth
from ypl.streamlit_server.permissions import Permission, get_current_user_email, has_permission
from ypl.structured_logger import get_logger

logger = get_logger()

st.set_page_config(page_title="Agent Harness Console", page_icon="🤖", layout="wide")
require_auth()

# Only check permission when auth is configured (skip in local dev mode)
if is_auth_configured() and not has_permission(Permission.AGENT_HARNESS_ADMIN):
    st.error("You do not have permission to view the Agent Harness Console.")
    st.stop()

_current_email = get_current_user_email()
_current_username = _current_email.split("@")[0] if _current_email else None

# ── Timezone helper ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _to_local(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_PACIFIC)
    return local.strftime(f"%Y-%m-%d %H:%M:%S {local.strftime('%Z')}")


def _time_ago(dt: datetime | None) -> str:
    """Return a human-readable relative time string like '3 min ago', '2 hr ago', '5 days ago'."""
    if dt is None:
        return ""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return f"{months} mo ago"


# ── Slack user-name resolution ────────────────────────────────────────────────

# Reverse map: Slack ID → short name (from the LINEAR_TO_SLACK_ID mapping).
# For duplicate IDs, prefer the longer (more descriptive) name.
_SLACK_ID_TO_NAME: dict[str, str] = {}
for _name, _sid in LINEAR_TO_SLACK_ID.items():
    if _sid not in _SLACK_ID_TO_NAME or len(_name) > len(_SLACK_ID_TO_NAME[_sid]):
        _SLACK_ID_TO_NAME[_sid] = _name


def _first_available_agent_bot_token() -> str | None:
    """Return the first configured SAG agent bot token, or None.

    Used only for nice-to-have Slack lookups (e.g., display-name resolution).
    """
    for key, value in os.environ.items():
        if key.startswith("SLACK_AGENT_GATEWAY_") and key.endswith("_BOT_TOKEN") and value:
            return value
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_slack_display_name(user_id: str) -> str | None:
    """Fetch display name from Slack API for user IDs not in the hardcoded map."""
    bot_token = _first_available_agent_bot_token()
    if not bot_token:
        return None
    try:
        from slack_sdk.web import WebClient

        client = WebClient(token=bot_token, timeout=3)
        response = client.users_info(user=user_id)
        profile = response["user"]["profile"]
        name = profile.get("display_name") or profile.get("real_name")
        return str(name) if name else None
    except Exception:
        logger.warning("Failed to fetch Slack display name for user", user_id=user_id, exc_info=True)
        return None


def _resolve_slack_user_name(user_id: str) -> str:
    """Resolve a Slack user ID to a human-readable name.

    Checks the hardcoded mapping first, then falls back to the Slack API (cached for 1 hr).
    """
    return _SLACK_ID_TO_NAME.get(user_id) or _fetch_slack_display_name(user_id) or user_id


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_all_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).order_by(col(Agent.name)))
        return list(result.all())


@retry_db
async def fetch_agent_stats() -> dict[uuid.UUID, dict[str, Any]]:
    """Fetch per-agent aggregate stats: total sessions, total messages, last active."""
    async with get_async_session_read_replica() as session:
        # Total sessions per agent
        sess_q = select(
            col(AgentSession.agent_id),
            func.count(col(AgentSession.agent_session_id)).label("total_sessions"),
        ).group_by(col(AgentSession.agent_id))
        sess_result = await session.exec(sess_q)
        sess_rows = list(sess_result.all())

        # Total messages + last active per agent
        msg_q = (
            select(
                col(AgentSession.agent_id),
                func.count(col(AgentSessionMessage.agent_session_message_id)).label("total_messages"),
                func.max(col(AgentSessionMessage.created_at)).label("last_active"),
            )
            .join(AgentSession, col(AgentSessionMessage.agent_session_id) == col(AgentSession.agent_session_id))
            .group_by(col(AgentSession.agent_id))
        )
        msg_result = await session.exec(msg_q)
        msg_rows = list(msg_result.all())

    stats: dict[uuid.UUID, dict[str, Any]] = {}
    for agent_id, total_sessions in sess_rows:
        stats.setdefault(agent_id, {})["total_sessions"] = total_sessions
    for agent_id, total_messages, last_active in msg_rows:
        stats.setdefault(agent_id, {})["total_messages"] = total_messages
        stats.setdefault(agent_id, {})["last_active"] = last_active

    return stats


@retry_db
async def fetch_single_session(session_id: uuid.UUID) -> AgentSession | None:
    """Fetch a single session with its agent and messages eagerly loaded."""
    async with get_async_session_read_replica() as session:
        query = (
            select(AgentSession)
            .options(
                selectinload(AgentSession.agent),  # type: ignore[arg-type]
                selectinload(AgentSession.messages),  # type: ignore[arg-type]
            )
            .where(col(AgentSession.agent_session_id) == session_id)
        )
        result = await session.exec(query)
        return result.one_or_none()


@retry_db
async def fetch_session_artifacts(session_id: uuid.UUID) -> list[AgentArtifact]:
    """Fetch all artifacts associated with a session."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact)
            .where(col(AgentArtifact.deleted_at).is_(None))
            .where(col(AgentArtifact.agent_session_id) == session_id)
            .order_by(col(AgentArtifact.created_at).asc())
        )
        result = await session.exec(stmt)
        return list(result.all())


@retry_db
async def fetch_distinct_slack_agent_names() -> list[str]:
    """Return sorted list of distinct slack_agent_name values from session context."""
    async with get_async_session_read_replica() as session:
        slack_name_col = AgentSession.context["slack_agent_name"].as_string()  # type: ignore[index]
        result = await session.exec(
            select(slack_name_col).where(slack_name_col.isnot(None)).group_by(slack_name_col).order_by(slack_name_col)
        )
        return [name for name in result.all() if name]


@retry_db
async def fetch_sessions(
    agent_name: str | None = None,
    session_id_str: str | None = None,
    sort_by: str = "last_message",
    limit: int = 50,
    date_from: date | None = None,
    date_to: date | None = None,
    trigger: str | None = None,
    slack_agent_name: str | None = None,
    creator_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return sessions with aggregate stats, sorted as requested.

    Uses SQL aggregates for message_count and last_message_time instead of
    loading all messages into memory.
    """
    async with get_async_session_read_replica() as session:
        # Compute aggregates via SQL — no need to load individual messages
        msg_count = func.count(col(AgentSessionMessage.agent_session_message_id)).label("message_count")
        last_msg = func.max(col(AgentSessionMessage.created_at)).label("last_message_time")

        # Correlated scalar subquery: first 3 USER messages (each truncated to 100 chars, newlines removed)
        # Uses ARRAY(SELECT ...) to collect first 3, then array_to_string to join with |||
        from sqlalchemy import literal_column

        first_user_msgs_subq: Any = literal_column(
            "(SELECT array_to_string(ARRAY("
            "SELECT replace(left(content, 100), E'\\n', ' ') "
            "FROM agent_session_messages "
            "WHERE agent_session_id = agent_sessions.agent_session_id "
            "AND role = 'USER' "
            "AND content IS NOT NULL "
            "ORDER BY turn_number "
            "LIMIT 3"
            "), '|||'))"
        ).label("first_user_messages")

        query = (
            select(AgentSession, msg_count, last_msg, first_user_msgs_subq)
            .outerjoin(
                AgentSessionMessage,
                col(AgentSession.agent_session_id) == col(AgentSessionMessage.agent_session_id),
            )
            .options(selectinload(AgentSession.agent))  # type: ignore[arg-type]
            .group_by(col(AgentSession.agent_session_id))
        )

        if agent_name:
            query = query.join(Agent).where(col(Agent.name) == agent_name)
        if trigger:
            query = query.where(col(AgentSession.trigger) == AgentSessionTrigger(trigger))
        if slack_agent_name:
            query = query.where(
                AgentSession.context["slack_agent_name"].as_string() == slack_agent_name  # type: ignore[index]
            )
        if creator_user_id:
            query = query.where(col(AgentSession.creator_user_id) == creator_user_id)
        if session_id_str:
            try:
                sid = uuid.UUID(session_id_str.strip())
                query = query.where(col(AgentSession.agent_session_id) == sid)
            except ValueError:
                pass  # ignore bad UUID

        # Date range filter at DB level
        if date_from:
            query = query.where(
                col(AgentSession.created_at) >= datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
            )
        if date_to:
            next_day = date_to + timedelta(days=1)
            query = query.where(
                col(AgentSession.created_at) < datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC)
            )

        # Sort and limit at DB level
        if sort_by == "last_message":
            query = query.order_by(last_msg.desc().nulls_last())
        elif sort_by == "message_count":
            query = query.order_by(msg_count.desc())
        else:
            query = query.order_by(col(AgentSession.created_at).desc())
        query = query.limit(limit)

        result = await session.exec(query)
        rows = list(result.all())

    out: list[dict[str, Any]] = []
    for row in rows:
        s, message_count, last_message_time, first_user_messages_raw = row[0], row[1], row[2], row[3]
        # Split the ||| delimited first 3 user messages
        first_user_messages = (
            [m.strip() for m in first_user_messages_raw.split("|||") if m.strip()] if first_user_messages_raw else []
        )
        # Extract slack metadata from session context JSONB
        ctx = s.context or {}
        out.append(
            {
                "session": s,
                "agent_name": s.agent.display_name if s.agent else "?",
                "agent_slug": s.agent.name if s.agent else "?",
                "last_message_time": last_message_time or s.created_at,
                "created_at": s.created_at,
                "message_count": message_count,
                "first_user_messages": first_user_messages,
                "slack_user_id": ctx.get("slack_user_id"),
                "slack_agent_name": ctx.get("slack_agent_name"),
                "parent_session_id": str(s.parent_session_id) if s.parent_session_id else None,
                "session_id": str(s.agent_session_id),
            }
        )

    return out


@retry_db
async def fetch_child_sessions(parent_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    """Fetch all sessions whose parent_session_id is in the given list, with aggregate stats."""
    if not parent_ids:
        return []
    async with get_async_session_read_replica() as session:
        msg_count = func.count(col(AgentSessionMessage.agent_session_message_id)).label("message_count")
        last_msg = func.max(col(AgentSessionMessage.created_at)).label("last_message_time")
        from sqlalchemy import literal_column

        first_user_msgs_subq: Any = literal_column(
            "(SELECT array_to_string(ARRAY("
            "SELECT replace(left(content, 100), E'\\n', ' ') "
            "FROM agent_session_messages "
            "WHERE agent_session_id = agent_sessions.agent_session_id "
            "AND role = 'USER' "
            "AND content IS NOT NULL "
            "ORDER BY turn_number "
            "LIMIT 3"
            "), '|||'))"
        ).label("first_user_messages")

        query = (
            select(AgentSession, msg_count, last_msg, first_user_msgs_subq)
            .outerjoin(
                AgentSessionMessage,
                col(AgentSession.agent_session_id) == col(AgentSessionMessage.agent_session_id),
            )
            .options(selectinload(AgentSession.agent))  # type: ignore[arg-type]
            .where(col(AgentSession.parent_session_id).in_(parent_ids))
            .group_by(col(AgentSession.agent_session_id))
            .order_by(col(AgentSession.created_at))
        )

        result = await session.exec(query)
        rows = list(result.all())

    out: list[dict[str, Any]] = []
    for row in rows:
        s, message_count, last_message_time, first_user_messages_raw = row[0], row[1], row[2], row[3]
        first_user_messages = (
            [m.strip() for m in first_user_messages_raw.split("|||") if m.strip()] if first_user_messages_raw else []
        )
        ctx = s.context or {}
        out.append(
            {
                "session": s,
                "agent_name": s.agent.display_name if s.agent else "?",
                "agent_slug": s.agent.name if s.agent else "?",
                "last_message_time": last_message_time or s.created_at,
                "created_at": s.created_at,
                "message_count": message_count,
                "first_user_messages": first_user_messages,
                "slack_user_id": ctx.get("slack_user_id"),
                "slack_agent_name": ctx.get("slack_agent_name"),
                "parent_session_id": str(s.parent_session_id) if s.parent_session_id else None,
                "session_id": str(s.agent_session_id),
            }
        )
    return out


@retry_db
async def fetch_feedbacks(limit: int = 100) -> list[dict[str, Any]]:
    async with get_async_session_read_replica() as session:
        query = (
            select(AgentFeedback)
            .options(
                selectinload(AgentFeedback.message),  # type: ignore[arg-type]
                selectinload(AgentFeedback.session),  # type: ignore[arg-type]
            )
            .order_by(col(AgentFeedback.created_at).desc())
            .limit(limit)
        )
        result = await session.exec(query)
        rows = list(result.all())

    out: list[dict[str, Any]] = []
    for fb in rows:
        msg_content = fb.message.content[:200] if fb.message and fb.message.content else None
        out.append(
            {
                "feedback": fb,
                "session_id": str(fb.agent_session_id),
                "message_id": str(fb.agent_session_message_id) if fb.agent_session_message_id else None,
                "message_preview": msg_content,
            }
        )
    return out


# ── Role styling ─────────────────────────────────────────────────────────────


def _estimate_container_height(content: str, max_height: int = 350, min_height: int = 80) -> int:
    """Estimate a reasonable container height based on content length.

    Caps at max_height (~10 visible lines of markdown).
    """
    line_count = content.count("\n") + 1
    # Also account for line wrapping (assume ~100 chars per visible line)
    wrapped_lines = max(line_count, len(content) / 100)
    height = int(wrapped_lines * 28 + 40)  # ~28px per rendered line + padding
    return max(min_height, min(max_height, height))


_ROLE_COLORS: dict[str, str] = {
    "USER": "#1976d2",
    "AGENT": "#388e3c",
    "SYSTEM": "#757575",
}


def _role_badge(role: str) -> str:
    color = _ROLE_COLORS.get(role, "#555")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:0.8em;">{role}</span>'
    )


# ── Event overview helpers ───────────────────────────────────────────────────


def _extract_event_summary(event: dict[str, Any]) -> tuple[str, str, str, str, str, bool]:
    """Return (emoji, type_label, key_msg, sub_detail, more_info, is_tool_call) for a single raw event."""
    etype = event.get("type", "?")
    subtype = event.get("subtype", "")
    type_label = f"{etype}/{subtype}" if subtype else etype
    key_msg = ""
    sub_detail = ""  # secondary line shown below key_msg in the same cell
    more_info = ""
    emoji = ""
    is_tool_call = False

    if etype == "system":
        if subtype in ("hook_started", "hook_response"):
            emoji = "⚙️"
            hook = event.get("hook_name", "?")
            key_msg = f"{hook} → {event.get('outcome', '?')}" if subtype == "hook_response" else hook
        elif subtype == "init":
            emoji = "🚀"
            model = event.get("model", "?")
            n_tools = len(event.get("tools", []))
            n_skills = len(set(event.get("skills", [])))
            key_msg = f"{model} | {n_tools} tools, {n_skills} skills"
        elif subtype == "task_started":
            emoji = "🔀"
            key_msg = event.get("description", "") or f"task {str(event.get('task_id', '?'))[:8]}"
        else:
            emoji = "⚙️"
            key_msg = subtype or str(event.get("uuid", ""))[:8]

    elif etype == "assistant":
        msg = event.get("message", {})
        has_tool_use = False
        parts: list[str] = []
        for c in msg.get("content", []):
            ct = c.get("type", "")
            if ct == "tool_use":
                has_tool_use = True
                name = c.get("name", "?")
                inp = c.get("input", {})
                if name == "Skill":
                    parts.append(f"Skill({inp.get('skill', '?')})")
                elif name == "Task":
                    parts.append(f'Task("{(inp.get("description") or "")[:50]}")')
                elif name == "Bash":
                    desc = inp.get("description", "")
                    raw_cmd = (inp.get("command") or "").strip()
                    # Collapse multiline command to single line for display
                    cmd_oneline = " ".join(raw_cmd.split())[:120]
                    parts.append(f"Bash({cmd_oneline})")
                    if desc:
                        sub_detail = (sub_detail + "; " + desc) if sub_detail else desc
                elif name in ("Read", "Edit", "Write"):
                    fp = inp.get("file_path", "")
                    parts.append(f"{name}({fp.rsplit('/', 1)[-1]})")
                elif name in ("Grep", "Glob"):
                    parts.append(f"{name}({(inp.get('pattern') or '')[:30]})")
                else:
                    parts.append(name)
            elif ct == "text":
                parts.append(f'"{(c.get("text") or "")[:80]}"')
        if has_tool_use:
            emoji = "🔧"
            is_tool_call = True
        else:
            emoji = "💬"
        key_msg = " + ".join(parts) if parts else "(empty)"
        # Model/token info
        usage = msg.get("usage", {})
        model = msg.get("model", "")
        if usage or model:
            info_parts: list[str] = []
            if model:
                info_parts.append(model)
            in_t = usage.get("input_tokens", 0)
            out_t = usage.get("output_tokens", 0)
            if in_t or out_t:
                info_parts.append(f"in:{in_t} out:{out_t}")
            cr = usage.get("cache_read_input_tokens", 0)
            if cr:
                info_parts.append(f"cache_read:{cr:,}")
            more_info = " | ".join(info_parts)

    elif etype == "user":
        msg = event.get("message", {})
        is_synthetic = event.get("isSynthetic", False)
        has_tool_result = False
        prefixes: list[str] = []
        if is_synthetic:
            prefixes.append("(synthetic)")
        if event.get("parent_tool_use_id"):
            prefixes.append("↳subagent")
        content_parts: list[str] = []
        for c in msg.get("content", []):
            ct = c.get("type", "")
            if ct == "tool_result":
                has_tool_result = True
                raw = c.get("content", "")
                if isinstance(raw, str):
                    content_parts.append(f'"{raw[:80]}"')
                elif isinstance(raw, list):
                    content_parts.extend(
                        f'"{(blk.get("text") or "")[:60]}"' for blk in raw[:1] if isinstance(blk, dict)
                    )
                if c.get("is_error"):
                    content_parts.append("⚠ ERROR")
            elif ct == "text":
                content_parts.append(f'"{(c.get("text") or "")[:80]}"')
        if has_tool_result:
            emoji = "📥"
        elif is_synthetic:
            emoji = "📄"
        else:
            emoji = "👤"
        key_msg = " ".join(prefixes + content_parts) if (prefixes or content_parts) else "(empty)"
        # tool_use_result metadata
        tur = event.get("tool_use_result")
        if isinstance(tur, str) and "Error" in tur[:20]:
            key_msg = f"⚠ {tur.split(chr(10))[0][:80]}"
        elif isinstance(tur, dict):
            tur_info: list[str] = []
            if tur.get("commandName"):
                tur_info.append(f"skill:{tur['commandName']}")
            if "status" in tur:
                tur_info.append(str(tur["status"]))
            if tur.get("totalTokens"):
                tur_info.append(f"{tur['totalTokens']:,} tok")
            if tur.get("totalToolUseCount"):
                tur_info.append(f"{tur['totalToolUseCount']} tools")
            dur = tur.get("totalDurationMs")
            if dur:
                s = dur / 1000
                tur_info.append(f"{int(s // 60)}m{int(s % 60)}s" if s >= 60 else f"{s:.0f}s")
            if tur_info:
                more_info = " | ".join(tur_info)

    elif etype == "tool_use":
        emoji = "🔧"
        is_tool_call = True
        name = event.get("name", "?")
        inp = event.get("input", {})
        if name == "Bash":
            raw_cmd = (inp.get("command") or "").strip()
            cmd_oneline = " ".join(raw_cmd.split())[:120]
            key_msg = f"Bash({cmd_oneline})"
            desc = inp.get("description", "")
            if desc:
                sub_detail = desc
        elif name in ("Read", "Edit", "Write"):
            fp = inp.get("file_path", "")
            key_msg = f"{name}({fp.rsplit('/', 1)[-1]})"
        elif name in ("Grep", "Glob"):
            key_msg = f"{name}({(inp.get('pattern') or '')[:30]})"
        elif name == "Task":
            key_msg = f'Task("{(inp.get("description") or "")[:50]}")'
        elif name == "Skill":
            key_msg = f"Skill({inp.get('skill', '?')})"
        else:
            key_msg = name

    elif etype == "tool_result":
        emoji = "📥"
        tool_name = event.get("name", "")
        is_err = event.get("is_error", False)
        raw = event.get("output") or event.get("content", "")
        if is_err:
            emoji = "⚠️"
            snippet = raw[:80] if isinstance(raw, str) else str(raw)[:80]
            key_msg = f"ERROR {tool_name}: {snippet}" if tool_name else f"ERROR: {snippet}"
        else:
            snippet = raw[:80] if isinstance(raw, str) else str(raw)[:80]
            key_msg = f"{tool_name}: {snippet}" if tool_name else snippet
        status = event.get("status", "")
        if status:
            more_info = str(status)

    elif etype == "error":
        emoji = "❌"
        err_msg = event.get("message") or event.get("error", "")
        key_msg = str(err_msg)[:140] if err_msg else "(unknown error)"
        code = event.get("code", "")
        if code:
            more_info = f"code: {code}"

    elif etype == "result":
        sub = event.get("subtype", "?")
        emoji = "✅" if sub == "success" else "❌"
        r_parts = [sub]
        if event.get("num_turns"):
            r_parts.append(f"{event['num_turns']} turns")
        est_cost = event.get("estimated_total_cost_usd") or event.get("total_cost_usd")
        if est_cost is not None:
            r_parts.append(f"~${est_cost:.2f}")
        dur = event.get("duration_ms")
        if dur:
            s = dur / 1000
            r_parts.append(f"{int(s // 60)}m{int(s % 60)}s" if s >= 60 else f"{s:.0f}s")
        key_msg = " | ".join(r_parts)
        mu = event.get("modelUsage", {})
        if mu:
            more_info = " · ".join(
                f"{m}: ~${u.get('estimatedCostUSD') or u.get('costUSD', 0):.3f}" for m, u in mu.items()
            )

    return emoji, type_label, key_msg, sub_detail, more_info, is_tool_call


_EVENTS_OVERVIEW_CSS = """\
<style>
.ev-tbl { width:100%; border-collapse:collapse; font-size:0.84em; }
.ev-tbl td { padding:2px 6px; vertical-align:top; }
.ev-tbl h1,.ev-tbl h2,.ev-tbl h3,.ev-tbl h4,.ev-tbl h5,.ev-tbl h6 {
    font-size:inherit !important; font-weight:normal !important;
    margin:0 !important; padding:0 !important; display:inline !important;
}
.ev-seq { width:30px; color:#999; text-align:right; font-size:0.85em; }
.ev-type { width:145px; white-space:nowrap; }
.ev-type code { font-size:0.82em; background:#e8e8e8; padding:0 3px; border-radius:2px; }
.ev-msg { word-break:break-word; }
.ev-detail { font-size:0.8em; color:#888; padding-bottom:5px; }
.ev-tool { background:#eef6ff; }
.ev-tool code { background:#d0e6ff; }
</style>
"""


def _sanitize_for_html(text: str) -> str:
    """Escape HTML and collapse whitespace so markdown syntax is never rendered."""
    # Collapse newlines/tabs to spaces first, then HTML-escape
    return html.escape(text.replace("\n", " ").replace("\r", " ").replace("\t", " "))


def _render_events_overview(events: list[dict[str, Any]]) -> None:
    """Render a compact table overview of raw events."""
    rows: list[str] = [_EVENTS_OVERVIEW_CSS, '<table class="ev-tbl">']
    for i, ev in enumerate(events):
        emoji, type_label, key_msg, sub_detail, more_info, is_tool = _extract_event_summary(ev)
        # Collapse whitespace before truncating
        key_msg = key_msg.replace("\n", " ").replace("\r", " ").strip()
        if len(key_msg) > 140:
            key_msg = key_msg[:140] + "…"
        row_cls = ' class="ev-tool"' if is_tool else ""
        rows.append(f"<tr{row_cls}>")
        rows.append(f'<td class="ev-seq">{i}.</td>')
        rows.append(f'<td class="ev-type">{emoji} <code>{html.escape(type_label)}</code></td>')
        msg_html = _sanitize_for_html(key_msg)
        if sub_detail:
            msg_html += (
                f'<br><span style="font-size:0.82em;color:#666;font-style:italic;">'
                f"{_sanitize_for_html(sub_detail)}</span>"
            )
        rows.append(f'<td class="ev-msg">{msg_html}</td>')
        rows.append("</tr>")
        if more_info:
            rows.append(f"<tr{row_cls}>")
            rows.append('<td></td><td colspan="2" class="ev-detail">')
            rows.append(f"{_sanitize_for_html(more_info)}</td></tr>")
    rows.append("</table>")
    st.markdown("\n".join(rows), unsafe_allow_html=True)


# ── Cost & Tokens tab helpers ────────────────────────────────────────────────

_COST_TOKENS_CSS = """\
<style>
.ct-tbl { width:100%; border-collapse:collapse; font-size:0.84em; }
.ct-tbl th { text-align:left; padding:4px 6px; border-bottom:2px solid #ccc; font-size:0.85em; color:#1976d2; }
.ct-tbl td { padding:2px 6px; vertical-align:top; }
.ct-tbl .ct-num { text-align:right; font-variant-numeric:tabular-nums; }
.ct-tbl .ct-sep { border-top:1px solid #eee; }
.ct-tbl .ct-total { font-weight:bold; border-top:2px solid #999; }
.ct-summary { font-size:0.9em; margin-bottom:8px; }
.ct-summary strong { color:#1976d2; }
</style>
"""


def _render_events_cost_tokens(events: list[dict[str, Any]]) -> None:
    """Render a table focused on cost and token usage extracted from raw events."""
    rows: list[str] = [_COST_TOKENS_CSS]

    # Collect per-event cost/token rows
    event_rows: list[dict[str, Any]] = []
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_reasoning = 0
    total_cost: float | None = None

    for i, ev in enumerate(events):
        etype = ev.get("type", "?")

        # assistant events carry usage in message.usage
        if etype == "assistant":
            msg = ev.get("message", {})
            usage = msg.get("usage", {})
            model = msg.get("model", "")
            if not usage and not model:
                continue

            in_t = usage.get("input_tokens", 0)
            out_t = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_tokens", 0) or usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_write_tokens", 0) or usage.get("cache_creation_input_tokens", 0) or 0
            # Legacy format: cache_creation as a dict of per-block counts
            cache_creation_data = usage.get("cache_creation", {})
            if isinstance(cache_creation_data, dict) and cache_creation_data:
                cache_create = sum(int(v) for v in cache_creation_data.values() if isinstance(v, (int, float)))
            reasoning = usage.get("reasoning_tokens", 0)
            service_tier = usage.get("service_tier", "")

            total_input += in_t
            total_output += out_t
            total_cache_read += cache_read
            total_cache_creation += cache_create
            total_reasoning += reasoning

            # Per-step estimated cost (raw executor emits this)
            step_cost = ev.get("estimated_cost_usd")

            # Check for tool_use in content to label the row
            label_parts: list[str] = []
            for c in msg.get("content", []):
                if c.get("type") == "tool_use":
                    label_parts.append(c.get("name", "?"))
                elif c.get("type") == "text":
                    label_parts.append("text")
            label = ", ".join(label_parts[:3]) or "response"

            event_rows.append(
                {
                    "idx": i,
                    "type": "assistant",
                    "label": label,
                    "model": model,
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "cache_read": cache_read,
                    "cache_creation": cache_create,
                    "reasoning": reasoning,
                    "service_tier": service_tier,
                    "cost": float(step_cost) if step_cost is not None else None,
                }
            )

        # user events may carry tool_use_result with token/cost info
        elif etype == "user":
            tur = ev.get("tool_use_result")
            if isinstance(tur, dict):
                tok = tur.get("totalTokens")
                cost = tur.get("totalCostUSD")
                if cost is None:
                    cost = tur.get("total_cost_usd")
                dur_ms = tur.get("totalDurationMs")
                tool_count = tur.get("totalToolUseCount")
                cmd = tur.get("commandName", "")
                usage_inner = tur.get("usage", {})
                if tok is not None or cost is not None or usage_inner:
                    in_t = usage_inner.get("input_tokens", 0)
                    out_t = usage_inner.get("output_tokens", 0)
                    cache_read = usage_inner.get("cache_read_input_tokens", 0)
                    cache_creation_data = usage_inner.get("cache_creation", {})
                    cache_create = 0
                    if isinstance(cache_creation_data, dict):
                        cache_create = sum(int(v) for v in cache_creation_data.values() if isinstance(v, (int, float)))

                    label = "↳ result"
                    if cmd:
                        label = f"↳ {cmd}"

                    event_rows.append(
                        {
                            "idx": i,
                            "type": "tool_result",
                            "label": label,
                            "model": "",
                            "input_tokens": in_t,
                            "output_tokens": out_t,
                            "cache_read": cache_read,
                            "cache_creation": cache_create,
                            "reasoning": 0,
                            "service_tier": "",
                            "cost": float(cost) if cost is not None else None,
                            "total_tokens": tok,
                            "duration_ms": dur_ms,
                            "tool_count": tool_count,
                        }
                    )

        # result events carry total cost and modelUsage breakdown
        elif etype == "result":
            cost_usd = ev.get("estimated_total_cost_usd") or ev.get("cost_usd") or ev.get("total_cost_usd")
            if cost_usd is not None:
                total_cost = cost_usd

            model_usage = ev.get("modelUsage", {})
            if model_usage:
                for model_name, mu in model_usage.items():
                    mu_cost = mu.get("estimatedCostUSD") or mu.get("costUSD", 0)
                    mu_in = mu.get("inputTokens", 0)
                    mu_out = mu.get("outputTokens", 0)
                    mu_cache_read = mu.get("cacheReadTokens") or mu.get("cacheReadInputTokens", 0)
                    mu_cache_create = mu.get("cacheWriteTokens") or mu.get("cacheCreationInputTokens", 0)
                    mu_reasoning = mu.get("reasoningTokens", 0)
                    event_rows.append(
                        {
                            "idx": i,
                            "type": "result_model",
                            "label": f"⊕ {model_name}",
                            "model": model_name,
                            "input_tokens": mu_in,
                            "output_tokens": mu_out,
                            "cache_read": mu_cache_read,
                            "cache_creation": mu_cache_create,
                            "reasoning": mu_reasoning,
                            "service_tier": "",
                            "cost": mu_cost,
                        }
                    )

            dur = ev.get("duration_ms")
            turns = ev.get("num_turns")
            result_sub = ev.get("subtype", "")
            if cost_usd is not None or dur or turns:
                meta_parts: list[str] = []
                if result_sub:
                    meta_parts.append(result_sub)
                if turns:
                    meta_parts.append(f"{turns} turns")
                if dur:
                    s = dur / 1000
                    meta_parts.append(f"{int(s // 60)}m{int(s % 60)}s" if s >= 60 else f"{s:.0f}s")
                if cost_usd is not None:
                    meta_parts.append(f"~${cost_usd:.4f}")
                event_rows.append(
                    {
                        "idx": i,
                        "type": "result_summary",
                        "label": "⊕ " + " | ".join(meta_parts),
                        "model": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read": 0,
                        "cache_creation": 0,
                        "reasoning": 0,
                        "service_tier": "",
                        "cost": cost_usd,
                    }
                )

    if not event_rows:
        st.caption("No cost or token data in these events.")
        return

    # Summary line
    summary_parts: list[str] = []
    if total_input or total_output:
        summary_parts.append(f"<strong>Input:</strong> {total_input:,}")
        summary_parts.append(f"<strong>Output:</strong> {total_output:,}")
    if total_cache_read:
        summary_parts.append(f"<strong>Cache read:</strong> {total_cache_read:,}")
    if total_cache_creation:
        summary_parts.append(f"<strong>Cache create:</strong> {total_cache_creation:,}")
    if total_reasoning:
        summary_parts.append(f"<strong>Reasoning:</strong> {total_reasoning:,}")
    if total_cost is not None:
        summary_parts.append(f"<strong>Est. total cost:</strong> ~${total_cost:.4f}")
    if summary_parts:
        rows.append(f'<div class="ct-summary">{" &nbsp;·&nbsp; ".join(summary_parts)}</div>')

    def _fmt_int(v: int) -> str:
        return f"{v:,}" if v else "—"

    def _fmt_cost(v: float | None) -> str:
        if v is None:
            return "—"
        return f"${v:.4f}"

    rows.append('<table class="ct-tbl">')
    rows.append(
        "<tr>"
        '<th style="width:30px;">#</th>'
        '<th style="width:90px;">Type</th>'
        "<th>Label</th>"
        "<th>Model</th>"
        '<th class="ct-num">Input</th>'
        '<th class="ct-num">Output</th>'
        '<th class="ct-num">Cache Read</th>'
        '<th class="ct-num">Cache Create</th>'
        '<th class="ct-num">Reasoning</th>'
        '<th class="ct-num">Est. Cost</th>'
        "<th>Extra</th>"
        "</tr>"
    )
    for er in event_rows:
        row_cls = ""
        if er["type"] in ("result_model", "result_summary"):
            row_cls = ' class="ct-sep"'

        extra_parts: list[str] = []
        if er.get("service_tier"):
            extra_parts.append(er["service_tier"])
        if er.get("total_tokens"):
            extra_parts.append(f"{er['total_tokens']:,} tok")
        if er.get("tool_count"):
            extra_parts.append(f"{er['tool_count']} tools")
        if er.get("duration_ms"):
            s = er["duration_ms"] / 1000
            extra_parts.append(f"{int(s // 60)}m{int(s % 60)}s" if s >= 60 else f"{s:.0f}s")
        extra = ", ".join(extra_parts)

        type_label = er["type"].replace("_", " ")
        rows.append(f"<tr{row_cls}>")
        rows.append(f'<td class="ct-num">{er["idx"]}</td>')
        rows.append(f"<td>{html.escape(type_label)}</td>")
        rows.append(f"<td>{_sanitize_for_html(er['label'])}</td>")
        rows.append(f"<td><code>{html.escape(er.get('model') or '—')}</code></td>")
        rows.append(f'<td class="ct-num">{_fmt_int(er["input_tokens"])}</td>')
        rows.append(f'<td class="ct-num">{_fmt_int(er["output_tokens"])}</td>')
        rows.append(f'<td class="ct-num">{_fmt_int(er["cache_read"])}</td>')
        rows.append(f'<td class="ct-num">{_fmt_int(er["cache_creation"])}</td>')
        rows.append(f'<td class="ct-num">{_fmt_int(er.get("reasoning", 0))}</td>')
        rows.append(f'<td class="ct-num">{_fmt_cost(er.get("cost"))}</td>')
        rows.append(f"<td>{_sanitize_for_html(extra)}</td>")
        rows.append("</tr>")

    rows.append("</table>")
    st.markdown("\n".join(rows), unsafe_allow_html=True)


def _extract_total_cost_from_events(events: list[dict[str, Any]]) -> float | None:
    """Extract the estimated total cost from a result event, or sum model costs."""
    for ev in events:
        if ev.get("type") == "result":
            cost = ev.get("estimated_total_cost_usd") or ev.get("total_cost_usd")
            if cost is not None:
                return float(cost)
            # Fall back to summing modelUsage costs
            mu = ev.get("modelUsage", {})
            if mu:
                return float(sum(float(u.get("estimatedCostUSD") or u.get("costUSD", 0)) for u in mu.values()))
    return None


# ── Chat thread CSS ──────────────────────────────────────────────────────────

_CHAT_CSS = """
<style>
.chat-thread { max-width: 900px; margin: 0 auto; }
.user-bubble-wrapper {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 18px;
}
.user-bubble-content {
    max-width: 80%;
    text-align: right;
}
.user-bubble-inner {
    display: inline-block;
    text-align: left;
    background: #1976d2;
    color: #fff;
    padding: 12px 16px;
    border-radius: 12px 12px 4px 12px;
    font-size: 0.95em;
    line-height: 1.5;
    word-wrap: break-word;
    overflow-wrap: break-word;
}
.user-bubble-inner pre { white-space: pre-wrap; word-wrap: break-word; }
.bubble-meta {
    font-size: 0.78em;
    color: #999;
    margin-top: 4px;
}
.user-bubble-content .bubble-meta { text-align: right; }
.chat-bubble { margin-bottom: 4px; }
.chat-bubble.agent { border-left: 3px solid #388e3c; padding-left: 8px; }
.chat-bubble.system { border-left: 3px solid #757575; padding-left: 8px; }
</style>
"""

# ── Chat thread view ─────────────────────────────────────────────────────────


_ARTIFACT_TYPE_ICON: dict[AgentArtifactType, str] = {
    AgentArtifactType.YUPPASTE: "📝",
    AgentArtifactType.CODE_REVIEW: "🔍",
    AgentArtifactType.OTHER: "📦",
}


def _md_cell(s: str) -> str:
    """Escape pipe characters and newlines so the string is safe in a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


def _render_artifacts_table(artifacts: list[AgentArtifact]) -> None:
    """Render a compact table of artifacts with links."""
    if not artifacts:
        st.caption("No artifacts for this session.")
        return
    rows = []
    for a in artifacts:
        icon = _ARTIFACT_TYPE_ICON.get(a.artifact_type, "📦")
        type_str = f"{icon} {a.artifact_type.value}"
        title_link = f"[{_md_cell(a.title)}]({a.url})"
        created_str = _to_local(a.created_at)
        raw_desc = (a.description or "")[:80] + ("…" if a.description and len(a.description) > 80 else "")
        desc = _md_cell(raw_desc)
        rows.append(f"| {type_str} | {title_link} | {desc} | {created_str} |")
    header = "| Type | Title | Description | Created |"
    sep = "|------|-------|-------------|---------|"
    st.markdown("\n".join([header, sep] + rows))


def _render_chat_thread(agent_session: AgentSession) -> None:
    """Render a single session as a chat thread in chronological order."""
    agent_name = agent_session.agent.display_name if agent_session.agent else "?"
    agent_slug = agent_session.agent.name if agent_session.agent else "?"

    if st.button("< Back to all sessions"):
        st.query_params.clear()
        st.rerun()

    created_ago = _time_ago(agent_session.created_at)
    created_str = (
        f"{_to_local(agent_session.created_at)} ({created_ago})" if created_ago else _to_local(agent_session.created_at)
    )

    sid = str(agent_session.agent_session_id)
    title_html = f"<strong>{html.escape(agent_session.title)}</strong> — " if agent_session.title else ""
    st.markdown(
        f"### {title_html}Session <code>{html.escape(sid)}</code>"
        f" <button onclick=\"navigator.clipboard.writeText('{sid}')\" "
        f'title="Copy session ID" style="border:none;background:none;cursor:pointer;font-size:0.6em;'
        f'vertical-align:middle;padding:2px;">📋</button>'
        f" with <strong>{html.escape(agent_name)}</strong> (<code>{html.escape(agent_slug)}</code>)",
        unsafe_allow_html=True,
    )
    war_room_url = f"https://war-room.yuppster.ai/session/{agent_session.agent_session_id}"
    meta_parts = [
        f"**Status** `{agent_session.status.value}`",
        f"**Trigger** `{agent_session.trigger.value}`",
        f"**Created** `{created_str}`",
        f"**Workspace** `{agent_session.workspace or '—'}`",
        f"**War Room** [Open]({war_room_url})",
    ]
    st.markdown(" | ".join(meta_parts))
    if agent_session.slack_session_id:
        st.markdown(f"**Slack session** `{agent_session.slack_session_id}`")
    if agent_session.llm_session_id:
        st.markdown(f"**LLM session** `{agent_session.llm_session_id}`")

    st.divider()

    # Sort messages: chronological (oldest first)
    msgs = sorted(
        agent_session.messages,
        key=lambda m: (m.turn_number, m.created_at or datetime.min.replace(tzinfo=UTC)),
    )

    st.caption(f"{len(msgs)} message(s)")

    # Inject chat CSS
    st.markdown(_CHAT_CSS, unsafe_allow_html=True)

    # Render each message as a chat bubble
    for msg in msgs:
        role = msg.role.value  # USER / AGENT / SYSTEM
        role_lower = role.lower()
        ts = _to_local(msg.created_at)

        # Build meta line
        meta_parts = [f"Turn {msg.turn_number}", ts]
        if msg.cost_usd:
            meta_parts.append(f"~${msg.cost_usd:.4f}")
        if msg.duration_ms:
            meta_parts.append(f"{msg.duration_ms}ms")
        if msg.llm_name:
            meta_parts.append(msg.llm_name)
        if msg.num_agent_turns:
            meta_parts.append(f"{msg.num_agent_turns} agent turns")
        meta_line = " · ".join(meta_parts)

        content = msg.content or "(no content)"
        msg_id = str(msg.agent_session_message_id)

        if role_lower == "user":
            # User messages: right-aligned bubble
            escaped = html.escape(content).replace("\n", "<br>")
            st.markdown(
                f'<div class="user-bubble-wrapper">'
                f'<div class="user-bubble-content">'
                f'<div class="user-bubble-inner">{escaped}</div>'
                f'<div class="bubble-meta">{html.escape(meta_line)}</div>'
                f'<div class="bubble-meta" style="font-size:0.7em;color:#bbb;">{msg_id}</div>'
                f"</div></div>",
                unsafe_allow_html=True,
            )
        else:
            # Agent / System: render markdown content inside a styled container
            css_class = role_lower  # "agent" or "system"

            # Extract cost from raw events for agent messages
            events_cost_str = ""
            if role_lower == "agent" and msg.raw_events:
                ev_list = msg.raw_events if isinstance(msg.raw_events, list) else [msg.raw_events]
                ev_cost = _extract_total_cost_from_events(ev_list)
                if ev_cost is not None:
                    events_cost_str = (
                        f' &nbsp; <span style="background:#e8f5e9;color:#2e7d32;'
                        f'padding:1px 6px;border-radius:8px;font-size:0.82em;">'
                        f"~${ev_cost:.4f}</span>"
                    )

            st.markdown(
                f'<div class="chat-bubble {css_class}">'
                f'<div style="max-width:80%;">'
                f'<div class="bubble-meta">{_role_badge(role)} &nbsp; {html.escape(meta_line)}'
                f"{events_cost_str}</div>"
                f"</div></div>",
                unsafe_allow_html=True,
            )
            # Render markdown in an adaptive container (capped at ~10 visible rows)
            container_h = _estimate_container_height(content)
            agent_col, _ = st.columns([4, 1])
            with agent_col, st.container(height=container_h, border=True):
                if len(content) > 10000:
                    st.markdown(content[:10000])
                    st.caption("... (truncated)")
                else:
                    st.markdown(content)
            st.markdown(
                f'<div style="font-size:0.7em;color:#bbb;margin-top:-12px;margin-bottom:8px;">{msg_id}</div>',
                unsafe_allow_html=True,
            )

        # Expandable raw events
        if msg.raw_events:
            events_list = msg.raw_events if isinstance(msg.raw_events, list) else [msg.raw_events]
            n_events = len(events_list)
            with st.expander(
                f"Event details ({n_events} event{'s' if n_events != 1 else ''})",
                expanded=False,
            ):
                ev_overview_tab, ev_cost_tab, ev_raw_tab = st.tabs(["Overview", "Cost & Tokens", "Raw"])
                with ev_overview_tab:
                    _render_events_overview(events_list)
                with ev_cost_tab:
                    _render_events_cost_tokens(events_list)
                with ev_raw_tab:
                    st.json(msg.raw_events)

    # ── Session artifacts ─────────────────────────────────────────────────────
    st.divider()
    with st.expander("📦 Artifacts", expanded=True):
        with st.spinner("Loading artifacts…"):
            _artifacts = run_coroutine_in_lit_worker(
                fetch_session_artifacts(agent_session.agent_session_id),
                timeout=30,
            )
        _render_artifacts_table(_artifacts or [])


# ── Agent config from repo ────────────────────────────────────────────────────

_AGENT_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "agent_harness_service" / "deploy" / "agent_configs"


@st.cache_data(ttl=300, show_spinner=False)
def _read_repo_agent_config(agent_name: str) -> dict[str, Any] | None:
    """Read config.json from the repo for a given agent name. Returns None if not found."""
    config_path = _AGENT_CONFIGS_DIR / agent_name / "config.json"
    if not config_path.is_file():
        return None
    try:
        return json.loads(config_path.read_text())  # type: ignore[no-any-return]
    except Exception:
        logger.warning("Failed to read repo agent config", agent_name=agent_name, exc_info=True)
        return None


# ── List-view render helper (used in Sessions tab) ──────────────────────────


def _render_session(data: dict[str, Any], indent_level: int = 0) -> None:
    s: AgentSession = data["session"]
    ago = _time_ago(data["last_message_time"])

    # Resolve display strings
    initiator = _resolve_slack_user_name(data["slack_user_id"]) if data.get("slack_user_id") else "n/a"
    slack_agent = data.get("slack_agent_name") or "—"
    agent_display = data["agent_name"]
    agent_slug = data["agent_slug"]

    # Session title and first user message excerpts
    title = s.title or ""
    first_user_messages: list[str] = data.get("first_user_messages") or []

    # Row 2: meta line
    sid = data["session_id"]
    row2_parts: list[str] = []
    row2_parts.append(f"{_to_local(data['last_message_time'])}")
    if ago:
        row2_parts.append(f"({ago})")
    row2_parts.append(f"· {data['message_count']} messages")
    row2_parts.append(f"· {s.status.value}")
    row2_parts.append(f"· `{sid}`")
    row2 = " ".join(row2_parts)

    # Outer layout: indent margin proportional to nesting level, then content + view button
    margin = 0.3 * indent_level
    if margin > 0:
        content_width = 9 - margin
        _margin, content_col, btn_col = st.columns([margin, content_width, 1])
    else:
        content_col, btn_col = st.columns([9, 1])

    with content_col:
        # Row 1: fixed-width columns for trigger, initiator, slack agent, AHS agent, message
        r1_trigger, r1_initiator, r1_slack, r1_agent, r1_msg = st.columns([0.6, 1, 1, 1.4, 6])
        with r1_trigger:
            st.markdown(f"`{s.trigger.value}`")
        with r1_initiator:
            st.markdown(f"**{html.escape(initiator)}**")
        with r1_slack:
            st.markdown(f"*{html.escape(slack_agent)}*")
        with r1_agent:
            st.markdown(f"**{html.escape(agent_display)}**<br>`{html.escape(agent_slug)}`", unsafe_allow_html=True)
        with r1_msg:
            # Build bullet list of first 3 user messages (truncated, one-liner each)
            msg_bullets = ""
            if first_user_messages:
                lines = []
                for m in first_user_messages[:3]:
                    oneliner = " ".join(m.split())[:100]
                    if len(m) > 100:
                        oneliner += "…"
                    lines.append(f"• {html.escape(oneliner)}")
                msg_bullets = "<br>".join(lines)

            if title:
                parts = f"##### {html.escape(title)}"
                if msg_bullets:
                    parts += f'\n<span style="color:#888;font-size:0.72em;font-weight:normal;">{msg_bullets}</span>'
                st.markdown(parts, unsafe_allow_html=True)
            elif msg_bullets:
                st.markdown(
                    f'<span style="font-size:0.9em;">{msg_bullets}</span>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("—")
        # Row 2: meta line
        st.caption(row2)

    with btn_col:
        if st.button("View", key=f"open_thread_{s.agent_session_id}"):
            st.query_params["session_id"] = str(s.agent_session_id)
            st.rerun()


# ── Main page logic ──────────────────────────────────────────────────────────

st.title("Agent Harness Console")

qp = st.query_params
url_session_id = qp.get("session_id", None)

# ── Thread view (when session_id is in URL) ──────────────────────────────────

if url_session_id:
    try:
        sid = uuid.UUID(url_session_id.strip())
    except ValueError:
        st.error(f"Invalid session ID: `{url_session_id}`")
        st.stop()

    with st.spinner("Loading session…"):
        agent_session = run_coroutine_in_lit_worker(fetch_single_session(sid), timeout=60)

    if agent_session is None:
        st.error(f"Session not found: `{sid}`")
        st.stop()

    _render_chat_thread(agent_session)
    st.stop()

# ── Normal tabs view (no session_id in URL) ──────────────────────────────────

# Load agents once for the dropdown
if "ahs_agents" not in st.session_state:
    with st.spinner("Loading agents…"):
        st.session_state.ahs_agents = run_coroutine_in_lit_worker(fetch_all_agents(), timeout=30)

agents: list[Agent] = st.session_state.ahs_agents
agent_names = [a.name for a in agents]

preselect_agent = qp.get("agent", None)

tab_labels = ["💬 Sessions", "🔮 Agents", "⭐ Feedbacks"]
tabs = st.tabs(tab_labels)

# ── Tab 1: Sessions ─────────────────────────────────────────────────────────

with tabs[0]:
    # Load distinct slack agent names for the filter dropdown
    if "ahs_slack_agent_names" not in st.session_state:
        with st.spinner("Loading slack agents…"):
            st.session_state.ahs_slack_agent_names = run_coroutine_in_lit_worker(
                fetch_distinct_slack_agent_names(), timeout=30
            )
    slack_agent_names: list[str] = st.session_state.ahs_slack_agent_names

    filter_cols = st.columns([2, 1, 1, 2, 1, 1])

    with filter_cols[0]:
        agent_options = ["(all)"] + agent_names
        default_idx = 0
        if preselect_agent and preselect_agent in agent_names:
            default_idx = agent_names.index(preselect_agent) + 1
        selected_agent = st.selectbox("Agent", agent_options, index=default_idx, key="sess_agent")

    with filter_cols[1]:
        trigger_options = ["(all)"] + [t.value for t in AgentSessionTrigger]
        selected_trigger = st.selectbox("Trigger", trigger_options, key="sess_trigger")

    with filter_cols[2]:
        slack_agent_options = ["(all)"] + slack_agent_names
        selected_slack_agent = st.selectbox("Slack Agent", slack_agent_options, key="sess_slack_agent")

    with filter_cols[3]:
        session_id_input = st.text_input("Session ID", key="sess_id_input")

    with filter_cols[4]:
        sort_option = st.selectbox("Sort by", ["last_message", "created", "message_count"], key="sess_sort")

    with filter_cols[5]:
        limit_options = [20, 50, 100, 200, 500]
        limit = st.selectbox("Limit", limit_options, index=1, key="sess_limit")

    # Date range filter
    date_cols = st.columns([1, 1, 4])
    with date_cols[0]:
        date_from = st.date_input("From date", value=None, key="sess_date_from")
    with date_cols[1]:
        date_to = st.date_input("To date", value=None, key="sess_date_to")

    agent_filter = None if selected_agent == "(all)" else selected_agent
    trigger_filter = None if selected_trigger == "(all)" else selected_trigger
    slack_agent_filter = None if selected_slack_agent == "(all)" else selected_slack_agent

    st.caption(f"Viewing all sessions (admin: {_current_username or 'local dev'})")

    with st.spinner("Loading sessions…"):
        try:
            session_data = run_coroutine_in_lit_worker(
                fetch_sessions(
                    agent_name=agent_filter,
                    session_id_str=session_id_input or None,
                    sort_by=sort_option,
                    limit=int(limit),
                    date_from=date_from,
                    date_to=date_to,
                    trigger=trigger_filter,
                    slack_agent_name=slack_agent_filter,
                ),
                timeout=60,
            )
        except Exception as e:
            st.error(f"Error loading sessions: {e}")
            session_data = []

    st.caption(f"{len(session_data)} session(s) loaded")

    # Group fetched sessions: those with a parent already in the result are children,
    # the rest are top-level roots.
    top_level: list[dict[str, Any]] = []
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    session_ids_in_result = {sd["session_id"] for sd in session_data}

    for sd in session_data:
        parent_id = sd.get("parent_session_id")
        if parent_id and parent_id in session_ids_in_result:
            children_by_parent.setdefault(parent_id, []).append(sd)
        else:
            top_level.append(sd)

    # Batch-fetch all children level by level to avoid N+1 queries.
    # Start with top-level IDs, fetch their children in one query, index them,
    # then repeat for the next level.
    all_children: dict[str, list[dict[str, Any]]] = dict(children_by_parent)
    max_depth = 5
    current_level_ids = [sd["session_id"] for sd in top_level]

    for _depth in range(max_depth):
        # Always fetch children for all current-level IDs (dedup handles duplicates)
        ids_to_fetch = current_level_ids
        if not ids_to_fetch:
            break

        try:
            db_children = run_coroutine_in_lit_worker(
                fetch_child_sessions([uuid.UUID(sid) for sid in ids_to_fetch]), timeout=30
            )
            for dc in db_children:
                parent_id = dc.get("parent_session_id")
                if parent_id:
                    all_children.setdefault(parent_id, [])
                    if not any(c["session_id"] == dc["session_id"] for c in all_children[parent_id]):
                        all_children[parent_id].append(dc)
        except Exception:
            logger.warning("Failed to fetch child sessions for level", depth=_depth, exc_info=True)

        # Next level: all children we just discovered
        next_ids: list[str] = []
        for sid in current_level_ids:
            next_ids.extend(c["session_id"] for c in all_children.get(sid, []))
        if not next_ids:
            break
        current_level_ids = next_ids

    def _render_session_tree(
        sd: dict[str, Any],
        level: int = 0,
    ) -> None:
        """Render a session and its pre-fetched children recursively."""
        _render_session(sd, indent_level=level)
        for child in all_children.get(sd["session_id"], []):
            _render_session_tree(child, level + 1)

    for sd in top_level:
        _render_session_tree(sd)
        st.divider()

# ── Tab 2: Agents ────────────────────────────────────────────────────────────

with tabs[1]:
    if not agents:
        st.info("No agents found.")
    else:
        # Filters
        agent_filter_cols = st.columns([1.5, 2, 3])
        with agent_filter_cols[0]:
            exec_type_options = ["(all)"] + [t.value for t in AgentExecutorType]
            selected_exec_type = st.selectbox("Executor type", exec_type_options, key="agent_exec_type")
        with agent_filter_cols[1]:
            agent_name_options = ["(all)"] + [a.name for a in agents]
            selected_agent_name = st.selectbox("Agent", agent_name_options, key="agent_name_filter")
        with agent_filter_cols[2]:
            keyword_input = st.text_input(
                "Keyword", key="agent_keyword", placeholder="Search name, display name, description…"
            )

        keyword_lower = keyword_input.strip().lower() if keyword_input else ""

        # Fetch aggregate stats
        with st.spinner("Loading agent stats…"):
            try:
                agent_stats = run_coroutine_in_lit_worker(fetch_agent_stats(), timeout=30)
            except Exception:
                logger.warning("Failed to fetch agent stats", exc_info=True)
                agent_stats = {}

        filtered_agents = agents
        if selected_exec_type != "(all)":
            filtered_agents = [
                a for a in filtered_agents if a.executor_type and a.executor_type.value == selected_exec_type
            ]
        if selected_agent_name != "(all)":
            filtered_agents = [a for a in filtered_agents if a.name == selected_agent_name]
        if keyword_lower:
            filtered_agents = [
                a
                for a in filtered_agents
                if keyword_lower in (a.display_name or "").lower()
                or keyword_lower in (a.name or "").lower()
                or keyword_lower in (a.description or "").lower()
            ]

        st.caption(f"{len(filtered_agents)} agent(s)")

        # Header row
        _BLUE_BOLD = 'style="color:#1976d2;font-weight:bold;font-size:0.85em;"'
        h_exec, h_name, h_desc, h_tools, h_subagents = st.columns([1.5, 1.5, 2, 2.5, 2.5])
        with h_exec:
            st.markdown(f"<span {_BLUE_BOLD}>Executor</span>", unsafe_allow_html=True)
        with h_name:
            st.markdown(f"<span {_BLUE_BOLD}>Agent</span>", unsafe_allow_html=True)
        with h_desc:
            st.markdown(f"<span {_BLUE_BOLD}>Description</span>", unsafe_allow_html=True)
        with h_tools:
            st.markdown(f"<span {_BLUE_BOLD}>Tool Permissions</span>", unsafe_allow_html=True)
        with h_subagents:
            st.markdown(f"<span {_BLUE_BOLD}>Allowed Subagents</span>", unsafe_allow_html=True)
        st.divider()

        for a in filtered_agents:
            stats = agent_stats.get(a.agent_id, {})
            total_sess = stats.get("total_sessions", 0)
            total_msgs = stats.get("total_messages", 0)
            last_active = stats.get("last_active")
            last_active_str = f"{_to_local(last_active)} ({_time_ago(last_active)})" if last_active else "—"

            db_config = a.config
            repo_config = _read_repo_agent_config(a.name)
            config = db_config if db_config is not None else repo_config
            config_source = "DB" if db_config is not None else "repo"

            # Prefer executor info from config over DB fields
            exec_config = (config or {}).get("executor_config", {})
            executor_type = exec_config.get("type") or (a.executor_type.value if a.executor_type else "—")
            executor_model = exec_config.get("harness") or exec_config.get("model") or a.executor_model or "—"

            # Extract tools/subagents from config, keep the rest
            tool_permissions: dict[str, str] = {}
            allowed_subagents: list[str] = []
            rest_config: dict[str, Any] = {}
            if config:
                tool_permissions = config.get("tool_permissions", {})
                allowed_subagents = config.get("allowed_subagents", [])
                rest_config = {k: v for k, v in config.items() if k not in ("tool_permissions", "allowed_subagents")}

            desc = a.description or "—"

            # Detect DB <> repo config conflicts
            conflicts: list[str] = []
            if repo_config:
                repo_exec = repo_config.get("executor_config", {})
                repo_type = repo_exec.get("type", "")
                repo_model = repo_exec.get("harness") or repo_exec.get("model") or ""
                db_type = a.executor_type.value.lower() if a.executor_type else ""
                if repo_type and db_type and repo_type.lower() != db_type:
                    conflicts.append(f"executor_type: DB=`{db_type}` vs repo=`{repo_type}`")
                if repo_model and a.executor_model and repo_model != a.executor_model:
                    conflicts.append(f"executor_model: DB=`{a.executor_model}` vs repo=`{repo_model}`")
                repo_display = repo_config.get("display_name", "")
                if repo_display and a.display_name and repo_display != a.display_name:
                    conflicts.append(f"display_name: DB=`{a.display_name}` vs repo=`{repo_display}`")
                repo_desc = repo_config.get("description", "")
                if repo_desc and a.description and repo_desc != a.description:
                    conflicts.append("description differs")

            # Row 1: executor type+model, name, description, tools, subagents
            r1_exec, r1_name, r1_desc, r1_tools, r1_subagents = st.columns([1.5, 1.5, 2, 2.5, 2.5])
            with r1_exec:
                st.markdown(f"`{executor_type}`<br>`{executor_model}`", unsafe_allow_html=True)
            with r1_name:
                st.markdown(
                    f'<span style="font-size:1.2em;font-weight:bold;">{html.escape(a.display_name)}</span>'
                    f"<br>`{html.escape(a.name)}`",
                    unsafe_allow_html=True,
                )
            with r1_desc:
                st.caption(desc)
            with r1_tools:
                if tool_permissions:
                    perms_str = ", ".join(f"`{k}`:`{v}`" for k, v in tool_permissions.items())
                else:
                    perms_str = "—"
                st.markdown(perms_str)
            with r1_subagents:
                subs_str = ", ".join(f"`{sub}`" for sub in allowed_subagents) if allowed_subagents else "—"
                st.markdown(subs_str)

            # Row 2: stats
            st.caption(f"{total_sess} sessions · {total_msgs} messages · last active {last_active_str}")

            # DB <> repo conflict warning
            if conflicts:
                st.warning(f"DB/repo mismatch: {' · '.join(conflicts)}")

            # Row 3: remaining config collapsible
            if rest_config:
                with st.expander(f"Full config ({config_source})", expanded=False):
                    st.json(rest_config)
            st.divider()

# ── Tab 3: Feedbacks ─────────────────────────────────────────────────────────

with tabs[2]:
    fb_limit = st.number_input("Limit", min_value=1, max_value=500, value=100, key="fb_limit")

    with st.spinner("Loading feedbacks…"):
        try:
            feedbacks = run_coroutine_in_lit_worker(fetch_feedbacks(limit=int(fb_limit)), timeout=60)
        except Exception as e:
            st.error(f"Error loading feedbacks: {e}")
            feedbacks = []

    st.caption(f"{len(feedbacks)} feedback(s) loaded")

    for fb_data in feedbacks:
        fb: AgentFeedback = fb_data["feedback"]
        rating_str = fb.rating.value if fb.rating else "—"
        rating_color = "#388e3c" if rating_str == "POSITIVE" else "#d32f2f" if rating_str == "NEGATIVE" else "#999"
        rating_badge = (
            f'<span style="background:{rating_color};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:0.85em;">{rating_str}</span>'
        )

        cols = st.columns([1, 2, 2, 2, 1])
        with cols[0]:
            st.markdown(rating_badge, unsafe_allow_html=True)
        with cols[1]:
            if st.button(
                f"Session {fb_data['session_id'][:8]}…",
                key=f"fb_sess_{fb.agent_feedback_id}",
            ):
                st.query_params["session_id"] = fb_data["session_id"]
                st.rerun()
        with cols[2]:
            st.markdown(fb.comment or "—")
        with cols[3]:
            if fb_data["message_preview"]:
                st.caption(fb_data["message_preview"][:120])
            else:
                st.caption("(session-level)")
        with cols[4]:
            st.caption(_to_local(fb.created_at))

        if fb.structured:
            with st.expander("Structured data", expanded=False):
                st.json(fb.structured)
