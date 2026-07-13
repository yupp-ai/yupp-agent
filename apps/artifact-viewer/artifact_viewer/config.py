"""Environment-driven config for the artifact viewer.

The viewer reads the **same .env file as the AHS monolith** (typically
``/data/ahs/.env`` in production). Viewer-specific settings are
namespaced with a ``VIEWER_`` prefix so they don't collide with AHS /
SAG / MCP variables. The only exception is
``AGENT_HARNESS_SERVICE_API_KEY``, which is deliberately shared — the
same secret the monolith uses to authenticate internal callers.

Access control is **not configured here** — the viewer authenticates
users via Google OAuth, then delegates membership checks to AHS
(``POST /ahs/resolve_user``). If a user has a row in the ``users``
table, they get in. Adding / removing users is an AHS responsibility.

For local dev (run outside the monolith install), drop a ``.env`` in
the current working directory and pydantic-settings will pick it up.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is environment-driven; defaults favour local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Branding ---
    # Per-deployment display name, shared with the AHS monolith (no VIEWER_
    # prefix). Rendered in the top-left header as "<name> Artifacts" (e.g.
    # "VoltCouch Artifacts"). Defaults to match the Streamlit hub branding.
    DEPLOYMENT_NAME: str = "AgenticCouch"

    @field_validator("DEPLOYMENT_NAME")
    @classmethod
    def _normalize_deployment_name(cls, v: str) -> str:
        # A whitespace-only override would otherwise render as " Artifacts";
        # fall back to the default instead.
        return v.strip() or "AgenticCouch"

    # --- Upstream AHS ---
    # URL to reach AHS from the viewer. The monolith deployment has AHS on the
    # same box, so the default is the public hostname; override for dev/staging.
    VIEWER_AHS_BASE_URL: str = "https://ahs.example.com"
    # Shared with the monolith (same shared secret). No VIEWER_ prefix.
    AGENT_HARNESS_SERVICE_API_KEY: str = ""

    # --- Google OAuth (viewer-only) ---
    VIEWER_GOOGLE_CLIENT_ID: str = ""
    VIEWER_GOOGLE_CLIENT_SECRET: str = ""
    # The absolute URL Google should redirect to after login.
    VIEWER_OAUTH_REDIRECT_URL: str = "https://artifacts.example.com/auth/callback"

    # --- Server ---
    VIEWER_HOST: str = "127.0.0.1"
    VIEWER_PORT: int = 8095
    # Signed-cookie secret; rotate and redeploy to invalidate all sessions.
    VIEWER_SESSION_SECRET_KEY: str = "change-me-in-prod"
    # Session cookie lifetime in seconds (default: 14 days).
    VIEWER_SESSION_MAX_AGE: int = 14 * 24 * 3600
    # Set to false for local HTTP dev; keep true in prod.
    VIEWER_SESSION_COOKIE_SECURE: bool = True


settings = Settings()
