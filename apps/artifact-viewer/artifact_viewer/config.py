"""Environment-driven config for the artifact viewer."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration is environment-driven; defaults favour local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Upstream AHS ---
    AHS_BASE_URL: str = "https://ahs.agcouch.com"
    AHS_API_KEY: str = ""

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # The absolute URL Google should redirect to after login.
    OAUTH_REDIRECT_URL: str = "https://artifacts.agcouch.com/auth/callback"

    # --- Access control ---
    # Comma-separated list of allowed email domains (without @).
    # Example: "agcouch.com,example.com". Empty string = refuse everyone.
    ALLOWED_EMAIL_DOMAINS: str = "agcouch.com"
    # Optional comma-separated per-email allowlist (takes precedence over domains).
    ALLOWED_EMAILS: str = ""

    # --- Server ---
    VIEWER_HOST: str = "127.0.0.1"
    VIEWER_PORT: int = 8095
    # Signed-cookie secret; rotate and redeploy to invalidate all sessions.
    SESSION_SECRET_KEY: str = "change-me-in-prod"
    # Session cookie lifetime in seconds (default: 14 days).
    SESSION_MAX_AGE: int = 14 * 24 * 3600
    # Set to false for local HTTP dev; keep true in prod.
    SESSION_COOKIE_SECURE: bool = True

    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.ALLOWED_EMAIL_DOMAINS.split(",") if d.strip()}

    def allowed_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.ALLOWED_EMAILS.split(",") if e.strip()}

    def is_email_allowed(self, email: str | None) -> bool:
        if not email:
            return False
        email_lower = email.lower()
        if email_lower in self.allowed_emails():
            return True
        domain = email_lower.split("@", 1)[-1] if "@" in email_lower else ""
        return domain in self.allowed_domains()


settings = Settings()
