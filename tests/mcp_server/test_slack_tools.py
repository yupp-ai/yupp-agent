"""Unit tests for ypl/mcp_server/tools/slack.py.

Covers:
- read_slack_thread: success (with/without pagination cursor), not_in_channel error,
  channel_not_found error, generic SlackApiError, generic exception, has_more=True,
  attachments and blocks passthrough.
- search_slack: success, ok=False response, generic exception, empty results.

OpsBot client + user-resolution helpers live in ``ypl.slack_common.ops_bot`` and
are exercised separately in ``tests/slack_common/test_ops_bot.py``. These tests
patch the imported references inside ``ypl.mcp_server.tools.slack`` so no real
Slack API connection is needed.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
import ypl.mcp_server.tools.slack as _slack_mod
from slack_sdk.errors import SlackApiError

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


def _patch_read(mock_client: AsyncMock | MagicMock) -> Any:
    """Patch the OpsBot user client + display-name resolver for read tests."""
    return (
        patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client),
        patch(
            "ypl.mcp_server.tools.slack.resolve_display_name",
            AsyncMock(side_effect=lambda uid: uid),  # returns raw ID unless overridden per-test
        ),
    )


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
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
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
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
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
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False

    async def test_not_in_channel_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("not_in_channel"))
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "not_in_channel"
        assert "not accessible" in result["error"]

    async def test_channel_not_found_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("channel_not_found"))
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
            result = await read_slack_thread("C999", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "channel_not_found"

    async def test_generic_slack_api_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=_make_slack_error("is_archived"))
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is False
        assert result["slack_error"] == "is_archived"

    async def test_generic_exception(self) -> None:
        mock_client = MagicMock()
        mock_client.conversations_replies = AsyncMock(side_effect=RuntimeError("network failure"))
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
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
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
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
        read_patch, resolver_patch = _patch_read(mock_client)

        with read_patch, resolver_patch:
            result = await read_slack_thread("C123", "111.000")

        assert result["success"] is True
        assert "blocks" in result["messages"][0]

    async def test_display_name_resolver_used(self) -> None:
        messages = [{"user": "U_ALICE", "text": "hi", "ts": "1.0", "thread_ts": "1.0"}]
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value=_make_conversations_replies_response(messages))

        name_map = {"U_ALICE": "Alice"}
        resolver_mock = AsyncMock(side_effect=lambda uid: name_map.get(uid, uid))

        with (
            patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client),
            patch("ypl.mcp_server.tools.slack.resolve_display_name", resolver_mock),
        ):
            result = await read_slack_thread("C123", "1.0")

        assert result["messages"][0]["user"] == "Alice"
        resolver_mock.assert_awaited_once_with("U_ALICE")


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

        with patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client):
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

        with patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client):
            result = await search_slack("test query")

        assert result["success"] is False

    async def test_generic_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(side_effect=RuntimeError("connection reset"))

        with patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client):
            result = await search_slack("error query")

        assert result["success"] is False
        assert "connection reset" in result["error"]

    async def test_empty_results(self) -> None:
        mock_client = AsyncMock()
        mock_client.search_messages = AsyncMock(return_value=self._make_search_response([], total=0))

        with patch("ypl.mcp_server.tools.slack.get_ops_bot_user_client", return_value=mock_client):
            result = await search_slack("no results query")

        assert result["success"] is True
        assert result["result_count"] == 0
        assert result["results"] == []
