"""Admin Roles & Permissions — view/edit RBAC roles, role permissions, and per-role membership."""

from __future__ import annotations
import uuid
from typing import Any

import streamlit as st
from sqlalchemy import func
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.rbac import Permission, Role, RoleName, RolePermission, UserRoleAssociation
from ypl.db.users import User, UserStatus
from ypl.streamlit_server.auth import require_admin_role, require_auth
from ypl.streamlit_server.permissions import get_current_user_email
from ypl.structured_logger import get_logger

st.set_page_config(page_title="Roles & Permissions", page_icon="🔐", layout="wide")
require_auth()
require_admin_role()

st.title("🔐 Roles & Permissions")

logger = get_logger()


# ── DB queries ────────────────────────────────────────────────────────────────


@retry_db
async def fetch_roles_summary() -> list[dict[str, Any]]:
    """Return one row per role with user count and permission count."""
    async with get_async_session_read_replica() as session:
        user_count_subq = (
            select(
                col(UserRoleAssociation.role_id).label("role_id"),
                func.count(col(UserRoleAssociation.user_id)).label("user_count"),
            )
            .group_by(col(UserRoleAssociation.role_id))
            .subquery()
        )
        perm_count_subq = (
            select(
                col(RolePermission.role_id).label("role_id"),
                func.count(col(RolePermission.permission)).label("perm_count"),
            )
            .group_by(col(RolePermission.role_id))
            .subquery()
        )

        query = (
            select(
                Role,
                func.coalesce(user_count_subq.c.user_count, 0).label("user_count"),
                func.coalesce(perm_count_subq.c.perm_count, 0).label("perm_count"),
            )
            .outerjoin(user_count_subq, col(Role.role_id) == user_count_subq.c.role_id)
            .outerjoin(perm_count_subq, col(Role.role_id) == perm_count_subq.c.role_id)
            .where(col(Role.deleted_at).is_(None))
            .order_by(col(Role.name))
        )
        result = await session.exec(query)
        rows = list(result.all())

    return [
        {
            "role_id": row[0].role_id,
            "name": row[0].name,
            "description": row[0].description or "",
            "user_count": int(row[1]),
            "perm_count": int(row[2]),
        }
        for row in rows
    ]


@retry_db
async def fetch_role_detail(role_id: uuid.UUID) -> dict[str, Any] | None:
    """Fetch a role with its users and permissions."""
    async with get_async_session_read_replica() as session:
        role_row = (await session.exec(select(Role).where(col(Role.role_id) == role_id))).one_or_none()
        if role_row is None:
            return None

        user_query = (
            select(User)
            .join(UserRoleAssociation, col(User.user_id) == col(UserRoleAssociation.user_id))
            .where(col(UserRoleAssociation.role_id) == role_id)
            .where(col(User.deleted_at).is_(None))
            .order_by(col(User.email))
        )
        user_rows = list((await session.exec(user_query)).all())

        perm_query = select(RolePermission.permission).where(col(RolePermission.role_id) == role_id)
        perm_rows: list[Permission] = [Permission(p) for p in (await session.exec(perm_query)).all()]

    return {
        "role_id": role_row.role_id,
        "name": role_row.name,
        "description": role_row.description or "",
        "users": [
            {
                "user_id": u.user_id,
                "email": u.email,
                "name": u.name,
                "status": u.status,
            }
            for u in user_rows
        ],
        "permissions": perm_rows,
    }


@retry_db
async def fetch_active_user_emails() -> list[dict[str, str]]:
    """Return all ACTIVE, non-deleted users as (user_id, email) dicts."""
    async with get_async_session_read_replica() as session:
        query = (
            select(User.user_id, User.email)
            .where(col(User.deleted_at).is_(None))
            .where(col(User.status) == UserStatus.ACTIVE)
            .order_by(col(User.email))
        )
        rows = list((await session.exec(query)).all())
    return [{"user_id": r[0], "email": r[1]} for r in rows]


@retry_db
async def count_admin_users() -> int:
    """Count ACTIVE users with the ADMIN role."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(func.count(func.distinct(User.user_id)))
            .select_from(User)
            .join(UserRoleAssociation, col(User.user_id) == col(UserRoleAssociation.user_id))
            .join(Role, col(UserRoleAssociation.role_id) == col(Role.role_id))
            .where(col(User.deleted_at).is_(None))
            .where(col(User.status) == UserStatus.ACTIVE)
            .where(col(Role.name) == RoleName.ADMIN)
            .where(col(Role.deleted_at).is_(None))
        )
        return int((await session.exec(stmt)).one())


# ── Mutations ─────────────────────────────────────────────────────────────────


@retry_db
async def add_user_to_role(user_id: str, role_id: uuid.UUID) -> bool:
    async with get_async_session() as session:
        existing = (
            await session.exec(
                select(UserRoleAssociation)
                .where(col(UserRoleAssociation.user_id) == user_id)
                .where(col(UserRoleAssociation.role_id) == role_id)
            )
        ).one_or_none()
        if existing is not None:
            return True
        session.add(UserRoleAssociation(user_id=user_id, role_id=role_id))
        await session.commit()
    return True


@retry_db
async def remove_user_from_role(user_id: str, role_id: uuid.UUID) -> bool:
    async with get_async_session() as session:
        assoc = (
            await session.exec(
                select(UserRoleAssociation)
                .where(col(UserRoleAssociation.user_id) == user_id)
                .where(col(UserRoleAssociation.role_id) == role_id)
            )
        ).one_or_none()
        if assoc is None:
            return False
        await session.delete(assoc)
        await session.commit()
    return True


@retry_db
async def add_role_permission(role_id: uuid.UUID, permission: Permission) -> bool:
    """Idempotent: grant a single permission to a role."""
    async with get_async_session() as session:
        existing = (
            await session.exec(
                select(RolePermission)
                .where(col(RolePermission.role_id) == role_id)
                .where(col(RolePermission.permission) == permission)
            )
        ).one_or_none()
        if existing is not None:
            return True
        session.add(RolePermission(role_id=role_id, permission=permission))
        await session.commit()
    return True


@retry_db
async def remove_role_permission(role_id: uuid.UUID, permission: Permission) -> bool:
    """Revoke a single permission from a role. Returns False if it was not granted."""
    async with get_async_session() as session:
        existing = (
            await session.exec(
                select(RolePermission)
                .where(col(RolePermission.role_id) == role_id)
                .where(col(RolePermission.permission) == permission)
            )
        ).one_or_none()
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
    return True


# ── Cached wrappers ───────────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _cached_roles_summary() -> list[dict[str, Any]]:
    return run_coroutine_in_lit_worker(fetch_roles_summary(), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_role_detail(role_id_str: str) -> dict[str, Any] | None:
    return run_coroutine_in_lit_worker(fetch_role_detail(uuid.UUID(role_id_str)), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_active_user_emails() -> list[dict[str, str]]:
    return run_coroutine_in_lit_worker(fetch_active_user_emails(), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_admin_user_count() -> int:
    return run_coroutine_in_lit_worker(count_admin_users(), timeout=10)


def _refresh_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── UI helpers ────────────────────────────────────────────────────────────────


def _current_user_id_lookup(email: str | None) -> str | None:
    """Look up the logged-in user's user_id by email (best-effort)."""
    if not email:
        return None
    normalized = email.strip().lower()
    for u in _cached_active_user_emails():
        if u["email"].lower() == normalized:
            return u["user_id"]
    return None


def _is_self(user_id: str) -> bool:
    return _current_user_id_lookup(get_current_user_email()) == user_id


def _render_roles() -> None:
    st.caption(
        "Roles are a fixed, seeded set. Edit role memberships and permissions here; "
        "use `seed_roles` in `ypl/mono_server/db.py` to add new roles. "
        "User creation/editing lives in the Users app."
    )

    roles = _cached_roles_summary()
    if not roles:
        st.info("No roles found. Run `seed_roles` to populate the RBAC tables.")
        return

    _COL_WIDTHS = [1.2, 2.6, 2.0, 0.6]
    header = st.columns(_COL_WIDTHS)
    with header[0]:
        st.markdown("**Role**")
    with header[1]:
        st.markdown("**Description**")
    with header[2]:
        st.markdown("**Permissions**")
    with header[3]:
        st.markdown("**Users**")
    st.divider()

    active_users = _cached_active_user_emails()
    email_to_user_id = {u["email"]: u["user_id"] for u in active_users}
    all_permissions = list(Permission)

    for role in roles:
        role_id: uuid.UUID = role["role_id"]
        role_name: RoleName = role["name"]
        detail = _cached_role_detail(str(role_id))

        summary_cols = st.columns(_COL_WIDTHS)
        with summary_cols[0]:
            st.markdown(
                f"<div style='font-size: 1.25rem; font-weight: 700'>{role_name.value}</div>",
                unsafe_allow_html=True,
            )
        with summary_cols[1]:
            st.markdown(role["description"] or "—")
        with summary_cols[2]:
            st.markdown(f"**{role['perm_count']}**")
            if detail and detail["permissions"]:
                perm_lines = "  \n".join(f"`{p.value}`" for p in sorted(detail["permissions"], key=lambda p: p.value))
                st.markdown(perm_lines)
            else:
                st.caption("—")
        with summary_cols[3]:
            st.markdown(str(role["user_count"]))

        with st.expander(f"Edit {role_name.value}", expanded=False):
            if detail is None:
                st.warning("Role not found.")
                continue

            st.markdown("#### Users in this role")
            if not detail["users"]:
                st.caption("No users assigned.")
            else:
                for member in detail["users"]:
                    user_cols = st.columns([4, 1])
                    with user_cols[0]:
                        label = member["email"]
                        if member["status"] != UserStatus.ACTIVE:
                            label = f"{label} (inactive)"
                        st.markdown(f"- `{label}` (`{member['user_id']}`)")
                    with user_cols[1]:
                        remove_key = f"remove_user_{role_id}_{member['user_id']}"
                        disable_self = role_name == RoleName.ADMIN and _is_self(member["user_id"])
                        if disable_self:
                            st.button(
                                "Remove",
                                key=remove_key,
                                disabled=True,
                                help="You cannot remove your own ADMIN role.",
                            )
                        elif st.button("Remove", key=remove_key):
                            if role_name == RoleName.ADMIN:
                                admin_count = _cached_admin_user_count()
                                if admin_count <= 1:
                                    st.toast("Cannot remove the last ADMIN user.", icon="⚠️")
                                    continue
                            try:
                                run_coroutine_in_lit_worker(
                                    remove_user_from_role(member["user_id"], role_id), timeout=10
                                )
                                st.toast(f"Removed {member['email']} from {role_name.value}", icon="✅")
                                _refresh_and_rerun()
                            except Exception as exc:
                                logger.exception("remove_user_from_role_failed")
                                st.error(f"Failed to remove user: {exc}")

            current_emails = {m["email"] for m in detail["users"]}
            addable_emails = sorted(e for e in email_to_user_id if e not in current_emails)
            add_cols = st.columns([4, 1])
            with add_cols[0]:
                selected_email = st.selectbox(
                    "Add user",
                    options=["— select user —", *addable_emails],
                    key=f"add_user_select_{role_id}",
                    index=0,
                )
            with add_cols[1]:
                st.markdown("&nbsp;")
                if st.button("Add", key=f"add_user_btn_{role_id}"):
                    if selected_email == "— select user —":
                        st.toast("Pick a user first.", icon="⚠️")
                    else:
                        target_user_id = email_to_user_id[selected_email]
                        try:
                            run_coroutine_in_lit_worker(add_user_to_role(target_user_id, role_id), timeout=10)
                            st.toast(f"Added {selected_email} to {role_name.value}", icon="✅")
                            _refresh_and_rerun()
                        except Exception as exc:
                            logger.exception("add_user_to_role_failed")
                            st.error(f"Failed to add user: {exc}")

            st.divider()
            st.markdown("#### Permissions for this role")
            current_perms: list[Permission] = sorted(detail["permissions"], key=lambda p: p.value)
            if not current_perms:
                st.caption("No permissions granted.")
            else:
                for perm in current_perms:
                    perm_cols = st.columns([4, 1])
                    with perm_cols[0]:
                        st.markdown(f"- `{perm.value}`")
                    with perm_cols[1]:
                        if st.button("Remove", key=f"remove_perm_{role_id}_{perm.value}"):
                            try:
                                run_coroutine_in_lit_worker(remove_role_permission(role_id, perm), timeout=10)
                                st.toast(f"Revoked {perm.value} from {role_name.value}", icon="✅")
                                _refresh_and_rerun()
                            except Exception as exc:
                                logger.exception("remove_role_permission_failed")
                                st.error(f"Failed to revoke permission: {exc}")

            current_perm_set = set(current_perms)
            addable_perms = [p for p in all_permissions if p not in current_perm_set]
            add_perm_cols = st.columns([4, 1])
            with add_perm_cols[0]:
                selected_perm_value = st.selectbox(
                    "Add permission",
                    options=["— select permission —", *(p.value for p in addable_perms)],
                    key=f"add_perm_select_{role_id}",
                    index=0,
                )
            with add_perm_cols[1]:
                st.markdown("&nbsp;")
                if st.button("Add", key=f"add_perm_btn_{role_id}"):
                    if selected_perm_value == "— select permission —":
                        st.toast("Pick a permission first.", icon="⚠️")
                    else:
                        try:
                            run_coroutine_in_lit_worker(
                                add_role_permission(role_id, Permission(selected_perm_value)), timeout=10
                            )
                            st.toast(f"Granted {selected_perm_value} to {role_name.value}", icon="✅")
                            _refresh_and_rerun()
                        except Exception as exc:
                            logger.exception("add_role_permission_failed")
                            st.error(f"Failed to grant permission: {exc}")

        st.divider()


# ── Render ────────────────────────────────────────────────────────────────────

_render_roles()
