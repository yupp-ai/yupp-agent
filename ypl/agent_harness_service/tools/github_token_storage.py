"""Encrypted Redis storage for GitHub tokens with refresh token support.

This module provides secure storage for GitHub OAuth tokens in Redis,
encrypted using Fernet (symmetric encryption). Tokens are keyed by Yupp user_id
to enable cross-session reuse.

Similar to the MCP server's OAuth token storage pattern in ypl/mcp_server/auth_oauth.py.

When the encryption key is not configured, all operations degrade gracefully:
- get_github_token_data returns None
- store_github_token_data returns False
- remove_github_token returns False
- refresh_github_token returns None
The service won't crash, but create_pr will fail with an auth-required error
(bot-attributed PRs are not allowed).

Token Refresh Flow:
- GitHub Apps with "User-to-server token expiration" enabled return refresh tokens
- Access tokens expire after 8 hours, refresh tokens after 6 months
- When access token is expired, use refresh_github_token() to get a new one
- If refresh fails (token revoked or expired), user must re-authorize
"""

import asyncio
import time
from enum import Enum
from typing import Any, Self

import httpx
from cryptography.fernet import Fernet
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
from pydantic import BaseModel

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

# Singleton storage instances and initialization lock
_redis_store: RedisStore | None = None
_encrypted_store: FernetEncryptionWrapper | None = None
_store_lock = asyncio.Lock()
_initialized = False

# Collection name for GitHub tokens within the prefixed store
_GITHUB_TOKENS_COLLECTION = "tokens"

# Buffer time before expiration to trigger refresh (5 minutes)
_REFRESH_BUFFER_SECONDS = 300

# GitHub OAuth error codes that indicate the refresh token is invalid and should be evicted
# Note: incorrect_client_credentials is a config error (wrong client_secret), not a token error,
# but we treat it as auth failure since retrying with the same config won't help.
_AUTH_FAILURE_ERRORS = frozenset(
    {
        "invalid_grant",
        "bad_refresh_token",
        "invalid_request",
        "incorrect_client_credentials",
    }
)


class RefreshResult(Enum):
    """Result of a token refresh attempt."""

    SUCCESS = "success"  # Token refreshed successfully
    AUTH_FAILURE = "auth_failure"  # Token is invalid/revoked, should evict
    TRANSIENT_ERROR = "transient_error"  # Temporary error, keep token for retry


class GitHubTokenData(BaseModel):
    """GitHub OAuth token data with optional refresh token support."""

    access_token: str
    # Refresh token (only present if GitHub App has expiring tokens enabled)
    refresh_token: str | None = None
    # Unix timestamp when access token expires (None = never expires)
    expires_at: float | None = None
    # Unix timestamp when refresh token expires (None = never expires)
    refresh_token_expires_at: float | None = None

    def is_access_token_expired(self) -> bool:
        """Check if access token is expired or about to expire."""
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - _REFRESH_BUFFER_SECONDS)

    def is_refresh_token_expired(self) -> bool:
        """Check if refresh token is expired."""
        if self.refresh_token_expires_at is None:
            return False
        return time.time() >= self.refresh_token_expires_at

    def can_refresh(self) -> bool:
        """Check if token can be refreshed."""
        return self.refresh_token is not None and not self.is_refresh_token_expired()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create from storage dictionary."""
        # Handle legacy format (just "token" field)
        if "token" in data and "access_token" not in data:
            return cls(access_token=data["token"])
        return cls.model_validate(data)

    @classmethod
    def from_oauth_response(cls, data: dict[str, Any]) -> Self:
        """Create from GitHub OAuth response.

        Args:
            data: OAuth response containing access_token, and optionally
                  refresh_token, expires_in, refresh_token_expires_in.
        """
        now = time.time()
        expires_at = None
        refresh_token_expires_at = None

        if "expires_in" in data:
            expires_at = now + int(data["expires_in"])
        if "refresh_token_expires_in" in data:
            refresh_token_expires_at = now + int(data["refresh_token_expires_in"])

        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
        )


async def _get_encrypted_store() -> FernetEncryptionWrapper | None:
    """Get or create the encrypted Redis store singleton.

    Returns:
        The encrypted store, or None if encryption key is not configured.
    """
    global _encrypted_store, _redis_store, _initialized

    # Fast path: already initialized (includes disabled case where _encrypted_store is None)
    if _initialized:
        return _encrypted_store

    async with _store_lock:
        # Double-check after acquiring lock (necessary for concurrent initialization)
        if _initialized:
            return _encrypted_store  # type: ignore[unreachable]

        if not settings.AHS_GITHUB_TOKEN_ENCRYPTION_KEY:
            logger.warning(
                "AHS_GITHUB_TOKEN_ENCRYPTION_KEY not configured. "
                "GitHub token storage disabled; create_pr will fail with auth-required error. "
                "Generate a key with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
            _initialized = True  # Mark as initialized with _encrypted_store = None
            return None

        logger.info("Initializing encrypted Redis storage for GitHub tokens")

        # Create Redis store with prefix and encryption
        _redis_store = RedisStore(url=settings.REDIS_URL)

        # Add prefix to isolate AHS GitHub token keys
        prefixed_store = PrefixCollectionsWrapper(
            key_value=_redis_store,
            prefix="ahs-github-tokens",
        )

        # Wrap with Fernet encryption for secure token storage
        _encrypted_store = FernetEncryptionWrapper(
            key_value=prefixed_store,
            fernet=Fernet(settings.AHS_GITHUB_TOKEN_ENCRYPTION_KEY.encode()),
        )

        _initialized = True
        return _encrypted_store


async def store_github_token_data(user_id: str, token_data: GitHubTokenData) -> bool:
    """Store GitHub token data for a user.

    Args:
        user_id: The Yupp user_id.
        token_data: The GitHub OAuth token data.

    Returns:
        True if the token was stored, False if storage is disabled.
    """
    store = await _get_encrypted_store()
    if store is None:
        return False
    await store.put(
        key=user_id,
        value=token_data.to_dict(),
        collection=_GITHUB_TOKENS_COLLECTION,
    )
    logger.debug(
        "GitHub token stored in Redis",
        user_id=user_id,
        has_refresh_token=token_data.refresh_token is not None,
        expires_at=token_data.expires_at,
    )
    return True


async def get_github_token_data(user_id: str) -> GitHubTokenData | None:
    """Retrieve GitHub token data for a user.

    Returns None if encryption key is not configured or no token exists.

    Args:
        user_id: The Yupp user_id.

    Returns:
        The GitHub token data, or None if not found or storage is disabled.
    """
    store = await _get_encrypted_store()
    if store is None:
        return None
    result: dict[str, Any] | None = await store.get(key=user_id, collection=_GITHUB_TOKENS_COLLECTION)
    if result is None:
        return None
    return GitHubTokenData.from_dict(result)


# Backwards compatibility: simple accessor for access token string
async def get_github_token(user_id: str) -> str | None:
    """Retrieve a GitHub access token for a user.

    This is a convenience wrapper for get_github_token_data() that returns
    just the access token string. Does NOT check expiration.

    Args:
        user_id: The Yupp user_id.

    Returns:
        The GitHub access token, or None if not found or storage is disabled.
    """
    token_data = await get_github_token_data(user_id)
    return token_data.access_token if token_data else None


# Backwards compatibility: simple store for access token string
async def store_github_token(user_id: str, token: str) -> bool:
    """Store a GitHub access token for a user (legacy format, no refresh token).

    This is a convenience wrapper for store_github_token_data() that stores
    just an access token without refresh token or expiration.

    Args:
        user_id: The Yupp user_id.
        token: The GitHub OAuth access token.

    Returns:
        True if the token was stored, False if storage is disabled.
    """
    return await store_github_token_data(user_id, GitHubTokenData(access_token=token))


async def remove_github_token(user_id: str) -> bool:
    """Remove a GitHub token for a user.

    Returns False if encryption key is not configured.

    Args:
        user_id: The Yupp user_id.

    Returns:
        True if a token was removed, False if no token existed or storage is disabled.
    """
    store = await _get_encrypted_store()
    if store is None:
        return False
    deleted: bool = await store.delete(key=user_id, collection=_GITHUB_TOKENS_COLLECTION)
    if deleted:
        logger.debug("GitHub token removed from Redis", user_id=user_id)
    return deleted


async def refresh_github_token(
    user_id: str, client_id: str, client_secret: str, token_data: GitHubTokenData
) -> tuple[RefreshResult, GitHubTokenData | None]:
    """Refresh a GitHub access token using the refresh token.

    This calls GitHub's OAuth endpoint to exchange the refresh token for a new
    access token and refresh token pair.

    Args:
        user_id: The Yupp user_id (for logging).
        client_id: The GitHub App client ID.
        client_secret: The GitHub App client secret.
        token_data: The current token data containing the refresh token.

    Returns:
        Tuple of (RefreshResult, new_token_data).
        - SUCCESS: Token refreshed, new_token_data is the new token
        - AUTH_FAILURE: Token is invalid/revoked, should evict from storage
        - TRANSIENT_ERROR: Temporary error, keep token for retry
    """
    if not token_data.can_refresh():
        logger.warning(
            "Cannot refresh GitHub token: no refresh token or expired",
            user_id=user_id,
            has_refresh_token=token_data.refresh_token is not None,
            refresh_expired=token_data.is_refresh_token_expired(),
        )
        return RefreshResult.AUTH_FAILURE, None

    store = await _get_encrypted_store()
    if store is None:
        return RefreshResult.TRANSIENT_ERROR, None

    try:
        # Build request data - only include client_secret if configured
        # (GitHub device flow allows refresh without client_secret)
        request_data: dict[str, Any] = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": token_data.refresh_token,
        }
        if client_secret:
            request_data["client_secret"] = client_secret

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                data=request_data,
                headers={"Accept": "application/json"},
            )

            # Check for HTTP errors before parsing JSON
            if resp.status_code >= 500:
                logger.warning(
                    "GitHub token refresh returned server error",
                    user_id=user_id,
                    status_code=resp.status_code,
                )
                return RefreshResult.TRANSIENT_ERROR, None

            try:
                data = resp.json()
            except Exception as json_err:
                logger.warning(
                    "GitHub token refresh returned non-JSON response",
                    user_id=user_id,
                    status_code=resp.status_code,
                    error=str(json_err),
                )
                return RefreshResult.TRANSIENT_ERROR, None

        if "error" in data:
            error = data.get("error")
            error_desc = data.get("error_description", error)
            is_auth_failure = error in _AUTH_FAILURE_ERRORS
            logger.warning(
                "GitHub token refresh failed",
                user_id=user_id,
                error=error,
                error_description=error_desc,
                is_auth_failure=is_auth_failure,
            )
            return (RefreshResult.AUTH_FAILURE if is_auth_failure else RefreshResult.TRANSIENT_ERROR), None

        if "access_token" not in data:
            logger.warning(
                "GitHub token refresh response missing access_token",
                user_id=user_id,
                response_keys=list(data.keys()),
            )
            return RefreshResult.TRANSIENT_ERROR, None

        new_token_data = GitHubTokenData.from_oauth_response(data)

        # Store the new tokens
        await store.put(
            key=user_id,
            value=new_token_data.to_dict(),
            collection=_GITHUB_TOKENS_COLLECTION,
        )

        logger.info(
            "GitHub token refreshed successfully",
            user_id=user_id,
            has_new_refresh_token=new_token_data.refresh_token is not None,
            new_expires_at=new_token_data.expires_at,
        )
        return RefreshResult.SUCCESS, new_token_data

    except Exception as e:
        logger.error(
            "GitHub token refresh request failed (transient)",
            user_id=user_id,
            error=str(e),
        )
        return RefreshResult.TRANSIENT_ERROR, None


async def close_store() -> None:
    """Close the encrypted store connection.

    Should be called during application shutdown.
    """
    global _encrypted_store, _redis_store, _initialized

    async with _store_lock:
        if _redis_store is not None:
            await _redis_store.close()
            _redis_store = None
            _encrypted_store = None
            _initialized = False
            logger.info("Closed encrypted GitHub token storage")
