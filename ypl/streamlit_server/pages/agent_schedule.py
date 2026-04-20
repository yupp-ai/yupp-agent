"""Agent Schedules — view and manage scheduled agent calls."""

from __future__ import annotations
import html
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.llm.db_helpers import get_user_id_by_email
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import (
    Agent,
    AgentArtifact,
    AgentArtifactType,
    AgentSchedule,
    AgentScheduleRun,
    AgentScheduleStatus,
    AgentScheduleType,
)
from ypl.db.users import User
from ypl.streamlit_server.auth import require_auth
from ypl.streamlit_server.permissions import get_current_user_email

st.set_page_config(page_title="Agent Schedules", page_icon="📅", layout="wide")
require_auth()

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
        return "in the future"
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


def _time_until(dt: datetime | None) -> str:
    """Return a human-readable relative time string like 'in 2 hrs 15 mins'."""
    if dt is None:
        return ""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "now"
    total_minutes = total_seconds // 60
    if total_minutes < 1:
        return "in <1 min"
    if total_minutes < 60:
        return f"in {total_minutes} mins"
    hours = total_minutes // 60
    mins = total_minutes % 60
    if hours < 24:
        return f"in {hours} hrs {mins} mins" if mins else f"in {hours} hrs"
    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours > 0:
        return f"in {days}d {remaining_hours}hrs"
    return f"in {days}d"


# ── Status styling ───────────────────────────────────────────────────────────

_STATUS_EMOJI: dict[AgentScheduleStatus, str] = {
    AgentScheduleStatus.PENDING: "🔵",
    AgentScheduleStatus.IN_PROGRESS: "🟢",
    AgentScheduleStatus.COMPLETED: "✅",
    AgentScheduleStatus.FAILED: "❌",
    AgentScheduleStatus.CANCELLED: "⚪",
    AgentScheduleStatus.PAUSED: "⏸️",
}

_RUN_STATUS_EMOJI: dict[str, str] = {
    "PENDING": "🔵",
    "IN_PROGRESS": "🟢",
    "COMPLETED": "✅",
    "FAILED": "❌",
}

_ARTIFACT_TYPE_ICON: dict[AgentArtifactType, str] = {
    AgentArtifactType.YUPPASTE: "📝",
    AgentArtifactType.CODE_REVIEW: "🔍",
    AgentArtifactType.OTHER: "📦",
}


def _md_cell(s: str) -> str:
    """Escape pipe characters and newlines so the string is safe in a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_all_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).order_by(col(Agent.name)))
        return list(result.all())


@retry_db
async def fetch_schedules(
    status: str | None = None,
    schedule_type: str | None = None,
    agent_name: str | None = None,
    created_by_user: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch schedules with optional filters, including runs and creator names."""
    async with get_async_session_read_replica() as session:
        query = (
            select(AgentSchedule)
            .options(
                selectinload(AgentSchedule.agent),  # type: ignore[arg-type]
                selectinload(AgentSchedule.runs),  # type: ignore[arg-type]
            )
            .where(col(AgentSchedule.deleted_at).is_(None))
        )

        if status and status != "(all)":
            query = query.where(col(AgentSchedule.status) == AgentScheduleStatus(status))

        if schedule_type and schedule_type != "(all)":
            query = query.where(col(AgentSchedule.schedule_type) == AgentScheduleType(schedule_type))

        if agent_name and agent_name != "(all)":
            query = query.join(Agent).where(col(Agent.name) == agent_name)

        if created_by_user:
            query = query.where(col(AgentSchedule.created_by_user) == created_by_user)

        query = query.order_by(col(AgentSchedule.created_at).desc()).limit(limit)

        result = await session.exec(query)
        schedules = list(result.all())

    # Resolve creator user names
    creator_ids = {s.created_by_user for s in schedules if s.created_by_user}
    creator_names: dict[str, str] = {}
    if creator_ids:
        async with get_async_session_read_replica() as user_session:
            user_result = await user_session.exec(select(User).where(col(User.user_id).in_(list(creator_ids))))
            for u in user_result.all():
                creator_names[u.user_id] = u.name or u.email or u.user_id

    # Build output with agent display names, creator names, and pre-sorted runs
    return [
        {
            "schedule": s,
            "agent_name": s.agent.display_name if s.agent else "?",
            "agent_slug": s.agent.name if s.agent else "?",
            "created_by_name": creator_names.get(s.created_by_user, s.created_by_user or "?"),
            "runs": sorted(
                [r for r in s.runs if r.deleted_at is None],
                key=lambda r: r.run_number,
                reverse=True,
            )[:10],
        }
        for s in schedules
    ]


@retry_db
async def fetch_schedule_runs(schedule_id: uuid.UUID, limit: int = 20) -> list[AgentScheduleRun]:
    """Fetch execution history for a schedule."""
    async with get_async_session_read_replica() as session:
        query = (
            select(AgentScheduleRun)
            .where(col(AgentScheduleRun.agent_schedule_id) == schedule_id)
            .where(col(AgentScheduleRun.deleted_at).is_(None))
            .order_by(col(AgentScheduleRun.run_number).desc())
            .limit(limit)
        )
        result = await session.exec(query)
        return list(result.all())


@retry_db
async def fetch_run_session_artifacts(session_ids: list[uuid.UUID]) -> list[AgentArtifact]:
    """Fetch artifacts for a list of session IDs (from schedule runs)."""
    if not session_ids:
        return []
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact)
            .where(col(AgentArtifact.deleted_at).is_(None))
            .where(col(AgentArtifact.agent_session_id).in_(session_ids))
            .order_by(col(AgentArtifact.created_at).asc())
        )
        result = await session.exec(stmt)
        return list(result.all())


@retry_db
async def fetch_user_display_name(user_id: str) -> str | None:
    """Fetch user's display name from user_id."""
    async with get_async_session_read_replica() as session:
        user = await session.get(User, user_id)
        if user:
            return user.name or user.email
        return None


@retry_db
async def cancel_schedule(schedule_id: uuid.UUID, user_id: str) -> tuple[bool, str]:
    """Cancel a schedule. Returns (success, message)."""
    async with get_async_session() as session:
        schedule = await session.get(AgentSchedule, schedule_id)

        if not schedule:
            return False, "Schedule not found"

        if schedule.deleted_at is not None:
            return False, "Schedule has been deleted"

        if schedule.status not in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED):
            return False, f"Cannot cancel schedule with status {schedule.status.value}"

        if schedule.created_by_user != user_id:
            return False, "You can only cancel schedules you created"

        schedule.status = AgentScheduleStatus.CANCELLED
        session.add(schedule)
        await session.commit()

        return True, "Schedule cancelled successfully"


async def trigger_schedule_now(schedule_id: uuid.UUID, user_id: str) -> dict[str, Any]:
    """Trigger a recurring schedule immediately via the AHS REST endpoint."""
    import aiohttp
    from ypl.backend.config import settings

    base_url = settings.AGENT_HARNESS_SERVICE_BASE_URL
    api_key = settings.AGENT_HARNESS_SERVICE_API_KEY
    if not base_url:
        return {"success": False, "error": "AHS base URL is not configured"}

    url = f"{base_url}/ahs/schedule/{schedule_id}/trigger"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {"user_id": user_id}

    async with aiohttp.ClientSession() as session, session.post(url, json=payload, headers=headers) as resp:
        data = await resp.json()
        if resp.status == 200:
            return {"success": True, **data}
        return {"success": False, "error": data.get("detail", f"HTTP {resp.status}")}


# ── Main page logic ──────────────────────────────────────────────────────────

st.title("Agent Schedules")

# Load agents once for the dropdown
if "schedule_agents" not in st.session_state:
    with st.spinner("Loading agents..."):
        st.session_state.schedule_agents = run_coroutine_in_lit_worker(fetch_all_agents(), timeout=30)

agents: list[Agent] = st.session_state.schedule_agents
agent_names = [a.name for a in agents]

# Get current user's DB id (for "my schedules" filter)
current_user_id: str | None = None
if _current_email:
    current_user_id = run_coroutine_in_lit_worker(get_user_id_by_email(_current_email), timeout=10)

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_labels = ["📋 Schedules", "➕ Create Schedule"]
tabs = st.tabs(tab_labels)

# ── Schedule detail expander (shared by both sections) ──────────────────────


def _render_schedule_detail(
    schedule: AgentSchedule,
    agent_display: str,
    agent_slug: str,
    current_user_id: str | None,
    runs: list[AgentScheduleRun],
    *,
    action_label: str,
) -> None:
    """Render the detail panel for a selected schedule (right column)."""
    status_emoji = _STATUS_EMOJI.get(schedule.status, "")
    st.markdown(f"**Status:** {status_emoji} {schedule.status.value}")

    if schedule.description:
        st.markdown(f"**Description:** {schedule.description}")

    if schedule.schedule_type == AgentScheduleType.SCHEDULED:
        st.markdown(f"**Execute at:** {_to_local(schedule.execute_at)}")
    else:
        st.markdown(f"**Cron:** `{schedule.cron_expression}` ({schedule.cron_timezone})")

    st.markdown(f"**Run count:** {schedule.run_count}")
    if schedule.max_runs:
        st.markdown(f"**Max runs:** {schedule.max_runs}")

    created_ago = _time_ago(schedule.created_at)
    created_str = f"{_to_local(schedule.created_at)} ({created_ago})" if created_ago else _to_local(schedule.created_at)
    creator_info = f"**Created:** {created_str}"
    if schedule.created_by_agent:
        creator_info += f" by agent `{schedule.created_by_agent}`"
    st.markdown(creator_info)

    # Action buttons
    is_owner = current_user_id and schedule.created_by_user == current_user_id
    can_trigger = (
        schedule.schedule_type == AgentScheduleType.RECURRING
        and schedule.status == AgentScheduleStatus.PENDING
        and is_owner
    )
    can_cancel = schedule.status in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED) and is_owner

    if can_trigger or can_cancel:
        action_cols = st.columns(2)
        if can_trigger:
            with action_cols[0]:
                if st.button(action_label, key=f"trigger_{schedule.agent_schedule_id}", type="primary"):
                    with st.spinner("Triggering..."):
                        result = run_coroutine_in_lit_worker(
                            trigger_schedule_now(
                                schedule.agent_schedule_id,
                                current_user_id,  # type: ignore[arg-type]
                            ),
                            timeout=60,
                        )
                    if result.get("success"):
                        st.success(f"Triggered run #{result['run_number']}. Session: `{result['session_id'][:8]}...`")
                        st.rerun()
                    else:
                        st.error(result.get("error", "Unknown error"))
        if can_cancel:
            with action_cols[1] if can_trigger else action_cols[0]:
                if st.button("Cancel", key=f"cancel_{schedule.agent_schedule_id}", type="secondary"):
                    success, msg = run_coroutine_in_lit_worker(
                        cancel_schedule(schedule.agent_schedule_id, current_user_id),  # type: ignore[arg-type]
                        timeout=30,
                    )
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

    # Runs table
    if runs:
        st.markdown(f"**Runs:** Showing {len(runs)} of {schedule.run_count}")
        runs_md_parts = ["| # | Status | Started | Session | Error |", "|---|--------|---------|---------|-------|"]
        for run in runs:
            run_emoji = _RUN_STATUS_EMOJI.get(run.status.value, "")
            started_str = _to_local(run.started_at) if run.started_at else "—"
            if run.session_id:
                sid = str(run.session_id)
                session_str = f"[{sid[:8]}...](/agent_harness_console?session_id={sid})"
            else:
                session_str = "—"
            error_str = run.error[:60] + "..." if run.error else ""
            runs_md_parts.append(f"| {run.run_number} | {run_emoji} | {started_str} | {session_str} | {error_str} |")
        st.markdown("\n".join(runs_md_parts))

    # ── Artifacts for run sessions ────────────────────────────────────────────
    run_session_ids = [run.session_id for run in runs if run.session_id is not None]
    if run_session_ids:
        with st.expander(f"📦 Artifacts ({len(run_session_ids)} run session(s))", expanded=False):
            with st.spinner("Loading artifacts…"):
                _artifacts = run_coroutine_in_lit_worker(
                    fetch_run_session_artifacts(run_session_ids),
                    timeout=30,
                )
            if not _artifacts:
                st.caption("No artifacts found for these run sessions.")
            else:
                rows = []
                for a in _artifacts:
                    icon = _ARTIFACT_TYPE_ICON.get(a.artifact_type, "📦")
                    type_str = f"{icon} {a.artifact_type.value}"
                    title_link = f"[{_md_cell(a.title)}]({a.url})"
                    if a.agent_session_id:
                        sid = str(a.agent_session_id)
                        run_ref = f"[{sid[:8]}](/agent_harness_console?session_id={sid})"
                    else:
                        run_ref = "—"
                    raw_desc = (a.description or "")[:60] + ("…" if a.description and len(a.description) > 60 else "")
                    desc = _md_cell(raw_desc)
                    rows.append(f"| {type_str} | {title_link} | {desc} | {run_ref} |")
                header = "| Type | Title | Description | Session |"
                sep = "|------|-------|-------------|---------|"
                st.markdown("\n".join([header, sep] + rows))

    # Message/prompt at the bottom
    st.markdown("**Message (prompt):**")
    msg_style = (
        "max-height:400px;overflow-y:auto;white-space:pre-wrap;"
        "word-wrap:break-word;background:#f8f9fa;"
        "color:#1a1a1a;border:1px solid #dee2e6;"
        "border-radius:6px;padding:12px;font-family:monospace;font-size:12px;"
    )
    escaped_message = html.escape(schedule.message)
    st.markdown(
        f'<div style="{msg_style}">{escaped_message}</div>',
        unsafe_allow_html=True,
    )


def _sort_key_next_run(sched_data: dict[str, Any]) -> tuple[int, float]:
    """Sort by status priority (pending/in-progress first), then next_run_at closeness."""
    schedule: AgentSchedule = sched_data["schedule"]
    # Pending/in-progress sort first (0), then paused (1), then failed (2)
    priority = {
        AgentScheduleStatus.PENDING: 0,
        AgentScheduleStatus.IN_PROGRESS: 0,
        AgentScheduleStatus.PAUSED: 1,
        AgentScheduleStatus.FAILED: 2,
    }
    status_rank = priority.get(schedule.status, 3)
    dt = schedule.next_run_at or schedule.execute_at
    if dt is None:
        return (status_rank, float("inf"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (status_rank, abs((dt - datetime.now(tz=UTC)).total_seconds()))


def _sort_key_last_run(sched_data: dict[str, Any]) -> float:
    """Sort by last run time descending (most recent first)."""
    schedule: AgentSchedule = sched_data["schedule"]
    # Use the most recent run's started_at, or fall back to created_at
    runs: list[AgentScheduleRun] = sched_data.get("runs", [])
    if runs:
        run_times = [t for r in runs if (t := (r.started_at or r.created_at)) is not None]
        latest = max(run_times) if run_times else None
        if latest:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            return -latest.timestamp()
    dt = schedule.created_at
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return -dt.timestamp()


# ── Tab 1: Schedules List ────────────────────────────────────────────────────

with tabs[0]:
    # Filters
    filter_cols = st.columns([1, 1, 2, 1, 1, 1])

    with filter_cols[0]:
        status_options = ["(all)"] + [s.value for s in AgentScheduleStatus]
        selected_status = st.selectbox("Status", status_options, key="sched_status")

    with filter_cols[1]:
        type_options = ["(all)"] + [t.value for t in AgentScheduleType]
        selected_type = st.selectbox("Type", type_options, key="sched_type")

    with filter_cols[2]:
        agent_options = ["(all)"] + agent_names
        selected_agent = st.selectbox("Agent", agent_options, key="sched_agent")

    with filter_cols[3]:
        limit_options = [20, 50, 100, 200]
        limit = st.selectbox("Limit", limit_options, index=1, key="sched_limit")

    with filter_cols[4]:
        my_schedules_only = st.checkbox("My schedules", key="sched_mine")

    with filter_cols[5]:
        show_old_inactive = st.checkbox("Show old inactive", key="sched_old_inactive", value=False)

    st.caption(f"Viewing schedules ({_current_username or 'local dev'})")

    created_by_filter = current_user_id if my_schedules_only else None
    schedules_data: list[dict[str, Any]] = []

    with st.spinner("Loading schedules..."):
        try:
            schedules_data = run_coroutine_in_lit_worker(
                fetch_schedules(
                    status=selected_status if selected_status != "(all)" else None,
                    schedule_type=selected_type if selected_type != "(all)" else None,
                    agent_name=selected_agent if selected_agent != "(all)" else None,
                    created_by_user=created_by_filter,
                    limit=int(limit),
                ),
                timeout=60,
            )
        except Exception as e:
            st.error(f"Error loading schedules: {e}")
            schedules_data = []

    # Split into one-time and recurring
    onetime_data = [d for d in schedules_data if d["schedule"].schedule_type == AgentScheduleType.SCHEDULED]
    recurring_data = [d for d in schedules_data if d["schedule"].schedule_type == AgentScheduleType.RECURRING]

    sub_tabs = st.tabs([f"Recurring ({len(recurring_data)})", f"One-time ({len(onetime_data)})"])

    _ACTIVE_STATUSES = {
        AgentScheduleStatus.PENDING,
        AgentScheduleStatus.IN_PROGRESS,
        AgentScheduleStatus.PAUSED,
        AgentScheduleStatus.FAILED,
    }
    _14_DAYS_AGO = datetime.now(tz=UTC) - timedelta(days=14)

    def _split_active_archived(
        data: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split schedules into active (pending/in-progress/paused/failed) and archived (completed/cancelled)."""
        active: list[dict[str, Any]] = []
        archived: list[dict[str, Any]] = []
        for d in data:
            s: AgentSchedule = d["schedule"]
            if s.status in _ACTIVE_STATUSES:
                active.append(d)
            else:
                # Completed/cancelled — apply 14-day cutoff unless show_old_inactive is on.
                # Use last run time (most recent activity) and fall back to created_at.
                # This prevents recently-completed jobs from being hidden just because
                # they were created >14 days ago (common for recurring jobs).
                last_run = _get_last_run_time(d)
                dt = last_run or s.modified_at or s.created_at
                if dt and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                if show_old_inactive or (dt and dt >= _14_DAYS_AGO):
                    archived.append(d)
        active.sort(key=_sort_key_next_run)
        archived.sort(key=_sort_key_last_run)
        return active, archived

    def _get_last_run_time(sched_data: dict[str, Any]) -> datetime | None:
        """Get the most recent run started_at from pre-loaded runs."""
        runs: list[AgentScheduleRun] = sched_data.get("runs", [])
        if not runs:
            return None
        return max((r.started_at for r in runs if r.started_at), default=None)

    def _render_job_list(
        data: list[dict[str, Any]],
        list_key: str,
        sel_key: str,
        is_recurring: bool,
        *,
        is_archived: bool = False,
    ) -> None:
        """Render the left-side job list rows. sel_key is the session_state key for selected schedule ID."""
        for idx, sched_data in enumerate(data):
            schedule: AgentSchedule = sched_data["schedule"]
            agent_display: str = sched_data["agent_name"]
            created_by_name: str = sched_data["created_by_name"]
            sched_id = str(schedule.agent_schedule_id)

            status_emoji = _STATUS_EMOJI.get(schedule.status, "")
            name_part = schedule.name or "Unnamed"
            has_next = schedule.status in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED)
            created_ago = _time_ago(schedule.created_at)

            if is_recurring:
                cron_str = schedule.cron_expression or "—"

                row = st.columns([0.4, 2, 1.2, 1.2, 1.2, 0.5, 1])
                row[0].markdown(status_emoji)
                row[4].markdown(f"`{cron_str}`")
                row[5].markdown(str(schedule.run_count))
            else:
                if is_archived:
                    # Show last run time instead of next run
                    last_run_dt = _get_last_run_time(sched_data)
                    time_col_str = _time_ago(last_run_dt) if last_run_dt else "—"
                else:
                    next_dt = (schedule.execute_at or schedule.next_run_at) if has_next else None
                    time_col_str = _time_until(next_dt) if next_dt else "—"

                row = st.columns([0.4, 2, 1.2, 1.2, 1, 1])
                row[0].markdown(status_emoji)
                row[4].markdown(created_by_name)
                row[5].markdown(created_ago)

            if row[1].button(
                name_part,
                key=f"sel_{list_key}_{idx}",
                use_container_width=True,
            ):
                st.session_state[sel_key] = sched_id
                st.rerun()

            row[2].markdown(agent_display)
            if is_recurring:
                next_run_dt_display = schedule.next_run_at if has_next else None
                next_str = _time_until(next_run_dt_display) if next_run_dt_display else "—"
                row[3].markdown(
                    f'<span style="font-size:0.9em">{next_str}</span>',
                    unsafe_allow_html=True,
                )
                row[6].markdown(created_by_name)
            else:
                row[3].markdown(
                    f'<span style="font-size:0.9em">{time_col_str}</span>',
                    unsafe_allow_html=True,
                )

    def _render_list_header(is_recurring: bool, *, is_archived: bool = False) -> None:
        if is_recurring:
            hdr = st.columns([0.4, 2, 1.2, 1.2, 1.2, 0.5, 1])
            hdr[0].markdown("**St**")
            hdr[1].markdown("**Name**")
            hdr[2].markdown("**Agent**")
            hdr[3].markdown("**Next Run**")
            hdr[4].markdown("**Cron**")
            hdr[5].markdown("**#**")
            hdr[6].markdown("**Owner**")
        else:
            time_label = "**Last Run**" if is_archived else "**Next Run**"
            hdr = st.columns([0.4, 2, 1.2, 1.2, 1, 1])
            hdr[0].markdown("**St**")
            hdr[1].markdown("**Name**")
            hdr[2].markdown("**Agent**")
            hdr[3].markdown(time_label)
            hdr[4].markdown("**Owner**")
            hdr[5].markdown("**Created**")
        st.markdown("<hr style='margin:2px 0'>", unsafe_allow_html=True)

    def _render_schedule_panel(
        data: list[dict[str, Any]],
        panel_key: str,
        action_label: str,
        is_recurring: bool,
    ) -> None:
        """Render a 2:1 split panel with active/archived job lists on the left and details on the right."""
        if not data:
            st.info("No schedules found.")
            return

        active, archived = _split_active_archived(data)
        combined = active + archived
        # Build lookup by schedule ID
        by_id = {str(d["schedule"].agent_schedule_id): d for d in combined}

        sel_key = f"selected_{panel_key}"

        left_col, right_col = st.columns([2, 1])

        with left_col:
            # ── Active table ──
            st.markdown(f"**Active ({len(active)})**")
            if active:
                _render_list_header(is_recurring)
                _render_job_list(active, f"{panel_key}_active", sel_key, is_recurring)
            else:
                st.caption("No active schedules.")

            st.markdown("---")

            # ── Archived table ──
            st.markdown(f"**Completed / Cancelled ({len(archived)})**")
            if archived:
                _render_list_header(is_recurring, is_archived=True)
                _render_job_list(archived, f"{panel_key}_archived", sel_key, is_recurring, is_archived=True)
            else:
                st.caption("No archived schedules.")

        with right_col:
            selected_id = st.session_state.get(sel_key)
            selected_data = by_id.get(selected_id) if selected_id else None
            if selected_data is None and combined:
                selected_data = combined[0]

            if selected_data:
                sel_schedule: AgentSchedule = selected_data["schedule"]
                st.markdown(f"#### {sel_schedule.name or 'Unnamed'}")
                _render_schedule_detail(
                    sel_schedule,
                    selected_data["agent_name"],
                    selected_data["agent_slug"],
                    current_user_id,
                    selected_data["runs"],
                    action_label=action_label,
                )

    with sub_tabs[0]:
        _render_schedule_panel(recurring_data, "recurring", "Run One-off", is_recurring=True)

    with sub_tabs[1]:
        _render_schedule_panel(onetime_data, "onetime", "Run Now", is_recurring=False)

# ── Tab 2: Create Schedule ───────────────────────────────────────────────────

with tabs[1]:
    st.markdown("### Create a New Schedule")
    st.markdown("Schedule an agent to run at a specific time or on a recurring basis.")

    if st.button("Create Schedule", type="primary"):
        st.info(
            "To create a new agent schedule, please talk to **@Lingfengovich** in the "
            "**#agentic-couch** Slack channel. Agent schedules should be created through agents "
            "to ensure proper configuration and validation."
        )
