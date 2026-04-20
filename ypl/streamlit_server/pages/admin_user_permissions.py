"""Admin User Permissions — browse and edit RBAC roles, role permissions, and user-role assignments."""

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

st.set_page_config(page_title="User Permissions", page_icon="🔐", layout="wide")
require_auth()
require_admin_role()

st.title("🔐 User Permissions")

logger = get_logger()

_USERS_PAGE_SIZE = 50


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
async def fetch_users_page(email_filter: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Return a page of users with their roles + effective permissions, plus the total count."""
    normalized = email_filter.strip().lower()
    async with get_async_session_read_replica() as session:
        base = select(User).where(col(User.deleted_at).is_(None))
        if normalized:
            base = base.where(func.lower(User.email).contains(normalized))

        count_stmt = select(func.count()).select_from(base.subquery())
        total = int((await session.exec(count_stmt)).one())

        page_stmt = base.order_by(col(User.email)).offset(offset).limit(limit)
        users = list((await session.exec(page_stmt)).all())

        user_ids = [u.user_id for u in users]
        roles_by_user: dict[str, list[RoleName]] = {uid: [] for uid in user_ids}
        perms_by_user: dict[str, set[Permission]] = {uid: set() for uid in user_ids}

        if user_ids:
            role_rows = list(
                (
                    await session.exec(
                        select(
                            UserRoleAssociation.user_id,
                            Role.role_id,
                            Role.name,
                        )
                        .join(Role, col(Role.role_id) == col(UserRoleAssociation.role_id))
                        .where(col(UserRoleAssociation.user_id).in_(user_ids))
                        .where(col(Role.deleted_at).is_(None))
                    )
                ).all()
            )
            role_ids_for_users = {row[1] for row in role_rows}
            perm_rows = (
                list(
                    (
                        await session.exec(
                            select(RolePermission.role_id, RolePermission.permission).where(
                                col(RolePermission.role_id).in_(role_ids_for_users)
                            )
                        )
                    ).all()
                )
                if role_ids_for_users
                else []
            )
            perms_by_role: dict[uuid.UUID, list[Permission]] = {}
            for rid, perm in perm_rows:
                perms_by_role.setdefault(rid, []).append(Permission(perm))

            for uid, rid, rname in role_rows:
                roles_by_user[uid].append(RoleName(rname))
                for p in perms_by_role.get(rid, []):
                    perms_by_user[uid].add(p)

    return (
        [
            {
                "user_id": u.user_id,
                "email": u.email,
                "name": u.name,
                "status": u.status,
                "user_type": u.user_type,
                "roles": sorted(roles_by_user.get(u.user_id, []), key=lambda r: r.value),
                "permissions": sorted(perms_by_user.get(u.user_id, set()), key=lambda p: p.value),
            }
            for u in users
        ],
        total,
    )


@retry_db
async def fetch_user_detail(user_id: str) -> dict[str, Any] | None:
    async with get_async_session_read_replica() as session:
        user = (await session.exec(select(User).where(col(User.user_id) == user_id))).one_or_none()
        if user is None:
            return None

        role_rows = list(
            (
                await session.exec(
                    select(Role.role_id, Role.name)
                    .join(UserRoleAssociation, col(Role.role_id) == col(UserRoleAssociation.role_id))
                    .where(col(UserRoleAssociation.user_id) == user_id)
                    .where(col(Role.deleted_at).is_(None))
                )
            ).all()
        )
        current_role_ids = [row[0] for row in role_rows]
        current_role_names: list[RoleName] = [RoleName(row[1]) for row in role_rows]

        perms: set[Permission] = set()
        if current_role_ids:
            perm_rows = list(
                (
                    await session.exec(
                        select(RolePermission.permission).where(col(RolePermission.role_id).in_(current_role_ids))
                    )
                ).all()
            )
            perms = {Permission(p) for p in perm_rows}

    return {
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
        "status": user.status,
        "user_type": user.user_type,
        "roles": sorted(current_role_names, key=lambda r: r.value),
        "permissions": sorted(perms, key=lambda p: p.value),
    }


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
async def set_role_permissions(role_id: uuid.UUID, desired: set[Permission]) -> bool:
    async with get_async_session() as session:
        existing = list(
            (await session.exec(select(RolePermission).where(col(RolePermission.role_id) == role_id))).all()
        )
        existing_set = {rp.permission for rp in existing}

        to_delete = [rp for rp in existing if rp.permission not in desired]
        to_add = desired - existing_set

        for rp in to_delete:
            await session.delete(rp)
        for perm in to_add:
            session.add(RolePermission(role_id=role_id, permission=perm))

        await session.commit()
    return True


@retry_db
async def set_user_roles(user_id: str, desired_role_ids: set[uuid.UUID]) -> bool:
    async with get_async_session() as session:
        existing = list(
            (await session.exec(select(UserRoleAssociation).where(col(UserRoleAssociation.user_id) == user_id))).all()
        )
        existing_ids = {a.role_id for a in existing}

        to_delete = [a for a in existing if a.role_id not in desired_role_ids]
        to_add = desired_role_ids - existing_ids

        for a in to_delete:
            await session.delete(a)
        for rid in to_add:
            session.add(UserRoleAssociation(user_id=user_id, role_id=rid))

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
def _cached_users_page(email_filter: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    return run_coroutine_in_lit_worker(fetch_users_page(email_filter, offset, limit), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_user_detail(user_id: str) -> dict[str, Any] | None:
    return run_coroutine_in_lit_worker(fetch_user_detail(user_id), timeout=10)


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


def _render_roles_tab() -> None:
    st.subheader("Roles")
    st.caption(
        "Roles are a fixed, seeded set. Edit role memberships and permissions here; "
        "use `seed_roles` in `ypl/mono_server/db.py` to add new roles."
    )

    roles = _cached_roles_summary()
    if not roles:
        st.info("No roles found. Run `seed_roles` to populate the RBAC tables.")
        return

    header = st.columns([1.4, 3, 0.8, 1])
    with header[0]:
        st.markdown("**Role**")
    with header[1]:
        st.markdown("**Description**")
    with header[2]:
        st.markdown("**Users**")
    with header[3]:
        st.markdown("**Permissions**")
    st.divider()

    active_users = _cached_active_user_emails()
    email_to_user_id = {u["email"]: u["user_id"] for u in active_users}
    all_permissions = list(Permission)

    for role in roles:
        role_id: uuid.UUID = role["role_id"]
        role_name: RoleName = role["name"]

        summary_cols = st.columns([1.4, 3, 0.8, 1])
        with summary_cols[0]:
            st.markdown(f"**{role_name.value}**")
        with summary_cols[1]:
            st.markdown(role["description"] or "—")
        with summary_cols[2]:
            st.markdown(str(role["user_count"]))
        with summary_cols[3]:
            st.markdown(str(role["perm_count"]))

        with st.expander(f"Edit {role_name.value}", expanded=False):
            detail = _cached_role_detail(str(role_id))
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
            current_perms: list[Permission] = list(detail["permissions"])
            selected_perms = st.multiselect(
                "Permissions",
                options=all_permissions,
                default=current_perms,
                format_func=lambda p: p.value,
                key=f"perms_multi_{role_id}",
            )
            if st.button("Save permissions", key=f"save_perms_{role_id}"):
                try:
                    run_coroutine_in_lit_worker(set_role_permissions(role_id, set(selected_perms)), timeout=15)
                    st.toast(f"Updated permissions for {role_name.value}", icon="✅")
                    _refresh_and_rerun()
                except Exception as exc:
                    logger.exception("set_role_permissions_failed")
                    st.error(f"Failed to update permissions: {exc}")

        st.divider()


def _render_users_tab() -> None:
    st.subheader("Users")

    filter_cols = st.columns([4, 1])
    with filter_cols[0]:
        email_filter = st.text_input(
            "Filter by email (substring match)",
            key="user_email_filter",
            value=st.session_state.get("user_email_filter", ""),
        )
    with filter_cols[1]:
        st.markdown("&nbsp;")
        if st.button("Refresh", key="users_refresh"):
            _refresh_and_rerun()

    if "users_page" not in st.session_state:
        st.session_state["users_page"] = 0
    page = int(st.session_state["users_page"])
    offset = page * _USERS_PAGE_SIZE

    with st.spinner("Loading users..."):
        users, total = _cached_users_page(email_filter, offset, _USERS_PAGE_SIZE)

    if total == 0:
        st.info("No users match the current filter.")
        return

    total_pages = (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE
    st.caption(f"Showing page {page + 1} of {total_pages} — {total} user(s) total")

    hdr = st.columns([3, 1, 2.5, 3.5, 0.8])
    for i, label in enumerate(("Email", "Status", "Roles", "Effective permissions", "")):
        with hdr[i]:
            st.markdown(f"**{label}**")

    selected_user_id: str | None = st.session_state.get("selected_user_id")

    for user in users:
        row = st.columns([3, 1, 2.5, 3.5, 0.8])
        with row[0]:
            st.markdown(f"`{user['email']}`")
            if user["name"]:
                st.caption(user["name"])
        with row[1]:
            st.markdown(user["status"].value)
        with row[2]:
            if user["roles"]:
                st.markdown(", ".join(r.value for r in user["roles"]))
            else:
                st.caption("—")
        with row[3]:
            if user["permissions"]:
                st.markdown(", ".join(p.value for p in user["permissions"]))
            else:
                st.caption("—")
        with row[4]:
            if st.button("Edit", key=f"edit_user_{user['user_id']}"):
                st.session_state["selected_user_id"] = user["user_id"]
                selected_user_id = user["user_id"]

    nav_cols = st.columns([1, 1, 6])
    with nav_cols[0]:
        if st.button("◀ Prev", disabled=page <= 0, key="users_prev"):
            st.session_state["users_page"] = page - 1
            st.rerun()
    with nav_cols[1]:
        if st.button("Next ▶", disabled=page + 1 >= total_pages, key="users_next"):
            st.session_state["users_page"] = page + 1
            st.rerun()

    if selected_user_id:
        _render_user_detail(selected_user_id)


def _render_user_detail(user_id: str) -> None:
    st.divider()
    st.subheader("User detail")

    detail = _cached_user_detail(user_id)
    if detail is None:
        st.warning("User not found.")
        if st.button("Close", key="close_user_detail"):
            st.session_state.pop("selected_user_id", None)
            st.rerun()
        return

    st.markdown(f"**Email:** `{detail['email']}`")
    st.markdown(f"**User ID:** `{detail['user_id']}`")
    st.markdown(f"**Status:** {detail['status'].value} · **Type:** {detail['user_type'].value}")

    roles_summary = _cached_roles_summary()
    role_name_to_id = {r["name"]: r["role_id"] for r in roles_summary}

    all_role_names = [rn for rn in RoleName if rn in role_name_to_id]
    current_names: list[RoleName] = list(detail["roles"])

    is_self = _is_self(user_id)
    disable_admin_removal = is_self and RoleName.ADMIN in current_names

    selected_names: list[RoleName] = st.multiselect(
        "Roles",
        options=all_role_names,
        default=current_names,
        format_func=lambda r: r.value,
        key=f"user_roles_multi_{user_id}",
    )

    if disable_admin_removal and RoleName.ADMIN not in selected_names:
        st.warning("You cannot remove your own ADMIN role — it has been re-added.")
        selected_names = [*selected_names, RoleName.ADMIN]

    save_col, close_col = st.columns([1, 1])
    with save_col:
        if st.button("Save roles", key=f"save_user_roles_{user_id}"):
            desired_ids = {role_name_to_id[n] for n in selected_names if n in role_name_to_id}
            removing_admin = RoleName.ADMIN in current_names and RoleName.ADMIN not in selected_names
            if removing_admin:
                admin_count = _cached_admin_user_count()
                if admin_count <= 1:
                    st.toast("Cannot remove the last ADMIN user.", icon="⚠️")
                    return
            try:
                run_coroutine_in_lit_worker(set_user_roles(user_id, desired_ids), timeout=15)
                st.toast(f"Updated roles for {detail['email']}", icon="✅")
                _refresh_and_rerun()
            except Exception as exc:
                logger.exception("set_user_roles_failed")
                st.error(f"Failed to update roles: {exc}")
    with close_col:
        if st.button("Close", key=f"close_user_detail_{user_id}"):
            st.session_state.pop("selected_user_id", None)
            st.rerun()

    st.markdown("#### Effective permissions (read-only)")
    if detail["permissions"]:
        st.markdown(", ".join(p.value for p in detail["permissions"]))
    else:
        st.caption("No permissions granted.")


# ── Render ────────────────────────────────────────────────────────────────────

tab_roles, tab_users = st.tabs(["Roles", "Users"])

with tab_roles:
    _render_roles_tab()

with tab_users:
    _render_users_tab()
