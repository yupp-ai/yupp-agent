"""Unit tests for ypl/agent_harness_service/tools/github_token_storage.py.

Covers:
- GitHubTokenData: is_access_token_expired, is_refresh_token_expired, can_refresh,
  from_dict (legacy format), from_oauth_response
- _get_encrypted_store: disabled when key not configured, initializes when key present
- store_github_token_data: disabled (returns False), enabled (calls store.put)
- get_github_token_data: disabled (returns None), enabled (returns parsed data), missing key
- get_github_token: convenience wrapper
- store_github_token: legacy wrapper
- remove_github_token: disabled (False), enabled (calls store.delete)
- refresh_github_token: AUTH_FAILURE when no refresh token, HTTP 5xx = TRANSIENT_ERROR,
  auth error codes, missing access_token field, successful refresh
- close_store: resets state correctly
"""

from __future__ import annotations
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Reset module singletons before each test to avoid state leakage
import ypl.agent_harness_service.tools.github_token_storage as gts
from ypl.agent_harness_service.tools.github_token_storage import (
    _AUTH_FAILURE_ERRORS,
    _GITHUB_TOKENS_COLLECTION,
    _REFRESH_BUFFER_SECONDS,
    GitHubTokenData,
    RefreshResult,
    close_store,
    get_github_token,
    get_github_token_data,
    refresh_github_token,
    remove_github_token,
    store_github_token,
    store_github_token_data,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "ypl.agent_harness_service.tools.github_token_storage"


def _reset_singletons() -> None:
    """Reset module-level singletons so each test gets a fresh state."""
    gts._redis_store = None
    gts._encrypted_store = None
    gts._initialized = False


@pytest.fixture(autouse=True)
def reset_state() -> None:  # type: ignore[misc]
    """Ensure each test starts with uninitialized store."""
    _reset_singletons()
    yield
    _reset_singletons()


# ---------------------------------------------------------------------------
# GitHubTokenData — pure logic
# ---------------------------------------------------------------------------


class TestGitHubTokenDataExpiry:
    def test_no_expires_at_never_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", expires_at=None)
        assert td.is_access_token_expired() is False

    def test_future_expires_at_not_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", expires_at=time.time() + 3600)
        assert td.is_access_token_expired() is False

    def test_past_expires_at_is_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", expires_at=time.time() - 1)
        assert td.is_access_token_expired() is True

    def test_within_buffer_is_considered_expired(self) -> None:
        # expires_at set to exactly REFRESH_BUFFER_SECONDS in the future → still expired
        td = GitHubTokenData(access_token="tok", expires_at=time.time() + _REFRESH_BUFFER_SECONDS - 1)
        assert td.is_access_token_expired() is True

    def test_refresh_token_no_expires_at_not_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", refresh_token_expires_at=None)
        assert td.is_refresh_token_expired() is False

    def test_refresh_token_future_not_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", refresh_token_expires_at=time.time() + 9999)
        assert td.is_refresh_token_expired() is False

    def test_refresh_token_past_expired(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", refresh_token_expires_at=time.time() - 1)
        assert td.is_refresh_token_expired() is True


class TestGitHubTokenDataCanRefresh:
    def test_no_refresh_token_cannot_refresh(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token=None)
        assert td.can_refresh() is False

    def test_valid_refresh_token_can_refresh(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", refresh_token_expires_at=time.time() + 9999)
        assert td.can_refresh() is True

    def test_expired_refresh_token_cannot_refresh(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", refresh_token_expires_at=time.time() - 1)
        assert td.can_refresh() is False


class TestGitHubTokenDataFromDict:
    def test_modern_format(self) -> None:
        data = {"access_token": "abc123", "refresh_token": "ref456"}
        td = GitHubTokenData.from_dict(data)
        assert td.access_token == "abc123"
        assert td.refresh_token == "ref456"

    def test_legacy_format_with_token_field(self) -> None:
        data = {"token": "legacy-token"}
        td = GitHubTokenData.from_dict(data)
        assert td.access_token == "legacy-token"
        assert td.refresh_token is None

    def test_full_modern_format(self) -> None:
        data = {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_at": 9999.0,
            "refresh_token_expires_at": 99999.0,
        }
        td = GitHubTokenData.from_dict(data)
        assert td.expires_at == 9999.0
        assert td.refresh_token_expires_at == 99999.0


class TestGitHubTokenDataFromOAuthResponse:
    def test_minimal_response(self) -> None:
        data = {"access_token": "tok123"}
        td = GitHubTokenData.from_oauth_response(data)
        assert td.access_token == "tok123"
        assert td.expires_at is None
        assert td.refresh_token is None
        assert td.refresh_token_expires_at is None

    def test_full_response_with_expiry(self) -> None:
        before = time.time()
        data = {
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }
        td = GitHubTokenData.from_oauth_response(data)
        assert td.access_token == "tok"
        assert td.refresh_token == "ref"
        assert td.expires_at is not None
        assert td.expires_at >= before + 3600
        assert td.refresh_token_expires_at is not None
        assert td.refresh_token_expires_at >= before + 86400

    def test_to_dict_roundtrip(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token="ref", expires_at=12345.0)
        d = td.to_dict()
        td2 = GitHubTokenData.from_dict(d)
        assert td2.access_token == td.access_token
        assert td2.refresh_token == td.refresh_token
        assert td2.expires_at == td.expires_at


# ---------------------------------------------------------------------------
# _get_encrypted_store — initialization
# ---------------------------------------------------------------------------


class TestGetEncryptedStore:
    async def test_returns_none_when_key_not_configured(self) -> None:
        mock_settings = MagicMock()
        mock_settings.AHS_GITHUB_TOKEN_ENCRYPTION_KEY = None

        with patch(f"{_MODULE}.settings", mock_settings):
            store = await gts._get_encrypted_store()

        assert store is None
        assert gts._initialized is True

    async def test_initializes_store_when_key_configured(self) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        mock_settings = MagicMock()
        mock_settings.AHS_GITHUB_TOKEN_ENCRYPTION_KEY = key
        mock_settings.REDIS_URL = "redis://localhost:6379"

        mock_redis_store = MagicMock()
        mock_prefixed = MagicMock()
        mock_encrypted = MagicMock()

        with (
            patch(f"{_MODULE}.settings", mock_settings),
            patch(f"{_MODULE}.RedisStore", return_value=mock_redis_store),
            patch(f"{_MODULE}.PrefixCollectionsWrapper", return_value=mock_prefixed),
            patch(f"{_MODULE}.FernetEncryptionWrapper", return_value=mock_encrypted),
        ):
            store = await gts._get_encrypted_store()

        assert store is mock_encrypted
        assert gts._initialized is True
        assert gts._encrypted_store is mock_encrypted

    async def test_fast_path_when_already_initialized(self) -> None:
        gts._initialized = True
        gts._encrypted_store = None  # disabled case
        # Should not call any imports
        store = await gts._get_encrypted_store()
        assert store is None


# ---------------------------------------------------------------------------
# store_github_token_data / get_github_token_data / remove_github_token
# ---------------------------------------------------------------------------


class TestStoreGitHubTokenData:
    async def test_returns_false_when_store_disabled(self) -> None:
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=None):
            result = await store_github_token_data("user-1", GitHubTokenData(access_token="tok"))
        assert result is False

    async def test_calls_store_put_when_enabled(self) -> None:
        mock_store = AsyncMock()
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await store_github_token_data("user-1", GitHubTokenData(access_token="tok"))
        assert result is True
        mock_store.put.assert_awaited_once()
        call_kwargs = mock_store.put.call_args[1]
        assert call_kwargs["key"] == "user-1"
        assert call_kwargs["collection"] == _GITHUB_TOKENS_COLLECTION


class TestGetGitHubTokenData:
    async def test_returns_none_when_store_disabled(self) -> None:
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=None):
            result = await get_github_token_data("user-1")
        assert result is None

    async def test_returns_none_when_key_not_found(self) -> None:
        mock_store = AsyncMock()
        mock_store.get.return_value = None
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await get_github_token_data("user-1")
        assert result is None

    async def test_returns_parsed_token_data(self) -> None:
        mock_store = AsyncMock()
        mock_store.get.return_value = {"access_token": "tok-abc", "refresh_token": None}
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await get_github_token_data("user-1")
        assert result is not None
        assert result.access_token == "tok-abc"

    async def test_returns_legacy_format(self) -> None:
        mock_store = AsyncMock()
        mock_store.get.return_value = {"token": "legacy-tok"}
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await get_github_token_data("user-1")
        assert result is not None
        assert result.access_token == "legacy-tok"


class TestGetGitHubToken:
    async def test_returns_access_token_string(self) -> None:
        with patch(
            f"{_MODULE}.get_github_token_data",
            new_callable=AsyncMock,
            return_value=GitHubTokenData(access_token="tok-xyz"),
        ):
            result = await get_github_token("user-1")
        assert result == "tok-xyz"

    async def test_returns_none_when_no_data(self) -> None:
        with patch(f"{_MODULE}.get_github_token_data", new_callable=AsyncMock, return_value=None):
            result = await get_github_token("user-1")
        assert result is None


class TestStoreGitHubToken:
    async def test_wraps_store_github_token_data(self) -> None:
        with patch(
            f"{_MODULE}.store_github_token_data",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_store:
            result = await store_github_token("user-1", "my-token")
        assert result is True
        call_args = mock_store.call_args[0]
        assert call_args[0] == "user-1"
        assert call_args[1].access_token == "my-token"


class TestRemoveGitHubToken:
    async def test_returns_false_when_store_disabled(self) -> None:
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=None):
            result = await remove_github_token("user-1")
        assert result is False

    async def test_returns_false_when_not_found(self) -> None:
        mock_store = AsyncMock()
        mock_store.delete.return_value = False
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await remove_github_token("user-1")
        assert result is False

    async def test_returns_true_when_deleted(self) -> None:
        mock_store = AsyncMock()
        mock_store.delete.return_value = True
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store):
            result = await remove_github_token("user-1")
        assert result is True
        mock_store.delete.assert_awaited_once_with(key="user-1", collection=_GITHUB_TOKENS_COLLECTION)


# ---------------------------------------------------------------------------
# refresh_github_token
# ---------------------------------------------------------------------------


class TestRefreshGitHubToken:
    def _valid_token_data(self) -> GitHubTokenData:
        return GitHubTokenData(
            access_token="old-tok",
            refresh_token="old-ref",
            refresh_token_expires_at=time.time() + 9999,
        )

    async def test_auth_failure_when_cannot_refresh(self) -> None:
        td = GitHubTokenData(access_token="tok", refresh_token=None)
        result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)
        assert result == RefreshResult.AUTH_FAILURE
        assert new_td is None

    async def test_transient_error_when_store_disabled(self) -> None:
        td = self._valid_token_data()
        with patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=None):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)
        assert result == RefreshResult.TRANSIENT_ERROR
        assert new_td is None

    async def test_transient_error_on_http_5xx(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.TRANSIENT_ERROR
        assert new_td is None

    async def test_transient_error_on_non_json_response(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.TRANSIENT_ERROR

    @pytest.mark.parametrize("error_code", list(_AUTH_FAILURE_ERRORS))
    async def test_auth_failure_error_codes(self, error_code: str) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": error_code, "error_description": "auth failed"}
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.AUTH_FAILURE
        assert new_td is None

    async def test_transient_error_on_unknown_error_code(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": "rate_limit_exceeded"}
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.TRANSIENT_ERROR

    async def test_transient_error_when_access_token_missing(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"scope": "repo"}  # no access_token
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.TRANSIENT_ERROR

    async def test_success_stores_and_returns_new_token(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-tok",
            "refresh_token": "new-ref",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.SUCCESS
        assert new_td is not None
        assert new_td.access_token == "new-tok"
        assert new_td.refresh_token == "new-ref"
        mock_store.put.assert_awaited_once()

    async def test_transient_error_on_network_exception(self) -> None:
        td = self._valid_token_data()
        mock_store = AsyncMock()

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", side_effect=ConnectionError("network down")),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "csecret", td)

        assert result == RefreshResult.TRANSIENT_ERROR

    async def test_no_client_secret_still_sends_request(self) -> None:
        """Client secret can be empty (device flow)."""
        td = self._valid_token_data()
        mock_store = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-tok"}
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post.return_value = mock_resp

        with (
            patch(f"{_MODULE}._get_encrypted_store", new_callable=AsyncMock, return_value=mock_store),
            patch(f"{_MODULE}.httpx.AsyncClient", return_value=mock_http_client),
        ):
            result, new_td = await refresh_github_token("user-1", "cid", "", td)

        assert result == RefreshResult.SUCCESS
        # Verify client_secret not in request data when empty
        call_kwargs = mock_http_client.post.call_args[1]
        assert "client_secret" not in call_kwargs.get("data", {})


# ---------------------------------------------------------------------------
# close_store
# ---------------------------------------------------------------------------


class TestCloseStore:
    async def test_close_when_initialized(self) -> None:
        mock_redis = AsyncMock()
        gts._redis_store = mock_redis
        gts._encrypted_store = MagicMock()
        gts._initialized = True

        await close_store()

        mock_redis.close.assert_awaited_once()
        assert gts._redis_store is None
        assert gts._encrypted_store is None  # type: ignore[unreachable]
        assert gts._initialized is False

    async def test_close_when_not_initialized_is_noop(self) -> None:
        gts._redis_store = None
        gts._initialized = False

        await close_store()

        assert gts._redis_store is None
        assert gts._initialized is False
