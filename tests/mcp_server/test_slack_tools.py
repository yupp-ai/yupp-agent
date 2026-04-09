"""Unit tests for ypl/mcp_server/tools/slack.py.

Covers:
- _resolve_slack_user: cache hit, successful lookup (display_name / real_name fallback), API error
- read_slack_thread: success (with/without pagination cursor), not_in_channel error,
  channel_not_found error, generic SlackApiError, generic exception, has_more=True
- search_slack: success, ok=False response, generic exception

All tests run without a real Slack API connection.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
import ypl.mcp_server.tools.slack as _slack_mod
from slack_sdk.errors import SlackApiError
from ypl.mcp_server.tools.slack import _get_slack_bot_client, _get_slack_user_client, _resolve_slack_user

read_slack_thread = _slack_mod.read_slack_thread.fn
search_slack = _slack_mod.search_slack.fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slack_error(error_code: str) -> SlackApiError:
    """Build a SlackApiError with the given error code."""
    response = MagicMock()
    response.get = MagicMock(side_effect=lambda key, default=None: error_code if key == "error" else default)
    response.__getitem__ = MagicMock(side_effect=lambda key: error_code if key == "error" else None)
    return SlackApiError(message=f"Slack error: {error_code}", response=response)  # type: ignore[no-untyped-call]


def _make_conversations_replies_response(
    messages: list[dict[str, Any]],
    has_more: bool = False,
    next_cursor: str = "",
) -> MagicMock:
    resp = MagicMock()
    resp.get = MagicMock(
        side_effect=lambda key, default=None: {
            "ok": True,
            "messages": messages,
            "has_more": has_more,
            "response_metadata": {"next_cursor": next_cursor} if next_cursor else {},
        }.get(key, default)
    )
    return resp


# ---------------------------------------------------------------------------
# _resolve_slack_user
# ---------------------------------------------------------------------------


class TestResolveSlackUser:
    async def test_cache_hit_returns_cached_name(self) -> None:
        _slack_mod._slack_user_cache["U12345"] = "Cached Alice"

        client = AsyncMock()
        result = await _resolve_slack_user(client, "U12345")

        assert result == "Cached Alice"
        client.users_info.assert_not_called()

        # cleanup
        del _slack_mod._slack_user_cache["U12345"]

    async def test_successful_lookup_with_display_name(self) -> None:
        # Ensure cache is clear for this user
        _slack_mod._slack_user_cache.pop("U99999", None)

        client = AsyncMock()
        client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {
                    "profile": {"display_name": "Bob Smith", "real_name": "Robert Smith"},
                    "real_name": "Robert Smith",
                    "name": "bsmith",
                },
            }
        )
        result = await _resolve_slack_user(client, "U99999")
        assert result == "Bob Smith"
        assert _slack_mod._slack_user_cache["U99999"] == "Bob Smith"

        # cleanup
        del _slack_mod._slack_user_cache["U99999"]

    async def test_fallback_to_real_name_when_display_empty(self) -> None:
        _slack_mod._slack_user_cache.pop("U88888", None)

        client = AsyncMock()
        client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {
                    "profile": {"display_name": "", "real_name": "Carol Jones"},
                    "real_name": "Carol Jones",
                    "name": "cjones",
                },
            }
        )
        result = await _resolve_slack_user(client, "U88888")
        assert result == "Carol Jones"

        # cleanup
        _slack_mod._slack_user_cache.pop("U88888", None)

    async def test_fallback_to_user_id_on_api_error(self) -> None:
        _slack_mod._slack_user_cache.pop("U77777", None)

        client = AsyncMock()
        client.users_info = AsyncMock(side_effect=_make_slack_error("user_not_found"))

        result = await _resolve_slack_user(client, "U77777")
        # On error, returns raw user_id without caching
        assert result == "U77777"
        assert "U77777" not in _slack_mod._slack_user_cache

    async def test_ok_false_returns_user_id(self) -> None:
        _slack_mod._slack_user_cache.pop("U66666", None)

        client = AsyncMock()
        client.users_info = AsyncMock(return_value={"ok": False})

        result = await _resolve_slack_user(client, "U66666")
        assert result == "U66666"


# ---------------------------------------------------------------------------
# read_slack_thread
# ---------------------------------------------------------------------------


class TestReadSlackThread:
    def _make_messages(self) -> list[dict[str, Any]]:
        return [
            {"user": "U111", "text": "Hello!", "ts": "111.000", "thread_ts": "111.000"},
            {"user": "U222", "text": "World!", "ts": "111.001", "thread_ts": "111.000"},
        ]

    async def test_success_basic(self) -> None:
        messages = self._make_messages()
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=_make_conversations_replies_response(messages))
        mock_client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {
                    "profile": {"display_name": "Alice", "real_name": "Alice"},
                    "real_name": "Alice",
                    "name": "alice",
                },
            }
        )

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            _slack_mod._slack_user_cache.clear()
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is True
        assert result["message_count"] == 2
        assert result["channel"] == "C123"

    async def test_success_with_cursor(self) -> None:
        messages = self._make_messages()
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value=_make_conversations_replies_response(messages, has_more=True, next_cursor="cursor123")
        )
        mock_client.users_info = AsyncMock(return_value={"ok": False})

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            _slack_mod._slack_user_cache.clear()
            result = await read_slack_thread("C123", "111.000", cursor="some_cursor")

        assert result["success"] is True
        assert result["has_more"] is True
        assert result.get("next_cursor") == "cursor123"

    async def test_ok_false_response(self) -> None:
        mock_client = AsyncMock()
        error_resp = MagicMock()
        error_resp.get = MagicMock(
            side_effect=lambda key, default=None: {"ok": False, "error": "some_error"}.get(key, default)
        )
        mock_client.conversations_replies = AsyncMock(return_value=error_resp)

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False

    async def test_not_in_channel_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("not_in_channel"))

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "not_in_channel"
        assert "invite" in result["error"]

    async def test_channel_not_found_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("channel_not_found"))

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            result = await read_slack_thread("C999", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "channel_not_found"

    async def test_generic_slack_api_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("is_archived"))

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "is_archived"

    async def test_generic_exception(self) -> None:
        mock_client = MagicMock()
        mock_client.conversations_replies = AsyncMock(side_effect=RuntimeError("network failure"))

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False
        assert "network failure" in result["error"]

    async def test_message_with_attachments(self) -> None:
        messages = [
            {
                "user": "",
                "username": "EmailBot",
                "text": "",
                "ts": "111.000",
                "thread_ts": "111.000",
                "attachments": [
                    {
                        "pretext": "Pre",
                        "title": "Title",
                        "text": "Body text",
                        "fields": [{"title": "Field", "value": "Val"}],
                    }
                ],
            }
        ]
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=_make_conversations_replies_response(messages))

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            _slack_mod._slack_user_cache.clear()
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is True
        msg = result["messages"][0]
        assert msg["user"] == "EmailBot"
        assert "Body text" in msg.get("attachments_text", "")

    async def test_message_with_blocks(self) -> None:
        messages = [
            {
                "user": "U111",
                "text": "Hello",
                "ts": "111.000",
                "thread_ts": "111.000",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Rich content"}}],
            }
        ]
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=_make_conversations_replies_response(messages))
        mock_client.users_info = AsyncMock(return_value={"ok": False})

        with patch("ypl.mcp_server.tools.slack._get_slack_bot_client", return_value=mock_client):
            _slack_mod._slack_user_cache.clear()
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is True
        assert "blocks" in result["messages"][0]


# ---------------------------------------------------------------------------
# search_slack
# ---------------------------------------------------------------------------


class TestSearchSlack:
    def _make_search_response(self, matches: list[dict[str, Any]], total: int = 0) -> MagicMock:
        resp = MagicMock()
        resp.get = MagicMock(
            side_effect=lambda key, default=None: {
                "ok": True,
                "messages": {"matches": matches, "total": total},
            }.get(key, default)
        )
        return resp

    async def test_success(self) -> None:
        matches = [
            {
                "text": "Hello world",
                "username": "alice",
                "ts": "111.000",
                "channel": {"id": "C123", "name": "general"},
                "permalink": "https://slack.com/archives/C123",
                "thread_ts": None,
            }
        ]
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(return_value=self._make_search_response(matches, total=1))

        with patch("ypl.mcp_server.tools.slack._get_slack_user_client", return_value=mock_client):
            result = await search_slack("hello world")

        assert result["success"] is True
        assert result["total_matches"] == 1
        assert result["result_count"] == 1
        assert result["results"][0]["text"] == "Hello world"
        assert result["results"][0]["channel_name"] == "general"

    async def test_ok_false_response(self) -> None:
        resp = MagicMock()
        resp.get = MagicMock(
            side_effect=lambda key, default=None: {"ok": False, "error": "not_allowed"}.get(key, default)
        )
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(return_value=resp)

        with patch("ypl.mcp_server.tools.slack._get_slack_user_client", return_value=mock_client):
            result = await search_slack("test query")

        assert result["success"] is False

    async def test_generic_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(side_effect=RuntimeError("connection reset"))

        with patch("ypl.mcp_server.tools.slack._get_slack_user_client", return_value=mock_client):
            result = await search_slack("error query")

        assert result["success"] is False
        assert "connection reset" in result["error"]

    async def test_empty_results(self) -> None:
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(return_value=self._make_search_response([], total=0))

        with patch("ypl.mcp_server.tools.slack._get_slack_user_client", return_value=mock_client):
            result = await search_slack("no results query")

        assert result["success"] is True
        assert result["result_count"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# Client initialization helpers
# ---------------------------------------------------------------------------


class TestSlackClientInit:
    def test_bot_client_raises_without_token(self) -> None:
        # Reset singleton
        _slack_mod._slack_bot_client = None
        with patch.dict("os.environ", {}, clear=True):
            import os

            os.environ.pop("SLACK_MCP_SERVER_APP_BOT_TOKEN", None)
            with pytest.raises(ValueError, match="SLACK_MCP_SERVER_APP_BOT_TOKEN"):
                _get_slack_bot_client()

    def test_user_client_raises_without_token(self) -> None:
        _slack_mod._slack_user_client = None
        with patch.dict("os.environ", {}, clear=True):
            import os

            os.environ.pop("SLACK_MCP_SERVER_APP_USER_TOKEN", None)
            with pytest.raises(ValueError, match="SLACK_MCP_SERVER_APP_USER_TOKEN"):
                _get_slack_user_client()

    def test_bot_client_singleton(self) -> None:
        mock_client = MagicMock()
        _slack_mod._slack_bot_client = mock_client

        result = _get_slack_bot_client()
        assert result is mock_client

        # Reset
        _slack_mod._slack_bot_client = None

    def test_user_client_singleton(self) -> None:
        mock_client = MagicMock()
        _slack_mod._slack_user_client = mock_client

        result = _get_slack_user_client()
        assert result is mock_client

        # Reset
        _slack_mod._slack_user_client = None
