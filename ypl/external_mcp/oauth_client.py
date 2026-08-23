"""Generic OAuth 2.0 authorization-code client for external MCPs.

The platform doesn't talk to one provider — admins can register any
OAuth-capable MCP (Gmail, Drive, Calendar, Linear, Notion, …).  Each
server stores its own ``oauth_config`` (authorize_url, token_url, scopes,
client_id) and ``oauth_client_secret_enc``; this module turns those into:

- :func:`build_authorize_url`  — kicks the user's browser to the provider.
- :func:`exchange_code`        — turns ``code`` into tokens.
- :func:`refresh_access_token` — refresh-token flow when a token expires.
- :func:`sign_state` / :func:`verify_state` — HS256 ``state`` JWT so the
  callback handler can trust ``(user_id, server_slug)`` without a side
  channel.  Reuses ``MCP_OAUTH_JWT_SIGNING_KEY`` (already used by the
  in-tree harness MCP OAuth provider).

Most providers conform; the ``extra_authorize_params`` slot on
``oauth_config`` is the escape hatch for the ones that don't (e.g. Google's
``access_type=offline`` + ``prompt=consent`` to force refresh-token issuance,
Notion's ``owner=user``).
"""

from __future__ import annotations
import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

_STATE_ALG = "HS256"
_STATE_LEEWAY_S = 30
_STATE_DEFAULT_TTL_S = 600  # 10 minutes — plenty for browser hop


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE (RFC 7636, S256).

    Some providers *require* PKCE for the authorization-code grant — arti
    rejects ``/oauth/authorize`` without a ``code_challenge`` — and it is a
    strict security improvement for the ones that merely tolerate it (Google).
    The verifier is a high-entropy secret kept by us until the token exchange;
    the challenge is its URL-safe base64 SHA-256, sent to the provider.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _signing_key() -> str:
    key = settings.MCP_OAUTH_JWT_SIGNING_KEY
    if not key:
        raise ValueError("MCP_OAUTH_JWT_SIGNING_KEY is not configured (used to sign OAuth state).")
    return key


def sign_state(
    user_id: str,
    server_slug: str,
    return_to: str = "",
    ttl_s: int = _STATE_DEFAULT_TTL_S,
    code_verifier: str = "",
) -> str:
    """Encode an opaque ``state`` token the provider will echo back.

    The token binds ``(user_id, server_slug)`` so the callback can resolve
    who's connecting which MCP without trusting URL params.  ``return_to``
    is the Lit page we redirect the browser to after the callback succeeds
    (typically ``/my_mcps``); empty string means "use the default".
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "slug": server_slug,
        "ret": return_to,
        # PKCE verifier round-trips inside the signed (tamper-proof) state so
        # the callback can present it at token exchange. Confidentiality rests
        # on the confidential client_secret arti also verifies; the signature
        # alone prevents an attacker substituting their own challenge/verifier.
        "cv": code_verifier,
        "iat": now,
        "exp": now + ttl_s,
        "nonce": jwt.utils.base64url_encode(time.time_ns().to_bytes(16, "big")).decode(),
    }
    return jwt.encode(payload, _signing_key(), algorithm=_STATE_ALG)


def verify_state(state: str) -> dict[str, Any]:
    """Decode + validate a ``state`` token.  Raises :class:`ValueError` if
    the token is tampered, expired, or wrong shape."""
    try:
        payload = jwt.decode(
            state,
            _signing_key(),
            algorithms=[_STATE_ALG],
            leeway=_STATE_LEEWAY_S,
            options={"require": ["sub", "slug", "iat", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise ValueError(f"Invalid OAuth state: {e}") from e
    return payload


def build_authorize_url(
    *,
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    extra_params: dict[str, str] | None = None,
    code_challenge: str = "",
) -> str:
    """Construct the provider's authorize URL.  Caller hands the result to
    the user's browser via 302."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if extra_params:
        # Provider-specific knobs win; e.g. Google needs access_type=offline
        # to issue a refresh token at all.
        params.update(extra_params)
    sep = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{sep}{urlencode(params)}"


async def exchange_code(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str = "",
) -> dict[str, Any]:
    """Trade an authorization ``code`` for an access (+ refresh) token.

    Returns the parsed JSON body the provider sent back.  Standard fields:
    ``access_token``, ``token_type``, ``expires_in`` (seconds),
    ``refresh_token`` (sometimes), ``scope``.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                **({"code_verifier": code_verifier} if code_verifier else {}),
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:500]}")
    payload: dict[str, Any] = resp.json()
    return payload


async def refresh_access_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Refresh an access token.  Returns the new JSON payload.

    Providers may or may not echo a fresh ``refresh_token``; if absent,
    the caller should keep the existing one.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text[:500]}")
    payload: dict[str, Any] = resp.json()
    return payload


def expiry_from_expires_in(expires_in: int | float | None) -> datetime | None:
    """Convert the provider's ``expires_in`` (seconds-from-now) into an
    absolute ``datetime``.  Returns ``None`` when the field is absent
    (some providers omit it — token won't auto-refresh; caller can flag)."""
    if expires_in is None:
        return None
    return datetime.now(UTC) + timedelta(seconds=int(expires_in))
