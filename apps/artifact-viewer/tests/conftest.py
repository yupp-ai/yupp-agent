"""Shared test fixtures.

Every test runs with a minimal set of ``VIEWER_*`` env vars so the
viewer's ``Settings`` loads without hitting the real OAuth / AHS.
"""

from __future__ import annotations

import os


def _set_test_env() -> None:
    os.environ.setdefault("VIEWER_ALLOWED_EMAIL_DOMAINS", "agcouch.com")
    os.environ.setdefault("VIEWER_ALLOWED_EMAILS", "")
    os.environ.setdefault("VIEWER_SESSION_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("VIEWER_SESSION_COOKIE_SECURE", "false")
    os.environ.setdefault("AGENT_HARNESS_SERVICE_API_KEY", "test-key")
    os.environ.setdefault("VIEWER_AHS_BASE_URL", "http://ahs.test")
    os.environ.setdefault("VIEWER_GOOGLE_CLIENT_ID", "test-client-id")
    os.environ.setdefault("VIEWER_GOOGLE_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("VIEWER_OAUTH_REDIRECT_URL", "http://127.0.0.1:8095/auth/callback")


_set_test_env()
