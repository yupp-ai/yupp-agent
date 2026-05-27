"""Google OAuth + session middleware for admin.voltcouch.com.

Pattern matches Streamlit / Artifact Viewer in this repo: classic OAuth
authorization-code flow, callback at `/auth/callback`, Google IdP, email
allowlist via `ADMIN_ALLOWED_EMAILS`. Reuses the same OAuth client.

Disabled by default (`ADMIN_OAUTH_ENABLED=false`) so local development is
not gated. When enabled, the middleware redirects un-authenticated HTML
requests to `/auth/login` and returns 401 JSON for API requests.
"""

from __future__ import annotations
import html as _html
import logging
import os
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


# Fail-secure: default to enabled so an unset env var never leaves the control
# plane open.  Local development that genuinely needs to skip OAuth must
# explicitly set ADMIN_OAUTH_ENABLED=false in .env — the noisy startup warning
# below makes the choice visible in logs.
OAUTH_ENABLED = _truthy(os.environ.get("ADMIN_OAUTH_ENABLED", "true"))
if not OAUTH_ENABLED:
    logger.warning(
        "ADMIN_OAUTH_ENABLED=false — admin control plane is UNAUTHENTICATED. "
        "Restart / deploy / rollback endpoints are open to any caller. "
        "Set ADMIN_OAUTH_ENABLED=true for any non-local deployment."
    )
# Reuse the OAuth client already configured for Streamlit + Artifact Viewer.
# Falls back to GOOGLE_OAUTH_* for installations that follow newer install.sh.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_AUTH_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_AUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_ALLOWED_EMAILS", "").split(",") if e.strip()}
PROD_REDIRECT_URI = os.environ.get(
    "ADMIN_OAUTH_REDIRECT_URI",
    "https://admin.voltcouch.com/auth/callback",
)
LOCAL_REDIRECT_URI = "http://127.0.0.1:8099/auth/callback"

router = APIRouter(prefix="/auth", tags=["auth"])


def _public_path(path: str) -> bool:
    """Paths that bypass the auth gate."""
    if path == "/healthz":
        return True
    return path.startswith("/auth/")


def _pick_redirect(request: Request) -> str:
    host = request.headers.get("host", "").lower()
    if "127.0.0.1" in host or "localhost" in host:
        return LOCAL_REDIRECT_URI
    return PROD_REDIRECT_URI


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every protected request on an allowed email in the session."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not OAUTH_ENABLED or _public_path(request.url.path):
            return await call_next(request)
        email = (request.session.get("email") or "").lower()
        if email and email in ALLOWED_EMAILS:
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        nxt = urllib.parse.quote(request.url.path)
        return RedirectResponse(url=f"/auth/login?next={nxt}")


@router.get("/login")
async def login(request: Request, next: str = "/") -> Any:
    if not OAUTH_ENABLED:
        return HTMLResponse(
            "<h2>OAuth disabled</h2><p>Set <code>ADMIN_OAUTH_ENABLED=true</code> "
            "+ <code>ADMIN_ALLOWED_EMAILS=you@example.com</code> in .env to require login.</p>"
            "<p><a href='/'>back</a></p>",
            status_code=200,
        )
    if not GOOGLE_CLIENT_ID:
        return HTMLResponse(
            "<h2>OAuth misconfigured</h2><p>Set <code>GOOGLE_OAUTH_CLIENT_ID</code> "
            "and <code>GOOGLE_OAUTH_CLIENT_SECRET</code> in .env.</p>",
            status_code=500,
        )
    # Reject absolute URLs in `next` to prevent open redirect: an attacker who
    # sends a victim to /auth/login?next=https://evil.com/ would get them
    # redirected there after a legitimate Google sign-in.  Only relative paths
    # (no scheme, no netloc) are safe to store and replay.
    _parsed_next = urllib.parse.urlparse(next)
    if _parsed_next.scheme or _parsed_next.netloc:
        logger.warning("login: rejecting non-relative next= param: %r", next)
        next = "/"
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = next
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _pick_redirect(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Any:
    if error:
        return HTMLResponse(f"<h2>OAuth error</h2><pre>{_html.escape(error)}</pre>", 400)
    if not OAUTH_ENABLED:
        raise HTTPException(404)
    if not state or state != request.session.get("oauth_state"):
        raise HTTPException(400, "state mismatch")
    redirect = _pick_redirect(request)
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(400, f"token exchange failed: {token_resp.text}")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(400, "no access_token in response")
        info_resp = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_resp.status_code != 200:
            raise HTTPException(400, f"userinfo failed: {info_resp.text}")
        info = info_resp.json()
    email = (info.get("email") or "").lower()
    if not info.get("email_verified") or email not in ALLOWED_EMAILS:
        return HTMLResponse(
            f"<h2>Access denied</h2>"
            f"<p><code>{_html.escape(email)}</code> is not on the admin allowlist.</p>"
            f"<p><a href='/auth/logout'>sign out and try a different account</a></p>",
            status_code=403,
        )
    request.session["email"] = email
    request.session["name"] = info.get("name", "")
    request.session["picture"] = info.get("picture", "")
    nxt = request.session.pop("oauth_next", "/")
    request.session.pop("oauth_state", None)
    logger.info("admin sign-in: %s", email)
    return RedirectResponse(url=nxt or "/", status_code=302)


@router.get("/logout")
async def logout(request: Request) -> Any:
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    return {
        "enabled": OAUTH_ENABLED,
        "email": request.session.get("email"),
        "name": request.session.get("name"),
        "picture": request.session.get("picture"),
        "allowed_count": len(ALLOWED_EMAILS),
    }


def current_actor(request: Request) -> str:
    return request.session.get("email") or "anonymous"
