"""Agent Schedules — view and manage scheduled agent calls.

Three rendering modes share this single page:

* **Browse** (default): a unified list of all schedules with filters. Schedule
  *names* are markdown links that navigate to the View page via the
  ``schedule_id`` query param.
* **View / Edit** (``?schedule_id=<uuid>``): a full-width detail panel that
  lets the creator edit description / prompt / agent / schedule / state with
  an explicit confirmation step before the change is committed.
* **Add** (the ➕ tab): a real form for creating one-time or recurring
  schedules.
"""

from __future__ import annotations
import html
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

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
from ypl.mcp_common.scheduled_agent_call_helpers import (
    compute_next_run_for_cron,
    edit_agent_schedule_fields,
    parse_execute_at,
    validate_cron_expression,
    validate_timezone,
)
from ypl.mcp_common.scheduled_agent_call_helpers import (
    create_agent_schedule as create_agent_schedule_helper,
)
from ypl.streamlit_server.auth import require_auth
from ypl.streamlit_server.permissions import get_current_user_email

st.set_page_config(page_title="Agent Schedules", page_icon="📅", layout="wide")
require_auth()

_current_email = get_current_user_email()
_current_username = _current_email.split("@")[0] if _current_email else None

# ── Timezone helper ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")
_COMMON_TIMEZONES = [
    "UTC",
    "America/Los_Angeles",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
]


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


# ── Status / type styling ─────────────────────────────────────────────────────

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
    AgentArtifactType.TEXT: "📝",
    AgentArtifactType.CODE_REVIEW: "🔍",
    AgentArtifactType.OTHER: "📦",
}

_TYPE_EMOJI: dict[AgentScheduleType, str] = {
    AgentScheduleType.RECURRING: "🔁",
    AgentScheduleType.SCHEDULED: "📅",
}

_TYPE_LABEL: dict[AgentScheduleType, str] = {
    AgentScheduleType.RECURRING: "Recurring",
    AgentScheduleType.SCHEDULED: "One-time",
}


def _md_cell(s: str) -> str:
    """Escape pipe characters and newlines so the string is safe in a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_all_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).where(col(Agent.deleted_at).is_(None)).order_by(col(Agent.name)))
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

    creator_ids = {s.created_by_user for s in schedules if s.created_by_user}
    creator_names: dict[str, str] = {}
    if creator_ids:
        async with get_async_session_read_replica() as user_session:
            user_result = await user_session.exec(select(User).where(col(User.user_id).in_(list(creator_ids))))
            for u in user_result.all():
                creator_names[u.user_id] = u.name or u.email or u.user_id

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
async def fetch_schedule_with_runs(schedule_id: uuid.UUID) -> dict[str, Any] | None:
    """Fetch a single schedule with eager-loaded agent + runs, plus creator name."""
    async with get_async_session_read_replica() as session:
        result = await session.exec(
            select(AgentSchedule)
            .options(
                selectinload(AgentSchedule.agent),  # type: ignore[arg-type]
                selectinload(AgentSchedule.runs),  # type: ignore[arg-type]
            )
            .where(col(AgentSchedule.agent_schedule_id) == schedule_id)
            .where(col(AgentSchedule.deleted_at).is_(None))
        )
        schedule = result.one_or_none()

    if schedule is None:
        return None

    creator_name = schedule.created_by_user or "?"
    if schedule.created_by_user:
        async with get_async_session_read_replica() as user_session:
            user = await user_session.get(User, schedule.created_by_user)
            if user:
                creator_name = user.name or user.email or user.user_id

    return {
        "schedule": schedule,
        "agent_name": schedule.agent.display_name if schedule.agent else "?",
        "agent_slug": schedule.agent.name if schedule.agent else "?",
        "created_by_name": creator_name,
        "runs": sorted(
            [r for r in schedule.runs if r.deleted_at is None],
            key=lambda r: r.run_number,
            reverse=True,
        )[:20],
    }


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


@retry_db
async def update_schedule_status(
    schedule_id: uuid.UUID,
    user_id: str,
    new_status: AgentScheduleStatus,
) -> tuple[bool, str]:
    """Transition a schedule between PENDING and PAUSED. Creator-only.

    Cancellation goes through ``cancel_schedule`` separately so the destructive
    path stays explicit.
    """
    if new_status not in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED):
        return False, f"Status {new_status.value} cannot be set from this UI."

    async with get_async_session() as session:
        schedule = await session.get(AgentSchedule, schedule_id)
        if not schedule or schedule.deleted_at is not None:
            return False, "Schedule not found"
        if schedule.created_by_user != user_id:
            return False, "You can only edit schedules you created"
        if schedule.status not in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED):
            return False, (
                f"Cannot change status from {schedule.status.value}. "
                "Only PENDING or PAUSED schedules can be paused/resumed."
            )
        if schedule.status == new_status:
            return True, f"Status already {new_status.value}"
        schedule.status = new_status
        session.add(schedule)
        await session.commit()
    return True, f"Status updated to {new_status.value}"


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


# ── Helpers used by both list and detail views ──────────────────────────────


def _schedule_when(schedule: AgentSchedule) -> str:
    """Return a short human-readable 'when' string for a schedule (cron expr or execute_at)."""
    if schedule.schedule_type == AgentScheduleType.RECURRING:
        cron = schedule.cron_expression or "—"
        return f"`{cron}` ({schedule.cron_timezone})"
    if schedule.execute_at is None:
        return "—"
    return _to_local(schedule.execute_at)


def _next_or_last_run(sched_data: dict[str, Any]) -> str:
    """Return 'in 2 hrs' (active) or 'last 5 min ago' (finished) for the table row."""
    schedule: AgentSchedule = sched_data["schedule"]
    if schedule.status in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED):
        next_dt = schedule.next_run_at or schedule.execute_at
        return _time_until(next_dt) if next_dt else "—"
    runs: list[AgentScheduleRun] = sched_data.get("runs", [])
    last_run_dt = max((r.started_at for r in runs if r.started_at), default=None)
    return f"last {_time_ago(last_run_dt)}" if last_run_dt else "—"


def _sort_key(sched_data: dict[str, Any]) -> tuple[int, int, float]:
    """Sort: type rank (recurring first), status priority, next_run closeness."""
    schedule: AgentSchedule = sched_data["schedule"]
    type_rank = 0 if schedule.schedule_type == AgentScheduleType.RECURRING else 1
    priority = {
        AgentScheduleStatus.PENDING: 0,
        AgentScheduleStatus.IN_PROGRESS: 0,
        AgentScheduleStatus.PAUSED: 1,
        AgentScheduleStatus.FAILED: 2,
    }
    status_rank = priority.get(schedule.status, 3)
    dt = schedule.next_run_at or schedule.execute_at
    if dt is None:
        return (type_rank, status_rank, float("inf"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (type_rank, status_rank, abs((dt - datetime.now(tz=UTC)).total_seconds()))


# ── Detail / Edit page ──────────────────────────────────────────────────────


def _back_to_browse_link() -> None:
    st.markdown("[← Back to Schedules](?/)", help="Return to the Browse view.")


def _render_runs_and_artifacts(
    schedule: AgentSchedule,
    runs: list[AgentScheduleRun],
) -> None:
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


def _datetime_widgets(
    label_prefix: str,
    initial_dt_utc: datetime | None,
    initial_tz: str,
    *,
    key_prefix: str,
) -> tuple[date, time, str]:
    """Render date / time / timezone widgets and return their current values.

    The default timezone is the schedule's saved tz; the displayed time is the
    UTC-stored time converted into that timezone so the user is editing the
    same instant they originally scheduled.
    """
    tz_options = sorted({initial_tz, *_COMMON_TIMEZONES})
    tz_value = st.selectbox(
        f"{label_prefix} timezone",
        options=tz_options,
        index=tz_options.index(initial_tz) if initial_tz in tz_options else 0,
        key=f"{key_prefix}_tz",
    )
    if initial_dt_utc is not None:
        if initial_dt_utc.tzinfo is None:
            initial_dt_utc = initial_dt_utc.replace(tzinfo=UTC)
        try:
            local_dt = initial_dt_utc.astimezone(ZoneInfo(tz_value))
        except Exception:
            local_dt = initial_dt_utc
    else:
        local_dt = datetime.now(tz=ZoneInfo(tz_value)) + timedelta(hours=1)

    col_d, col_t = st.columns(2)
    with col_d:
        date_value = st.date_input(f"{label_prefix} date", value=local_dt.date(), key=f"{key_prefix}_date")
    with col_t:
        time_value = st.time_input(
            f"{label_prefix} time",
            value=local_dt.time().replace(microsecond=0),
            key=f"{key_prefix}_time",
        )
    return date_value, time_value, tz_value


def _changes_diff(
    current: AgentSchedule,
    *,
    new_name: str,
    new_description: str,
    new_message: str,
    new_agent_slug: str,
    current_agent_slug: str,
    new_status: AgentScheduleStatus,
    new_cron: str | None,
    new_execute_at_iso: str | None,
    new_timezone: str | None,
    new_max_runs: int | None,
) -> list[tuple[str, str, str]]:
    """Compute (field, old_repr, new_repr) tuples for every changed field."""
    diffs: list[tuple[str, str, str]] = []

    def _add(field: str, old: Any, new: Any) -> None:
        if (old or "") == (new or ""):
            return
        diffs.append(
            (
                field,
                str(old) if old not in (None, "") else "—",
                str(new) if new not in (None, "") else "—",
            )
        )

    _add("name", current.name, new_name)
    _add("description", current.description, new_description)
    _add("message (prompt)", current.message, new_message)
    _add("agent", current_agent_slug, new_agent_slug)
    _add("status", current.status.value, new_status.value)
    if current.schedule_type == AgentScheduleType.RECURRING:
        _add("cron", current.cron_expression, new_cron)
        _add("timezone", current.cron_timezone, new_timezone)
        _add("max_runs", current.max_runs, new_max_runs)
    else:
        old_iso = current.execute_at.isoformat() if current.execute_at else None
        _add("execute_at", old_iso, new_execute_at_iso)
        _add("timezone", current.cron_timezone, new_timezone)
    return diffs


def _render_view_schedule(
    schedule_id: str,
    agents: list[Agent],
    current_user_id: str | None,
) -> None:
    """Render the full-width detail / edit page for a single schedule."""
    _back_to_browse_link()

    try:
        sched_uuid = uuid.UUID(schedule_id.strip())
    except ValueError:
        st.error(f"Invalid schedule ID: `{schedule_id}`")
        return

    with st.spinner("Loading schedule…"):
        sched_data = run_coroutine_in_lit_worker(fetch_schedule_with_runs(sched_uuid), timeout=30)

    if sched_data is None:
        st.error(f"Schedule not found: `{schedule_id}`")
        return

    schedule: AgentSchedule = sched_data["schedule"]
    agent_slug: str = sched_data["agent_slug"]
    runs: list[AgentScheduleRun] = sched_data["runs"]

    type_emoji = _TYPE_EMOJI.get(schedule.schedule_type, "")
    type_label = _TYPE_LABEL.get(schedule.schedule_type, schedule.schedule_type.value)
    status_emoji = _STATUS_EMOJI.get(schedule.status, "")

    st.markdown(
        f"## {type_emoji} {html.escape(schedule.name or 'Unnamed')} "
        f"<span style='font-size:0.7em;color:#666'>{type_label}</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"`{schedule.agent_schedule_id}`")

    info_cols = st.columns(4)
    info_cols[0].markdown(f"**Status:** {status_emoji} {schedule.status.value}")
    info_cols[1].markdown(f"**Run count:** {schedule.run_count}")
    if schedule.schedule_type == AgentScheduleType.RECURRING:
        info_cols[2].markdown(
            f"**Next run:** {_time_until(schedule.next_run_at)}" if schedule.next_run_at else "**Next run:** —"
        )
    else:
        info_cols[2].markdown(
            f"**Execute at:** {_to_local(schedule.execute_at)}" if schedule.execute_at else "**Execute at:** —"
        )
    info_cols[3].markdown(f"**Created:** {_to_local(schedule.created_at)} ({_time_ago(schedule.created_at)})")

    is_owner = bool(current_user_id and schedule.created_by_user == current_user_id)
    is_editable = schedule.status in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED)
    can_edit = is_owner and is_editable

    can_trigger = (
        schedule.schedule_type == AgentScheduleType.RECURRING
        and schedule.status == AgentScheduleStatus.PENDING
        and is_owner
    )
    can_cancel = schedule.status in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED) and is_owner

    if can_trigger or can_cancel:
        action_cols = st.columns([1, 1, 6])
        if can_trigger:
            with action_cols[0]:
                if st.button("▶️ Run Now", key=f"trigger_{schedule.agent_schedule_id}", type="primary"):
                    with st.spinner("Triggering…"):
                        result = run_coroutine_in_lit_worker(
                            trigger_schedule_now(schedule.agent_schedule_id, current_user_id),  # type: ignore[arg-type]
                            timeout=60,
                        )
                    if result.get("success"):
                        st.success(f"Triggered run #{result['run_number']}. Session: `{result['session_id'][:8]}...`")
                        st.rerun()
                    else:
                        st.error(result.get("error", "Unknown error"))
        if can_cancel:
            with action_cols[1]:
                cancel_confirm_key = f"confirm_cancel_{schedule.agent_schedule_id}"
                if st.session_state.get(cancel_confirm_key):
                    if st.button("✅ Confirm cancel", key=f"do_cancel_{schedule.agent_schedule_id}", type="primary"):
                        success, msg = run_coroutine_in_lit_worker(
                            cancel_schedule(schedule.agent_schedule_id, current_user_id),  # type: ignore[arg-type]
                            timeout=30,
                        )
                        st.session_state.pop(cancel_confirm_key, None)
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                elif st.button("🛑 Cancel schedule", key=f"ask_cancel_{schedule.agent_schedule_id}"):
                    st.session_state[cancel_confirm_key] = True
                    st.rerun()

    st.divider()

    if not is_owner:
        st.info("You can only edit schedules you created. Read-only view follows.")
    elif not is_editable:
        st.info(f"Schedule status is **{schedule.status.value}** — only PENDING or PAUSED schedules can be edited.")

    confirm_key = f"confirm_edit_{schedule.agent_schedule_id}"
    pending_changes_key = f"pending_changes_{schedule.agent_schedule_id}"

    edit_disabled = not can_edit
    agent_options = [a.name for a in agents]
    agent_display: dict[str, str] = {a.name: f"{a.display_name} (`{a.name}`)" for a in agents}

    with st.form(f"edit_form_{schedule.agent_schedule_id}", clear_on_submit=False):
        st.markdown("### Edit schedule")
        new_name = st.text_input("Name", value=schedule.name or "", disabled=edit_disabled)
        new_description = st.text_area(
            "Description",
            value=schedule.description or "",
            height=80,
            disabled=edit_disabled,
        )
        new_message = st.text_area(
            "Message (prompt)",
            value=schedule.message,
            height=240,
            disabled=edit_disabled,
            help="The message that will be sent to the agent on each run.",
        )

        if agent_slug not in agent_options and agent_slug != "?":
            agent_options = [agent_slug, *agent_options]
            agent_display[agent_slug] = f"{agent_slug} (current)"
        agent_idx = agent_options.index(agent_slug) if agent_slug in agent_options else 0
        new_agent_slug = st.selectbox(
            "Agent",
            options=agent_options,
            index=agent_idx,
            format_func=lambda n: agent_display.get(n, n),
            disabled=edit_disabled,
        )

        state_options = [AgentScheduleStatus.PENDING.value, AgentScheduleStatus.PAUSED.value]
        if schedule.status.value not in state_options:
            state_options = [schedule.status.value]
        new_status_str = st.selectbox(
            "State",
            options=state_options,
            index=state_options.index(schedule.status.value) if schedule.status.value in state_options else 0,
            disabled=edit_disabled or len(state_options) == 1,
            help="Pause to skip upcoming runs without cancelling. Use the Cancel button to permanently stop.",
        )
        new_status = AgentScheduleStatus(new_status_str)

        new_cron: str | None = None
        new_execute_at_iso: str | None = None
        new_timezone: str | None = None
        new_max_runs: int | None = None

        if schedule.schedule_type == AgentScheduleType.RECURRING:
            st.markdown("**Recurrence**")
            col_cron, col_tz = st.columns([2, 1])
            with col_cron:
                new_cron = st.text_input(
                    "Cron expression",
                    value=schedule.cron_expression or "",
                    disabled=edit_disabled,
                    help="Standard 5-field cron (e.g. `0 9 * * MON`).",
                )
            with col_tz:
                tz_options = sorted({schedule.cron_timezone, *_COMMON_TIMEZONES})
                new_timezone = st.selectbox(
                    "Cron timezone",
                    options=tz_options,
                    index=tz_options.index(schedule.cron_timezone) if schedule.cron_timezone in tz_options else 0,
                    disabled=edit_disabled,
                )
            new_max_runs_raw = st.number_input(
                "Max runs (0 = unlimited)",
                min_value=0,
                value=int(schedule.max_runs or 0),
                step=1,
                disabled=edit_disabled,
                help="Hard cap on total runs. Leave 0 for an unbounded recurring schedule.",
            )
            new_max_runs = int(new_max_runs_raw) if new_max_runs_raw > 0 else None
        else:
            st.markdown("**Execute at**")
            d, t, tz = _datetime_widgets(
                "Execute",
                schedule.execute_at,
                schedule.cron_timezone or "UTC",
                key_prefix=f"execute_{schedule.agent_schedule_id}",
            )
            local_dt = datetime.combine(d, t, tzinfo=ZoneInfo(tz))
            new_execute_at_iso = local_dt.isoformat()
            new_timezone = tz

        review_clicked = st.form_submit_button(
            "Review changes…",
            type="primary",
            disabled=edit_disabled,
        )

    if review_clicked and can_edit:
        diffs = _changes_diff(
            schedule,
            new_name=new_name.strip(),
            new_description=new_description.strip(),
            new_message=new_message,
            new_agent_slug=str(new_agent_slug),
            current_agent_slug=agent_slug,
            new_status=new_status,
            new_cron=new_cron.strip() if new_cron else None,
            new_execute_at_iso=new_execute_at_iso,
            new_timezone=new_timezone,
            new_max_runs=new_max_runs,
        )
        if not diffs:
            st.info("No changes to save.")
            st.session_state.pop(confirm_key, None)
            st.session_state.pop(pending_changes_key, None)
        else:
            st.session_state[confirm_key] = True
            st.session_state[pending_changes_key] = {
                "name": new_name.strip(),
                "description": new_description.strip(),
                "message": new_message,
                "agent_name": str(new_agent_slug),
                "status": new_status.value,
                "cron_expression": new_cron.strip() if new_cron else None,
                "execute_at": new_execute_at_iso,
                "timezone": new_timezone,
                "max_runs": new_max_runs,
                "diffs": diffs,
            }

    if st.session_state.get(confirm_key) and can_edit:
        pending = st.session_state.get(pending_changes_key, {})
        diffs_pending: list[tuple[str, str, str]] = pending.get("diffs", [])
        st.warning("Please review the pending changes before submitting.")
        diff_md = ["| Field | Before | After |", "|-------|--------|-------|"]
        for field, old, new in diffs_pending:
            old_repr = _md_cell(old)
            new_repr = _md_cell(new)
            if len(old_repr) > 80:
                old_repr = old_repr[:80] + "…"
            if len(new_repr) > 80:
                new_repr = new_repr[:80] + "…"
            diff_md.append(f"| **{field}** | {old_repr} | {new_repr} |")
        st.markdown("\n".join(diff_md))

        confirm_cols = st.columns([1, 1, 4])
        with confirm_cols[0]:
            if st.button("✅ Submit change", type="primary", key=f"submit_edit_{schedule.agent_schedule_id}"):
                _apply_edit(schedule, current_user_id, pending)
                st.session_state.pop(confirm_key, None)
                st.session_state.pop(pending_changes_key, None)
        with confirm_cols[1]:
            if st.button("Cancel", key=f"cancel_edit_{schedule.agent_schedule_id}"):
                st.session_state.pop(confirm_key, None)
                st.session_state.pop(pending_changes_key, None)
                st.rerun()

    st.divider()
    st.markdown("### Run history")
    _render_runs_and_artifacts(schedule, runs)


def _apply_edit(
    schedule: AgentSchedule,
    current_user_id: str | None,
    pending: dict[str, Any],
) -> None:
    """Apply pending edit to the schedule via mcp_common helpers, then rerun."""
    if not current_user_id:
        st.error("Not signed in.")
        return

    edit_kwargs: dict[str, Any] = {
        "agent_schedule_id": str(schedule.agent_schedule_id),
        "caller_user_id": current_user_id,
    }
    if pending["name"] != (schedule.name or ""):
        edit_kwargs["name"] = pending["name"]
    if pending["description"] != (schedule.description or ""):
        edit_kwargs["description"] = pending["description"]
    if pending["message"] != schedule.message:
        edit_kwargs["message"] = pending["message"]

    new_agent_name = pending["agent_name"]
    current_agent_slug = schedule.agent.name if schedule.agent else None
    if new_agent_name and new_agent_name != current_agent_slug:
        edit_kwargs["agent_name"] = new_agent_name

    if schedule.schedule_type == AgentScheduleType.RECURRING:
        new_cron = pending.get("cron_expression")
        if new_cron and new_cron != schedule.cron_expression:
            edit_kwargs["cron_expression"] = new_cron
        new_tz = pending.get("timezone")
        if new_tz and new_tz != schedule.cron_timezone:
            edit_kwargs["timezone"] = new_tz
        if pending.get("max_runs") != schedule.max_runs:
            edit_kwargs["max_runs"] = pending.get("max_runs")
    else:
        new_iso = pending.get("execute_at")
        old_iso = schedule.execute_at.isoformat() if schedule.execute_at else None
        if new_iso and new_iso != old_iso:
            edit_kwargs["execute_at"] = new_iso
        new_tz = pending.get("timezone")
        if new_tz and new_tz != schedule.cron_timezone:
            edit_kwargs["timezone"] = new_tz

    field_changes = {k: v for k, v in edit_kwargs.items() if k not in ("agent_schedule_id", "caller_user_id")}

    error: str | None = None
    if field_changes:
        result = run_coroutine_in_lit_worker(
            edit_agent_schedule_fields(**edit_kwargs),
            timeout=30,
        )
        if not result.get("success"):
            error = result.get("error", "Unknown error")

    if not error:
        new_status = AgentScheduleStatus(pending["status"])
        if new_status != schedule.status:
            success, msg = run_coroutine_in_lit_worker(
                update_schedule_status(schedule.agent_schedule_id, current_user_id, new_status),
                timeout=15,
            )
            if not success:
                error = msg

    if error:
        st.error(f"Save failed: {error}")
    else:
        st.success("Schedule updated.")
        st.rerun()


# ── Browse list view ────────────────────────────────────────────────────────


def _render_browse(
    schedules_data: list[dict[str, Any]],
    selected_type_filter: str,
    show_old_inactive: bool,
) -> None:
    """Render the unified list of schedules."""
    filtered = list(schedules_data)
    if selected_type_filter == "Recurring":
        filtered = [d for d in filtered if d["schedule"].schedule_type == AgentScheduleType.RECURRING]
    elif selected_type_filter == "One-time":
        filtered = [d for d in filtered if d["schedule"].schedule_type == AgentScheduleType.SCHEDULED]

    cutoff = datetime.now(tz=UTC) - timedelta(days=14)
    if not show_old_inactive:
        terminal = (AgentScheduleStatus.COMPLETED, AgentScheduleStatus.CANCELLED)
        kept = []
        for d in filtered:
            s: AgentSchedule = d["schedule"]
            if s.status not in terminal:
                kept.append(d)
                continue
            runs: list[AgentScheduleRun] = d.get("runs", [])
            last_run = max((r.started_at for r in runs if r.started_at), default=None)
            ref = last_run or s.modified_at or s.created_at
            if ref and ref.tzinfo is None:
                ref = ref.replace(tzinfo=UTC)
            if ref and ref >= cutoff:
                kept.append(d)
        filtered = kept

    filtered.sort(key=_sort_key)

    if not filtered:
        st.info("No schedules match the current filters.")
        return

    header = "| | Type | Name | Agent | When | Next / Last | Owner | Created |"
    sep = "|--|------|------|-------|------|-------------|-------|---------|"
    lines = [header, sep]

    for d in filtered:
        s = d["schedule"]
        sched_id = str(s.agent_schedule_id)
        st_emoji = _STATUS_EMOJI.get(s.status, "")
        type_emoji = _TYPE_EMOJI.get(s.schedule_type, "")
        type_label = _TYPE_LABEL.get(s.schedule_type, s.schedule_type.value)
        type_cell = f"{type_emoji} {type_label}"

        name = s.name or "Unnamed"
        name_cell = f"[{_md_cell(name)}](?schedule_id={sched_id})"

        agent_cell = _md_cell(d["agent_name"])
        when_cell = _md_cell(_schedule_when(s))
        next_or_last = _md_cell(_next_or_last_run(d))
        owner_cell = _md_cell(d["created_by_name"])
        created_cell = _md_cell(_time_ago(s.created_at) or "—")

        lines.append(
            f"| {st_emoji} | {type_cell} | {name_cell} | {agent_cell} | {when_cell} "
            f"| {next_or_last} | {owner_cell} | {created_cell} |"
        )

    st.markdown("\n".join(lines), unsafe_allow_html=False)
    st.caption(f"{len(filtered)} schedule(s) shown. Click a name to open the detail view.")


# ── Add schedule tab ────────────────────────────────────────────────────────


def _render_add_schedule(agents: list[Agent], current_user_id: str | None) -> None:
    """Render the Add Schedule form."""
    st.markdown("### Create a new schedule")
    st.caption(
        "Schedules are stored in the `agent_schedules` table. The agent will receive the **message** "
        "as a prompt every time the schedule fires."
    )

    if not current_user_id:
        st.warning("Sign in is required to create a schedule.")
        return
    if not agents:
        st.warning("No agents are available — create one in the Agents page first.")
        return

    schedule_kind = st.radio(
        "Schedule type",
        options=[_TYPE_LABEL[AgentScheduleType.RECURRING], _TYPE_LABEL[AgentScheduleType.SCHEDULED]],
        index=0,
        horizontal=True,
        key="add_schedule_kind",
    )
    is_recurring = schedule_kind == _TYPE_LABEL[AgentScheduleType.RECURRING]

    confirm_key = "add_schedule_confirm"
    pending_key = "add_schedule_pending"
    agent_slugs = [a.name for a in agents]
    agent_display = {a.name: f"{a.display_name} (`{a.name}`)" for a in agents}

    with st.form("add_schedule_form", clear_on_submit=False):
        name = st.text_input("Name", help="A short identifier for this schedule.")
        description = st.text_area("Description (optional)", height=60)
        agent_slug = st.selectbox(
            "Agent",
            options=agent_slugs,
            format_func=lambda n: agent_display.get(n, n),
        )
        message = st.text_area(
            "Message (prompt)",
            height=240,
            help="The text the agent will receive when this schedule fires.",
        )

        cron_expression: str | None = None
        execute_at_iso: str | None = None
        timezone_value: str = "UTC"
        max_runs: int | None = None
        context_str: str = ""

        if is_recurring:
            col_cron, col_tz = st.columns([2, 1])
            with col_cron:
                cron_expression = st.text_input(
                    "Cron expression",
                    value="",
                    placeholder="e.g. 0 9 * * MON  (every Monday at 09:00)",
                )
            with col_tz:
                timezone_value = st.selectbox("Timezone", options=_COMMON_TIMEZONES, index=1, key="add_tz_recurring")
            max_runs_raw = st.number_input("Max runs (0 = unlimited)", min_value=0, value=0, step=1)
            max_runs = int(max_runs_raw) if max_runs_raw > 0 else None
        else:
            d, t, tz = _datetime_widgets(
                "Execute",
                None,
                "America/Los_Angeles",
                key_prefix="add_execute",
            )
            local_dt = datetime.combine(d, t, tzinfo=ZoneInfo(tz))
            execute_at_iso = local_dt.isoformat()
            timezone_value = tz

        with st.expander("Advanced — context JSON (optional)"):
            context_str = st.text_area(
                "Context",
                value="",
                height=120,
                help="Optional JSON object passed to the agent session as `context`. Leave blank for none.",
            )

        review_clicked = st.form_submit_button("Review…", type="primary")

    if review_clicked:
        errors: list[str] = []
        if not name.strip():
            errors.append("Name is required.")
        if not message.strip():
            errors.append("Message (prompt) is required.")
        if is_recurring:
            if not cron_expression or not cron_expression.strip():
                errors.append("Cron expression is required for recurring schedules.")
            else:
                tz_err = validate_timezone(timezone_value)
                if tz_err:
                    errors.append(tz_err)
                else:
                    cron_err = validate_cron_expression(cron_expression.strip(), timezone_value)
                    if cron_err:
                        errors.append(cron_err)
        else:
            tz_err = validate_timezone(timezone_value)
            if tz_err:
                errors.append(tz_err)
            elif execute_at_iso is not None:
                _, dt_err = parse_execute_at(execute_at_iso, timezone_value)
                if dt_err:
                    errors.append(dt_err)

        context_dict: dict[str, Any] | None = None
        if context_str.strip():
            try:
                parsed = json.loads(context_str)
                if not isinstance(parsed, dict):
                    errors.append("Context must be a JSON object.")
                else:
                    context_dict = parsed
            except json.JSONDecodeError as e:
                errors.append(f"Invalid context JSON: {e}")

        if errors:
            for err in errors:
                st.error(err)
            st.session_state.pop(confirm_key, None)
            st.session_state.pop(pending_key, None)
        else:
            st.session_state[confirm_key] = True
            st.session_state[pending_key] = {
                "name": name.strip(),
                "description": description.strip() or None,
                "agent_name": str(agent_slug),
                "message": message,
                "schedule_type": (AgentScheduleType.RECURRING if is_recurring else AgentScheduleType.SCHEDULED),
                "cron_expression": (cron_expression.strip() if cron_expression else None) if is_recurring else None,
                "execute_at_iso": None if is_recurring else execute_at_iso,
                "timezone": timezone_value,
                "max_runs": max_runs if is_recurring else None,
                "context": context_dict,
            }

    if st.session_state.get(confirm_key):
        pending = st.session_state.get(pending_key, {})
        st.warning("Review the new schedule before submitting:")
        rows: list[tuple[str, str]] = [
            ("Type", _TYPE_LABEL[pending["schedule_type"]]),
            ("Name", pending["name"]),
            ("Description", pending["description"] or "—"),
            ("Agent", pending["agent_name"]),
            (
                "When",
                f"`{pending['cron_expression']}` ({pending['timezone']})"
                if pending["schedule_type"] == AgentScheduleType.RECURRING
                else f"{pending['execute_at_iso']} ({pending['timezone']})",
            ),
        ]
        if pending["schedule_type"] == AgentScheduleType.RECURRING:
            rows.append(("Max runs", str(pending["max_runs"]) if pending["max_runs"] else "unlimited"))
        rows.append(
            (
                "Message preview",
                pending["message"][:200] + ("…" if len(pending["message"]) > 200 else ""),
            )
        )
        if pending.get("context"):
            rows.append(("Context", "set (see JSON)"))

        diff_md = ["| Field | Value |", "|-------|-------|"]
        for k, v in rows:
            diff_md.append(f"| **{k}** | {_md_cell(str(v))} |")
        st.markdown("\n".join(diff_md))

        c1, c2, _ = st.columns([1, 1, 4])
        with c1:
            if st.button("✅ Create schedule", type="primary", key="add_submit"):
                _do_create_schedule(pending, current_user_id)
                st.session_state.pop(confirm_key, None)
                st.session_state.pop(pending_key, None)
        with c2:
            if st.button("Cancel", key="add_cancel"):
                st.session_state.pop(confirm_key, None)
                st.session_state.pop(pending_key, None)
                st.rerun()


def _do_create_schedule(pending: dict[str, Any], current_user_id: str) -> None:
    """Call the shared create_agent_schedule helper and show the result."""
    schedule_type: AgentScheduleType = pending["schedule_type"]
    timezone_value: str = pending["timezone"]
    execute_at_utc: datetime | None = None
    next_run_utc: datetime | None = None
    cron_expression: str | None = None

    if schedule_type == AgentScheduleType.RECURRING:
        cron_expression = pending["cron_expression"]
        next_run_utc = compute_next_run_for_cron(cron_expression, timezone_value)
    else:
        iso = pending["execute_at_iso"]
        if iso is None:
            st.error("Missing execute_at — please retry.")
            return
        parsed_dt, dt_err = parse_execute_at(iso, timezone_value)
        if dt_err:
            st.error(dt_err)
            return
        execute_at_utc = parsed_dt

    result = run_coroutine_in_lit_worker(
        create_agent_schedule_helper(
            agent_name=pending["agent_name"],
            message=pending["message"],
            schedule_type=schedule_type,
            context_dict=pending.get("context"),
            created_by_user=current_user_id,
            created_by_agent=None,
            name=pending["name"],
            description=pending["description"],
            execute_at_utc=execute_at_utc,
            cron_expression=cron_expression,
            cron_timezone=timezone_value,
            next_run_utc=next_run_utc,
            max_runs=pending.get("max_runs"),
        ),
        timeout=30,
    )

    if result.get("success"):
        new_id = result["agent_schedule_id"]
        st.success(f"Schedule created: `{new_id}`")
        st.markdown(f"[Open the new schedule →](?schedule_id={new_id})")
    else:
        st.error(result.get("error", "Unknown error"))


# ── Main page logic ──────────────────────────────────────────────────────────

st.title("📅 Agent Schedules")

if "schedule_agents" not in st.session_state:
    with st.spinner("Loading agents..."):
        st.session_state.schedule_agents = run_coroutine_in_lit_worker(fetch_all_agents(), timeout=30)

agents: list[Agent] = st.session_state.schedule_agents
agent_names = [a.name for a in agents]

current_user_id: str | None = None
if _current_email:
    current_user_id = run_coroutine_in_lit_worker(get_user_id_by_email(_current_email), timeout=10)

# Imported only so the dependency on the `tzdata`-backed timezone DB stays
# explicit in the file's surface area.
_ = available_timezones

qp = st.query_params
url_schedule_id = qp.get("schedule_id", None)

if url_schedule_id:
    _render_view_schedule(url_schedule_id, agents, current_user_id)
    st.stop()

tab_browse, tab_add = st.tabs(["📋 Browse", "➕ Add Schedule"])

with tab_browse:
    filter_cols = st.columns([1, 1, 2, 1, 1, 1])

    with filter_cols[0]:
        type_filter_label = st.selectbox(
            "Type",
            options=["All", "Recurring", "One-time"],
            index=0,
            key="sched_type_filter",
        )

    with filter_cols[1]:
        status_options = ["(all)"] + [s.value for s in AgentScheduleStatus]
        selected_status = st.selectbox("Status", status_options, key="sched_status")

    with filter_cols[2]:
        agent_options_filter = ["(all)"] + agent_names
        selected_agent = st.selectbox("Agent", agent_options_filter, key="sched_agent")

    with filter_cols[3]:
        limit_options = [20, 50, 100, 200]
        limit = st.selectbox("Limit", limit_options, index=1, key="sched_limit")

    with filter_cols[4]:
        my_schedules_only = st.checkbox("My schedules", key="sched_mine")

    with filter_cols[5]:
        show_old_inactive = st.checkbox("Show old inactive", key="sched_old_inactive", value=False)

    st.caption(f"Viewing schedules ({_current_username or 'local dev'}). Recurring are listed first.")

    if type_filter_label == "Recurring":
        type_filter_db: str | None = AgentScheduleType.RECURRING.value
    elif type_filter_label == "One-time":
        type_filter_db = AgentScheduleType.SCHEDULED.value
    else:
        type_filter_db = None

    created_by_filter = current_user_id if my_schedules_only else None

    schedules_data: list[dict[str, Any]] = []
    with st.spinner("Loading schedules..."):
        try:
            schedules_data = run_coroutine_in_lit_worker(
                fetch_schedules(
                    status=selected_status if selected_status != "(all)" else None,
                    schedule_type=type_filter_db,
                    agent_name=selected_agent if selected_agent != "(all)" else None,
                    created_by_user=created_by_filter,
                    limit=int(limit),
                ),
                timeout=60,
            )
        except Exception as e:
            st.error(f"Error loading schedules: {e}")
            schedules_data = []

    _render_browse(schedules_data, type_filter_label, show_old_inactive)

with tab_add:
    _render_add_schedule(agents, current_user_id)
