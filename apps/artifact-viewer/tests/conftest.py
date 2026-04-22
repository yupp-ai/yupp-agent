"""Shared test fixtures.

Every test runs with:
  - ``ALLOWED_EMAIL_DOMAINS=agcouch.com`` so login checks work
  - ``SESSION_COOKIE_SECURE=false`` so Starlette's TestClient can round-trip
    the session cookie over http
"""

from __future__ import annotations

import os


def _set_test_env() -> None:
    os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "agcouch.com")
    os.environ.setdefault("ALLOWED_EMAILS", "")
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
    os.environ.setdefault("AHS_API_KEY", "test-key")
    os.environ.setdefault("AHS_BASE_URL", "http://ahs.test")
    os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
    os.environ.setdefault("OAUTH_REDIRECT_URL", "http://127.0.0.1:8095/auth/callback")


_set_test_env()
