"""My MCPs — connect / revoke external MCP servers for the logged-in user."""

from __future__ import annotations
import os
import uuid
from datetime import UTC, datetime

import streamlit as st
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.external_mcp import (
    McpAuthType,
    McpServer,
    McpServerRoleAccess,
    McpUserGrant,
)
from ypl.db.rbac import UserRoleAssociation
from ypl.db.users import User
from ypl.external_mcp import grants as grants_svc
from ypl.streamlit_server.auth import require_auth
from ypl.streamlit_server.permissions import get_current_user_email
from ypl.structured_logger import get_logger

st.set_page_config(page_title="My MCPs", page_icon="🔌", layout="wide")
require_auth()

logger = get_logger()
st.title("🔌 My MCPs")
st.caption(
    "Connect external MCP servers (Gmail, Drive, Calendar, …) to your account. "
    "Once connected, any agent your roles allow can use these tools on your behalf."
)


# ----------------------- DB lookups -----------------------


async def _current_user_id(email: str) -> str | None:
    async with get_async_session_read_replica() as session:
        return (await session.exec(select(User.user_id).where(User.email == email))).first()


async def _visible_servers(user_id: str) -> list[dict]:
    """Servers this user may connect (RBAC intersection)."""
    async with get_async_session_read_replica() as session:
        # User's role IDs
        user_roles = set(
            (
                await session.exec(select(UserRoleAssociation.role_id).where(UserRoleAssociation.user_id == user_id))
            ).all()
        )
        servers = (
            await session.exec(
                select(McpServer).where(McpServer.enabled.is_(True)).order_by(col(McpServer.display_name))  # type: ignore[attr-defined]
            )
        ).all()

        out: list[dict] = []
        for srv in servers:
            srv_roles = set(
                (
                    await session.exec(
                        select(McpServerRoleAccess.role_id).where(
                            McpServerRoleAccess.mcp_server_id == srv.mcp_server_id
                        )
                    )
                ).all()
            )
            # No role restriction → visible to everyone.  Otherwise need intersection.
            if srv_roles and not (user_roles & srv_roles):
                continue
            grant = (
                await session.exec(
                    select(McpUserGrant)
                    .where(col(McpUserGrant.user_id) == user_id)
                    .where(col(McpUserGrant.mcp_server_id) == srv.mcp_server_id)
                    .where(col(McpUserGrant.revoked_at).is_(None))
                )
            ).first()
            out.append({"server": srv, "grant": grant})
        return out


async def _revoke(user_id: str, mcp_server_id: uuid.UUID) -> None:
    async with get_async_session() as session:
        await grants_svc.revoke_grant(session, user_id=user_id, mcp_server_id=mcp_server_id)
        await session.commit()


async def _set_m2m(user_id: str, mcp_server_id: uuid.UUID, api_key: str) -> None:
    async with get_async_session() as session:
        await grants_svc.upsert_m2m_grant(session, user_id=user_id, mcp_server_id=mcp_server_id, api_key=api_key)
        await session.commit()


# ----------------------- UI -----------------------


email = get_current_user_email()
if not email:
    st.error("Could not resolve your Google identity. Sign in again from the home page.")
    st.stop()

user_id = run_coroutine_in_lit_worker(_current_user_id(email), timeout=10)
if not user_id:
    st.warning(f"No user record for {email}. Ask an admin to create one on **Users → Add User**.")
    st.stop()

servers = run_coroutine_in_lit_worker(_visible_servers(user_id), timeout=15) or []
if not servers:
    st.info("No external MCPs are available to your roles. Ask an admin to grant access.")
    st.stop()


def _grant_status_badge(grant: McpUserGrant | None) -> tuple[str, str]:
    """Return (icon, text) describing the grant state."""
    if grant is None:
        return "⚪", "Not connected"
    if grant.last_refresh_error:
        return "⚠️", f"Refresh failed: {grant.last_refresh_error[:80]}"
    if grant.expires_at is not None:
        remaining = grant.expires_at - datetime.now(UTC)
        mins = int(remaining.total_seconds() // 60)
        if mins < 0:
            return "🟡", "Token expired (will auto-refresh on next use)"
        if mins < 60:
            return "🟢", f"Connected · expires in {mins}m"
        hours = mins // 60
        return "🟢", f"Connected · expires in {hours}h"
    return "🟢", "Connected"


# AHS public URL — where the OAuth start endpoint lives.  Default to the
# variable the installer writes (GATEWAY_BASE_URL); fall back to localhost
# for dev runs.
_AHS_BASE = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/") or "http://localhost:8090"

# Where the browser should come back to after the OAuth callback finishes.
# Lit on the same host: /my_mcps.
try:
    # Best-effort: Streamlit exposes the request host via st.context.headers
    # in recent versions; fall back to AHS_LIT_BASE_URL.
    _lit_base = (st.context.headers.get("origin") or "").rstrip("/")
except Exception:
    _lit_base = ""
if not _lit_base:
    _lit_base = os.environ.get("AHS_LIT_BASE_URL", "").rstrip("/")
_return_to = f"{_lit_base}/my_mcps" if _lit_base else ""


for entry in servers:
    srv: McpServer = entry["server"]
    grant: McpUserGrant | None = entry["grant"]
    icon, status = _grant_status_badge(grant)

    with st.container(border=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"### {icon} {srv.display_name}")
            st.caption(f"`{srv.slug}` · {srv.auth_type.value} · {srv.url}")
            if srv.description:
                st.write(srv.description)
            st.write(f"**Status:** {status}")
            if grant and grant.scopes:
                st.caption("Scopes: " + ", ".join(grant.scopes))

        with c2:
            if srv.auth_type == McpAuthType.OAUTH_OBO:
                connect_url = f"{_AHS_BASE}/mcp_oauth/start?server={srv.slug}&user_id={user_id}" + (
                    f"&return_to={_return_to}" if _return_to else ""
                )
                label = "Reconnect" if grant else "Connect"
                st.link_button(label, connect_url, type="primary")
                if grant:
                    if st.button("Revoke", key=f"revoke_{srv.slug}"):
                        run_coroutine_in_lit_worker(_revoke(user_id, srv.mcp_server_id), timeout=10)
                        st.success(f"Revoked {srv.slug}.")
                        st.rerun()

            elif srv.auth_type == McpAuthType.M2M_PER_USER:
                with st.form(f"m2m_{srv.slug}"):
                    key = st.text_input("API key / bearer token", type="password")
                    save = st.form_submit_button("Save")
                    if save and key.strip():
                        run_coroutine_in_lit_worker(
                            _set_m2m(user_id, srv.mcp_server_id, key.strip()),
                            timeout=10,
                        )
                        st.success(f"Saved key for {srv.slug}.")
                        st.rerun()
                if grant:
                    if st.button("Revoke", key=f"revoke_m2m_{srv.slug}"):
                        run_coroutine_in_lit_worker(_revoke(user_id, srv.mcp_server_id), timeout=10)
                        st.success(f"Revoked {srv.slug}.")
                        st.rerun()

            elif srv.auth_type == McpAuthType.M2M_SHARED:
                st.info(
                    "This server uses a shared admin-managed key — no per-user setup needed. "
                    "It will appear in your agents automatically if your role has access."
                )
            else:
                st.info("Open server (no auth).  Available to agents your role allows.")
