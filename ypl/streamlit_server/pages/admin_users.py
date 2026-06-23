"""Admin Users — browse/edit the ``users`` table and add new user records."""

from __future__ import annotations
import re
import uuid
from typing import Any

import sqlalchemy as sa
import streamlit as st
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.rbac import Permission, Role, RoleName, RolePermission, UserRoleAssociation
from ypl.db.users import User, UserStatus, UserType
from ypl.streamlit_server.auth import require_admin_role, require_auth
from ypl.streamlit_server.permissions import get_current_user_email
from ypl.structured_logger import get_logger

st.set_page_config(page_title="Users", page_icon="👥", layout="wide")
require_auth()
require_admin_role()

st.title("👥 Users")

logger = get_logger()

_USERS_PAGE_SIZE = 50

# Small emoji glyph per UserType — used in the Browse table's first column.
_USER_TYPE_EMOJI: dict[UserType, str] = {
    UserType.HUMAN: "👤",
    UserType.AGENT: "🤖",
    UserType.SYSTEM: "⚙️",
}

# Sentinel value for "no user_type filter" in the dropdown.
_USER_TYPE_FILTER_ALL = "All"

# Slack member IDs look like ``U0B8385SG3U`` (users) or ``W...`` (enterprise-grid
# users) — an uppercase U/W prefix followed by uppercase alphanumerics. Validated
# leniently so we reject obvious mistakes (emails, display names) without being
# brittle about exact length.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _normalize_slack_user_id(raw: str | None) -> str | None:
    """Trim a Slack member ID; treat blank as ``None`` (i.e. unlinked)."""
    value = (raw or "").strip()
    return value or None


# ── DB queries ────────────────────────────────────────────────────────────────


@retry_db
async def fetch_users_page(
    email_filter: str,
    user_type_filter: str,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return a page of users with their roles + effective permissions, plus the total count."""
    normalized = email_filter.strip().lower()
    async with get_async_session_read_replica() as session:
        base = select(User).where(col(User.deleted_at).is_(None))
        if normalized:
            base = base.where(func.lower(User.email).contains(normalized))
        if user_type_filter and user_type_filter != _USER_TYPE_FILTER_ALL:
            base = base.where(col(User.user_type) == UserType(user_type_filter))

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
        "slack_user_id": user.slack_user_id,
        "roles": sorted(current_role_names, key=lambda r: r.value),
        "permissions": sorted(perms, key=lambda p: p.value),
    }


@retry_db
async def fetch_roles_summary() -> list[dict[str, Any]]:
    """Return one row per role (id, name) — used for the role-picker in Browse."""
    async with get_async_session_read_replica() as session:
        query = select(Role).where(col(Role.deleted_at).is_(None)).order_by(col(Role.name))
        rows = list((await session.exec(query)).all())
    return [{"role_id": r.role_id, "name": r.name, "description": r.description or ""} for r in rows]


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


@retry_db
async def fetch_active_user_emails() -> list[dict[str, str]]:
    async with get_async_session_read_replica() as session:
        query = (
            select(User.user_id, User.email)
            .where(col(User.deleted_at).is_(None))
            .where(col(User.status) == UserStatus.ACTIVE)
            .order_by(col(User.email))
        )
        rows = list((await session.exec(query)).all())
    return [{"user_id": r[0], "email": r[1]} for r in rows]


# ── Mutations ─────────────────────────────────────────────────────────────────


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


@retry_db
async def set_user_slack_id(user_id: str, slack_user_id: str | None) -> tuple[bool, str]:
    """Set (or clear, when ``None``) a user's ``slack_user_id``. Returns (success, message).

    Enforces the partial-unique ``idx_users_slack_user_id_unique`` constraint by
    mapping its IntegrityError to a friendly "already linked" message.
    """
    async with get_async_session() as session:
        user = (await session.exec(select(User).where(col(User.user_id) == user_id))).one_or_none()
        if user is None:
            return False, "User not found."
        user.slack_user_id = slack_user_id
        session.add(user)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            detail = str(getattr(exc, "orig", exc))
            if "slack_user_id" in detail:
                return False, f"Slack member ID '{slack_user_id}' is already linked to another user."
            logger.exception("admin_users.set_user_slack_id_unexpected_integrity_error")
            return False, f"Could not update Slack member ID: {detail}"
    return True, ("Slack member ID cleared." if slack_user_id is None else "Slack member ID updated.")


@retry_db
async def insert_user(
    email: str,
    name: str | None,
    user_type: UserType,
    status: UserStatus,
    role_ids: set[uuid.UUID],
    slack_user_id: str | None = None,
) -> tuple[bool, str, str | None]:
    """Insert a new User row and assign roles. Returns (success, message, user_id)."""
    normalized_email = email.strip().lower()
    async with get_async_session() as session:
        existing = (
            await session.exec(select(User.user_id).where(sa.func.lower(User.email) == normalized_email))
        ).one_or_none()
        if existing is not None:
            return False, f"A user with email '{normalized_email}' already exists.", None

        new_user_id = str(uuid.uuid4())
        row = User(
            user_id=new_user_id,
            email=normalized_email,
            name=name or None,
            status=status,
            user_type=user_type,
            slack_user_id=slack_user_id,
        )
        session.add(row)
        try:
            # Flush the parent users INSERT before staging child user_roles
            # rows — without this, the unit-of-work has been observed to emit
            # user_roles INSERTs first, tripping fk_user_roles_user_id_users.
            await session.flush()
            for rid in role_ids:
                session.add(UserRoleAssociation(user_id=new_user_id, role_id=rid))
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            return False, _format_insert_user_integrity_error(exc, normalized_email), None
    return True, f"User '{normalized_email}' created.", new_user_id


def _format_insert_user_integrity_error(exc: IntegrityError, normalized_email: str) -> str:
    """Map an IntegrityError from insert_user to a human-readable admin-facing message.

    The precheck on email + the DB unique constraint are not atomic, so concurrent
    submissions can land here on `users_email_key`. We also see FK failures when a
    role is deleted between the role-picker render and submit. Anything else is
    genuinely unexpected — log it so we have a server-side trail.
    """
    constraint = (getattr(exc.orig, "constraint_name", "") or "").lower()
    detail = str(exc.orig)

    if (
        "users_email_key" in constraint
        or "idx_users_lower_email" in constraint
        or "users_email_key" in detail
        or "idx_users_lower_email" in detail
    ):
        return f"A user with email '{normalized_email}' was just created by another session. Please refresh and retry."
    if "idx_users_slack_user_id_unique" in constraint or "slack_user_id" in detail:
        return "That Slack member ID is already linked to another user. Clear it or use a different one."
    if constraint.startswith("fk_user_roles_role_id") or "fk_user_roles_role_id" in detail:
        return "One of the selected roles no longer exists. Please refresh and retry."
    if constraint.startswith("fk_user_roles_user_id") or "fk_user_roles_user_id" in detail:
        return f"Could not create user (internal error attaching roles): {detail}"

    logger.exception("admin_users.insert_user_unexpected_integrity_error", constraint=constraint)
    return f"Could not create user: {detail}"


# ── Cached wrappers ───────────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _cached_users_page(
    email_filter: str, user_type_filter: str, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    return run_coroutine_in_lit_worker(fetch_users_page(email_filter, user_type_filter, offset, limit), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_user_detail(user_id: str) -> dict[str, Any] | None:
    return run_coroutine_in_lit_worker(fetch_user_detail(user_id), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_roles_summary() -> list[dict[str, Any]]:
    return run_coroutine_in_lit_worker(fetch_roles_summary(), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_admin_user_count() -> int:
    return run_coroutine_in_lit_worker(count_admin_users(), timeout=10)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_active_user_emails() -> list[dict[str, str]]:
    return run_coroutine_in_lit_worker(fetch_active_user_emails(), timeout=10)


def _refresh_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── UI helpers ────────────────────────────────────────────────────────────────


def _current_user_id_lookup(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    for u in _cached_active_user_emails():
        if u["email"].lower() == normalized:
            return u["user_id"]
    return None


def _is_self(user_id: str) -> bool:
    return _current_user_id_lookup(get_current_user_email()) == user_id


def _render_browse() -> None:
    filter_cols = st.columns([3, 1.5, 1])
    with filter_cols[0]:
        email_filter = st.text_input(
            "Filter by email (substring match)",
            key="user_email_filter",
            value=st.session_state.get("user_email_filter", ""),
        )
    with filter_cols[1]:
        user_type_options = [_USER_TYPE_FILTER_ALL, *(t.value for t in UserType)]
        default_user_type = st.session_state.get("user_type_filter", UserType.HUMAN.value)
        if default_user_type not in user_type_options:
            default_user_type = UserType.HUMAN.value
        user_type_filter = st.selectbox(
            "User type",
            options=user_type_options,
            index=user_type_options.index(default_user_type),
            key="user_type_filter",
            format_func=lambda v: (
                v if v == _USER_TYPE_FILTER_ALL else f"{_USER_TYPE_EMOJI.get(UserType(v), '')} {v}".strip()
            ),
        )
    with filter_cols[2]:
        st.markdown("&nbsp;")
        if st.button("Refresh", key="users_refresh"):
            _refresh_and_rerun()

    if "users_page" not in st.session_state:
        st.session_state["users_page"] = 0
    page = int(st.session_state["users_page"])
    offset = page * _USERS_PAGE_SIZE

    with st.spinner("Loading users..."):
        users, total = _cached_users_page(email_filter, user_type_filter, offset, _USERS_PAGE_SIZE)

    if total == 0:
        st.info("No users match the current filter.")
        return

    total_pages = (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE
    st.caption(f"Showing page {page + 1} of {total_pages} — {total} user(s) total")

    _COL_WIDTHS = [0.6, 2.2, 3.0, 0.9, 2.2, 3.2, 0.7]
    hdr = st.columns(_COL_WIDTHS)
    for i, label in enumerate(("Type", "Name", "Email", "Status", "Roles", "Effective permissions", "")):
        with hdr[i]:
            st.markdown(f"**{label}**")

    selected_user_id: str | None = st.session_state.get("selected_user_id")

    for user in users:
        row = st.columns(_COL_WIDTHS)
        with row[0]:
            user_type: UserType = user["user_type"]
            emoji = _USER_TYPE_EMOJI.get(user_type, "")
            st.markdown(
                f"<span title='{user_type.value}' style='font-size: 1.25rem'>{emoji}</span>",
                unsafe_allow_html=True,
            )
        with row[1]:
            name = user["name"] or "—"
            st.markdown(
                f"<div style='font-size: 1.15rem; font-weight: 600'>{name}</div>",
                unsafe_allow_html=True,
            )
        with row[2]:
            st.markdown(f"`{user['email']}`")
        with row[3]:
            st.markdown(user["status"].value)
        with row[4]:
            if user["roles"]:
                st.markdown(", ".join(r.value for r in user["roles"]))
            else:
                st.caption("—")
        with row[5]:
            if user["permissions"]:
                st.markdown(", ".join(p.value for p in user["permissions"]))
            else:
                st.caption("—")
        with row[6]:
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


def _render_slack_link_editor(user_id: str, current_slack_user_id: str | None) -> None:
    """Edit the ``users.slack_user_id`` mapping that lets SAG attribute Slack messages.

    Without this link a Slack author resolves to ``None`` and the agent treats
    their messages as the SYSTEM user — i.e. it won't act as them.
    """
    st.markdown("#### Slack link")
    slack_input = st.text_input(
        "Slack member ID",
        value=current_slack_user_id or "",
        key=f"user_slack_id_{user_id}",
        help=(
            "Slack member ID like `U0B8385SG3U` (Slack profile → ⋮ → Copy member ID). "
            "Required for this user to message agents in Slack. Leave blank to unlink."
        ),
    )
    st.caption("Takes effect after the Slack Gateway's resolution cache expires (up to ~12h) or a service restart.")
    if st.button("Save Slack ID", key=f"save_user_slack_{user_id}"):
        normalized = _normalize_slack_user_id(slack_input)
        if normalized is not None and not _SLACK_USER_ID_RE.match(normalized):
            st.error("That doesn't look like a Slack member ID (expected like `U0B8385SG3U`).")
            return
        if normalized == (current_slack_user_id or None):
            st.toast("No change to Slack member ID.", icon="ℹ️")
            return
        try:
            ok, msg = run_coroutine_in_lit_worker(set_user_slack_id(user_id, normalized), timeout=15)
            if ok:
                st.toast(msg, icon="✅")
                _refresh_and_rerun()
            else:
                st.error(msg)
        except Exception as exc:
            logger.exception("set_user_slack_id_failed")
            st.error(f"Failed to update Slack member ID: {exc}")


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

    _render_slack_link_editor(user_id, detail.get("slack_user_id"))

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


def _render_add_user() -> None:
    st.subheader("Add new user")
    st.caption(
        "Creates a row in the ``users`` table and assigns at least one RBAC role. "
        "You can adjust roles later from the *Browse* tab (or from the Roles & Permissions app)."
    )

    user_type_values = [t.value for t in UserType]
    status_values = [s.value for s in UserStatus]

    roles_summary = _cached_roles_summary()
    role_name_to_id: dict[RoleName, uuid.UUID] = {RoleName(r["name"]): r["role_id"] for r in roles_summary}
    available_role_names: list[RoleName] = [rn for rn in RoleName if rn in role_name_to_id]

    if not available_role_names:
        st.error(
            "No RBAC roles found in the database. Run `seed_roles` (see `ypl/mono_server/db.py`) before adding users."
        )
        return

    with st.form("add_user_form", clear_on_submit=True):
        email = st.text_input("Email", help="Used as the unique identifier. Stored lowercased.").strip()
        name = st.text_input("Display name (optional)").strip()
        slack_user_id_raw = st.text_input(
            "Slack member ID (optional)",
            help=(
                "Slack member ID like `U0B8385SG3U` (Slack profile → ⋮ → Copy member ID). "
                "Required for this user to message agents in Slack. Can be set later from *Browse*."
            ),
        ).strip()
        user_type_value = st.selectbox(
            "User type", options=user_type_values, index=user_type_values.index(UserType.HUMAN.value)
        )
        status_value = st.selectbox("Status", options=status_values, index=status_values.index(UserStatus.ACTIVE.value))
        selected_role_names: list[RoleName] = st.multiselect(
            "Roles",
            options=available_role_names,
            default=[],
            format_func=lambda r: r.value,
            help="Pick one or more RBAC roles. At least one role must be selected.",
        )
        submitted = st.form_submit_button("Create user", type="primary")

    if not submitted:
        return

    if not email:
        st.error("Email is required.")
        return
    if "@" not in email:
        st.error("Email looks malformed.")
        return
    if not selected_role_names:
        st.error("At least one role must be assigned.")
        return

    slack_user_id = _normalize_slack_user_id(slack_user_id_raw)
    if slack_user_id is not None and not _SLACK_USER_ID_RE.match(slack_user_id):
        st.error("Slack member ID doesn't look valid (expected like `U0B8385SG3U`). Leave blank to skip.")
        return

    role_ids = {role_name_to_id[rn] for rn in selected_role_names if rn in role_name_to_id}

    success, message, new_user_id = run_coroutine_in_lit_worker(
        insert_user(
            email=email,
            name=name or None,
            user_type=UserType(user_type_value),
            status=UserStatus(status_value),
            role_ids=role_ids,
            slack_user_id=slack_user_id,
        ),
        timeout=10,
    )
    if success:
        st.toast(message, icon="✅")
        if new_user_id:
            roles_str = ", ".join(rn.value for rn in selected_role_names)
            st.success(f"Created user_id: `{new_user_id}` with roles: {roles_str}")
        _refresh_and_rerun()
    else:
        st.error(message)


# ── Render ────────────────────────────────────────────────────────────────────

tab_browse, tab_add = st.tabs(["Browse", "Add User"])

with tab_browse:
    _render_browse()

with tab_add:
    _render_add_user()
