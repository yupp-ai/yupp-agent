"""Google OAuth login for ahstui.

Opens the user's browser for Google sign-in, caches tokens locally so
re-login is only needed when the refresh token expires (rare).

Configurable via env vars:
    AHS_TUI_ALLOWED_DOMAIN      Restrict logins to this email domain
                                 (empty = any domain accepted). Default: unset.
    AHS_TUI_OAUTH_CLIENT_ID     Google OAuth Desktop client ID.
    AHS_TUI_OAUTH_CLIENT_SECRET Client secret (not confidential for Desktop apps).
    AHS_TUI_OAUTH_PROJECT_ID    GCP project that owns the OAuth client.

The defaults point at the legacy yupp-llms Desktop OAuth client. When
that project gets deleted, create a fresh Desktop OAuth client in
whatever GCP project you use and set the three AHS_TUI_OAUTH_* env vars.
"""

from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Scopes: we only need the user's email
_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]

# Empty string = no domain restriction. Previously hard-coded to "yupp.ai".
_ALLOWED_DOMAIN = os.environ.get("AHS_TUI_ALLOWED_DOMAIN", "")

# Persist tokens under ~/.config/ahstui/
_CONFIG_DIR = Path.home() / ".config" / "ahstui"
_TOKEN_PATH = _CONFIG_DIR / "token.json"
_EMAIL_CACHE_PATH = _CONFIG_DIR / "email.txt"

# OAuth client config (Desktop/Installed app — secret is not confidential per Google docs).
# Overridable via env vars so users outside yupp-llms can register their own OAuth client.
_CLIENT_CONFIG: dict[str, Any] = {
    "installed": {
        "client_id": os.environ.get(
            "AHS_TUI_OAUTH_CLIENT_ID",
            "451082535721-rt8inmimemumdhfm528ert2b37v09s8t.apps.googleusercontent.com",
        ),
        "project_id": os.environ.get("AHS_TUI_OAUTH_PROJECT_ID", "yupp-llms"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": os.environ.get(
            "AHS_TUI_OAUTH_CLIENT_SECRET",
            "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",
        ),
        "redirect_uris": ["http://localhost"],
    }
}


def _email_allowed(email: str) -> bool:
    """Whether the email passes the configured domain allowlist."""
    if not _ALLOWED_DOMAIN:
        return True
    return email.endswith(f"@{_ALLOWED_DOMAIN}")


def _login_prompt_message() -> str:
    if _ALLOWED_DOMAIN:
        return f"Login required. You must sign in with a @{_ALLOWED_DOMAIN} Google account."
    return "Login required. Sign in with your Google account."


def _load_cached_credentials() -> Credentials | None:
    """Load credentials from the local cache file, if present."""
    if not _TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(_TOKEN_PATH.read_text())
        return Credentials.from_authorized_user_info(data, _SCOPES)  # type: ignore[no-any-return,no-untyped-call]
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _save_credentials(creds: Credentials, email: str | None = None) -> None:
    """Persist credentials (and optionally cached email) to local files."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())  # type: ignore[no-untyped-call]
    if email:
        fd = os.open(str(_EMAIL_CACHE_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(email)


def _load_cached_email() -> str | None:
    """Load cached email if present."""
    try:
        return _EMAIL_CACHE_PATH.read_text().strip() or None
    except (OSError, ValueError):
        return None


def _extract_email(creds: Credentials) -> str:
    """Extract email by calling the Google userinfo endpoint."""
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        info = json.loads(resp.read())
    email: str = info.get("email", "")
    if not email:
        raise RuntimeError("Could not extract email from Google credentials")
    return email.lower()


def login() -> str:
    """Authenticate the user via Google OAuth and return the email.

    Uses cached credentials when available. Opens a browser for the OAuth
    consent flow when no valid credentials exist.

    Raises SystemExit if the email is not in the allowed domain (when
    AHS_TUI_ALLOWED_DOMAIN is set; any email passes otherwise).
    """
    creds = _load_cached_credentials()

    if creds and creds.valid:
        # Fast path: use cached email to skip the network call on every startup.
        cached_email = _load_cached_email()
        if cached_email and _email_allowed(cached_email):
            return cached_email
        # No cached email — fetch it (first run after upgrade, or cache cleared).
        try:
            email = _extract_email(creds)
            if _email_allowed(email):
                _save_credentials(creds, email)
                return email
        except (urllib.error.URLError, json.JSONDecodeError, RuntimeError):
            pass  # Token might be invalid, try refresh

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())  # type: ignore[no-untyped-call]
            email = _extract_email(creds)
            _save_credentials(creds, email)
            if _email_allowed(email):
                return email
        except (RefreshError, urllib.error.URLError, json.JSONDecodeError, RuntimeError):
            pass  # Refresh failed — fall through to full login

    # Run the browser-based OAuth flow
    print()
    print(_login_prompt_message())
    input("Press Enter to open browser and sign in...")

    flow = InstalledAppFlow.from_client_config(_CLIENT_CONFIG, _SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message="Waiting for sign-in in browser...",
        success_message="Login successful! You can close this tab and return to the terminal.",
    )

    try:
        email = _extract_email(creds)
    except (urllib.error.URLError, RuntimeError) as e:
        # Credentials are saved — retry will work. Give a friendly message.
        _save_credentials(creds)
        print(f"ERROR: Sign-in succeeded but could not verify email: {e}", file=sys.stderr)
        print("Please run ahstui again — cached credentials will be used.", file=sys.stderr)
        sys.exit(1)

    if not _email_allowed(email):
        print(f"ERROR: Only @{_ALLOWED_DOMAIN} accounts are allowed (got {email})", file=sys.stderr)
        _TOKEN_PATH.unlink(missing_ok=True)
        _EMAIL_CACHE_PATH.unlink(missing_ok=True)
        sys.exit(1)

    _save_credentials(creds, email)
    return email


def logout() -> None:
    """Remove cached credentials and email."""
    _TOKEN_PATH.unlink(missing_ok=True)
    _EMAIL_CACHE_PATH.unlink(missing_ok=True)
    print("Logged out. Cached credentials removed.")
