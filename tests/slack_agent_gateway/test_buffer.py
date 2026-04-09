"""Unit tests for ypl/slack_agent_gateway/buffer.py.

Tests UTF-16 counting, message truncation, and flush logic.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.slack_agent_gateway.buffer import (
    _find_slack_truncation_point,
    _slack_len,
    append_to_reply,
    discard_buffer,
    flush_buffer,
    process_due_flushes,
    process_due_status_flushes,
)
from ypl.slack_agent_gateway.types import AppendToReplyRequest

# ---------------------------------------------------------------------------
# Tests: _slack_len (basic - extended coverage)
# ---------------------------------------------------------------------------


class TestSlackLen:
    def test_ascii_single_char(self) -> None:
        assert _slack_len("a") == 1

    def test_emoji_single(self) -> None:
        # 😀 is U+1F600, outside BMP → 2 UTF-16 units
        assert _slack_len("😀") == 2

    def test_null_char(self) -> None:
        assert _slack_len("\x00") == 1

    def test_mixed_bmp_and_astral(self) -> None:
        # "A😀B" = 1 + 2 + 1 = 4
        assert _slack_len("A😀B") == 4

    def test_long_ascii(self) -> None:
        text = "a" * 1000
        assert _slack_len(text) == 1000

    def test_cjk_char_in_bmp(self) -> None:
        # 你 is U+4F60, within BMP
        assert _slack_len("你") == 1

    def test_mathematical_symbols_in_bmp(self) -> None:
        # ∑ is U+2211, within BMP
        assert _slack_len("∑") == 1

    def test_astral_plane_math(self) -> None:
        # 𝕳 is U+1D573, outside BMP
        assert _slack_len("𝕳") == 2


# ---------------------------------------------------------------------------
# Tests: _find_slack_truncation_point (extended coverage)
# ---------------------------------------------------------------------------


class TestFindSlackTruncationPoint:
    def test_empty_at_zero_limit(self) -> None:
        assert _find_slack_truncation_point("", 0) == 0

    def test_single_emoji_fits(self) -> None:
        assert _find_slack_truncation_point("😀", 2) == 1

    def test_single_emoji_does_not_fit(self) -> None:
        assert _find_slack_truncation_point("😀", 1) == 0

    def test_all_chars_fit(self) -> None:
        text = "hello world"
        assert _find_slack_truncation_point(text, 100) == len(text)

    def test_large_limit_all_fit(self) -> None:
        text = "A" * 500 + "😀" * 100
        limit = _slack_len(text)
        assert _find_slack_truncation_point(text, limit) == len(text)


# ---------------------------------------------------------------------------
# Tests: append_to_reply
# ---------------------------------------------------------------------------


class TestAppendToReply:
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch(
            "ypl.slack_agent_gateway.buffer.get_session",
            AsyncMock(return_value=None),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="hello")
            result = await append_to_reply(req)

        assert result.success is False
        assert "Session not found" in (result.error or "")

    async def test_returns_error_when_no_last_reply_ts(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = None
        mock_session.last_reply_type = None

        with patch(
            "ypl.slack_agent_gateway.buffer.get_session",
            AsyncMock(return_value=mock_session),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="hello")
            result = await append_to_reply(req)

        assert result.success is False
        assert "No previous reply" in (result.error or "")

    async def test_buffers_text_successfully(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "text"

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=5)),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", AsyncMock(return_value=10)),
            patch("ypl.slack_agent_gateway.buffer.set_buffer_type", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.schedule_flush", AsyncMock()),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="hello", reply_type="text")
            result = await append_to_reply(req)

        assert result.success is True
        assert result.buffered is True

    async def test_force_flushes_when_buffer_too_large(self) -> None:
        from ypl.slack_agent_gateway.constants import MAX_BUFFER_SIZE_CHARS

        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "text"

        # Buffer size is at or above max
        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=MAX_BUFFER_SIZE_CHARS)),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", AsyncMock(return_value=MAX_BUFFER_SIZE_CHARS)),
            patch("ypl.slack_agent_gateway.buffer.set_buffer_type", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.schedule_flush", AsyncMock()),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=True),
            ) as mock_flush,
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="x" * 100, reply_type="text")
            result = await append_to_reply(req)

        mock_flush.assert_called_once_with("sess-1")
        assert result.success is True
        assert result.buffered is False

    async def test_flushes_on_type_change(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "text"

        mock_add_result = MagicMock()
        mock_add_result.success = True
        mock_add_result.error = None
        mock_add_result.message_ts = "67890.0"

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=0)),
            # Type change from "text" to "thinking"
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=True),
            ),
            patch(
                "ypl.slack_agent_gateway.callbacks.add_reply",
                AsyncMock(return_value=mock_add_result),
            ),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="thinking...", reply_type="thinking")
            result = await append_to_reply(req)

        # Result depends on add_reply success
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: flush_buffer
# ---------------------------------------------------------------------------


class TestFlushBuffer:
    async def test_returns_false_when_session_not_found(self) -> None:
        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=None)),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
        ):
            result = await flush_buffer("sess-missing")

        assert result is False

    async def test_returns_false_when_no_reply_ts(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = None

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
        ):
            result = await flush_buffer("sess-1")

        assert result is False

    async def test_returns_true_when_buffer_empty(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="")),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer_type", AsyncMock()),
        ):
            result = await flush_buffer("sess-1")

        assert result is True

    async def test_returns_false_when_no_app_config(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.app_id = "A123"
        mock_session.last_reply_content = ""

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="new content")),
            patch("ypl.slack_agent_gateway.buffer.get_agent_config_by_app_id", AsyncMock(return_value=None)),
        ):
            result = await flush_buffer("sess-1")

        assert result is False

    async def test_flushes_successfully(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.channel_id = "C123"
        mock_session.app_id = "A123"
        mock_session.last_reply_content = "existing"

        mock_app_config = MagicMock()
        mock_app_config.bot_token = "bot-token-123"

        mock_slack_client = AsyncMock()
        mock_slack_client.chat_update = AsyncMock()

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value=" new content")),
            patch(
                "ypl.slack_agent_gateway.buffer.get_agent_config_by_app_id",
                AsyncMock(return_value=mock_app_config),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.AsyncWebClient",
                return_value=mock_slack_client,
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.render_reply_blocks",
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.buffer.record_reply", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=0)),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer_type", AsyncMock()),
        ):
            result = await flush_buffer("sess-1")

        assert result is True
        mock_slack_client.chat_update.assert_called_once()

    async def test_returns_false_on_slack_exception(self) -> None:
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.channel_id = "C123"
        mock_session.app_id = "A123"
        mock_session.last_reply_content = ""

        mock_app_config = MagicMock()
        mock_app_config.bot_token = "bot-token-123"

        mock_slack_client = AsyncMock()
        mock_slack_client.chat_update = AsyncMock(side_effect=Exception("Slack API error"))

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="content")),
            patch(
                "ypl.slack_agent_gateway.buffer.get_agent_config_by_app_id",
                AsyncMock(return_value=mock_app_config),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.AsyncWebClient",
                return_value=mock_slack_client,
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.render_reply_blocks",
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", AsyncMock(return_value=10)),
        ):
            result = await flush_buffer("sess-1")

        assert result is False


# ---------------------------------------------------------------------------
# Tests: discard_buffer
# ---------------------------------------------------------------------------


class TestDiscardBuffer:
    async def test_discards_buffer_content(self) -> None:
        with (
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="discarded content")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer_type", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
        ):
            result = await discard_buffer("sess-1")

        assert result == "discarded content"

    async def test_returns_empty_when_no_buffer(self) -> None:
        with (
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer_type", AsyncMock()),
            patch("ypl.slack_agent_gateway.buffer.remove_from_flush_schedule", AsyncMock()),
        ):
            result = await discard_buffer("sess-1")

        assert result == ""


# ---------------------------------------------------------------------------
# Tests: process_due_flushes
# ---------------------------------------------------------------------------


class TestProcessDueFlushes:
    async def test_returns_zero_when_no_due_sessions(self) -> None:
        with patch("ypl.slack_agent_gateway.buffer.get_due_flushes", AsyncMock(return_value=[])):
            result = await process_due_flushes()

        assert result == 0

    async def test_flushes_due_sessions(self) -> None:
        with (
            patch(
                "ypl.slack_agent_gateway.buffer.get_due_flushes",
                AsyncMock(return_value=["sess-1", "sess-2"]),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=True),
            ) as mock_flush,
        ):
            result = await process_due_flushes()

        assert result == 2
        assert mock_flush.call_count == 2

    async def test_continues_on_flush_exception(self) -> None:
        with (
            patch(
                "ypl.slack_agent_gateway.buffer.get_due_flushes",
                AsyncMock(return_value=["sess-fail", "sess-ok"]),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(side_effect=[Exception("Oops"), True]),
            ),
        ):
            result = await process_due_flushes()

        assert result == 1  # Only sess-ok succeeded


# ---------------------------------------------------------------------------
# Tests: process_due_status_flushes
# ---------------------------------------------------------------------------


class TestProcessDueStatusFlushes:
    async def test_returns_zero_when_no_due_sessions(self) -> None:
        with patch("ypl.slack_agent_gateway.buffer.get_due_status_flushes", AsyncMock(return_value=[])):
            result = await process_due_status_flushes()

        assert result == 0

    async def test_flushes_status_when_ratelimit_acquired(self) -> None:
        mock_flush_result = MagicMock()
        mock_flush_result.success = True

        with (
            patch(
                "ypl.slack_agent_gateway.buffer.get_due_status_flushes",
                AsyncMock(return_value=["sess-1"]),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.try_acquire_status_ratelimit",
                AsyncMock(return_value=True),
            ),
            patch(
                "ypl.slack_agent_gateway.callbacks.flush_status_update",
                AsyncMock(return_value=mock_flush_result),
            ),
        ):
            result = await process_due_status_flushes()

        assert result == 1

    async def test_reschedules_when_ratelimit_not_acquired(self) -> None:
        with (
            patch(
                "ypl.slack_agent_gateway.buffer.get_due_status_flushes",
                AsyncMock(return_value=["sess-1"]),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.try_acquire_status_ratelimit",
                AsyncMock(return_value=False),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.schedule_status_flush",
                AsyncMock(),
            ) as mock_reschedule,
        ):
            result = await process_due_status_flushes()

        assert result == 0
        mock_reschedule.assert_called_once()

    async def test_removes_stale_session_from_schedule(self) -> None:
        mock_flush_result = MagicMock()
        mock_flush_result.success = False
        mock_flush_result.error = "Session not found"

        with (
            patch(
                "ypl.slack_agent_gateway.buffer.get_due_status_flushes",
                AsyncMock(return_value=["sess-expired"]),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.try_acquire_status_ratelimit",
                AsyncMock(return_value=True),
            ),
            patch(
                "ypl.slack_agent_gateway.callbacks.flush_status_update",
                AsyncMock(return_value=mock_flush_result),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.remove_from_status_flush_schedule",
                AsyncMock(),
            ) as mock_remove,
        ):
            result = await process_due_status_flushes()

        assert result == 0
        mock_remove.assert_called_once_with("sess-expired")
