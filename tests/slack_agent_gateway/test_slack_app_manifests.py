"""Unit tests for ypl/slack_agent_gateway/slack_app_manifests.py.

Covers:
- build_manifest (pure data construction)
- build_oauth_install_url (pure URL builder)
- _get_cached_token (async, Redis + crypto mocked)
- _cache_token (async, Redis mocked)
- _refresh_access_token (async, httpx mocked)
- get_valid_app_config_token (async, delegates to cache/refresh)
- clear_token_cache (async, Redis mocked)
- create_slack_app (async, httpx + token flow mocked)
- delete_slack_app (async, httpx + token flow mocked)
- exchange_oauth_code (async, httpx mocked)
- add_app_collaborator (async, httpx + token flow mocked)
"""

from __future__ import annotations
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.slack_agent_gateway.slack_app_manifests import (
    _BOT_SCOPES,
    _REDIS_KEY_APP_CONFIG_TOKEN,
    _TOKEN_EXPIRY_BUFFER_SECONDS,
    _cache_token,
    _get_cached_token,
    _refresh_access_token,
    add_app_collaborator,
    build_manifest,
    build_oauth_install_url,
    clear_token_cache,
    create_slack_app,
    delete_slack_app,
    exchange_oauth_code,
    get_valid_app_config_token,
)

MODULE = "ypl.slack_agent_gateway.slack_app_manifests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis(get_value: str | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.get.return_value = get_value
    redis.set.return_value = True
    redis.delete.return_value = 1
    return redis


def _make_httpx_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.return_value = None
    return resp


def _future_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# build_manifest — pure
# ---------------------------------------------------------------------------


class TestBuildManifest:
    @pytest.fixture(autouse=True)
    def _stub_gateway_url(self) -> Iterator[None]:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", "https://sag.example.com"):
            yield

    def test_returns_dict_with_required_keys(self) -> None:
        result = build_manifest("mybot", "My Bot")
        assert "display_information" in result
        assert "features" in result
        assert "oauth_config" in result
        assert "settings" in result

    def test_display_name_is_set(self) -> None:
        result = build_manifest("mybot", "My Bot Display")
        assert result["display_information"]["name"] == "My Bot Display"
        assert result["features"]["bot_user"]["display_name"] == "My Bot Display"

    def test_bot_always_online(self) -> None:
        result = build_manifest("mybot", "My Bot")
        assert result["features"]["bot_user"]["always_online"] is True

    def test_urls_use_configured_gateway_base(self) -> None:
        result = build_manifest("giladovski", "Giladovski")
        base = "https://sag.example.com/api/v1/slack"
        assert result["settings"]["event_subscriptions"]["request_url"] == f"{base}/events"
        assert result["settings"]["interactivity"]["request_url"] == f"{base}/interactions"
        assert result["oauth_config"]["redirect_urls"][0] == f"{base}/oauth/callback"

    def test_raises_when_gateway_url_unset(self) -> None:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", ""), pytest.raises(RuntimeError):
            build_manifest("bot", "Bot")

    def test_bot_scopes_are_included(self) -> None:
        result = build_manifest("bot", "Bot")
        scopes = result["oauth_config"]["scopes"]["bot"]
        for scope in _BOT_SCOPES:
            assert scope in scopes

    def test_event_subscriptions_include_app_mention(self) -> None:
        result = build_manifest("bot", "Bot")
        assert "app_mention" in result["settings"]["event_subscriptions"]["bot_events"]

    def test_socket_mode_disabled(self) -> None:
        result = build_manifest("bot", "Bot")
        assert result["settings"]["socket_mode_enabled"] is False

    def test_interactivity_enabled(self) -> None:
        result = build_manifest("bot", "Bot")
        assert result["settings"]["interactivity"]["is_enabled"] is True

    def test_token_rotation_disabled(self) -> None:
        result = build_manifest("bot", "Bot")
        assert result["settings"]["token_rotation_enabled"] is False


# ---------------------------------------------------------------------------
# build_oauth_install_url — pure
# ---------------------------------------------------------------------------


class TestBuildOauthInstallUrl:
    @pytest.fixture(autouse=True)
    def _stub_gateway_url(self) -> Iterator[None]:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", "https://sag.example.com"):
            yield

    def test_returns_slack_oauth_url(self) -> None:
        url = build_oauth_install_url("client-123")
        assert url.startswith("https://slack.com/oauth/v2/authorize?")

    def test_includes_client_id(self) -> None:
        url = build_oauth_install_url("my-client-id")
        assert "client_id=my-client-id" in url

    def test_includes_redirect_uri(self) -> None:
        url = build_oauth_install_url("cid")
        assert "redirect_uri=" in url
        assert "sag.example.com" in url

    def test_includes_scopes(self) -> None:
        url = build_oauth_install_url("cid")
        assert "scope=" in url
        for scope in _BOT_SCOPES[:3]:
            assert scope.replace(":", "%3A") in url or scope in url

    def test_no_state_by_default(self) -> None:
        url = build_oauth_install_url("cid")
        assert "state=" not in url

    def test_state_included_when_provided(self) -> None:
        url = build_oauth_install_url("cid", state="my-csrf-token")
        assert "state=my-csrf-token" in url

    def test_raises_when_gateway_url_unset(self) -> None:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", ""), pytest.raises(RuntimeError):
            build_oauth_install_url("cid")


# ---------------------------------------------------------------------------
# _get_cached_token — async
# ---------------------------------------------------------------------------


class TestGetCachedToken:
    async def test_returns_none_when_redis_has_no_value(self) -> None:
        redis = _make_redis(get_value=None)
        with patch(f"{MODULE}.get_redis_client", return_value=redis):
            result = await _get_cached_token()
        assert result is None

    async def test_returns_none_when_decrypt_fails(self) -> None:
        redis = _make_redis(get_value="bad-encrypted-data")
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.decrypt_token_data", return_value=None),
        ):
            result = await _get_cached_token()
        assert result is None

    async def test_returns_none_when_token_expired(self) -> None:
        redis = _make_redis(get_value="encrypted-value")
        expired_at = datetime.now(UTC) - timedelta(hours=1)
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.decrypt_token_data", return_value=("token", expired_at)),
        ):
            result = await _get_cached_token()
        assert result is None

    async def test_returns_none_when_token_expiring_soon(self) -> None:
        redis = _make_redis(get_value="encrypted-value")
        # Expires within the buffer window
        expires_at = datetime.now(UTC) + timedelta(seconds=_TOKEN_EXPIRY_BUFFER_SECONDS - 30)
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.decrypt_token_data", return_value=("mytoken", expires_at)),
        ):
            result = await _get_cached_token()
        assert result is None

    async def test_returns_token_when_valid(self) -> None:
        redis = _make_redis(get_value="encrypted-value")
        expires_at = _future_expires_at()
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.decrypt_token_data", return_value=("valid-token", expires_at)),
        ):
            result = await _get_cached_token()
        assert result == "valid-token"

    async def test_re_caches_with_new_key_when_legacy_used(self) -> None:
        redis = _make_redis(get_value="encrypted-value")
        expires_at = _future_expires_at()

        # First call (new key) returns None, second (legacy key) returns result
        decrypt_results = [None, ("legacy-token", expires_at)]
        decrypt_call_count = 0

        def _decrypt_side_effect(encrypted: str, use_legacy_key: bool = False) -> tuple | None:
            nonlocal decrypt_call_count
            result = decrypt_results[decrypt_call_count]
            decrypt_call_count += 1
            return result

        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.decrypt_token_data", side_effect=_decrypt_side_effect),
            patch(f"{MODULE}._cache_token", new_callable=AsyncMock) as mock_cache,
        ):
            result = await _get_cached_token()

        assert result == "legacy-token"
        mock_cache.assert_awaited_once()

    async def test_returns_none_on_redis_error(self) -> None:
        redis = AsyncMock()
        redis.get.side_effect = Exception("Redis connection failed")
        with patch(f"{MODULE}.get_redis_client", return_value=redis):
            result = await _get_cached_token()
        assert result is None


# ---------------------------------------------------------------------------
# _cache_token — async
# ---------------------------------------------------------------------------


class TestCacheToken:
    async def test_caches_encrypted_token_in_redis(self) -> None:
        redis = _make_redis()
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.encrypt_token_data", return_value="encrypted-value"),
        ):
            await _cache_token("mytoken", expires_in_seconds=3600)
        redis.set.assert_awaited_once()
        call_args = redis.set.call_args
        assert call_args[0][0] == _REDIS_KEY_APP_CONFIG_TOKEN
        assert call_args[0][1] == "encrypted-value"

    async def test_uses_default_ttl_when_none_provided(self) -> None:
        redis = _make_redis()
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.encrypt_token_data", return_value="enc"),
        ):
            await _cache_token("tok")
        redis.set.assert_awaited_once()

    async def test_handles_redis_error_gracefully(self) -> None:
        redis = AsyncMock()
        redis.set.side_effect = Exception("Redis down")
        with (
            patch(f"{MODULE}.get_redis_client", return_value=redis),
            patch(f"{MODULE}.encrypt_token_data", return_value="enc"),
        ):
            # Should not raise
            await _cache_token("tok")


# ---------------------------------------------------------------------------
# _refresh_access_token — async
# ---------------------------------------------------------------------------


class TestRefreshAccessToken:
    async def test_returns_new_token_on_success(self) -> None:
        data = {"ok": True, "token": "new-xoxe-token", "refresh_token": None}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            patch(f"{MODULE}._cache_token", new_callable=AsyncMock),
        ):
            result = await _refresh_access_token("xoxe-1-refresh-token")

        assert result == "new-xoxe-token"

    async def test_raises_on_api_error(self) -> None:
        data = {"ok": False, "error": "invalid_auth"}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="Failed to refresh app config token"),
        ):
            await _refresh_access_token("bad-refresh-token")

    async def test_invalidates_refresh_token_on_invalid_refresh_token_error(self) -> None:
        data = {"ok": False, "error": "invalid_refresh_token"}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            patch(f"{MODULE}.invalidate_refresh_token", new_callable=AsyncMock) as mock_invalidate,
            pytest.raises(RuntimeError),
        ):
            await _refresh_access_token("stale-token")

        mock_invalidate.assert_awaited_once()

    async def test_saves_new_refresh_token_when_provided(self) -> None:
        data = {"ok": True, "token": "new-access", "refresh_token": "new-xoxe-1-refresh"}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            patch(f"{MODULE}.save_bot_father_refresh_token", new_callable=AsyncMock) as mock_save,
            patch(f"{MODULE}._cache_token", new_callable=AsyncMock),
        ):
            result = await _refresh_access_token("old-refresh")

        assert result == "new-access"
        mock_save.assert_awaited_once_with("new-xoxe-1-refresh")

    async def test_continues_even_if_save_refresh_token_fails(self) -> None:
        data = {"ok": True, "token": "new-access", "refresh_token": "new-refresh"}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            patch(f"{MODULE}.save_bot_father_refresh_token", new_callable=AsyncMock, side_effect=Exception("DB error")),
            patch(f"{MODULE}._cache_token", new_callable=AsyncMock),
        ):
            result = await _refresh_access_token("old-refresh")

        # Should still return the token despite save failure
        assert result == "new-access"

    async def test_computes_expires_in_from_exp_field(self) -> None:
        import time

        future_exp = int(time.time()) + 7200  # 2 hours from now
        data = {"ok": True, "token": "tok", "exp": future_exp}
        mock_response = _make_httpx_response(json_data=data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            patch(f"{MODULE}._cache_token", new_callable=AsyncMock) as mock_cache,
        ):
            await _refresh_access_token("r-token")

        # expires_in should be approximately 7200
        # _cache_token is called as _cache_token(token, expires_in) — positional
        call_args = mock_cache.call_args
        pos_args = tuple(call_args[0]) if call_args[0] else ()
        kw_args = dict(call_args[1]) if call_args[1] else {}
        expires_in = kw_args.get("expires_in_seconds") or (pos_args[1] if len(pos_args) > 1 else 0)
        assert expires_in > 7100


# ---------------------------------------------------------------------------
# get_valid_app_config_token — async
# ---------------------------------------------------------------------------


class TestGetValidAppConfigToken:
    async def test_returns_cached_token_without_refresh(self) -> None:
        with (
            patch(f"{MODULE}._get_cached_token", new_callable=AsyncMock, return_value="cached-token"),
            patch(f"{MODULE}._refresh_access_token", new_callable=AsyncMock) as mock_refresh,
        ):
            result = await get_valid_app_config_token("refresh-tok")

        assert result == "cached-token"
        mock_refresh.assert_not_awaited()

    async def test_refreshes_when_no_cached_token(self) -> None:
        with (
            patch(f"{MODULE}._get_cached_token", new_callable=AsyncMock, return_value=None),
            patch(f"{MODULE}._refresh_access_token", new_callable=AsyncMock, return_value="fresh-token"),
        ):
            result = await get_valid_app_config_token("r-tok")

        assert result == "fresh-token"

    async def test_uses_cached_token_after_lock_acquired(self) -> None:
        """Double-check after lock: if another coroutine already refreshed, use that."""
        call_count = 0

        async def _get_cached_side_effect() -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return "refreshed-by-other"
            return None

        with (
            patch(f"{MODULE}._get_cached_token", side_effect=_get_cached_side_effect),
            patch(f"{MODULE}._refresh_access_token", new_callable=AsyncMock) as mock_refresh,
        ):
            result = await get_valid_app_config_token("r-tok")

        assert result == "refreshed-by-other"
        mock_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# clear_token_cache — async
# ---------------------------------------------------------------------------


class TestClearTokenCache:
    async def test_deletes_redis_key(self) -> None:
        redis = _make_redis()
        with patch(f"{MODULE}.get_redis_client", return_value=redis):
            await clear_token_cache()
        redis.delete.assert_awaited_once_with(_REDIS_KEY_APP_CONFIG_TOKEN)

    async def test_handles_redis_error_gracefully(self) -> None:
        redis = AsyncMock()
        redis.delete.side_effect = Exception("Redis down")
        with patch(f"{MODULE}.get_redis_client", return_value=redis):
            await clear_token_cache()  # Should not raise


# ---------------------------------------------------------------------------
# create_slack_app — async
# ---------------------------------------------------------------------------


class TestCreateSlackApp:
    @pytest.fixture(autouse=True)
    def _stub_gateway_url(self) -> Iterator[None]:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", "https://sag.example.com"):
            yield

    async def test_returns_app_id_and_credentials_on_success(self) -> None:
        manifest = build_manifest("bot", "Bot")
        response_data = {
            "ok": True,
            "app_id": "A999",
            "credentials": {"signing_secret": "sec", "oauth_client_id": "cid"},
        }
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="access-tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await create_slack_app(manifest, "refresh-tok")

        assert result["app_id"] == "A999"
        assert result["credentials"]["signing_secret"] == "sec"

    async def test_raises_on_api_error(self) -> None:
        manifest: dict[str, object] = {}
        response_data = {"ok": False, "error": "some_error"}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="apps.manifest.create failed"),
        ):
            await create_slack_app(manifest, "refresh-tok")

    async def test_retries_on_token_expired_error(self) -> None:
        manifest: dict[str, object] = {}
        first_response = _make_httpx_response(json_data={"ok": False, "error": "token_expired"})
        second_response = _make_httpx_response(json_data={"ok": True, "app_id": "A777", "credentials": {}})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.side_effect = [first_response, second_response]

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.clear_token_cache", new_callable=AsyncMock) as mock_clear,
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await create_slack_app(manifest, "refresh-tok")

        assert result["app_id"] == "A777"
        mock_clear.assert_awaited_once()

    async def test_raises_after_retry_if_still_failing(self) -> None:
        manifest: dict[str, object] = {}
        response = _make_httpx_response(json_data={"ok": False, "error": "token_expired"})
        second_response = _make_httpx_response(json_data={"ok": False, "error": "still_bad"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.side_effect = [response, second_response]

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.clear_token_cache", new_callable=AsyncMock),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="apps.manifest.create failed"),
        ):
            await create_slack_app(manifest, "refresh-tok")


# ---------------------------------------------------------------------------
# delete_slack_app — async
# ---------------------------------------------------------------------------


class TestDeleteSlackApp:
    async def test_deletes_successfully(self) -> None:
        response_data = {"ok": True}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            await delete_slack_app("A123", "refresh-tok")  # Should not raise

    async def test_raises_on_api_error(self) -> None:
        response_data = {"ok": False, "error": "app_not_found"}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="apps.manifest.delete failed"),
        ):
            await delete_slack_app("A999", "refresh-tok")

    async def test_retries_on_token_expired(self) -> None:
        first_response = _make_httpx_response(json_data={"ok": False, "error": "token_expired"})
        second_response = _make_httpx_response(json_data={"ok": True})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.side_effect = [first_response, second_response]

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.clear_token_cache", new_callable=AsyncMock) as mock_clear,
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            await delete_slack_app("A123", "r-tok")

        mock_clear.assert_awaited_once()


# ---------------------------------------------------------------------------
# exchange_oauth_code — async
# ---------------------------------------------------------------------------


class TestExchangeOauthCode:
    @pytest.fixture(autouse=True)
    def _stub_gateway_url(self) -> Iterator[None]:
        with patch(f"{MODULE}.settings.GATEWAY_BASE_URL", "https://sag.example.com"):
            yield

    async def test_returns_access_token_on_success(self) -> None:
        response_data = {
            "ok": True,
            "access_token": "xoxb-bot-token",
            "app_id": "A123",
            "team": {"id": "T001"},
            "authed_user": {"id": "U001"},
            "bot_user_id": "B001",
        }
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client):
            result = await exchange_oauth_code("code-123", "cid", "csecret")

        assert result["access_token"] == "xoxb-bot-token"
        assert result["app_id"] == "A123"
        assert result["team"]["id"] == "T001"

    async def test_raises_on_oauth_error(self) -> None:
        response_data = {"ok": False, "error": "invalid_code"}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="OAuth code exchange failed"),
        ):
            await exchange_oauth_code("bad-code", "cid", "csec")

    async def test_raises_when_access_token_missing(self) -> None:
        response_data = {"ok": True}  # No access_token
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="OAuth response missing access_token"),
        ):
            await exchange_oauth_code("code", "cid", "csec")

    async def test_redirect_uri_uses_configured_gateway_url(self) -> None:
        response_data = {"ok": True, "access_token": "xoxb-tok"}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client):
            await exchange_oauth_code("code", "cid", "csec")

        call_kwargs = mock_client.post.call_args[1]
        redirect_uri = call_kwargs["data"]["redirect_uri"]
        assert redirect_uri == "https://sag.example.com/api/v1/slack/oauth/callback"


# ---------------------------------------------------------------------------
# add_app_collaborator — async
# ---------------------------------------------------------------------------


class TestAddAppCollaborator:
    async def test_adds_collaborator_successfully(self) -> None:
        response_data = {"ok": True}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            await add_app_collaborator("A123", "user@example.com", "owner", "refresh-tok")

    async def test_raises_on_api_error(self) -> None:
        response_data = {"ok": False, "error": "user_not_found"}
        mock_response = _make_httpx_response(json_data=response_data)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_response

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="Failed to add collaborator"),
        ):
            await add_app_collaborator("A123", "bad@email.com", "reader", "refresh-tok")

    async def test_retries_on_token_expired(self) -> None:
        first_response = _make_httpx_response(json_data={"ok": False, "error": "token_expired"})
        second_response = _make_httpx_response(json_data={"ok": True})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.side_effect = [first_response, second_response]

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.clear_token_cache", new_callable=AsyncMock) as mock_clear,
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
        ):
            await add_app_collaborator("A123", "user@example.com", "owner", "refresh-tok")

        mock_clear.assert_awaited_once()

    async def test_raises_on_second_attempt_error(self) -> None:
        first_response = _make_httpx_response(json_data={"ok": False, "error": "token_expired"})
        second_response = _make_httpx_response(json_data={"ok": False, "error": "fatal_error"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.side_effect = [first_response, second_response]

        with (
            patch(f"{MODULE}.get_valid_app_config_token", new_callable=AsyncMock, return_value="tok"),
            patch(f"{MODULE}.clear_token_cache", new_callable=AsyncMock),
            patch(f"{MODULE}.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="Failed to add collaborator"),
        ):
            await add_app_collaborator("A123", "user@example.com", "reader", "refresh-tok")
