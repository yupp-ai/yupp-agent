"""OAuth authentication for the agcouch MCP server.

Validates Google OAuth bearer tokens, restricts access to the
``ALLOWED_MCP_EMAIL_DOMAINS`` allowlist, gates on the ``USE_MCP``
permission, and resolves the verified email to a platform ``user_id``
**once** at ``verify_token`` time. Tools downstream consume only the
typed :class:`~ypl.mcp_common.auth_context.RequestContext` populated
here — they never see the email.

Used when ``MCP_SERVER_MODE`` is ``OAUTH``.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

# Import all models to ensure SQLAlchemy mappers are fully configured before any query.
# This avoids lazy initialization errors when models have cross-references (e.g. Memory -> ChatMessage).
import ypl.db.all_models  # noqa: F401
from ypl.backend.config import settings
from ypl.backend.utils.soul_utils import has_permission_cached
from ypl.db.rbac import Permission
from ypl.mcp_common.auth_context import RequestContext, lookup_user_id_by_email, request_context
from ypl.structured_logger import get_logger

logger = get_logger()


def is_allowed_email_domain(email: str) -> bool:
    """Check if email domain is in the allowed list for MCP access."""
    if not email:
        return False
    email_domain = email.split("@")[-1].lower()
    allowed_domains = [d.lower() for d in settings.ALLOWED_MCP_EMAIL_DOMAINS]
    return email_domain in allowed_domains


class AllowedDomainsGoogleProvider(GoogleProvider):
    """GoogleProvider that restricts access to allowed email domains.

    Validates user email domain during token verification to ensure
    only users from ALLOWED_MCP_EMAIL_DOMAINS can access the MCP server.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify token, gate on domain + USE_MCP, and publish a typed context.

        Calls parent ``GoogleProvider.verify_token()`` then:

        1. Rejects when the email domain isn't in
           ``ALLOWED_MCP_EMAIL_DOMAINS``.
        2. Rejects when the user lacks the ``USE_MCP`` permission.
        3. Resolves the email to a platform ``user_id`` (best-effort —
           may be ``None`` for users not yet provisioned in
           ``users``; the audit trail still keeps the email).
        4. Publishes a :class:`~ypl.mcp_common.auth_context.RequestContext`
           on the ``request_context`` ContextVar so tools and the audit
           middleware can read identity off a single typed object.
        """
        access_token = await super().verify_token(token)

        if access_token is None:
            return None

        # Extract email from token claims
        email = access_token.claims.get("email") if access_token.claims else None

        if not email:
            logger.warning("OAuth token has no email claim")
            return None

        # Extract email local part for logging (avoids redaction of full email)
        email_local_part = email.split("@")[0]

        # Validate email domain
        if not is_allowed_email_domain(email):
            logger.warning(
                "OAuth authentication rejected - email domain not allowed",
                email_local_part=email_local_part,
                allowed_domains=settings.ALLOWED_MCP_EMAIL_DOMAINS,
            )
            return None

        # Check if user has USE_MCP permission
        if not await has_permission_cached(email, Permission.USE_MCP):
            logger.warning(
                "OAuth authentication rejected - user lacks USE_MCP permission. "
                "Please contact your TLM to add the permission.",
                email_local_part=email_local_part,
            )
            return None

        # Resolve email → user_id once. None for unprovisioned users — the
        # audit trail still records the email; tools that require an
        # attributable user will get a clean PermissionError via
        # require_caller_user_id() instead of an "unknown email" string.
        try:
            requesting_user_id = await lookup_user_id_by_email(email)
        except Exception:
            logger.exception(
                "OAuth user_id lookup failed; falling back to audit-only email",
                email_local_part=email_local_part,
            )
            requesting_user_id = None

        logger.info(
            "OAuth authentication successful",
            email_local_part=email_local_part,
            user_id_resolved=requesting_user_id is not None,
        )

        # The OAuth client identifier (the OAuth-2.0 ``client_id`` claim
        # the access token was issued to). Audit-only — surfaces in
        # ``MCPAuditLog.callback_url`` so security review can attribute
        # calls to the OAuth client that obtained the token.
        callback_url = getattr(access_token, "client_id", None)

        # Populate the typed request context so ToolCallLoggingMiddleware
        # and tools see one shape regardless of which mount the request
        # arrived on. ContextVar is scoped to the current asyncio task
        # (one per stateless HTTP request), so no cross-request leakage.
        request_context.set(
            RequestContext(
                auth_kind="oauth_user",
                requesting_user_id=requesting_user_id,
                # OAuth has no impersonation: principal == requesting_user.
                principal_user_id=requesting_user_id,
                audit_email=email,
                callback_url=callback_url,
                # IP / UA are not available from the OAuth provider layer.
                # The harness path enriches them at the ASGI middleware.
                ip_address=None,
                user_agent=None,
            )
        )

        return access_token


def create_oauth_provider() -> AllowedDomainsGoogleProvider:
    """Create AllowedDomainsGoogleProvider with RedisStorage.

    This function creates the OAuth provider for use when MCP_SERVER_MODE is "OAUTH".
    It requires all OAuth settings to be configured.

    Raises:
        ValueError: If required OAuth settings are missing.
    """
    if not settings.MCP_OAUTH_GOOGLE_CLIENT_ID or not settings.MCP_OAUTH_GOOGLE_CLIENT_SECRET:
        raise ValueError("MCP OAuth not configured - missing Google OAuth credentials")

    if not settings.MCP_OAUTH_JWT_SIGNING_KEY:
        raise ValueError("MCP OAuth JWT signing key not configured")

    if not settings.MCP_OAUTH_STORAGE_ENCRYPTION_KEY:
        raise ValueError("MCP OAuth storage encryption key not configured")

    logger.info("Configuring MCP OAuth with Redis storage")

    # Create Redis store with prefix and encryption for OAuth token storage
    # Using prefix to namespace MCP OAuth keys separately from other app data
    redis_store = RedisStore(url=settings.REDIS_URL)

    # Add prefix to isolate MCP OAuth keys (e.g., "mcp-oauth:tokens", "mcp-oauth:clients")
    prefixed_store = PrefixCollectionsWrapper(
        key_value=redis_store,
        prefix="mcp-oauth",
    )

    # Wrap with encryption for secure token storage
    encrypted_storage = FernetEncryptionWrapper(
        key_value=prefixed_store,
        fernet=Fernet(settings.MCP_OAUTH_STORAGE_ENCRYPTION_KEY.encode()),
    )

    return AllowedDomainsGoogleProvider(
        client_id=settings.MCP_OAUTH_GOOGLE_CLIENT_ID,
        client_secret=settings.MCP_OAUTH_GOOGLE_CLIENT_SECRET,
        base_url=settings.MCP_SERVER_BASE_URL,
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        jwt_signing_key=settings.MCP_OAUTH_JWT_SIGNING_KEY,
        client_storage=encrypted_storage,
    )
