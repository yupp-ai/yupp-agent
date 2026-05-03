"""DevToken authentication for the agcouch MCP server.

Authenticates ``yupp_dev_*`` Bearer tokens against the ``mcp_dev_token``
table. Used when ``MCP_SERVER_MODE=DEV_TOKEN`` and (during the phase-5
deprecation window) when the agcouch mount in mono mode receives a dev
token.

Identity emitted to tools is the typed
:class:`~ypl.mcp_common.auth_context.RequestContext` — same shape as the
OAuth path. The DevToken row reference is kept on a private ContextVar
(:data:`_devtoken_audit_var`) consulted by the audit middleware to
populate ``MCPAuditLog.mcp_dev_token_id`` until dev tokens are deleted in
phase 5b.
"""

from __future__ import annotations
import hashlib
import secrets
import string
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import bcrypt
from sqlmodel import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Import all models to ensure SQLAlchemy mappers are fully configured before any query.
# This avoids lazy initialization errors when models have cross-references.
import ypl.db.all_models  # noqa: F401
from ypl.backend.config import settings
from ypl.backend.db import get_async_session, retry_db
from ypl.backend.utils.soul_utils import has_permission_cached
from ypl.db.mcp import MCPDevToken, MCPTokenStatus, MCPTokenType
from ypl.db.rbac import Permission
from ypl.mcp_common.auth_context import RequestContext, lookup_user_id_by_email, request_context
from ypl.structured_logger import get_logger

logger = get_logger()

# Alphanumeric characters for token generation (base62)
TOKEN_ALPHABET = string.ascii_letters + string.digits  # a-z, A-Z, 0-9
TOKEN_LENGTH = 32  # Length of the random suffix

# Paths that skip authentication
PUBLIC_PATHS = {"/health", "/healthz"}


# ---------------------------------------------------------------------------
# Transitional DevToken audit metadata
# ---------------------------------------------------------------------------
#: Per-request DevToken DB row, used solely to populate
#: ``MCPAuditLog.mcp_dev_token_id`` during the phase-5 deprecation window.
#: ``None`` for OAuth and harness-MCP requests. Removed alongside the rest
#: of the DevToken machinery in phase 5b.
_devtoken_audit_var: ContextVar[MCPDevToken | None] = ContextVar("mcp_devtoken_audit", default=None)


def current_devtoken_for_audit() -> MCPDevToken | None:
    """Return the active DevToken DB row, if the request used one.

    Read by ``ToolCallLoggingMiddleware`` to populate
    ``MCPAuditLog.mcp_dev_token_id``. Tools must not depend on this — they
    consume identity from
    :func:`~ypl.mcp_common.auth_context.current_request_context` only.
    """
    return _devtoken_audit_var.get()


def generate_token() -> str:
    """Generate a secure random token.

    Format: yupp_dev_<32 alphanumeric characters>
    Example: yupp_dev_AbCdEfGh1234...

    Uses alphanumeric (base62) for better data density than hex.
    """
    random_suffix = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))
    return f"yupp_dev_{random_suffix}"


def get_token_lookup_key(token: str) -> str:
    """Extract the lookup key from a token (first 4 + last 4 chars of suffix).

    This allows for O(1) database lookup instead of scanning all tokens.
    The lookup key is not secret - it just narrows down the search.
    """
    # Remove the "yupp_dev_" prefix to get the suffix
    if token.startswith("yupp_dev_"):
        suffix = token[9:]  # len("yupp_dev_") == 9
    else:
        suffix = token

    if len(suffix) < 8:
        # Fallback for short tokens
        return suffix

    return suffix[:4] + suffix[-4:]


def _prehash_token(token: str) -> bytes:
    """Pre-hash token with SHA-256 to handle bcrypt's 72-byte limit."""
    return hashlib.sha256(token.encode()).digest()


def hash_token(token: str) -> str:
    """Hash a token using bcrypt (with SHA-256 pre-hash for length)."""
    prehashed = _prehash_token(token)
    hashed: bytes = bcrypt.hashpw(prehashed, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_token_hash(token: str, token_hash: str) -> bool:
    """Verify a token against its hash."""
    try:
        prehashed = _prehash_token(token)
        return bool(bcrypt.checkpw(prehashed, token_hash.encode()))
    except Exception:
        return False


@retry_db
async def validate_token(token: str) -> tuple[MCPDevToken | None, MCPTokenStatus | None]:
    """Validate a token and return the associated MCPDevToken and its status.

    Uses O(1) lookup by token_lookup_key, then verifies the hash.

    Returns:
        tuple of (MCPDevToken, MCPTokenStatus) if token is valid and ACTIVE
        tuple of (None, MCPTokenStatus) if token exists but is not ACTIVE (REVOKED or EXPIRED)
        tuple of (None, None) if token doesn't exist or hash doesn't match
    """
    lookup_key = get_token_lookup_key(token)

    async with get_async_session() as session:
        # Direct lookup by key - O(1) instead of O(n)
        statement = select(MCPDevToken).where(
            MCPDevToken.token_lookup_key == lookup_key,
        )
        result = await session.exec(statement)
        candidates = result.all()

        # Verify hash for matching candidates (usually just 1)
        for db_token in candidates:
            if verify_token_hash(token, db_token.token_hash):
                # Check if already revoked
                if db_token.status == MCPTokenStatus.REVOKED:
                    return None, MCPTokenStatus.REVOKED

                # Check expiration and update status if expired
                if db_token.expires_at and db_token.expires_at < datetime.now(UTC):
                    if db_token.status != MCPTokenStatus.EXPIRED:
                        db_token.status = MCPTokenStatus.EXPIRED
                        session.add(db_token)
                        await session.commit()
                    return None, MCPTokenStatus.EXPIRED

                # Token is valid and active - update last_used_at
                db_token.last_used_at = datetime.now(UTC)
                session.add(db_token)
                await session.commit()
                await session.refresh(db_token)

                return db_token, MCPTokenStatus.ACTIVE

        return None, None


@retry_db
async def create_token(
    email: str,
    description: str | None,
    expires_at: datetime | None = None,
) -> tuple[str, MCPDevToken]:
    """Create a new developer token.

    Returns:
        tuple of (plaintext_token, MCPDevToken record)

    Raises:
        ValueError: If email domain is not allowed
        PermissionError: If user does not have USE_MCP permission
    """
    # Validate email domain
    email_domain = email.split("@")[-1]
    allowed_domains = settings.ALLOWED_MCP_EMAIL_DOMAINS
    if email_domain not in allowed_domains:
        raise ValueError(f"Email domain {email_domain} not in allowed domains: {allowed_domains}")

    # Check if user has USE_MCP permission
    if not await has_permission_cached(email, Permission.USE_MCP):
        logger.warning("Token creation denied - user lacks USE_MCP permission", email_local_part=email.split("@")[0])
        raise PermissionError(
            f"User {email} does not have USE_MCP permission. Please contact your TLM to add the permission."
        )

    # Generate token
    plaintext_token = generate_token()
    token_hash = hash_token(plaintext_token)
    lookup_key = get_token_lookup_key(plaintext_token)

    async with get_async_session() as session:
        # Create DB record
        db_token = MCPDevToken(
            token_lookup_key=lookup_key,
            token_hash=token_hash,
            email=email,
            description=description,
            expires_at=expires_at,
            status=MCPTokenStatus.ACTIVE,
        )

        session.add(db_token)
        await session.commit()
        await session.refresh(db_token)

        return plaintext_token, db_token


@retry_db
async def revoke_token(
    token_id: str,
    revoked_by: str,
    reason: str,
) -> MCPDevToken | None:
    """Revoke a token."""
    async with get_async_session() as session:
        statement = select(MCPDevToken).where(MCPDevToken.mcp_dev_token_id == token_id)
        result = await session.exec(statement)
        db_token = result.first()

        if not db_token:
            return None

        db_token.status = MCPTokenStatus.REVOKED
        db_token.revoked_at = datetime.now(UTC)
        db_token.revoked_by = revoked_by
        db_token.revoked_reason = reason

        session.add(db_token)
        await session.commit()
        await session.refresh(db_token)

        return db_token


async def _can_assert_user_identity(db_token: MCPDevToken) -> bool:
    """Return True if the token owner may assert another user's identity.

    The token owner is trusted to set the ``X-User-ID`` header — and the
    ``X-AHS-Agent-Name`` / ``X-AHS-Session-ID`` AHS identity headers — iff
    they hold ``MANAGE_AGENT_SESSIONS``. This is the permission granted to
    administrators and to the AHS service principal; holders can already
    manage any user's agent sessions, so also letting them stamp
    cross-user attribution on MCP requests adds no new privilege.

    Regular DevToken holders fail this check, which means the middleware
    drops their ``X-User-ID`` header and they act as themselves — preventing
    token holders from impersonating other users to mutate data.
    """
    return await has_permission_cached(db_token.email, Permission.MANAGE_AGENT_SESSIONS)


async def build_request_context(
    db_token: MCPDevToken,
    request: Request,
) -> RequestContext:
    """Build the typed :class:`RequestContext` for a DevToken request.

    Resolves the token's email to a platform ``user_id`` once, applies
    the ``MANAGE_AGENT_SESSIONS`` gate to optional impersonation headers,
    and returns the immutable context the audit middleware and tools will
    consume.

    The dev-token path emits ``auth_kind="dev_token"`` so future tool
    code can distinguish a verified-Google-identity caller from a
    token-holder caller. The dev-token DB row is surfaced separately on
    :data:`_devtoken_audit_var` so the audit log can keep stamping
    ``mcp_dev_token_id`` until phase 5b removes the column.
    """
    can_impersonate = await _can_assert_user_identity(db_token)

    # Default identity: the token owner. We resolve the email → user_id
    # once here so tools never have to re-resolve. ``None`` means the
    # token holder isn't bound to an active platform user — the audit
    # trail still records the email for review.
    #
    # Mirror the OAuth path's defensive ``try/except``: a transient DB
    # blip, a case-insensitive email collision, or any other failure
    # downgrades to ``own_user_id=None`` and surfaces a clean
    # ``PermissionError`` via ``require_caller_user_id()`` rather than
    # a 500 from the middleware. Non-impersonating callers without an
    # active platform user end up with ``requesting_user_id=None`` and
    # tools refuse politely.
    try:
        own_user_id: str | None = await lookup_user_id_by_email(db_token.email)
    except Exception:
        logger.exception(
            "DevToken user_id lookup failed; falling back to audit-only email",
            email_local_part=db_token.email.split("@")[0],
        )
        own_user_id = None

    # Optional impersonation: privileged callers (AHS service principal,
    # admins) can override the user_id and AHS identity headers. Regular
    # token holders are silently downgraded to acting as themselves.
    requesting_user_id: str | None = own_user_id
    raw_user_header = request.headers.get("x-user-id")
    if raw_user_header:
        if can_impersonate:
            requesting_user_id = raw_user_header
        else:
            logger.warning(
                "Ignoring X-User-ID from non-privileged token",
                token_email=db_token.email.split("@")[0],
            )

    # AHS identity headers (X-AHS-Agent-Name / X-AHS-Session-ID) follow
    # the same gate. Tamper-proof inside the AHS sandbox; trusted only
    # when MANAGE_AGENT_SESSIONS is held.
    ahs_agent_name: str | None = None
    ahs_session_id: str | None = None
    if can_impersonate:
        ahs_agent_name = request.headers.get("x-ahs-agent-name") or None
        ahs_session_id = request.headers.get("x-ahs-session-id") or None

    return RequestContext(
        auth_kind="dev_token",
        requesting_user_id=requesting_user_id,
        # Credential holder is always the token owner — even when
        # impersonating. Tools that gate on USE_MCP / similar
        # credential-level permissions check against this so an admin
        # acting on behalf of a regular user isn't blocked by the
        # regular user's missing permissions.
        principal_user_id=own_user_id,
        ahs_session_id=ahs_session_id,
        ahs_agent_name=ahs_agent_name,
        audit_email=db_token.email,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


# Backward-compatible alias for callers that still expect the old dict
# return type. The dict shape is preserved enough for ``unified_mcp.py``
# and the tests during the deprecation window; it will be deleted with
# the rest of the DevToken machinery in phase 5b.
async def create_request_context(db_token: MCPDevToken, request: Request) -> dict[str, Any]:  # pragma: no cover - shim
    """Deprecated: returns the legacy dict shape, retained for callers.

    New code should call :func:`build_request_context` and use the typed
    :class:`RequestContext` directly.
    """
    ctx = await build_request_context(db_token, request)
    return {
        "token": db_token,
        "token_type": MCPTokenType.DEV_TOKEN,
        "ip_address": ctx.ip_address,
        "user_agent": ctx.user_agent,
        "requesting_user_id": ctx.requesting_user_id,
        "ahs_agent_name": ctx.ahs_agent_name,
        "ahs_session_id": ctx.ahs_session_id,
    }


class DevTokenAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to authenticate requests using DevToken Bearer tokens.

    This middleware is used when MCP_SERVER_MODE is set to "DEV_TOKEN".
    It validates yupp_dev_* tokens against the database.
    """

    def __init__(self, app: Any, request_context_var: Any | None = None) -> None:
        """Initialize middleware.

        Args:
            app: ASGI app.
            request_context_var: Deprecated. Retained for binary
                compatibility with callers that pass the legacy dict
                ``ContextVar`` — ignored. The typed
                :data:`ypl.mcp_common.auth_context.request_context` is
                always used.
        """
        super().__init__(app)
        # Keep the attribute around so tests inspecting it don't break.
        self.request_context_var = request_context_var or request_context

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Validate Bearer token before processing request."""
        path = request.url.path

        # Skip auth for health check endpoints
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Extract Authorization header
        auth_header = request.headers.get("authorization", "")

        if not auth_header:
            return JSONResponse(
                {"error": "Missing Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Parse Bearer token
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                {"error": "Invalid Authorization header format. Expected: Bearer <token>"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = parts[1]

        # Validate token format
        if not token.startswith("yupp_dev_"):
            return JSONResponse(
                {"error": "Invalid token format. Expected: yupp_dev_* token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Validate token against database
        db_token, token_status = await validate_token(token)

        if not db_token:
            if token_status == MCPTokenStatus.REVOKED:
                detail = "Token has been revoked"
            elif token_status == MCPTokenStatus.EXPIRED:
                detail = "Token has expired"
            else:
                detail = "Invalid token"

            return JSONResponse(
                {"error": detail},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Extract email local part for logging (avoids redaction of full email)
        email_local_part = db_token.email.split("@")[0]

        # Check if user has USE_MCP permission
        if not await has_permission_cached(db_token.email, Permission.USE_MCP):
            logger.warning(
                "DevToken authentication rejected - user lacks USE_MCP permission",
                email_local_part=email_local_part,
            )
            return JSONResponse(
                {"error": "User does not have permission to use MCP. Please contact your TLM to add the permission."},
                status_code=403,
            )

        # Store token info in request state for downstream use
        request.state.mcp_token = db_token
        request.state.engineer_email = db_token.email
        request.state.token_type = MCPTokenType.DEV_TOKEN

        # Publish the typed RequestContext + the per-request DevToken
        # row reference (for audit logging only). Both are scoped to the
        # current asyncio task so resets at the end of the request take
        # care of cleanup.
        ctx = await build_request_context(db_token, request)
        ctx_token = request_context.set(ctx)
        audit_token = _devtoken_audit_var.set(db_token)

        logger.debug("DevToken authenticated", engineer=email_local_part, path=request.url.path)

        try:
            return await call_next(request)
        finally:
            request_context.reset(ctx_token)
            _devtoken_audit_var.reset(audit_token)
