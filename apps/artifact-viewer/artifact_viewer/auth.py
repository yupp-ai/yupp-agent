"""Google OAuth login + signed-cookie session.

Flow:
    /auth/login     → redirect to Google with a CSRF state
    /auth/callback  → exchange code, check AHS ``resolve_user``, set session
    /auth/logout    → clear session
    (anything else) → ``require_login`` middleware redirects to /auth/login

Membership is not decided here. After Google confirms the email, we
ask AHS ``POST /ahs/resolve_user`` — if it returns a user_id, the user
is allowed; 404 means "not in the users table" and we deny access.
Managing who's in the users table is an AHS responsibility.

Session shape (signed, HttpOnly cookie via Starlette SessionMiddleware)::

    {"email": "alice@example.com", "name": "Alice", "picture": "https://..."}
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from artifact_viewer import ahs_client
from artifact_viewer.ahs_client import AHSError
from artifact_viewer.config import settings

_oauth: OAuth | None = None


def get_oauth() -> OAuth:
    """Lazy-construct the OAuth client so tests can patch settings first."""
    global _oauth
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            name="google",
            client_id=settings.VIEWER_GOOGLE_CLIENT_ID,
            client_secret=settings.VIEWER_GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def login(request: Request) -> Response:
    """Redirect to Google with a CSRF state cookie."""
    # Remember where the user was trying to go.
    next_url = request.query_params.get("next") or "/"
    request.session["next_url"] = next_url
    oauth = get_oauth()
    return await oauth.google.authorize_redirect(request, settings.VIEWER_OAUTH_REDIRECT_URL)  # type: ignore[no-any-return]


async def callback(request: Request) -> Response:
    """Exchange the OAuth code, check AHS membership, set session cookie."""
    oauth = get_oauth()
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/auth/error?reason=oauth_exchange_failed")

    userinfo: dict[str, Any] | None = token.get("userinfo")
    if userinfo is None:
        # Some deployments need an explicit userinfo fetch.
        userinfo = await oauth.google.userinfo(token=token)

    email = (userinfo or {}).get("email")
    if not email:
        request.session.clear()
        return RedirectResponse("/auth/error?reason=no_email")

    # Delegate membership to AHS: is there a row in the users table?
    email_q = quote(email, safe="")
    try:
        user_id = await ahs_client.resolve_user(email)
    except AHSError:
        # Upstream unreachable / unexpected error — deny access but tell
        # the operator what went wrong via the error page.
        return RedirectResponse(f"/auth/error?reason=ahs_unavailable&email={email_q}")

    if user_id is None:
        request.session.clear()
        return RedirectResponse(f"/auth/error?reason=not_a_user&email={email_q}")

    request.session["email"] = email
    request.session["user_id"] = user_id
    request.session["name"] = userinfo.get("name", "")
    request.session["picture"] = userinfo.get("picture", "")
    next_url = request.session.pop("next_url", "/")
    return RedirectResponse(next_url)


async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/auth/login")


async def error(request: Request) -> Response:
    from artifact_viewer.templating import templates

    reason = request.query_params.get("reason", "unknown")
    email = request.query_params.get("email", "")
    return templates.TemplateResponse(
        request,
        "error.html",
        {"reason": reason, "email": email},
        status_code=403,
    )


auth_routes = [
    Route("/auth/login", login, name="login"),
    Route("/auth/callback", callback, name="callback"),
    Route("/auth/logout", logout, name="logout"),
    Route("/auth/error", error, name="auth_error"),
]


# ---------------------------------------------------------------------------
# Login gate middleware
# ---------------------------------------------------------------------------


_PUBLIC_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/auth/error",
        "/healthz",
        "/favicon.ico",
    }
)


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


class RequireLoginMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated users to Google login, preserving ``next``.

    The actual membership check happens once in ``/auth/callback`` and
    the verdict is baked into the signed session cookie, so we only
    check here that ``email`` is present.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)  # type: ignore[no-any-return]
        email = request.session.get("email")
        if not email:
            # Preserve where they were trying to go.
            next_qp = request.url.path
            if request.url.query:
                next_qp += f"?{request.url.query}"
            from urllib.parse import quote

            return RedirectResponse(f"/auth/login?next={quote(next_qp, safe='')}")
        return await call_next(request)  # type: ignore[no-any-return]
