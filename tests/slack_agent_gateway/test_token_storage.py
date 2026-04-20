"""Unit tests for SAG token_storage module.

Tests cover Redis cache helpers (get/set/clear), the public
get_bot_father_refresh_token retrieval chain, and save helpers.
All Redis calls and crypto functions are mocked.
"""

from __future__ import annotations
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.slack_agent_gateway import token_storage as ts
from ypl.slack_agent_gateway.token_storage import (
    _REDIS_KEY_REFRESH_TOKEN,
    _REFRESH_TOKEN_CACHE_TTL_SECONDS,
    _cache_refresh_token,
    _get_refresh_token_from_cache,
    clear_refresh_token_cache,
    get_bot_father_refresh_token,
    save_bot_father_refresh_token,
    save_bot_father_tokens,
)


@pytest.fixture()
def mock_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def patch_redis(mock_redis: AsyncMock) -> Generator[AsyncMock]:
    with patch("ypl.slack_agent_gateway.token_storage.get_redis_client", return_value=mock_redis):
        yield mock_redis


@pytest.fixture(autouse=True)
def patch_crypto() -> Generator[None]:
    """Patch encrypt/decrypt so tests don't need real Fernet keys."""
    with (
        patch("ypl.slack_agent_gateway.token_storage.encrypt_secret", side_effect=lambda s: f"enc:{s}"),
        patch(
            "ypl.slack_agent_gateway.token_storage.decrypt_secret",
            side_effect=lambda s: s[4:] if s.startswith("enc:") else None,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# _get_refresh_token_from_cache
# ---------------------------------------------------------------------------


class TestGetRefreshTokenFromCache:
    async def test_returns_decrypted_token_when_cached(self, patch_redis: AsyncMock) -> None:
        patch_redis.get.return_value = "enc:my-refresh-token"

        result = await _get_refresh_token_from_cache()

        assert result == "my-refresh-token"
        patch_redis.get.assert_awaited_once_with(_REDIS_KEY_REFRESH_TOKEN)

    async def test_returns_none_when_cache_miss(self, patch_redis: AsyncMock) -> None:
        patch_redis.get.return_value = None

        result = await _get_refresh_token_from_cache()

        assert result is None

    async def test_returns_none_on_decrypt_failure(self, patch_redis: AsyncMock) -> None:
        # Return something that decrypt_secret returns None for (not enc: prefix)
        patch_redis.get.return_value = "corrupted-data"

        result = await _get_refresh_token_from_cache()

        assert result is None

    async def test_returns_none_on_redis_error(self, patch_redis: AsyncMock) -> None:
        patch_redis.get.side_effect = ConnectionError("Redis down")

        result = await _get_refresh_token_from_cache()

        # Should catch exception and return None
        assert result is None


# ---------------------------------------------------------------------------
# _cache_refresh_token
# ---------------------------------------------------------------------------


class TestCacheRefreshToken:
    async def test_caches_encrypted_token(self, patch_redis: AsyncMock) -> None:
        await _cache_refresh_token("my-refresh-token")

        patch_redis.set.assert_awaited_once()
        call_args = patch_redis.set.call_args
        assert call_args[0][0] == _REDIS_KEY_REFRESH_TOKEN
        assert call_args[0][1] == "enc:my-refresh-token"
        assert call_args[1].get("ex") == _REFRESH_TOKEN_CACHE_TTL_SECONDS

    async def test_handles_redis_error_gracefully(self, patch_redis: AsyncMock) -> None:
        patch_redis.set.side_effect = ConnectionError("Redis down")

        # Should not raise
        await _cache_refresh_token("my-token")


# ---------------------------------------------------------------------------
# clear_refresh_token_cache
# ---------------------------------------------------------------------------


class TestClearRefreshTokenCache:
    async def test_deletes_cache_key(self, patch_redis: AsyncMock) -> None:
        await clear_refresh_token_cache()

        patch_redis.delete.assert_awaited_once_with(_REDIS_KEY_REFRESH_TOKEN)

    async def test_handles_redis_error_gracefully(self, patch_redis: AsyncMock) -> None:
        patch_redis.delete.side_effect = ConnectionError("Redis down")

        # Should not raise
        await clear_refresh_token_cache()


# ---------------------------------------------------------------------------
# get_bot_father_refresh_token — retrieval chain
# ---------------------------------------------------------------------------


class TestGetBotFatherRefreshToken:
    async def test_returns_cached_token_when_available(self, patch_redis: AsyncMock) -> None:
        """Cache hit: should return immediately without DB or Secret Manager."""
        patch_redis.get.return_value = "enc:cached-token"

        with patch.object(ts, "_get_refresh_token_from_db", new_callable=AsyncMock) as mock_db:
            result = await get_bot_father_refresh_token()

        assert result == "cached-token"
        mock_db.assert_not_awaited()

    async def test_falls_through_to_db(self, patch_redis: AsyncMock) -> None:
        """Cache miss: should try DB and return its token."""
        patch_redis.get.return_value = None  # cache miss

        with (
            patch.object(ts, "_get_refresh_token_from_db", new_callable=AsyncMock, return_value=("db-token", True)),
            patch.object(ts, "_cache_refresh_token", new_callable=AsyncMock) as mock_cache,
        ):
            result = await get_bot_father_refresh_token()

        assert result == "db-token"
        mock_cache.assert_awaited_once_with("db-token")

    async def test_seeds_from_env_on_db_miss(self, patch_redis: AsyncMock) -> None:
        """Cache miss + DB miss: seed from SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN env var."""
        patch_redis.get.return_value = None  # cache miss

        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = "seed-token"

        with (
            patch.object(
                ts,
                "_get_refresh_token_from_db",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch.object(ts, "save_bot_father_refresh_token", new_callable=AsyncMock) as mock_save,
            patch.object(ts, "_cache_refresh_token", new_callable=AsyncMock) as mock_cache,
            patch("ypl.slack_agent_gateway.token_storage.settings", mock_settings),
        ):
            result = await get_bot_father_refresh_token()

        assert result == "seed-token"
        mock_cache.assert_awaited()
        mock_save.assert_awaited_once_with("seed-token")

    async def test_raises_when_no_token_anywhere(self, patch_redis: AsyncMock) -> None:
        """All sources empty: should raise ValueError."""
        patch_redis.get.return_value = None

        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = None

        with (
            patch.object(
                ts,
                "_get_refresh_token_from_db",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch("ypl.slack_agent_gateway.token_storage.settings", mock_settings),
            pytest.raises(ValueError, match="No Bot Father refresh token"),
        ):
            await get_bot_father_refresh_token()


# ---------------------------------------------------------------------------
# save_bot_father_refresh_token
# ---------------------------------------------------------------------------


class TestSaveBotFatherRefreshToken:
    async def test_saves_to_db_and_cache(self, patch_redis: AsyncMock) -> None:
        with (
            patch.object(ts, "_save_refresh_token_to_db", new_callable=AsyncMock) as mock_db,
            patch.object(ts, "_cache_refresh_token", new_callable=AsyncMock) as mock_cache,
        ):
            await save_bot_father_refresh_token("new-refresh-token")

        mock_db.assert_awaited_once_with("new-refresh-token")
        mock_cache.assert_awaited_once_with("new-refresh-token")


# ---------------------------------------------------------------------------
# save_bot_father_tokens
# ---------------------------------------------------------------------------


class TestSaveBotFatherTokens:
    async def test_saves_tokens_to_db_and_cache(self, patch_redis: AsyncMock) -> None:
        with (
            patch.object(ts, "_save_tokens_to_db", new_callable=AsyncMock) as mock_db,
            patch.object(ts, "_cache_refresh_token", new_callable=AsyncMock) as mock_cache,
        ):
            await save_bot_father_tokens(
                refresh_token="refresh-tok",
                access_token="access-tok",
            )

        mock_db.assert_awaited_once()
        mock_cache.assert_awaited_once_with("refresh-tok")

    async def test_saves_refresh_token_without_access_token(self, patch_redis: AsyncMock) -> None:
        with (
            patch.object(ts, "_save_tokens_to_db", new_callable=AsyncMock) as mock_db,
            patch.object(ts, "_cache_refresh_token", new_callable=AsyncMock),
        ):
            await save_bot_father_tokens(refresh_token="refresh-only")

        mock_db.assert_awaited_once()
        call_args = mock_db.call_args[0]
        assert call_args[0] == "refresh-only"
        assert call_args[1] is None
