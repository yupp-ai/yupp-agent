"""Shared authentication utilities for Streamlit server."""

import os
from typing import Any

import sqlalchemy as sa
import streamlit as st
from sqlmodel import select
from streamlit.errors import StreamlitAPIException

from ypl.backend.db import get_async_session_read_replica
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.rbac import Role, RoleName, UserRoleAssociation
from ypl.db.users import User, UserStatus
from ypl.structured_logger import get_logger

LOGGER = get_logger()


def is_selfhosted() -> bool:
    """True when running in the selfhosted (one-box) deployment mode."""
    return os.environ.get("ENVIRONMENT", "").lower() == "selfhosted"


async def _email_exists_in_users(email: str) -> bool:
    """Return True if an ACTIVE user row exists whose email matches (case-insensitive)."""
    normalized = email.strip().lower()
    async with get_async_session_read_replica() as session:
        stmt = select(User.user_id).where(
            sa.func.lower(User.email) == normalized,
            User.status == UserStatus.ACTIVE,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


@st.cache_data(ttl=60, show_spinner=False)
def _email_is_allowed_cached(email: str) -> bool:
    """Cached DB lookup — refreshes at most once per minute per email."""
    try:
        return run_coroutine_in_lit_worker(_email_exists_in_users(email), timeout=10)
    except Exception as exc:
        # DB blip shouldn't 500 the page; fail closed by denying access.
        LOGGER.warning("is_allowed_email_db_error", email=email, error=str(exc))
        return False


def is_allowed_email(email: Any) -> bool:
    """Allow access only if the email corresponds to an ACTIVE user in the ``users`` table.

    Comparison is case-insensitive and matches the ``idx_users_lower_email`` unique index.
    """
    if email is None or not isinstance(email, str) or not email.strip():
        return False
    return _email_is_allowed_cached(email.strip().lower())


def is_auth_configured() -> bool:
    """Check if authentication is properly configured."""
    try:
        return "auth" in st.secrets and "client_id" in st.secrets.auth
    except Exception:
        return False


async def _user_has_admin_role(email: str) -> bool:
    """Return True if the email maps to an ACTIVE user with the ADMIN role."""
    normalized = email.strip().lower()
    async with get_async_session_read_replica() as session:
        stmt = (
            select(User.user_id)
            .join(UserRoleAssociation, User.user_id == UserRoleAssociation.user_id)  # type: ignore[arg-type]
            .join(Role, UserRoleAssociation.role_id == Role.role_id)  # type: ignore[arg-type]
            .where(
                sa.func.lower(User.email) == normalized,
                User.status == UserStatus.ACTIVE,
                Role.name == RoleName.ADMIN,
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


@st.cache_data(ttl=60, show_spinner=False)
def _is_admin_cached(email: str) -> bool:
    try:
        return run_coroutine_in_lit_worker(_user_has_admin_role(email), timeout=10)
    except Exception as exc:
        LOGGER.warning("is_admin_db_error", email=email, error=str(exc))
        return False


def is_admin(email: Any = None) -> bool:
    """True if ``email`` (or the current logged-in user) has the ADMIN role."""
    if email is None:
        try:
            email = st.user.email if st.user is not None and st.user.is_logged_in else None
        except Exception:
            email = None
    if email is None or not isinstance(email, str) or not email.strip():
        return False
    return _is_admin_cached(email.strip().lower())


def require_admin_role() -> None:
    """Stop the page render unless the logged-in user has the ADMIN role.

    Call this AFTER ``require_auth()`` on any admin-only page.
    """
    if not is_admin():
        st.error("🚫 Admin access required.")
        st.warning("This page is only available to users with the ADMIN role.")
        st.stop()


def auth_required() -> bool:
    """True when the server must refuse access unless the user is logged in.

    Selfhosted deployments MUST always require auth — there is no unauthenticated
    fallback mode. Other deployments fall back to "dev mode" when auth secrets
    aren't configured.
    """
    return is_selfhosted() or is_auth_configured()


def require_auth_standalone() -> None:
    """
    Require authentication for standalone Streamlit apps.
    Call this at the top of standalone apps to enforce authentication.
    Shows error messages instead of redirecting (since there's no app.py to redirect to).
    """
    AUTH_ENABLED = is_auth_configured()

    if not AUTH_ENABLED:
        if is_selfhosted():
            st.error("🔒 **Authentication Required**")
            st.warning(
                "Selfhosted mode requires Google OAuth to be configured. "
                "Set `GOOGLE_AUTH_CLIENT_ID`, `GOOGLE_AUTH_CLIENT_SECRET`, "
                "`GOOGLE_AUTH_REDIRECT_URI`, and `GOOGLE_AUTH_COOKIE_SECRET` "
                "in your `.env`, then restart the service."
            )
            st.stop()
        st.warning(
            "⚠️ **Authentication Not Configured** - Running in development mode. "
            "See [AUTHENTICATION.md](./AUTHENTICATION.md) for setup instructions."
        )
        # Allow access in development mode when auth is not configured
        return

    # Check if user is logged in
    if not st.user.is_logged_in:
        st.error("🔒 **Authentication Required**")
        st.info(
            "Please configure authentication to access this app. "
            "See [AUTHENTICATION.md](./AUTHENTICATION.md) for setup instructions."
        )
        st.stop()

    # Check if user is authorized
    if not is_allowed_email(st.user.email):
        st.error("🚫 **Access Denied**")
        st.warning(
            f"Your account ({st.user.email}) is not a registered user. Ask an admin to create a user for this email."
        )
        st.stop()


def _redirect_to_home(page_name: str = "app.py") -> None:
    try:
        st.switch_page(page_name)
        st.stop()
    except StreamlitAPIException as exc:
        LOGGER.warning(
            "auth_redirect_failed",
            page_name=page_name,
            error_message=str(exc),
        )
        st.error(
            "Authentication required, but automatic redirect failed. "
            "Open `ypl/streamlit_server/app.py` via `streamlit run` to complete authentication."
        )
        st.stop()


def require_auth() -> None:
    """
    Require authentication for a page.
    Call this at the top of each page to enforce authentication.
    Redirects to login if not authenticated.
    """
    # Constants
    HIDE_SIDEBAR_STYLE = "<style>[data-testid='stSidebar'] {display: none;}</style>"
    # Explicitly restore sidebar visibility when authenticated (using !important to override any previous hide styles)
    SHOW_SIDEBAR_STYLE = (
        "<style>"
        "section[data-testid='stSidebar'], [data-testid='stSidebar'] {"
        "    display: block !important;"
        "    visibility: visible !important;"
        "}"
        "</style>"
    )
    AUTH_ENABLED = is_auth_configured()

    if not AUTH_ENABLED:
        # Hide sidebar immediately to prevent flash
        st.markdown(HIDE_SIDEBAR_STYLE, unsafe_allow_html=True)
        if is_selfhosted():
            # Selfhosted must never expose pages without auth.
            _redirect_to_home()
            return
        st.info(
            "Authentication secrets are not configured; continuing in standalone mode. "
            "Run `streamlit run ypl/streamlit_server/app.py` to enable authentication."
        )
        return

    # Check if user is logged in
    if st.user is None or not st.user.is_logged_in:
        # Hide sidebar immediately to prevent flash
        st.markdown(HIDE_SIDEBAR_STYLE, unsafe_allow_html=True)
        # Automatically redirect to login page (home)
        _redirect_to_home()

    # Check if user is authorized
    if st.user is None or not is_allowed_email(st.user.email):
        # Hide sidebar immediately to prevent flash
        st.markdown(HIDE_SIDEBAR_STYLE, unsafe_allow_html=True)
        # Redirect to home page which will show the access denied screen
        _redirect_to_home()

    # Authentication successful - explicitly restore sidebar visibility
    # This overrides any previous hide styles that might persist from earlier reruns
    st.markdown(SHOW_SIDEBAR_STYLE, unsafe_allow_html=True)
