"""Helpers for email-domain identity gating.

Centralizes the "is this user from an allowed org" check so AHS, MCP, and
other services stay consistent when ``settings.ALLOWED_EMAIL_DOMAINS`` changes.
"""

from __future__ import annotations

from ypl.backend.config import settings


def is_allowed_email_domain(email: str, *, domains: list[str] | None = None) -> bool:
    """Return True if ``email``'s domain is allowed.

    When the active domain list is empty, gating is disabled and all emails
    pass. Pass ``domains`` to check against a specific list (e.g. MCP's
    ``ALLOWED_MCP_EMAIL_DOMAINS``); otherwise falls back to the general
    ``settings.ALLOWED_EMAIL_DOMAINS``.
    """
    active = domains if domains is not None else settings.ALLOWED_EMAIL_DOMAINS
    if not active:
        return True
    email_domain = email.rsplit("@", 1)[-1].lower()
    return email_domain in {d.lower() for d in active}
