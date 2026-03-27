"""Permissions system for Streamlit server.

This module provides a simple YAML-based permissions system. Permissions are defined
in permissions.yaml, where each permission lists the emails that are granted access.
"""

from __future__ import annotations
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import streamlit as st
import yaml

from ypl.structured_logger import get_logger

LOGGER = get_logger()

# Path to the permissions config file (in ypl/ root to ensure code review)
PERMISSIONS_FILE = Path(__file__).parent.parent / "lit_permissions.yaml"


class Permission(StrEnum):
    """Available permissions in the system."""

    SEARCH_ALL_CHATS = "SEARCH_ALL_CHATS"
    AGENT_HARNESS_ADMIN = "AGENT_HARNESS_ADMIN"


@lru_cache(maxsize=1)
def _load_permissions_config() -> dict[str, set[str]]:
    """Load permissions from YAML config file.

    Returns a dict mapping permission names to sets of authorized emails.
    Results are cached for performance.
    """
    if not PERMISSIONS_FILE.exists():
        LOGGER.warning("permissions_file_not_found", path=str(PERMISSIONS_FILE))
        return {}

    try:
        with open(PERMISSIONS_FILE) as f:
            config = yaml.safe_load(f)

        if not config or "permissions" not in config:
            LOGGER.warning("permissions_config_empty_or_invalid", path=str(PERMISSIONS_FILE))
            return {}

        permissions_dict: dict[str, set[str]] = {}
        for perm_name, perm_config in config["permissions"].items():
            emails = perm_config.get("emails", []) if perm_config else []
            permissions_dict[perm_name] = set(emails)

        return permissions_dict

    except Exception as e:
        LOGGER.error("permissions_config_load_error", error=str(e), path=str(PERMISSIONS_FILE))
        return {}


def reload_permissions() -> None:
    """Clear the permissions cache to force reload from file."""
    _load_permissions_config.cache_clear()


def get_current_user_email() -> str | None:
    """Get the email of the currently logged in user."""
    try:
        if st.user is not None and st.user.is_logged_in:
            email = st.user.email
            if isinstance(email, str):
                return email
    except Exception:
        pass
    return None


def has_permission(permission: Permission, email: str | None = None) -> bool:
    """Check if a user has a specific permission.

    Args:
        permission: The permission to check
        email: The user's email. If not provided, uses the current Streamlit user.

    Returns:
        True if the user has the permission, False otherwise.
    """
    if email is None:
        email = get_current_user_email()

    if email is None:
        return False

    permissions_config = _load_permissions_config()
    authorized_emails = permissions_config.get(permission.value, set())

    return email in authorized_emails
