"""Admin External MCPs — register external MCP servers and grant role access."""

from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa
import streamlit as st
from sqlmodel import col, select

import os

from ypl.backend.db import get_async_session, get_async_session_read_replica
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.external_mcp import (
    McpAuthType,
    McpServer,
    McpServerRoleAccess,
    McpServerSecrets,
    McpTransport,
    McpUserGrant,
)
from ypl.db.rbac import Role, RoleName
from ypl.external_mcp import crypto
from ypl.external_mcp.catalog import CATALOG, CatalogEntry, get as catalog_get
from ypl.streamlit_server.auth import require_admin_role, require_auth
from ypl.structured_logger import get_logger

st.set_page_config(page_title="External MCPs", page_icon="🧩", layout="wide")
require_auth()
require_admin_role()

logger = get_logger()
st.title("🧩 External MCP Servers")
st.caption(
    "Register external MCP endpoints (Gmail, Drive, Calendar, …) so agents can attach them. "
    "Per-user OAuth tokens are stored in `mcp_user_grants`; this page only manages the "
    "**registry** + which roles can use which server."
)


# ----------------------- DB helpers -----------------------


async def _list_servers() -> list[dict[str, Any]]:
    async with get_async_session_read_replica() as session:
        rows = (await session.exec(select(McpServer).order_by(col(McpServer.slug)))).all()
        results: list[dict[str, Any]] = []
        for srv in rows:
            n_roles = (
                await session.exec(
                    select(sa.func.count())
                    .select_from(McpServerRoleAccess)
                    .where(McpServerRoleAccess.mcp_server_id == srv.mcp_server_id)
                )
            ).one()
            n_grants = (
                await session.exec(
                    select(sa.func.count())
                    .select_from(McpUserGrant)
                    .where(
                        sa.and_(
                            McpUserGrant.mcp_server_id == srv.mcp_server_id,
                            McpUserGrant.revoked_at.is_(None),  # type: ignore[attr-defined]
                        )
                    )
                )
            ).one()
            results.append(
                {
                    "mcp_server_id": str(srv.mcp_server_id),
                    "slug": srv.slug,
                    "display_name": srv.display_name,
                    "auth_type": srv.auth_type.value,
                    "transport": srv.transport.value,
                    "url": srv.url,
                    "enabled": srv.enabled,
                    "n_roles": n_roles,
                    "n_grants": n_grants,
                }
            )
        return results


async def _all_roles() -> list[Role]:
    async with get_async_session_read_replica() as session:
        return list((await session.exec(select(Role).order_by(col(Role.name)))).all())


async def _create_server(
    *,
    slug: str,
    display_name: str,
    description: str,
    url: str,
    transport: McpTransport,
    auth_type: McpAuthType,
    oauth_config: dict | None,
    client_secret: str | None,
    m2m_shared_token: str | None,
    role_ids: list[uuid.UUID],
    enabled: bool,
) -> tuple[bool, str]:
    async with get_async_session() as session:
        if (await session.exec(select(McpServer).where(McpServer.slug == slug))).first():
            return False, f"Server with slug {slug!r} already exists."
        srv = McpServer(
            slug=slug,
            display_name=display_name,
            description=description or None,
            url=url,
            transport=transport,
            auth_type=auth_type,
            oauth_config=oauth_config,
            enabled=enabled,
        )
        session.add(srv)
        await session.flush()  # need srv.mcp_server_id

        if client_secret or m2m_shared_token:
            session.add(
                McpServerSecrets(
                    mcp_server_id=srv.mcp_server_id,
                    oauth_client_secret_enc=crypto.encrypt(client_secret) if client_secret else None,
                    m2m_shared_token_enc=crypto.encrypt(m2m_shared_token) if m2m_shared_token else None,
                )
            )
        for rid in role_ids:
            session.add(McpServerRoleAccess(mcp_server_id=srv.mcp_server_id, role_id=rid))
        await session.commit()
    return True, f"Registered {slug!r}."


async def _toggle_enabled(mcp_server_id: uuid.UUID, enabled: bool) -> None:
    async with get_async_session() as session:
        srv = await session.get(McpServer, mcp_server_id)
        if srv is not None:
            srv.enabled = enabled
            session.add(srv)
            await session.commit()


async def _delete_server(mcp_server_id: uuid.UUID) -> None:
    async with get_async_session() as session:
        srv = await session.get(McpServer, mcp_server_id)
        if srv is not None:
            await session.delete(srv)
            await session.commit()


async def _create_from_catalog(
    entry: CatalogEntry,
    *,
    client_id: str,
    client_secret: str,
    role_ids: list[uuid.UUID],
    enabled: bool,
) -> tuple[bool, str]:
    """Insert a server using a catalog entry's hardcoded URL / OAuth metadata,
    plus the admin-supplied OAuth client_id + client_secret."""
    return await _create_server(
        slug=entry.slug,
        display_name=entry.display_name,
        description=entry.description,
        url=entry.url,
        transport=entry.transport,
        auth_type=entry.auth_type,
        oauth_config={
            "client_id": client_id,
            "authorize_url": entry.authorize_url,
            "token_url": entry.token_url,
            "scopes": list(entry.scopes),
            "extra_authorize_params": dict(entry.extra_authorize_params),
        },
        client_secret=client_secret or None,
        m2m_shared_token=None,
        role_ids=role_ids,
        enabled=enabled,
    )


async def _existing_slugs() -> set[str]:
    async with get_async_session_read_replica() as session:
        rows = (await session.exec(select(McpServer.slug))).all()
        return set(rows)


async def _set_server_roles(mcp_server_id: uuid.UUID, role_ids: list[uuid.UUID]) -> None:
    async with get_async_session() as session:
        existing = (
            await session.exec(
                select(McpServerRoleAccess).where(McpServerRoleAccess.mcp_server_id == mcp_server_id)
            )
        ).all()
        want = set(role_ids)
        have = {r.role_id for r in existing}
        for r in existing:
            if r.role_id not in want:
                await session.delete(r)
        for rid in want - have:
            session.add(McpServerRoleAccess(mcp_server_id=mcp_server_id, role_id=rid))
        await session.commit()


# ----------------------- UI tabs -----------------------


tab_browse, tab_catalog, tab_add = st.tabs(["Browse", "Add from catalog", "Add custom"])

with tab_browse:
    servers = run_coroutine_in_lit_worker(_list_servers(), timeout=15) or []
    if not servers:
        st.info("No external MCPs registered yet. Use the **Add server** tab.")
    else:
        st.dataframe(
            [
                {
                    "Slug": s["slug"],
                    "Name": s["display_name"],
                    "Auth": s["auth_type"],
                    "Transport": s["transport"],
                    "Enabled": "✓" if s["enabled"] else "✗",
                    "URL": s["url"],
                    "Roles": s["n_roles"],
                    "Active grants": s["n_grants"],
                }
                for s in servers
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("### Edit / Delete")
        slug_choices = {s["slug"]: s for s in servers}
        picked = st.selectbox("Pick a server", list(slug_choices.keys()))
        if picked:
            srv = slug_choices[picked]
            mcp_server_id = uuid.UUID(srv["mcp_server_id"])

            col1, col2 = st.columns(2)
            with col1:
                new_enabled = st.toggle("Enabled", value=srv["enabled"], key=f"toggle_{picked}")
                if new_enabled != srv["enabled"]:
                    run_coroutine_in_lit_worker(
                        _toggle_enabled(mcp_server_id, new_enabled), timeout=10
                    )
                    st.cache_data.clear()
                    st.rerun()

            with col2:
                if st.button("Delete server", type="secondary"):
                    run_coroutine_in_lit_worker(_delete_server(mcp_server_id), timeout=10)
                    st.success(f"Deleted {picked!r}.")
                    st.cache_data.clear()
                    st.rerun()

            all_roles = run_coroutine_in_lit_worker(_all_roles(), timeout=10) or []
            role_by_id = {r.role_id: r for r in all_roles}

            async def _current_role_ids(mid: uuid.UUID = mcp_server_id) -> list[uuid.UUID]:
                async with get_async_session_read_replica() as session:
                    rows = (
                        await session.exec(
                            select(McpServerRoleAccess.role_id).where(
                                McpServerRoleAccess.mcp_server_id == mid
                            )
                        )
                    ).all()
                return list(rows)

            current = run_coroutine_in_lit_worker(_current_role_ids(), timeout=10) or []
            current_set = set(current)
            sel = st.multiselect(
                "Allowed roles",
                options=[r.role_id for r in all_roles],
                default=[rid for rid in current if rid in role_by_id],
                format_func=lambda rid: role_by_id[rid].name.value if rid in role_by_id else str(rid),
                help="Users carrying any of these roles will see this server on **My MCPs**.",
            )
            if set(sel) != current_set:
                if st.button("Save role assignments"):
                    run_coroutine_in_lit_worker(
                        _set_server_roles(mcp_server_id, list(sel)), timeout=10
                    )
                    st.success("Saved.")
                    st.cache_data.clear()
                    st.rerun()


with tab_catalog:
    st.markdown("### Catalog")
    st.caption(
        "Curated registry of well-known MCPs.  Everything except your OAuth "
        "client_id/secret is filled in for you — and when those are already in "
        "`.env` (which they are on a Mac-installed deploy), you can register all "
        "three with a single button."
    )

    existing = run_coroutine_in_lit_worker(_existing_slugs(), timeout=10) or set()
    catalog_roles = run_coroutine_in_lit_worker(_all_roles(), timeout=10) or []

    env_cid = os.environ.get("GOOGLE_AUTH_CLIENT_ID", "")
    env_csec = os.environ.get("GOOGLE_AUTH_CLIENT_SECRET", "")
    admin_role_id = next(
        (r.role_id for r in catalog_roles if r.name == RoleName.ADMIN), None
    )

    # One-click bulk install for the operator who's already on the
    # straightforward path: .env creds + ADMIN role.
    missing_in_db = [e for e in CATALOG if e.slug not in existing]
    if env_cid and env_csec and admin_role_id and missing_in_db:
        slugs_left = ", ".join(e.slug for e in missing_in_db)
        col1, col2 = st.columns([3, 2])
        with col1:
            st.markdown("#### Install all catalog MCPs now")
            st.caption(
                f"Will register **{slugs_left}** with the OAuth client in `.env` "
                f"and grant the `ADMIN` role.  Re-run safely — already-registered "
                f"servers are skipped."
            )
        with col2:
            if st.button("⚡ Install all", type="primary", use_container_width=True):
                summary: list[str] = []
                for entry in missing_in_db:
                    ok, msg = run_coroutine_in_lit_worker(
                        _create_from_catalog(
                            entry,
                            client_id=env_cid,
                            client_secret=env_csec,
                            role_ids=[admin_role_id],
                            enabled=True,
                        ),
                        timeout=15,
                    ) or (False, f"{entry.slug}: timed out")
                    summary.append(f"{'✓' if ok else '✗'} {msg}")
                for line in summary:
                    (st.success if line.startswith("✓") else st.error)(line)
                st.cache_data.clear()
                st.rerun()
    elif not (env_cid and env_csec):
        st.warning(
            "`GOOGLE_AUTH_CLIENT_ID` / `GOOGLE_AUTH_CLIENT_SECRET` not present in the "
            "Streamlit container env — falling back to per-card forms."
        )
    elif not missing_in_db:
        st.success("All catalog MCPs are already registered. Manage them on the **Browse** tab.")

    st.divider()

    for entry in CATALOG:
        is_registered = entry.slug in existing
        with st.container(border=True):
            head_l, head_r = st.columns([3, 1])
            with head_l:
                st.markdown(f"### {entry.display_name}  `{entry.slug}`")
                st.caption(entry.description)
                st.markdown(f"**URL:** `{entry.url}`")
                st.markdown(
                    f"**Auth:** {entry.auth_type.value} · "
                    f"**Scopes:** {', '.join(entry.scopes) or '—'}"
                )
                if entry.setup_hint:
                    st.info(entry.setup_hint)
            with head_r:
                if is_registered:
                    st.success("✓ Registered")
                    st.caption("Edit it on the Browse tab.")

            if not is_registered:
                # Two paths:
                # - .env carries GOOGLE_AUTH_CLIENT_ID/SECRET and ADMIN role
                #   exists → render a single bare button, true one-click.
                # - Otherwise → fall back to the per-card form so the
                #   operator can paste different creds / pick roles.
                if env_cid and env_csec and admin_role_id:
                    if st.button(
                        f"⚡ Add {entry.display_name}",
                        key=f"oneclick_{entry.slug}",
                        type="primary",
                    ):
                        ok, msg = run_coroutine_in_lit_worker(
                            _create_from_catalog(
                                entry,
                                client_id=env_cid,
                                client_secret=env_csec,
                                role_ids=[admin_role_id],
                                enabled=True,
                            ),
                            timeout=15,
                        ) or (False, "submission timed out")
                        if ok:
                            st.success(msg)
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    with st.form(f"catalog_add_{entry.slug}"):
                        cid = st.text_input(
                            "OAuth client_id", value=env_cid, key=f"cid_{entry.slug}"
                        )
                        csec = st.text_input(
                            "OAuth client_secret",
                            type="password",
                            value=env_csec,
                            key=f"csec_{entry.slug}",
                        )
                        sel = st.multiselect(
                            "Allowed roles",
                            options=[r.role_id for r in catalog_roles],
                            format_func=lambda rid, _r=catalog_roles: next(
                                (r.name.value for r in _r if r.role_id == rid), str(rid)
                            ),
                            key=f"roles_{entry.slug}",
                            help="Users with any of these roles will see + be able to connect this MCP.",
                        )
                        en = st.checkbox("Enable immediately", value=True, key=f"en_{entry.slug}")
                        if st.form_submit_button(f"Add {entry.display_name}"):
                            if not cid or not csec:
                                st.error("client_id and client_secret are required.")
                            else:
                                ok, msg = run_coroutine_in_lit_worker(
                                    _create_from_catalog(
                                        entry,
                                        client_id=cid,
                                        client_secret=csec,
                                        role_ids=list(sel),
                                        enabled=en,
                                    ),
                                    timeout=15,
                                ) or (False, "submission timed out")
                                if ok:
                                    st.success(msg)
                                    st.cache_data.clear()
                                    st.rerun()
                                else:
                                    st.error(msg)


with tab_add:
    st.markdown("### Register a custom MCP")
    st.caption(
        "Use this when the MCP isn't in the catalog (internal service, niche provider, "
        "or a non-OAuth endpoint).  Everything is typed in by hand."
    )

    with st.form("add_mcp_server", clear_on_submit=False):
        slug = st.text_input("Slug (lowercase, used in agent configs)")
        display_name = st.text_input("Display name")
        description = st.text_area("Description", height=80)
        url = st.text_input("MCP URL")
        transport = st.selectbox(
            "Transport", [t.value for t in McpTransport], index=0
        )
        auth_type = st.selectbox(
            "Auth type",
            [a.value for a in McpAuthType],
            index=[a.value for a in McpAuthType].index("OAUTH_OBO"),
            help=(
                "OAUTH_OBO: per-user OAuth flow.  M2M_SHARED: one admin-owned key for everyone.  "
                "M2M_PER_USER: each user pastes their own key.  NONE: no auth header."
            ),
        )

        st.markdown("#### Auth details")
        oauth_client_id = st.text_input(
            "OAuth client_id", disabled=auth_type != "OAUTH_OBO"
        )
        oauth_client_secret = st.text_input(
            "OAuth client_secret",
            type="password",
            disabled=auth_type != "OAUTH_OBO",
        )
        oauth_authorize_url = st.text_input(
            "OAuth authorize_url",
            disabled=auth_type != "OAUTH_OBO",
        )
        oauth_token_url = st.text_input(
            "OAuth token_url",
            disabled=auth_type != "OAUTH_OBO",
        )
        oauth_scopes = st.text_input(
            "OAuth scopes (space-separated)",
            disabled=auth_type != "OAUTH_OBO",
        )
        oauth_extra = st.text_area(
            "OAuth extra authorize params (JSON object, optional — e.g. Google needs "
            '`{"access_type": "offline", "prompt": "consent"}`)',
            height=70,
            disabled=auth_type != "OAUTH_OBO",
        )
        m2m_shared = st.text_input(
            "M2M shared bearer token",
            type="password",
            disabled=auth_type != "M2M_SHARED",
        )

        st.markdown("#### Access")
        roles = run_coroutine_in_lit_worker(_all_roles(), timeout=10) or []
        sel_roles = st.multiselect(
            "Allowed roles",
            options=[r.role_id for r in roles],
            format_func=lambda rid: next((r.name.value for r in roles if r.role_id == rid), str(rid)),
            help="Users with any of these roles will see + be able to connect this MCP.",
        )
        enabled = st.checkbox("Enabled", value=True)

        submitted = st.form_submit_button("Register")
        if submitted:
            errs: list[str] = []
            if not slug or not display_name or not url:
                errs.append("slug, display_name, url are required.")
            oauth_cfg: dict | None = None
            if auth_type == "OAUTH_OBO":
                if not (oauth_client_id and oauth_authorize_url and oauth_token_url):
                    errs.append("OAuth client_id, authorize_url, token_url are required.")
                extra_dict: dict[str, str] = {}
                if oauth_extra.strip():
                    try:
                        extra_dict = json.loads(oauth_extra)
                        if not isinstance(extra_dict, dict):
                            raise ValueError("must be a JSON object")
                    except Exception as e:
                        errs.append(f"Extra params must be a JSON object: {e}")
                oauth_cfg = {
                    "client_id": oauth_client_id,
                    "authorize_url": oauth_authorize_url,
                    "token_url": oauth_token_url,
                    "scopes": oauth_scopes.split(),
                    "extra_authorize_params": extra_dict,
                }
            if errs:
                for e in errs:
                    st.error(e)
            else:
                ok, msg = run_coroutine_in_lit_worker(
                    _create_server(
                        slug=slug,
                        display_name=display_name,
                        description=description,
                        url=url,
                        transport=McpTransport(transport),
                        auth_type=McpAuthType(auth_type),
                        oauth_config=oauth_cfg,
                        client_secret=oauth_client_secret or None,
                        m2m_shared_token=m2m_shared or None,
                        role_ids=list(sel_roles),
                        enabled=enabled,
                    ),
                    timeout=15,
                ) or (False, "submission timed out")
                if ok:
                    st.success(msg)
                    st.cache_data.clear()
                else:
                    st.error(msg)
