"""DevToken authentication for MCP server.

This module handles authentication via developer tokens (yupp_dev_* format).
Used when MCP_SERVER_MODE is set to "DEV_TOKEN".
"""

import hashlib
import secrets
import string
from collections.abc import Awaitable, Callable
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
from ypl.db.soul_rbac import SoulPermission
from ypl.structured_logger import get_logger

logger = get_logger()

# Alphanumeric characters for token generation (base62)
TOKEN_ALPHABET = string.ascii_letters + string.digits  # a-z, A-Z, 0-9
TOKEN_LENGTH = 32  # Length of the random suffix

# Paths that skip authentication
PUBLIC_PATHS = {"/health", "/healthz"}


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
    if not await has_permission_cached(email, SoulPermission.USE_MCP):
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


def _is_service_token(db_token: MCPDevToken) -> bool:
    """Return True if the token belongs to a designated AHS service account."""
    allowed_emails = {e.strip().lower() for e in settings.AHS_SERVICE_TOKEN_EMAILS.split(",") if e.strip()}
    if not allowed_emails:
        # No allowlist configured — trust all tokens (backward compat for local dev)
        return True
    return db_token.email.lower() in allowed_emails


def create_request_context(
    db_token: MCPDevToken,
    request: Request,
) -> dict[str, Any]:
    """Create request context for MCP middleware."""
    is_service = _is_service_token(db_token)

    # Only trust X-User-ID from designated service tokens (e.g. AHS).
    # This prevents regular DevToken holders from impersonating other users.
    requesting_user_id: str | None = None
    raw_header = request.headers.get("x-user-id")
    if raw_header:
        if is_service:
            requesting_user_id = raw_header
        else:
            logger.warning(
                "Ignoring X-User-ID from non-service token",
                token_email=db_token.email.split("@")[0],
            )

    # Only trust AHS identity headers from designated service tokens.
    # X-AHS-Agent-Name is injected by the AHS runner into .mcp.json and is
    # tamper-proof within the sandbox — it cannot be changed by the agent.
    ahs_agent_name: str | None = None
    ahs_session_id: str | None = None
    if is_service:
        ahs_agent_name = request.headers.get("x-ahs-agent-name") or None
        ahs_session_id = request.headers.get("x-ahs-session-id") or None

    return {
        "token": db_token,
        "token_type": MCPTokenType.DEV_TOKEN,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "requesting_user_id": requesting_user_id,
        "ahs_agent_name": ahs_agent_name,
        "ahs_session_id": ahs_session_id,
    }


class DevTokenAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to authenticate requests using DevToken Bearer tokens.

    This middleware is used when MCP_SERVER_MODE is set to "DEV_TOKEN".
    It validates yupp_dev_* tokens against the database.
    """

    def __init__(self, app: Any, request_context_var: Any) -> None:
        """Initialize middleware with the request context variable."""
        super().__init__(app)
        self.request_context_var = request_context_var

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
        if not await has_permission_cached(db_token.email, SoulPermission.USE_MCP):
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

        # Set context variable for MCP middleware to access
        ctx = create_request_context(db_token, request)
        ctx_token = self.request_context_var.set(ctx)

        logger.debug("DevToken authenticated", engineer=email_local_part, path=request.url.path)

        try:
            return await call_next(request)
        finally:
            self.request_context_var.reset(ctx_token)
