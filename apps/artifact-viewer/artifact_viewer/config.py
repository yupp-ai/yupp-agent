"""Environment-driven config for the artifact viewer.

The viewer reads the **same .env file as the AHS monolith** (typically
``/opt/yupp-agent/.env`` in production). Viewer-specific settings are
namespaced with a ``VIEWER_`` prefix so they don't collide with AHS /
SAG / MCP variables. The only exception is
``AGENT_HARNESS_SERVICE_API_KEY``, which is deliberately shared — the
same secret the monolith uses to authenticate internal callers.

For local dev (run outside the monolith install), drop a ``.env`` in
the current working directory and pydantic-settings will pick it up.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is environment-driven; defaults favour local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Upstream AHS ---
    # URL to reach AHS from the viewer. The monolith deployment has AHS on the
    # same box, so the default is the public hostname; override for dev/staging.
    VIEWER_AHS_BASE_URL: str = "https://ahs.agcouch.com"
    # Shared with the monolith (same shared secret). No VIEWER_ prefix.
    AGENT_HARNESS_SERVICE_API_KEY: str = ""

    # --- Google OAuth (viewer-only) ---
    VIEWER_GOOGLE_CLIENT_ID: str = ""
    VIEWER_GOOGLE_CLIENT_SECRET: str = ""
    # The absolute URL Google should redirect to after login.
    VIEWER_OAUTH_REDIRECT_URL: str = "https://artifacts.agcouch.com/auth/callback"

    # --- Access control (viewer-only) ---
    # Comma-separated list of allowed email domains (without @).
    # Example: "agcouch.com,example.com". Empty string = refuse everyone.
    VIEWER_ALLOWED_EMAIL_DOMAINS: str = "agcouch.com"
    # Optional comma-separated per-email allowlist (takes precedence over domains).
    VIEWER_ALLOWED_EMAILS: str = ""

    # --- Server ---
    VIEWER_HOST: str = "127.0.0.1"
    VIEWER_PORT: int = 8095
    # Signed-cookie secret; rotate and redeploy to invalidate all sessions.
    VIEWER_SESSION_SECRET_KEY: str = "change-me-in-prod"
    # Session cookie lifetime in seconds (default: 14 days).
    VIEWER_SESSION_MAX_AGE: int = 14 * 24 * 3600
    # Set to false for local HTTP dev; keep true in prod.
    VIEWER_SESSION_COOKIE_SECURE: bool = True

    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.VIEWER_ALLOWED_EMAIL_DOMAINS.split(",") if d.strip()}

    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.VIEWER_ALLOWED_EMAILS.split(",") if e.strip()}

    def is_email_allowed(self, email: str | None) -> bool:
        if not email:
            return False
        email_lower = email.lower()
        if email_lower in self.allowed_emails():
            return True
        domain = email_lower.split("@", 1)[-1] if "@" in email_lower else ""
        return domain in self.allowed_domains()


settings = Settings()
