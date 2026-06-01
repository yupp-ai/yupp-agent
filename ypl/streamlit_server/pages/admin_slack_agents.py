"""Admin page — manage the ``slack_agents`` table (Slack Agent Gateway bot registrations)."""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import streamlit as st
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import Agent
from ypl.db.slack_agent import SlackAgent, SlackAgentStatus
from ypl.db.users import User, UserStatus
from ypl.slack_agent_gateway.crypto import encrypt_secret
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
                "has_bot_token_encrypted": bool(r.bot_token_encrypted),
                "has_signing_secret_encrypted": bool(r.signing_secret_encrypted),
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
async def _lookup_user_id_by_email(email: str) -> str | None:
    """Return the user_id of the ACTIVE user with the given email, case-insensitive."""
    normalized = email.strip().lower()
    if not normalized:
        return None
    async with get_async_session_read_replica() as session:
        stmt = (
            select(User.user_id)
            .where(sa.func.lower(User.email) == normalized)
            .where(col(User.status) == UserStatus.ACTIVE)
            .where(col(User.deleted_at).is_(None))
            .limit(1)
        )
        result = await session.exec(stmt)
        return result.one_or_none()


@retry_db
async def _insert_slack_agent(
    app_id: str,
    agent_name: str,
    bot_name: str,
    display_name: str,
    status: SlackAgentStatus,
    created_by_user_id: str | None,
    bot_token: str | None = None,
    signing_secret: str | None = None,
) -> tuple[bool, str]:
    """Insert a new SlackAgent row. Returns (success, message)."""
    normalized_bot_token = (bot_token or "").strip()
    normalized_signing_secret = (signing_secret or "").strip()
    if bool(normalized_bot_token) != bool(normalized_signing_secret):
        return False, "Provide both bot token and signing secret, or leave both blank."

    async with get_async_session() as session:
        row = SlackAgent(
            app_id=app_id,
            agent_name=agent_name,
            bot_name=bot_name,
            display_name=display_name,
            status=status,
            created_by_user_id=created_by_user_id,
            bot_token_encrypted=encrypt_secret(normalized_bot_token) if normalized_bot_token else None,
            signing_secret_encrypted=encrypt_secret(normalized_signing_secret) if normalized_signing_secret else None,
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


@retry_db
async def _update_slack_agent_secrets(
    slack_agent_id: uuid.UUID,
    bot_token: str,
    signing_secret: str,
) -> tuple[bool, str]:
    """Encrypt and store per-agent Slack secrets on the row."""
    bot_token = bot_token.strip()
    signing_secret = signing_secret.strip()
    if not bot_token or not signing_secret:
        return False, "Bot token and signing secret are both required."

    try:
        encrypted_bot_token = encrypt_secret(bot_token)
        encrypted_signing_secret = encrypt_secret(signing_secret)
    except Exception as exc:
        return False, f"Could not encrypt secrets: {exc}"

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
        row.bot_token_encrypted = encrypted_bot_token
        row.signing_secret_encrypted = encrypted_signing_secret
        session.add(row)
        await session.commit()
    return True, "Encrypted Slack secrets stored on the agent row."


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


def _current_user_email() -> str | None:
    """Best-effort lookup of the logged-in user's email for audit purposes."""
    try:
        if st.user is not None and st.user.is_logged_in:
            email = st.user.email
            return email if isinstance(email, str) else None
    except Exception:
        return None
    return None


def _current_user_id() -> str | None:
    """Resolve the logged-in user's ``users.user_id`` by email, or None if not found."""
    email = _current_user_email()
    if not email:
        return None
    return run_coroutine_in_lit_worker(_lookup_user_id_by_email(email), timeout=10)


def _render_list(rows: list[dict[str, Any]]) -> None:
    st.subheader(f"Registered Slack agents ({len(rows)})")
    if not rows:
        st.info("No Slack agents registered yet. Use the *Add New Slack Agent* tab to add one.")
        return
    display_rows = [
        {
            "app_id": r["app_id"],
            "agent_name": r["agent_name"],
            "bot_name": r["bot_name"],
            "display_name": r["display_name"],
            "status": r["status"],
            "encrypted_bot_token": r["has_bot_token_encrypted"],
            "encrypted_signing_secret": r["has_signing_secret_encrypted"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    st.dataframe(display_rows, use_container_width=True, hide_index=True)


def _render_add_form(agent_names: list[str]) -> None:
    st.subheader("Add new Slack agent")
    if not agent_names:
        st.warning("No agents found in the ``agents`` table — create an agent before registering a Slack bot.")
        return
    with st.form("add_slack_agent_form", clear_on_submit=True):
        app_id = st.text_input("App ID", help="Slack app ID, e.g. 'A123CONFUCIUS'.").strip()
        agent_name = st.selectbox("Agent name", options=agent_names, index=0)
        bot_name = st.text_input("Bot name", help="Used for GCP secret lookup, e.g. 'giladovski'.").strip()
        display_name = st.text_input("Display name", help="Human-readable name shown in the UI.").strip()
        st.caption("Optional: store bot token and signing secret encrypted on the Slack agent row during creation.")
        bot_token = st.text_input(
            "Bot token (optional)",
            type="password",
            help="Slack bot user OAuth token (xoxb-...). Leave blank to use env-var fallback instead.",
        ).strip()
        signing_secret = st.text_input(
            "Signing secret (optional)",
            type="password",
            help="Slack app signing secret. Leave blank to use env-var fallback instead.",
        ).strip()
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
    created_by_user_id = _current_user_id()
    if created_by_user_id is None:
        st.error(
            "Could not resolve your user_id from the users table — ask an admin to create a user "
            "record for your email, then retry."
        )
        return
    success, message = run_coroutine_in_lit_worker(
        _insert_slack_agent(
            app_id=app_id,
            agent_name=agent_name,
            bot_name=bot_name,
            display_name=display_name,
            status=SlackAgentStatus(status_value),
            created_by_user_id=created_by_user_id,
            bot_token=bot_token,
            signing_secret=signing_secret,
        ),
        timeout=10,
    )
    if success:
        st.toast(message, icon="✅")
        _clear_caches_and_rerun()
    else:
        st.error(message)


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

    token_status = "present" if row["has_bot_token_encrypted"] else "missing"
    secret_status = "present" if row["has_signing_secret_encrypted"] else "missing"
    st.caption(f"Encrypted DB secrets: bot token {token_status}, signing secret {secret_status}.")

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
        return

    st.divider()
    st.subheader("Store encrypted Slack secrets")
    st.caption(
        "These values are encrypted with SLACK_AGENT_GW_ENCRYPTION_KEY and stored on the slack_agents row. "
        "Once saved, the gateway can use the DB-backed secrets instead of env-var fallback.",
    )
    with st.form(f"edit_slack_agent_secrets_form_{slack_agent_id}", clear_on_submit=True):
        bot_token = st.text_input(
            "Bot token",
            type="password",
            help="Slack bot user OAuth token (xoxb-...).",
        ).strip()
        signing_secret = st.text_input(
            "Signing secret",
            type="password",
            help="Slack app signing secret.",
        ).strip()
        save_secrets_clicked = st.form_submit_button("Encrypt and save secrets", type="primary")

    if save_secrets_clicked:
        success, message = run_coroutine_in_lit_worker(
            _update_slack_agent_secrets(
                slack_agent_id=slack_agent_id,
                bot_token=bot_token,
                signing_secret=signing_secret,
            ),
            timeout=10,
        )
        if success:
            st.toast(message, icon="🔐")
            _clear_caches_and_rerun()
        else:
            st.error(message)


# ── Page body ────────────────────────────────────────────────────────────────

_rows = _load_slack_agents()
_agent_names = _load_agent_names()

tab_browse, tab_add = st.tabs(["Browse", "Add New Slack Agent"])

with tab_browse:
    _render_list(_rows)
    st.divider()
    _render_edit_section(_rows, _agent_names)

with tab_add:
    _render_add_form(_agent_names)
