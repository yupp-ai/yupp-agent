"""Shared authentication utilities for Streamlit server."""

from typing import Any

import streamlit as st
from streamlit.errors import StreamlitAPIException

from ypl.structured_logger import get_logger

LOGGER = get_logger()


def is_allowed_email(email: Any) -> bool:
    """
    Just simple access control for now, anyone with a yupp.ai email can access.
    TODO(Tian): add access control maybe reusing the soul permission system.
    """
    return email is not None and isinstance(email, str) and email.endswith("@yupp.ai")


def is_auth_configured() -> bool:
    """Check if authentication is properly configured."""
    try:
        return "auth" in st.secrets and "client_id" in st.secrets.auth
    except Exception:
        return False


def require_auth_standalone() -> None:
    """
    Require authentication for standalone Streamlit apps.
    Call this at the top of standalone apps to enforce authentication.
    Shows error messages instead of redirecting (since there's no app.py to redirect to).
    """
    AUTH_ENABLED = is_auth_configured()

    if not AUTH_ENABLED:
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
            f"Your account ({st.user.email}) does not have access to this application. "
            "Only yupp.ai emails are authorized."
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
