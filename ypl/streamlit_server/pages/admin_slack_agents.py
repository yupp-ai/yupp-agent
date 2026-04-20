"""Admin page — manage the ``slack_agents`` table (Slack Agent Gateway bot registrations)."""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any

import streamlit as st
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import Agent
from ypl.db.slack_agent import SlackAgent, SlackAgentStatus
from ypl.streamlit_server.auth import require_admin_role, require_auth

st.set_page_config(page_title="Slack Agents", page_icon="💬", layout="wide")
require_auth()
require_admin_role()

st.title("💬 Slack Agents")


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def _fetch_slack_agents_raw() -> list[dict[str, Any]]:
    """Return non-deleted SlackAgent rows as dicts (safe for Streamlit caching)."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(SlackAgent).where(col(SlackAgent.deleted_at).is_(None)).order_by(col(SlackAgent.created_at).desc())
        )
        result = await session.exec(stmt)
        rows = result.all()
        return [
            {
                "slack_agent_id": str(r.slack_agent_id),
                "app_id": r.app_id,
                "agent_name": r.agent_name,
                "bot_name": r.bot_name,
                "display_name": r.display_name,
                "status": r.status.value,
                "created_by_user_id": r.created_by_user_id or "",
                "bot_creation_request_id": r.bot_creation_request_id or "",
                "created_at": r.created_at,
                "modified_at": r.modified_at,
            }
            for r in rows
        ]


@retry_db
async def _fetch_agent_names_raw() -> list[str]:
    """Return all non-deleted Agent names, sorted."""
    async with get_async_session_read_replica() as session:
        stmt = select(Agent.name).where(col(Agent.deleted_at).is_(None)).order_by(Agent.name)
        result = await session.exec(stmt)
        return list(result.all())


@retry_db
async def _insert_slack_agent(
    app_id: str,
    agent_name: str,
    bot_name: str,
    display_name: str,
    status: SlackAgentStatus,
    created_by_user_id: str | None,
) -> tuple[bool, str]:
    """Insert a new SlackAgent row. Returns (success, message)."""
    async with get_async_session() as session:
        row = SlackAgent(
            app_id=app_id,
            agent_name=agent_name,
            bot_name=bot_name,
            display_name=display_name,
            status=status,
            created_by_user_id=created_by_user_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            return False, f"Could not create Slack agent (duplicate app_id?): {exc.orig}"
    return True, "Slack agent created."


@retry_db
async def _update_slack_agent(
    slack_agent_id: uuid.UUID,
    app_id: str,
    agent_name: str,
    bot_name: str,
    display_name: str,
    status: SlackAgentStatus,
) -> tuple[bool, str]:
    """Update an existing SlackAgent row. Returns (success, message)."""
    async with get_async_session() as session:
        query = (
            select(SlackAgent)
            .where(col(SlackAgent.slack_agent_id) == slack_agent_id)
            .where(col(SlackAgent.deleted_at).is_(None))
            .with_for_update()
        )
        result = await session.exec(query)
        row = result.one_or_none()
        if row is None:
            return False, "Slack agent not found (may have been deleted)."
        row.app_id = app_id
        row.agent_name = agent_name
        row.bot_name = bot_name
        row.display_name = display_name
        row.status = status
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            return False, f"Could not update Slack agent (duplicate app_id?): {exc.orig}"
    return True, "Slack agent updated."


@retry_db
async def _soft_delete_slack_agent(slack_agent_id: uuid.UUID) -> bool:
    """Soft-delete a SlackAgent row by setting ``deleted_at``. Returns True on success."""
    async with get_async_session() as session:
        query = (
            select(SlackAgent)
            .where(col(SlackAgent.slack_agent_id) == slack_agent_id)
            .where(col(SlackAgent.deleted_at).is_(None))
            .with_for_update()
        )
        result = await session.exec(query)
        row = result.one_or_none()
        if row is None:
            return False
        row.deleted_at = datetime.now(tz=UTC)
        session.add(row)
        await session.commit()
    return True


# ── Cached loaders ───────────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _load_slack_agents() -> list[dict[str, Any]]:
    """Load Slack agent rows; cached for 30 seconds."""
    return run_coroutine_in_lit_worker(_fetch_slack_agents_raw(), timeout=10) or []


@st.cache_data(ttl=30, show_spinner=False)
def _load_agent_names() -> list[str]:
    """Load Agent.name values for the dropdown; cached for 30 seconds."""
    return run_coroutine_in_lit_worker(_fetch_agent_names_raw(), timeout=10) or []


def _clear_caches_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── UI ───────────────────────────────────────────────────────────────────────


_STATUS_OPTIONS: list[SlackAgentStatus] = list(SlackAgentStatus)
_STATUS_VALUES: list[str] = [s.value for s in _STATUS_OPTIONS]


def _render_list(rows: list[dict[str, Any]]) -> None:
    st.subheader(f"Registered Slack agents ({len(rows)})")
    if not rows:
        st.info("No Slack agents registered yet. Use the form below to add one.")
        return
    display_rows = [
        {
            "app_id": r["app_id"],
            "agent_name": r["agent_name"],
            "bot_name": r["bot_name"],
            "display_name": r["display_name"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    st.dataframe(display_rows, use_container_width=True, hide_index=True)


def _render_add_form(agent_names: list[str]) -> None:
    with st.expander("➕ Add new Slack agent", expanded=False):
        if not agent_names:
            st.warning("No agents found in the ``agents`` table — create an agent before registering a Slack bot.")
            return
        with st.form("add_slack_agent_form", clear_on_submit=True):
            app_id = st.text_input("App ID", help="Slack app ID, e.g. 'A123CONFUCIUS'.").strip()
            agent_name = st.selectbox("Agent name", options=agent_names, index=0)
            bot_name = st.text_input("Bot name", help="Used for GCP secret lookup, e.g. 'giladovski'.").strip()
            display_name = st.text_input("Display name", help="Human-readable name shown in the UI.").strip()
            status_value = st.selectbox(
                "Status",
                options=_STATUS_VALUES,
                index=_STATUS_VALUES.index(SlackAgentStatus.ACTIVE.value),
            )
            submitted = st.form_submit_button("Create", type="primary")
        if not submitted:
            return
        missing = [
            label
            for label, value in (
                ("App ID", app_id),
                ("Agent name", agent_name),
                ("Bot name", bot_name),
                ("Display name", display_name),
            )
            if not value
        ]
        if missing:
            st.error(f"Required field(s) missing: {', '.join(missing)}")
            return
        created_by = _current_user_email()
        success, message = run_coroutine_in_lit_worker(
            _insert_slack_agent(
                app_id=app_id,
                agent_name=agent_name,
                bot_name=bot_name,
                display_name=display_name,
                status=SlackAgentStatus(status_value),
                created_by_user_id=created_by,
            ),
            timeout=10,
        )
        if success:
            st.toast(message, icon="✅")
            _clear_caches_and_rerun()
        else:
            st.error(message)


def _current_user_email() -> str | None:
    """Best-effort lookup of the logged-in user's email for audit purposes."""
    try:
        if st.user is not None and st.user.is_logged_in:
            email = st.user.email
            return email if isinstance(email, str) else None
    except Exception:
        return None
    return None


def _render_edit_section(rows: list[dict[str, Any]], agent_names: list[str]) -> None:
    st.subheader("Edit / delete an existing Slack agent")
    if not rows:
        st.caption("Nothing to edit yet.")
        return

    labels = [f"{r['display_name']} ({r['app_id']})" for r in rows]
    label_to_row = dict(zip(labels, rows, strict=True))
    selected_label = st.selectbox("Select a Slack agent", options=labels, index=0, key="slack_agent_edit_picker")
    row = label_to_row[selected_label]
    slack_agent_id = uuid.UUID(row["slack_agent_id"])

    # Build the agent-name dropdown: include the current value even if it's no longer in ``agents``.
    agent_options = list(agent_names)
    if row["agent_name"] not in agent_options:
        agent_options = [row["agent_name"], *agent_options]
    status_index = _STATUS_VALUES.index(row["status"]) if row["status"] in _STATUS_VALUES else 0

    with st.form(f"edit_slack_agent_form_{slack_agent_id}"):
        st.caption(
            "Changing ``app_id`` will break incoming webhooks until Slack is reconfigured — edit with care.",
        )
        new_app_id = st.text_input("App ID", value=row["app_id"]).strip()
        new_agent_name = st.selectbox(
            "Agent name",
            options=agent_options,
            index=agent_options.index(row["agent_name"]),
        )
        new_bot_name = st.text_input("Bot name", value=row["bot_name"]).strip()
        new_display_name = st.text_input("Display name", value=row["display_name"]).strip()
        new_status_value = st.selectbox("Status", options=_STATUS_VALUES, index=status_index)
        col_save, col_delete = st.columns(2)
        save_clicked = col_save.form_submit_button("Save changes", type="primary")
        delete_clicked = col_delete.form_submit_button("Delete", type="secondary")

    if save_clicked:
        missing = [
            label
            for label, value in (
                ("App ID", new_app_id),
                ("Agent name", new_agent_name),
                ("Bot name", new_bot_name),
                ("Display name", new_display_name),
            )
            if not value
        ]
        if missing:
            st.error(f"Required field(s) missing: {', '.join(missing)}")
            return
        success, message = run_coroutine_in_lit_worker(
            _update_slack_agent(
                slack_agent_id=slack_agent_id,
                app_id=new_app_id,
                agent_name=new_agent_name,
                bot_name=new_bot_name,
                display_name=new_display_name,
                status=SlackAgentStatus(new_status_value),
            ),
            timeout=10,
        )
        if success:
            st.toast(message, icon="✅")
            _clear_caches_and_rerun()
        else:
            st.error(message)
        return

    if delete_clicked:
        confirmed = run_coroutine_in_lit_worker(_soft_delete_slack_agent(slack_agent_id), timeout=10)
        if confirmed:
            st.toast(f"Deleted Slack agent '{row['display_name']}'.", icon="🗑️")
            _clear_caches_and_rerun()
        else:
            st.error("Could not delete Slack agent (already removed?).")


# ── Page body ────────────────────────────────────────────────────────────────

_rows = _load_slack_agents()
_agent_names = _load_agent_names()

_render_list(_rows)
st.divider()
_render_add_form(_agent_names)
st.divider()
_render_edit_section(_rows, _agent_names)
