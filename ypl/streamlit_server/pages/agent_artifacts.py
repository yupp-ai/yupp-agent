"""Agent Artifacts — browse all tracked artifacts across sessions, tasks, and agents."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import func
from sqlmodel import col, select
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.streamlit_server.auth import is_auth_configured, require_auth
from ypl.streamlit_server.permissions import Permission, has_permission

st.set_page_config(page_title="Agent Artifacts", page_icon="📦", layout="wide")
require_auth()

if is_auth_configured() and not has_permission(Permission.AGENT_HARNESS_ADMIN):
    st.error("You do not have permission to view Agent Artifacts.")
    st.stop()

# ── Timezone helpers ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _to_local(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_PACIFIC)
    return local.strftime(f"%Y-%m-%d %H:%M:%S {local.strftime('%Z')}")


def _time_ago(dt: datetime | None) -> str:
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


# ── Artifact type metadata ────────────────────────────────────────────────────

_ARTIFACT_TYPE_ICON: dict[AgentArtifactType, str] = {
    AgentArtifactType.YUPPASTE: "📝",
    AgentArtifactType.CODE_REVIEW: "🔍",
    AgentArtifactType.OTHER: "📦",
}

_ALL_TYPES = [t.value for t in AgentArtifactType]


# ── DB queries ────────────────────────────────────────────────────────────────


@retry_db
async def _fetch_artifacts_raw(
    artifact_type: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Fetch artifacts with optional type filter, returning dicts for caching."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact)
            .where(col(AgentArtifact.deleted_at).is_(None))
            .order_by(col(AgentArtifact.created_at).desc())
            .limit(limit)
        )
        if artifact_type:
            stmt = stmt.where(col(AgentArtifact.artifact_type) == AgentArtifactType(artifact_type))
        result = await session.exec(stmt)
        artifacts = result.all()
        return [
            {
                "artifact_id": str(a.agent_artifact_id),
                "type": a.artifact_type.value,
                "title": a.title,
                "description": a.description or "",
                "url": a.url,
                "session_id": str(a.agent_session_id) if a.agent_session_id else None,
                "task_id": str(a.agent_task_id) if a.agent_task_id else None,
                "creator_user_id": a.creator_user_id or "",
                "creator_agent_id": str(a.creator_agent_id) if a.creator_agent_id else None,
                "created_at": a.created_at,
                "metadata": a.artifact_metadata or {},
            }
            for a in artifacts
        ]


@retry_db
async def _fetch_artifact_counts() -> dict[str, int]:
    """Fetch artifact count per type."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(
                col(AgentArtifact.artifact_type),
                func.count(col(AgentArtifact.agent_artifact_id)).label("cnt"),
            )
            .where(col(AgentArtifact.deleted_at).is_(None))
            .group_by(col(AgentArtifact.artifact_type))
        )
        result = await session.exec(stmt)
        return {str(row[0].value): int(row[1]) for row in result.all()}


# ── Cached data loader ────────────────────────────────────────────────────────


@st.cache_data(ttl=60, show_spinner=False)
def _load_artifacts(artifact_type: str | None, limit: int) -> list[dict[str, Any]]:
    """Load artifacts, cached for 60 seconds."""
    return (
        run_coroutine_in_lit_worker(
            _fetch_artifacts_raw(artifact_type=artifact_type, limit=limit),
            timeout=30,
        )
        or []
    )


@st.cache_data(ttl=120, show_spinner=False)
def _load_counts() -> dict[str, int]:
    """Load artifact counts per type, cached for 2 minutes."""
    return run_coroutine_in_lit_worker(_fetch_artifact_counts(), timeout=30) or {}


# ── Filtering helpers ─────────────────────────────────────────────────────────


def _apply_filters(
    rows: list[dict[str, Any]],
    keyword: str,
    group_by: str,
) -> list[dict[str, Any]]:
    """Filter rows by keyword search."""
    if keyword:
        kw = keyword.lower()
        rows = [
            r
            for r in rows
            if kw in r["title"].lower()
            or kw in r["description"].lower()
            or kw in (r["session_id"] or "").lower()
            or kw in (r["task_id"] or "").lower()
            or kw in r["type"].lower()
        ]
    return rows


def _group_rows(
    rows: list[dict[str, Any]],
    group_by: str,
) -> dict[str, list[dict[str, Any]]]:
    """Group rows by the chosen dimension."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if group_by == "Type":
            key = f"{_ARTIFACT_TYPE_ICON.get(AgentArtifactType(r['type']), '📦')} {r['type']}"
        elif group_by == "Session":
            key = r["session_id"] or "(no session)"
        else:
            key = "(all)"
    for r in rows:
        if group_by == "Type":
            key = f"{_ARTIFACT_TYPE_ICON.get(AgentArtifactType(r['type']), '📦')} {r['type']}"
        elif group_by == "Session":
            key = r["session_id"] or "(no session)"
        else:
            key = "(all)"
        groups.setdefault(key, []).append(r)
    return groups


# ── Row rendering ─────────────────────────────────────────────────────────────


def _render_artifact_rows(rows: list[dict[str, Any]], *, show_type: bool = True) -> None:
    """Render a list of artifact rows as a markdown table."""
    if not rows:
        st.caption("No artifacts found.")
        return

    header_parts = []
    sep_parts = []
    if show_type:
        header_parts += ["Type"]
        sep_parts += ["------"]
    header_parts += ["Title", "Description", "Session", "Created"]
    sep_parts += ["-------", "-------------", "--------", "--------"]

    header = "| " + " | ".join(header_parts) + " |"
    sep = "| " + " | ".join(sep_parts) + " |"

    table_rows = []
    for r in rows:
        icon = _ARTIFACT_TYPE_ICON.get(AgentArtifactType(r["type"]), "📦")
        type_str = f"{icon} {r['type']}"
        title_link = f"[{r['title']}]({r['url']})"
        desc = (r["description"] or "")[:70] + ("…" if r["description"] and len(r["description"]) > 70 else "")
        if r["session_id"]:
            sid = r["session_id"]
            session_str = f"[{sid[:8]}…](/agent_harness_console?session_id={sid})"
        elif r["task_id"]:
            session_str = f"task `{r['task_id'][:8]}…`"
        else:
            session_str = "—"
        created_str = _to_local(r["created_at"])
        ago_str = _time_ago(r["created_at"])
        created_display = f"{created_str} ({ago_str})" if ago_str else created_str

        row_parts = []
        if show_type:
            row_parts.append(type_str)
        row_parts += [title_link, desc, session_str, created_display]
        table_rows.append("| " + " | ".join(row_parts) + " |")

    st.markdown("\n".join([header, sep] + table_rows))


# ── Page layout ───────────────────────────────────────────────────────────────

st.title("📦 Agent Artifacts")

# Top stats row
with st.spinner("Loading counts…"):
    counts = _load_counts()

if counts:
    total = sum(counts.values())
    metric_cols = st.columns(len(_ALL_TYPES) + 1)
    with metric_cols[0]:
        st.metric("Total", total)
    for i, t in enumerate(_ALL_TYPES, 1):
        icon = _ARTIFACT_TYPE_ICON.get(AgentArtifactType(t), "📦")
        with metric_cols[i]:
            st.metric(f"{icon} {t}", counts.get(t, 0))

st.divider()

# ── Filters sidebar ───────────────────────────────────────────────────────────

filter_cols = st.columns([2, 2, 3, 1])

with filter_cols[0]:
    type_filter = st.selectbox(
        "Filter by type",
        options=["(all)"] + _ALL_TYPES,
        key="artifact_type_filter",
    )

with filter_cols[1]:
    group_by = st.selectbox(
        "Group by",
        options=["(none)", "Type", "Session"],
        key="artifact_group_by",
    )

with filter_cols[2]:
    keyword = st.text_input("🔍 Search (title, description, session ID…)", key="artifact_keyword")

with filter_cols[3]:
    limit = st.selectbox("Max rows", options=[100, 250, 500, 1000], index=1, key="artifact_limit")

refresh_col, _ = st.columns([1, 9])
with refresh_col:
    if st.button("🔄 Refresh", key="artifact_refresh"):
        st.cache_data.clear()
        st.rerun()

# ── Load & filter data ────────────────────────────────────────────────────────

with st.spinner("Loading artifacts…"):
    type_param = type_filter if type_filter != "(all)" else None
    all_rows = _load_artifacts(type_param, limit)

filtered = _apply_filters(all_rows, keyword=keyword.strip(), group_by=group_by)

_count_suffix = f" (filtered from {len(all_rows)})" if len(filtered) != len(all_rows) else ""
st.caption(f"Showing {len(filtered)} artifact(s){_count_suffix}")

# ── Render ────────────────────────────────────────────────────────────────────

if group_by != "(none)":
    grouped = _group_rows(filtered, group_by)
    for group_key in sorted(grouped.keys()):
        group_rows = grouped[group_key]
        with st.expander(f"{group_key} ({len(group_rows)})", expanded=True):
            _render_artifact_rows(group_rows, show_type=(group_by != "Type"))
else:
    _render_artifact_rows(filtered, show_type=True)
