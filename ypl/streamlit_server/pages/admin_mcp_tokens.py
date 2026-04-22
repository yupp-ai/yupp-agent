"""Admin MCP Tokens — view, issue, and revoke MCP developer tokens."""

from __future__ import annotations
import uuid
from datetime import UTC, date, datetime, time
from typing import Any

import streamlit as st
from sqlmodel import col, select
from ypl.backend.config import settings
from ypl.backend.db import get_async_engine_for, get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.mcp import MCPDevToken, MCPTokenStatus
from ypl.mono_server.db import create_mcp_dev_token
from ypl.streamlit_server.auth import require_admin_role, require_auth
from ypl.streamlit_server.permissions import get_current_user_email
from ypl.structured_logger import get_logger

st.set_page_config(page_title="MCP Tokens", page_icon="🔑", layout="wide")
require_auth()
require_admin_role()

st.title("🔑 MCP Tokens")

logger = get_logger()

_SELECTED_KEY = "mcp_token_selected_id"


def _fmt_dt(dt: datetime | None, *, empty: str = "never") -> str:
    if dt is None:
        return empty
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_tokens(
    email_filter: str | None,
    status_filter: MCPTokenStatus | None,
) -> list[MCPDevToken]:
    async with get_async_session_read_replica() as session:
        stmt = select(MCPDevToken).where(col(MCPDevToken.deleted_at).is_(None))
        if email_filter:
            stmt = stmt.where(col(MCPDevToken.email).ilike(f"%{email_filter.strip().lower()}%"))
        if status_filter is not None:
            stmt = stmt.where(col(MCPDevToken.status) == status_filter)
        stmt = stmt.order_by(col(MCPDevToken.created_at).desc())
        result = await session.exec(stmt)
        return list(result.all())


async def _issue_token(email: str, description: str, expires_at: datetime | None) -> str:
    engine = get_async_engine_for(settings.DEFAULT_DB)
    token = await create_mcp_dev_token(engine, email=email, description=description)

    if expires_at is not None:
        async with get_async_session() as session:
            stmt = (
                select(MCPDevToken)
                .where(col(MCPDevToken.email) == email.lower())
                .where(col(MCPDevToken.status) == MCPTokenStatus.ACTIVE)
                .order_by(col(MCPDevToken.created_at).desc())
                .limit(1)
            )
            result = await session.exec(stmt)
            row = result.first()
            if row is not None:
                row.expires_at = expires_at
                session.add(row)
                await session.commit()

    return token


@retry_db
async def revoke_token(token_id: uuid.UUID, revoked_by: str, reason: str) -> bool:
    async with get_async_session() as session:
        stmt = select(MCPDevToken).where(col(MCPDevToken.mcp_dev_token_id) == token_id).with_for_update()
        result = await session.exec(stmt)
        row = result.one_or_none()
        if row is None:
            return False
        row.status = MCPTokenStatus.REVOKED
        row.revoked_at = datetime.now(tz=UTC)
        row.revoked_by = revoked_by
        row.revoked_reason = reason
        session.add(row)
        await session.commit()
        return True


@retry_db
async def reactivate_token(token_id: uuid.UUID) -> bool:
    async with get_async_session() as session:
        stmt = select(MCPDevToken).where(col(MCPDevToken.mcp_dev_token_id) == token_id).with_for_update()
        result = await session.exec(stmt)
        row = result.one_or_none()
        if row is None:
            return False
        row.status = MCPTokenStatus.ACTIVE
        row.revoked_at = None
        row.revoked_by = None
        row.revoked_reason = None
        session.add(row)
        await session.commit()
        return True


# ── Cached wrappers ──────────────────────────────────────────────────────────


@st.cache_data(ttl=15, show_spinner=False)
def _cached_tokens(email_filter: str | None, status_filter_value: str | None) -> list[dict[str, Any]]:
    status_filter = MCPTokenStatus(status_filter_value) if status_filter_value else None
    try:
        tokens = run_coroutine_in_lit_worker(fetch_tokens(email_filter, status_filter), timeout=10)
    except Exception as exc:
        logger.warning("admin_mcp_tokens_fetch_failed", error=str(exc))
        raise

    return [
        {
            "mcp_dev_token_id": str(t.mcp_dev_token_id),
            "email": t.email,
            "description": t.description or "",
            "status": t.status.value,
            "created_at": t.created_at,
            "last_used_at": t.last_used_at,
            "expires_at": t.expires_at,
            "revoked_at": t.revoked_at,
            "revoked_by": t.revoked_by or "",
            "revoked_reason": t.revoked_reason or "",
        }
        for t in tokens
    ]


def _invalidate_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── UI ───────────────────────────────────────────────────────────────────────

_COL_WIDTHS = [2.8, 3.2, 0.9, 2.0, 2.0, 0.7]


def _render_token_list(rows: list[dict[str, Any]]) -> None:
    st.caption(f"{len(rows)} token(s)")
    if not rows:
        st.info("No tokens match the current filters.")
        return

    hdr = st.columns(_COL_WIDTHS)
    for i, label in enumerate(("Email", "Description", "Status", "Created", "Last used", "")):
        with hdr[i]:
            st.markdown(f"**{label}**")
    st.divider()

    for r in rows:
        row = st.columns(_COL_WIDTHS)
        with row[0]:
            st.markdown(f"`{r['email']}`")
        with row[1]:
            st.markdown(r["description"] or "—")
        with row[2]:
            st.markdown(r["status"])
        with row[3]:
            st.markdown(_fmt_dt(r["created_at"], empty="—"))
        with row[4]:
            st.markdown(_fmt_dt(r["last_used_at"]))
        with row[5]:
            if st.button("Manage", key=f"select_token_{r['mcp_dev_token_id']}"):
                st.session_state[_SELECTED_KEY] = r["mcp_dev_token_id"]
                st.rerun()


def _render_manage_section(rows: list[dict[str, Any]]) -> None:
    selected_id = st.session_state.get(_SELECTED_KEY)
    if not selected_id:
        return

    selected: dict[str, Any] | None = next((r for r in rows if r["mcp_dev_token_id"] == selected_id), None)
    if selected is None:
        # Selection doesn't match the current filter; clear it.
        st.session_state.pop(_SELECTED_KEY, None)
        return

    st.divider()
    header_cols = st.columns([6, 1])
    with header_cols[0]:
        st.subheader(f"Manage token — {selected['email']}")
    with header_cols[1]:
        if st.button("Close", key="mcp_manage_close"):
            st.session_state.pop(_SELECTED_KEY, None)
            st.rerun()

    meta_cols = st.columns(2)
    with meta_cols[0]:
        st.markdown(f"**Token ID:** `{selected['mcp_dev_token_id']}`")
        st.markdown(f"**Email:** {selected['email']}")
        st.markdown(f"**Description:** {selected['description'] or '—'}")
        st.markdown(f"**Status:** {selected['status']}")
        st.markdown(f"**Created:** {_fmt_dt(selected['created_at'], empty='—')}")
    with meta_cols[1]:
        st.markdown(f"**Last used:** {_fmt_dt(selected['last_used_at'])}")
        st.markdown(f"**Expires:** {_fmt_dt(selected['expires_at'])}")
        st.markdown(f"**Revoked at:** {_fmt_dt(selected['revoked_at'], empty='—')}")
        st.markdown(f"**Revoked by:** {selected['revoked_by'] or '—'}")
        st.markdown(f"**Revoked reason:** {selected['revoked_reason'] or '—'}")

    token_uuid = uuid.UUID(selected["mcp_dev_token_id"])

    if selected["status"] == MCPTokenStatus.ACTIVE.value:
        with st.form(f"mcp_revoke_form_{selected['mcp_dev_token_id']}"):
            reason = st.text_input("Revocation reason", key=f"mcp_revoke_reason_{selected['mcp_dev_token_id']}")
            confirm = st.form_submit_button("Revoke token", type="primary")
        if confirm:
            reason_clean = (reason or "").strip()
            if not reason_clean:
                st.error("A revocation reason is required.")
            else:
                admin_email = get_current_user_email() or "unknown-admin"
                try:
                    ok = run_coroutine_in_lit_worker(
                        revoke_token(token_uuid, admin_email, reason_clean),
                        timeout=10,
                    )
                except Exception as exc:
                    logger.exception("admin_mcp_tokens_revoke_failed", token_id=str(token_uuid))
                    st.error(f"Failed to revoke token: {exc}")
                else:
                    if ok:
                        st.success("Token revoked.")
                        _invalidate_and_rerun()
                    else:
                        st.error("Token not found.")

    elif selected["status"] == MCPTokenStatus.REVOKED.value:
        if st.button("Reactivate token", key=f"mcp_reactivate_{selected['mcp_dev_token_id']}"):
            try:
                ok = run_coroutine_in_lit_worker(reactivate_token(token_uuid), timeout=10)
            except Exception as exc:
                logger.exception("admin_mcp_tokens_reactivate_failed", token_id=str(token_uuid))
                st.error(f"Failed to reactivate token: {exc}")
            else:
                if ok:
                    st.success("Token reactivated.")
                    _invalidate_and_rerun()
                else:
                    st.error("Token not found.")


def _render_browse() -> None:
    filter_cols = st.columns([2, 1, 2])
    with filter_cols[0]:
        email_filter = st.text_input("Email contains", value="", key="mcp_token_email_filter").strip()
    with filter_cols[1]:
        status_options = ["All", *(s.value for s in MCPTokenStatus)]
        status_choice = st.selectbox(
            "Status",
            status_options,
            index=status_options.index(MCPTokenStatus.ACTIVE.value),
            key="mcp_token_status_filter",
        )
    status_filter_value: str | None = None if status_choice == "All" else status_choice

    try:
        rows = _cached_tokens(email_filter or None, status_filter_value)
    except Exception as exc:
        st.error(f"Failed to load tokens: {exc}")
        rows = []

    _render_token_list(rows)
    _render_manage_section(rows)


def _render_issue_form() -> None:
    st.subheader("Issue new token")
    with st.form("mcp_issue_token_form", clear_on_submit=False):
        new_email = st.text_input("Email", key="mcp_issue_email")
        new_description = st.text_input(
            "Description",
            value="Issued via Lit admin",
            key="mcp_issue_description",
        )
        new_expires_date: date | None = st.date_input(
            "Expires at (optional)",
            value=None,
            key="mcp_issue_expires",
        )
        submitted = st.form_submit_button("Issue token", type="primary")

    if not submitted:
        return

    email_clean = (new_email or "").strip().lower()
    if not email_clean:
        st.error("Email is required.")
        return

    expires_at: datetime | None = None
    if new_expires_date is not None:
        expires_at = datetime.combine(new_expires_date, time(23, 59, 59), tzinfo=UTC)

    try:
        plaintext_token = run_coroutine_in_lit_worker(
            _issue_token(email_clean, new_description or "Issued via Lit admin", expires_at),
            timeout=30,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        logger.exception("admin_mcp_tokens_issue_failed", email=email_clean)
        st.error(f"Failed to issue token: {exc}")
        return

    st.success(f"Token issued for {email_clean}.")
    st.code(plaintext_token, language="text")
    st.warning("⚠️ Copy this token now — it will never be shown again.")
    st.info("Share this token with the user over a secure channel (e.g. 1Password, encrypted DM).")
    st.cache_data.clear()


# ── Render ───────────────────────────────────────────────────────────────────

tab_browse, tab_issue = st.tabs(["Browse", "Issue New Token"])

with tab_browse:
    _render_browse()

with tab_issue:
    _render_issue_form()
