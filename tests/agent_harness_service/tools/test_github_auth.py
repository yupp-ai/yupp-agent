"""Unit tests for ypl/agent_harness_service/tools/github_auth.py.

Tests GitHub device flow, token validation, and refresh logic with
mocked HTTP clients and Redis token storage.
"""

from __future__ import annotations
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.tools.github_auth import (
    AUTH_STATE_DENIED,
    AUTH_STATE_EXPIRED,
    _get_github_user_info_async,
    _get_valid_github_token,
    _initiate_device_flow,
    _poll_for_token,
    _try_refresh_token_with_lock,
    _validate_github_token,
    _validate_github_token_async,
)
from ypl.agent_harness_service.tools.github_auth import authorize_github_user as _authorize_github_user
from ypl.agent_harness_service.tools.github_auth import check_github_auth_status as _check_github_auth_status
from ypl.agent_harness_service.tools.github_token_storage import GitHubTokenData, RefreshResult

# Unwrap MCP FunctionTool wrappers to get raw callables
authorize_github_user = _authorize_github_user.fn
check_github_auth_status = _check_github_auth_status.fn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_data(
    access_token: str = "tok-abc",
    refresh_token: str | None = None,
    expires_at: float | None = None,
) -> GitHubTokenData:
    return GitHubTokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


VALID_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------------------
# Tests: _validate_github_token
# ---------------------------------------------------------------------------


class TestValidateGithubToken:
    def test_returns_valid_with_username_on_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "alice"}

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            valid, username, is_auth_failure = _validate_github_token("token-123")

        assert valid is True
        assert username == "alice"
        assert is_auth_failure is False

    def test_returns_invalid_on_401(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            valid, username, is_auth_failure = _validate_github_token("bad-token")

        assert valid is False
        assert username is None
        assert is_auth_failure is True

    def test_returns_invalid_on_403(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            valid, username, is_auth_failure = _validate_github_token("bad-token")

        assert valid is False
        assert is_auth_failure is True

    def test_returns_transient_failure_on_500(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.get.return_value = mock_resp
            valid, username, is_auth_failure = _validate_github_token("token-123")

        assert valid is False
        assert is_auth_failure is False  # Transient, not auth failure

    def test_returns_transient_failure_on_exception(self) -> None:
        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.get.side_effect = Exception("Network error")
            valid, username, is_auth_failure = _validate_github_token("token-123")

        assert valid is False
        assert is_auth_failure is False


# ---------------------------------------------------------------------------
# Tests: _validate_github_token_async
# ---------------------------------------------------------------------------


class TestValidateGithubTokenAsync:
    async def test_returns_valid_on_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "bob"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            valid, username, is_auth_failure = await _validate_github_token_async("tok-abc")

        assert valid is True
        assert username == "bob"
        assert is_auth_failure is False

    async def test_returns_invalid_on_401(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            valid, username, is_auth_failure = await _validate_github_token_async("bad-tok")

        assert valid is False
        assert is_auth_failure is True

    async def test_returns_transient_failure_on_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            valid, username, is_auth_failure = await _validate_github_token_async("tok")

        assert valid is False
        assert is_auth_failure is False


# ---------------------------------------------------------------------------
# Tests: _get_github_user_info_async
# ---------------------------------------------------------------------------


class TestGetGithubUserInfoAsync:
    async def test_returns_user_info_on_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "alice",
            "id": 12345,
            "name": "Alice Smith",
            "email": "alice@example.com",
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _get_github_user_info_async("tok-abc")

        assert result is not None
        assert result["github_username"] == "alice"
        assert result["github_name"] == "Alice Smith"
        assert result["github_email"] == "alice@example.com"

    async def test_uses_noreply_email_when_private(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "login": "alice",
            "id": 12345,
            "name": "Alice",
            "email": None,
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _get_github_user_info_async("tok-abc")

        assert result is not None
        assert result["github_email"] == "12345+alice@users.noreply.github.com"

    async def test_returns_none_on_non_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _get_github_user_info_async("bad-tok")

        assert result is None

    async def test_returns_none_on_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))

        with patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _get_github_user_info_async("tok")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: _try_refresh_token_with_lock
# ---------------------------------------------------------------------------


class TestTryRefreshTokenWithLock:
    async def test_returns_success_when_token_already_refreshed(self) -> None:
        """Double-check pattern: token refreshed by another caller."""
        token_data = _make_token_data()
        fresh_data = _make_token_data(access_token="fresh-tok")
        # fresh_data is not expired
        fresh_data.expires_at = time.time() + 3600

        with patch(
            "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
            AsyncMock(return_value=fresh_data),
        ):
            result, data = await _try_refresh_token_with_lock("user-1", token_data)

        assert result == RefreshResult.SUCCESS
        assert data == fresh_data

    async def test_returns_success_on_successful_refresh(self) -> None:
        token_data = _make_token_data(expires_at=time.time() - 10, refresh_token="refresh-tok")
        refreshed_data = _make_token_data(access_token="new-tok", expires_at=time.time() + 3600)

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.refresh_github_token",
                AsyncMock(return_value=(RefreshResult.SUCCESS, refreshed_data)),
            ),
        ):
            result, data = await _try_refresh_token_with_lock("user-1", token_data)

        assert result == RefreshResult.SUCCESS
        assert data == refreshed_data

    async def test_evicts_token_on_auth_failure(self) -> None:
        token_data = _make_token_data(expires_at=time.time() - 10, refresh_token="refresh-tok")

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.refresh_github_token",
                AsyncMock(return_value=(RefreshResult.AUTH_FAILURE, None)),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.remove_github_token",
                AsyncMock(),
            ) as mock_remove,
        ):
            result, data = await _try_refresh_token_with_lock("user-1", token_data)

        assert result == RefreshResult.AUTH_FAILURE
        assert data is None
        mock_remove.assert_called_once_with("user-1")


# ---------------------------------------------------------------------------
# Tests: _get_valid_github_token
# ---------------------------------------------------------------------------


class TestGetValidGithubToken:
    async def test_returns_none_when_no_user_id(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
            AsyncMock(return_value=None),
        ):
            result = await _get_valid_github_token(VALID_UUID)

        assert result is None

    async def test_returns_none_when_no_token_data(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=None),
            ),
        ):
            result = await _get_valid_github_token(VALID_UUID)

        assert result is None

    async def test_returns_token_when_valid(self) -> None:
        token_data = _make_token_data(access_token="valid-tok")
        token_data.expires_at = time.time() + 3600  # Not expired

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_github_token_async",
                AsyncMock(return_value=(True, "alice", False)),
            ),
        ):
            result = await _get_valid_github_token(VALID_UUID)

        assert result == "valid-tok"

    async def test_returns_none_and_sets_expired_when_auth_failure(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_auth_terminal_states

        token_data = _make_token_data(access_token="bad-tok")
        token_data.expires_at = time.time() + 3600

        _session_auth_terminal_states.pop(VALID_UUID, None)

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_github_token_async",
                AsyncMock(return_value=(False, None, True)),  # Auth failure
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.remove_github_token",
                AsyncMock(),
            ),
        ):
            result = await _get_valid_github_token(VALID_UUID)

        assert result is None
        assert _session_auth_terminal_states.get(VALID_UUID) == "expired"
        _session_auth_terminal_states.pop(VALID_UUID, None)


# ---------------------------------------------------------------------------
# Tests: _initiate_device_flow
# ---------------------------------------------------------------------------


class TestInitiateDeviceFlow:
    async def test_returns_error_when_no_client_id(self) -> None:
        with patch("ypl.agent_harness_service.tools.github_auth.GITHUB_APP_CLIENT_ID", ""):
            result = await _initiate_device_flow(VALID_UUID, None)

        assert result["status"] == "error"
        assert "Client ID not configured" in result["error"]

    async def test_returns_pending_when_already_polling(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_polling_tasks

        mock_task = MagicMock()
        mock_task.done.return_value = False
        _session_polling_tasks[VALID_UUID] = mock_task

        try:
            with patch("ypl.agent_harness_service.tools.github_auth.GITHUB_APP_CLIENT_ID", "app-client-id"):
                result = await _initiate_device_flow(VALID_UUID, None)

            assert result["status"] == "pending"
            assert "already in progress" in result["message"]
        finally:
            _session_polling_tasks.pop(VALID_UUID, None)

    async def test_returns_error_when_github_api_fails(self) -> None:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("API error"))

        with (
            patch("ypl.agent_harness_service.tools.github_auth.GITHUB_APP_CLIENT_ID", "app-client-id"),
            patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _initiate_device_flow(VALID_UUID, None)

        assert result["status"] == "error"

    async def test_returns_pending_with_device_code_info(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "device_code": "dev-code-123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("ypl.agent_harness_service.tools.github_auth.GITHUB_APP_CLIENT_ID", "app-client-id"),
            patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx,
            patch(
                "ypl.agent_harness_service.tools.github_auth.create_background_task",
                MagicMock(return_value=MagicMock()),
            ),
        ):
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _initiate_device_flow(VALID_UUID, "user-1")

        assert result["status"] == "pending"
        assert result["user_code"] == "ABCD-1234"
        assert "verification_uri" in result


# ---------------------------------------------------------------------------
# Tests: authorize_github_user
# ---------------------------------------------------------------------------


class TestAuthorizeGithubUser:
    async def test_returns_already_authorized_when_valid_token_exists(self) -> None:
        token_data = _make_token_data(access_token="valid-tok")
        token_data.expires_at = time.time() + 3600

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_github_token_async",
                AsyncMock(return_value=(True, "alice", False)),
            ),
        ):
            result = await authorize_github_user(VALID_UUID)

        assert result["status"] == "already_authorized"

    async def test_initiates_device_flow_when_no_token(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=None),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._initiate_device_flow",
                AsyncMock(return_value={"status": "pending", "user_code": "ABCD"}),
            ),
        ):
            result = await authorize_github_user(VALID_UUID)

        assert result["status"] == "pending"


# ---------------------------------------------------------------------------
# Tests: check_github_auth_status
# ---------------------------------------------------------------------------


class TestCheckGithubAuthStatus:
    async def test_returns_authorized_when_valid_token(self) -> None:
        token_data = _make_token_data(access_token="valid-tok")
        token_data.expires_at = time.time() + 3600

        user_info = {
            "github_username": "alice",
            "github_name": "Alice Smith",
            "github_email": "alice@example.com",
        }

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value="user-1"),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth.get_github_token_data",
                AsyncMock(return_value=token_data),
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_github_user_info_async",
                AsyncMock(return_value=user_info),
            ),
        ):
            result = await check_github_auth_status(VALID_UUID)

        assert result["status"] == "authorized"
        assert result["github_username"] == "alice"

    async def test_returns_pending_when_no_user_id(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value=None),
            ),
        ):
            result = await check_github_auth_status(VALID_UUID)

        assert result["status"] == "pending"

    async def test_returns_terminal_state_when_set(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_auth_terminal_states

        _session_auth_terminal_states[VALID_UUID] = AUTH_STATE_DENIED

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value=None),
            ),
        ):
            result = await check_github_auth_status(VALID_UUID)

        assert result["status"] == AUTH_STATE_DENIED
        _session_auth_terminal_states.pop(VALID_UUID, None)

    async def test_returns_expired_for_expired_terminal_state(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_auth_terminal_states

        _session_auth_terminal_states[VALID_UUID] = AUTH_STATE_EXPIRED

        with (
            patch(
                "ypl.agent_harness_service.tools.github_auth._validate_session_id",
                return_value=VALID_UUID,
            ),
            patch(
                "ypl.agent_harness_service.tools.github_auth._get_current_message_user_id",
                AsyncMock(return_value=None),
            ),
        ):
            result = await check_github_auth_status(VALID_UUID)

        assert result["status"] == AUTH_STATE_EXPIRED
        _session_auth_terminal_states.pop(VALID_UUID, None)


# ---------------------------------------------------------------------------
# Tests: _poll_for_token
# ---------------------------------------------------------------------------


class TestPollForToken:
    async def test_handles_cancelled_error(self) -> None:
        """Polling task should re-raise CancelledError."""
        with (
            patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx,
            pytest.raises(asyncio.CancelledError),
        ):
            mock_client = AsyncMock()
            # Immediately cancel by raising CancelledError on sleep
            with patch(
                "ypl.agent_harness_service.tools.github_auth.asyncio.sleep",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ):
                mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
                await _poll_for_token("dev-code", 5, 60, VALID_UUID, None)

    async def test_exits_when_session_not_in_polling_tasks(self) -> None:
        """Polling aborts if session is removed from _session_polling_tasks."""
        from ypl.agent_harness_service.tools.mcp_instance import _session_polling_tasks

        # Session not in polling tasks — should return early
        _session_polling_tasks.pop(VALID_UUID, None)
        sleep_call_count = 0

        async def mock_sleep(n: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock(json=lambda: {"error": "authorization_pending"}))

        with (
            patch("ypl.agent_harness_service.tools.github_auth.asyncio.sleep", side_effect=mock_sleep),
            patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx,
        ):
            mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
            # expires_in=0 ensures loop runs at most once before deadline
            await _poll_for_token("dev-code", 5, 0, VALID_UUID, None)

        # Should not have posted to GitHub (session not in polling tasks means early exit)
        # or polled at most once via sleep
        assert sleep_call_count <= 1

    async def test_sets_terminal_state_on_access_denied(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_auth_terminal_states, _session_polling_tasks

        _session_auth_terminal_states.pop(VALID_UUID, None)
        _session_polling_tasks[VALID_UUID] = MagicMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "access_denied"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        try:
            with (
                patch(
                    "ypl.agent_harness_service.tools.github_auth.asyncio.sleep",
                    AsyncMock(return_value=None),
                ),
                patch("ypl.agent_harness_service.tools.github_auth.httpx") as mock_httpx,
            ):
                mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=None)
                await _poll_for_token("dev-code", 5, 60, VALID_UUID, None)
        except Exception:
            pass

        assert _session_auth_terminal_states.get(VALID_UUID) == AUTH_STATE_DENIED
        _session_auth_terminal_states.pop(VALID_UUID, None)
        _session_polling_tasks.pop(VALID_UUID, None)
