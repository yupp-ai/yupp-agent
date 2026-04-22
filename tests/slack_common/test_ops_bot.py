"""Tests for ypl/slack_common/ops_bot.py.

Covers:
- Client singletons: token env checks, lazy creation, reuse.
- ``resolve_display_name``: cache hit, display_name / real_name fallback,
  SlackApiError fallback to raw user_id (and not cached), ``ok=False`` handling.
- ``agent_has_slack_presence``: encrypted token path, env-var fallback path,
  missing row, empty agent_name.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ypl.slack_common.ops_bot as ops_bot
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from ypl.slack_common import agent_has_slack_presence, resolve_display_name


def _reset_singletons() -> None:
    ops_bot._ops_bot_write_client = None
    ops_bot._ops_bot_user_client = None
    ops_bot._user_name_cache.clear()


# ---------------------------------------------------------------------------
# Client singletons
# ---------------------------------------------------------------------------


class TestClientSingletons:
    def setup_method(self) -> None:
        _reset_singletons()

    def teardown_method(self) -> None:
        _reset_singletons()

    def test_write_client_raises_without_token(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="SLACK_MCP_SERVER_APP_BOT_TOKEN"),
        ):
            ops_bot.get_ops_bot_write_client()

    def test_user_client_raises_without_token(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="SLACK_MCP_SERVER_APP_USER_TOKEN"),
        ):
            ops_bot.get_ops_bot_user_client()

    def test_write_client_created_with_token(self) -> None:
        with patch.dict("os.environ", {"SLACK_MCP_SERVER_APP_BOT_TOKEN": "xoxb-test"}):
            client = ops_bot.get_ops_bot_write_client()
            assert isinstance(client, AsyncWebClient)

    def test_user_client_created_with_token(self) -> None:
        with patch.dict("os.environ", {"SLACK_MCP_SERVER_APP_USER_TOKEN": "xoxp-test"}):
            client = ops_bot.get_ops_bot_user_client()
            assert isinstance(client, AsyncWebClient)

    def test_singletons_are_reused(self) -> None:
        with patch.dict(
            "os.environ",
            {"SLACK_MCP_SERVER_APP_BOT_TOKEN": "xoxb-test", "SLACK_MCP_SERVER_APP_USER_TOKEN": "xoxp-test"},
        ):
            assert ops_bot.get_ops_bot_write_client() is ops_bot.get_ops_bot_write_client()
            assert ops_bot.get_ops_bot_user_client() is ops_bot.get_ops_bot_user_client()

    def test_read_alias_matches_user_client(self) -> None:
        assert ops_bot.get_ops_bot_read_client is ops_bot.get_ops_bot_user_client


# ---------------------------------------------------------------------------
# resolve_display_name
# ---------------------------------------------------------------------------


class TestResolveDisplayName:
    def setup_method(self) -> None:
        _reset_singletons()

    def teardown_method(self) -> None:
        _reset_singletons()

    async def test_cache_hit_skips_api(self) -> None:
        ops_bot._user_name_cache["U_HIT"] = "Cached Alice"

        mock_client = AsyncMock()
        with patch.object(ops_bot, "get_ops_bot_write_client", return_value=mock_client):
            assert await resolve_display_name("U_HIT") == "Cached Alice"
        mock_client.users_info.assert_not_called()

    async def test_display_name_wins_when_present(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {
                    "profile": {"display_name": "Bob", "real_name": "Robert"},
                    "real_name": "Robert",
                    "name": "bobby",
                },
            }
        )
        with patch.object(ops_bot, "get_ops_bot_write_client", return_value=mock_client):
            assert await resolve_display_name("U_BOB") == "Bob"
        assert ops_bot._user_name_cache["U_BOB"] == "Bob"

    async def test_falls_back_to_real_name_when_display_empty(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {
                    "profile": {"display_name": "", "real_name": "Carol Jones"},
                    "real_name": "Carol Jones",
                    "name": "cjones",
                },
            }
        )
        with patch.object(ops_bot, "get_ops_bot_write_client", return_value=mock_client):
            assert await resolve_display_name("U_CAROL") == "Carol Jones"

    async def test_api_error_returns_raw_id_and_skips_cache(self) -> None:
        mock_client = AsyncMock()
        response = MagicMock()
        response.get = MagicMock(side_effect=lambda key, default=None: "user_not_found" if key == "error" else default)
        mock_client.users_info = AsyncMock(
            side_effect=SlackApiError(message="user_not_found", response=response)  # type: ignore[no-untyped-call]
        )
        with patch.object(ops_bot, "get_ops_bot_write_client", return_value=mock_client):
            assert await resolve_display_name("U_MISSING") == "U_MISSING"
        assert "U_MISSING" not in ops_bot._user_name_cache

    async def test_ok_false_returns_raw_id(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(return_value={"ok": False})
        with patch.object(ops_bot, "get_ops_bot_write_client", return_value=mock_client):
            assert await resolve_display_name("U_NOPE") == "U_NOPE"


# ---------------------------------------------------------------------------
# agent_has_slack_presence
# ---------------------------------------------------------------------------


def _make_db_with_row(row: tuple | None) -> AsyncMock:
    """Create a mock AsyncSession whose execute().fetchone() returns ``row``."""
    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.fetchone = MagicMock(return_value=row)
    db.execute = AsyncMock(return_value=exec_result)
    return db


class TestAgentHasSlackPresence:
    async def test_empty_agent_name_returns_false(self) -> None:
        db = _make_db_with_row(None)
        assert await agent_has_slack_presence(db, None) is False
        assert await agent_has_slack_presence(db, "") is False
        db.execute.assert_not_called()

    async def test_missing_row_returns_false(self) -> None:
        db = _make_db_with_row(None)
        assert await agent_has_slack_presence(db, "no-such-agent") is False

    async def test_encrypted_token_present_returns_true(self) -> None:
        # (bot_name, bot_token_encrypted)
        db = _make_db_with_row(("Raccoon", "<fernet-blob>"))
        assert await agent_has_slack_presence(db, "eng-raccoon") is True

    async def test_no_encrypted_token_but_env_fallback_returns_true(self) -> None:
        db = _make_db_with_row(("Raccoon", None))
        with patch.dict("os.environ", {"SLACK_AGENT_GATEWAY_RACCOON_BOT_TOKEN": "xoxb-raccoon"}):
            assert await agent_has_slack_presence(db, "eng-raccoon") is True

    async def test_no_encrypted_token_and_no_env_fallback_returns_false(self) -> None:
        db = _make_db_with_row(("Raccoon", None))
        # Clear the fallback env var to isolate this case
        with patch.dict("os.environ", {}, clear=True):
            assert await agent_has_slack_presence(db, "eng-raccoon") is False
