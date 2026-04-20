"""Encrypted token storage for Slack OAuth tokens.

Provides durable storage for OAuth tokens with Redis caching for fast access.
Tokens are encrypted at rest using Fernet encryption keyed off
``SLACK_AGENT_GW_ENCRYPTION_KEY``.

Architecture:
- Durable storage: PostgreSQL (``slack_oauth_tokens`` table)
- Fast cache: Redis with a 24-hour TTL
- Seed on first run: ``SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN`` env var
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session, retry_db
from ypl.db.redis import get_redis_client
from ypl.db.slack_oauth_token import SlackOAuthToken, SlackOAuthTokenType
from ypl.slack_agent_gateway.crypto import decrypt_secret, encrypt_secret
from ypl.structured_logger import get_logger

logger = get_logger()

# Redis key for cached refresh token
_REDIS_KEY_REFRESH_TOKEN = "slack_agent_gw:bot_father:refresh_token"

# Redis TTL for refresh token cache (24 hours)
_REFRESH_TOKEN_CACHE_TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_bot_father_refresh_token() -> str:
    """Get the Bot Father refresh token.

    Retrieval order:
    1. Redis cache (fastest)
    2. Database (durable, source of truth)
    3. ``settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN`` env var — seed
       value written to the DB on first access, then never read again.

    Raises:
        ValueError: If no refresh token is found anywhere.
    """
    cached = await _get_refresh_token_from_cache()
    if cached:
        return cached

    db_token, _ = await _get_refresh_token_from_db()
    if db_token:
        await _cache_refresh_token(db_token)
        return db_token

    seed_token = settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN
    if not seed_token:
        raise ValueError(
            "No Bot Father refresh token found in DB or env. "
            "Set SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN in .env on first run — "
            "after the first successful refresh it will be persisted to slack_oauth_tokens."
        )

    logger.info("Seeding Bot Father refresh token from env → slack_oauth_tokens")
    await save_bot_father_refresh_token(seed_token)
    await _cache_refresh_token(seed_token)
    return seed_token


async def save_bot_father_refresh_token(refresh_token: str) -> None:
    """Save the refresh token to durable storage and cache.

    This is called after each token refresh to persist the new (rotated) token.

    Args:
        refresh_token: The new refresh token from Slack.
    """
    await _save_refresh_token_to_db(refresh_token)
    await _cache_refresh_token(refresh_token)

    logger.info(
        "Bot Father refresh token saved",
        refresh_token_prefix=refresh_token[:20] + "...",
    )


async def save_bot_father_tokens(
    refresh_token: str,
    access_token: str | None = None,
    access_token_expires_at: datetime | None = None,
) -> None:
    """Save both refresh and access tokens to durable storage.

    Primarily saves the refresh token (required). Access token is optional
    since it's typically cached in Redis separately.

    Args:
        refresh_token: The refresh token from Slack.
        access_token: Optional access token.
        access_token_expires_at: Optional expiry time for access token.
    """
    await _save_tokens_to_db(refresh_token, access_token, access_token_expires_at)
    await _cache_refresh_token(refresh_token)

    logger.info(
        "Bot Father tokens saved",
        refresh_token_prefix=refresh_token[:20] + "...",
        has_access_token=access_token is not None,
    )


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------


async def _get_refresh_token_from_cache() -> str | None:
    """Get refresh token from Redis cache."""
    try:
        redis = await get_redis_client()
        encrypted: str | None = await redis.get(_REDIS_KEY_REFRESH_TOKEN)
        if not encrypted:
            return None

        decrypted = decrypt_secret(encrypted)
        if not decrypted:
            logger.warning("Failed to decrypt cached refresh token")
            return None

        return decrypted
    except Exception as e:
        logger.warning("Failed to get refresh token from Redis cache", error=str(e))
        return None


async def _cache_refresh_token(refresh_token: str) -> None:
    """Cache refresh token in Redis."""
    try:
        encrypted = encrypt_secret(refresh_token)
        redis = await get_redis_client()
        await redis.set(_REDIS_KEY_REFRESH_TOKEN, encrypted, ex=_REFRESH_TOKEN_CACHE_TTL_SECONDS)
        logger.debug("Refresh token cached in Redis", ttl_seconds=_REFRESH_TOKEN_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("Failed to cache refresh token in Redis", error=str(e))


async def clear_refresh_token_cache() -> None:
    """Clear the cached refresh token from Redis.

    Useful for testing or forcing a refresh from DB.
    """
    try:
        redis = await get_redis_client()
        await redis.delete(_REDIS_KEY_REFRESH_TOKEN)
        logger.info("Refresh token cache cleared")
    except Exception as e:
        logger.warning("Failed to clear refresh token cache", error=str(e))


async def invalidate_refresh_token() -> None:
    """Invalidate the stored refresh token in both Redis and DB.

    Call this when Slack returns ``invalid_refresh_token``. Clears the cache
    and soft-deletes the DB row, forcing the next request to reseed from
    ``SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN`` (operator must update that
    env var with a fresh token first).
    """
    # Clear Redis cache
    await clear_refresh_token_cache()

    # Soft-delete DB record
    await _soft_delete_refresh_token_from_db()

    logger.info("Refresh token invalidated (cleared from Redis and soft-deleted from DB)")


@retry_db
async def _soft_delete_refresh_token_from_db() -> None:
    """Soft-delete the refresh token record from the database."""
    async with get_async_session() as session:
        stmt = select(SlackOAuthToken).where(
            SlackOAuthToken.token_type == SlackOAuthTokenType.BOT_FATHER_APP_CONFIG,
            SlackOAuthToken.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        result = await session.exec(stmt)
        record = result.first()

        if record:
            record.deleted_at = datetime.now(UTC)
            session.add(record)
            await session.commit()
            logger.info("Refresh token soft-deleted from DB")
        else:
            logger.debug("No active refresh token record to delete")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


@retry_db
async def _get_refresh_token_from_db() -> tuple[str | None, bool]:
    """Get refresh token from database.

    Returns:
        Tuple of (token, record_exists):
        - (token, True) if record exists and decryption succeeded
        - (None, True) if record exists but decryption failed
        - (None, False) if no record exists
    """
    async with get_async_session() as session:
        stmt = select(SlackOAuthToken).where(
            SlackOAuthToken.token_type == SlackOAuthTokenType.BOT_FATHER_APP_CONFIG,
            SlackOAuthToken.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        result = await session.exec(stmt)
        record = result.first()

        if not record:
            return None, False

        # Decrypt the stored token
        decrypted = decrypt_secret(record.encrypted_refresh_token)
        if not decrypted:
            logger.error("Failed to decrypt refresh token from DB - token may be corrupted")
            return None, True  # Record exists but decryption failed

        return decrypted, True


async def _save_refresh_token_to_db(refresh_token: str) -> None:
    """Save refresh token to database (upsert)."""
    await _save_tokens_to_db(refresh_token, None, None)


@retry_db
async def _save_tokens_to_db(
    refresh_token: str,
    access_token: str | None,
    access_token_expires_at: datetime | None,
) -> None:
    """Save tokens to database using atomic upsert (INSERT ... ON CONFLICT DO UPDATE)."""
    encrypted_refresh = encrypt_secret(refresh_token)
    encrypted_access = encrypt_secret(access_token) if access_token else None

    async with get_async_session() as session:
        # Build upsert statement: INSERT ... ON CONFLICT (token_type) DO UPDATE
        values: dict[str, str | SlackOAuthTokenType | datetime | None] = {
            "token_type": SlackOAuthTokenType.BOT_FATHER_APP_CONFIG,
            "encrypted_refresh_token": encrypted_refresh,
        }
        if encrypted_access:
            values["encrypted_access_token"] = encrypted_access
            values["access_token_expires_at"] = access_token_expires_at

        stmt = pg_insert(SlackOAuthToken).values(**values)

        # On conflict, update the refresh token (and optionally access token)
        # NOTE: pg_insert bypasses ORM hooks, so we must explicitly set modified_at
        # Also reset deleted_at to NULL to "un-delete" a soft-deleted row
        update_dict: dict[str, str | datetime | sa.TextClause | None] = {
            "encrypted_refresh_token": encrypted_refresh,
            "modified_at": sa.text("now()"),
            "deleted_at": None,
        }
        if encrypted_access:
            update_dict["encrypted_access_token"] = encrypted_access
            update_dict["access_token_expires_at"] = access_token_expires_at

        stmt = stmt.on_conflict_do_update(
            index_elements=["token_type"],
            set_=update_dict,
        )

        await session.exec(stmt)
        await session.commit()
        logger.debug("Tokens saved to database", token_type=SlackOAuthTokenType.BOT_FATHER_APP_CONFIG.value)
