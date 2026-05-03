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

    # ---- Type-switch flush-failure regression tests ---------------------
    # These cover the path that previously dropped the agent's final summary
    # when the pre-switch flush failed (e.g. Slack rate-limit gate held).
    # See buffer.py ``if not flushed:`` branch inside append_to_reply.

    async def test_type_switch_flush_failure_still_delivers_new_text(self) -> None:
        """Pre-switch flush fails with empty buffer: incoming text must still be posted.

        Regression for the data-loss path where append_to_reply returned
        ``success=False, buffered=False`` and silently dropped the incoming
        new-type text. After the fix it must call add_reply for the new text
        and return its success.
        """
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "thinking"

        mock_add_result = MagicMock()
        mock_add_result.success = True
        mock_add_result.error = None
        mock_add_result.message_ts = "67890.0"

        add_reply_mock = AsyncMock(return_value=mock_add_result)

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            # Buffer is empty — ``effective_current_type`` falls back to
            # ``session.last_reply_type = "thinking"``, which differs from
            # the incoming ``reply_type=None``, triggering the type switch.
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value=None)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=0)),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=False),  # <- rate-limit gate held
            ),
            # No pending buffer to rescue; discard returns empty.
            patch(
                "ypl.slack_agent_gateway.buffer.discard_buffer",
                AsyncMock(return_value=""),
            ),
            patch("ypl.slack_agent_gateway.callbacks.add_reply", add_reply_mock),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="final summary", reply_type=None)
            result = await append_to_reply(req)

        # The new-type summary must not be silently dropped.
        assert result.success is True
        assert result.buffered is False
        # add_reply was called exactly once — for the new-type payload.
        assert add_reply_mock.await_count == 1
        call_args = add_reply_mock.await_args_list[0].args[0]
        assert call_args.text == "final summary"
        assert call_args.reply_type is None

    async def test_type_switch_flush_failure_rescues_pending_old_type_content(self) -> None:
        """Pre-switch flush fails with pending content: rescue via add_reply.

        The pending old-type content must not be abandoned in the buffer
        (where a later flush would stamp it onto the new-type message and
        corrupt it). The fix discards the buffer and re-posts its content as
        a new message of the OLD type, then posts the incoming new-type text.
        """
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "thinking"

        mock_rescue_result = MagicMock()
        mock_rescue_result.success = True
        mock_rescue_result.error = None
        mock_rescue_result.message_ts = "old-rescue-ts"

        mock_new_result = MagicMock()
        mock_new_result.success = True
        mock_new_result.error = None
        mock_new_result.message_ts = "new-ts"

        add_reply_mock = AsyncMock(side_effect=[mock_rescue_result, mock_new_result])
        discard_mock = AsyncMock(return_value="pending thinking chunk")

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            # Buffer currently holds "thinking" content.
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="thinking")),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=42)),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=False),  # <- rate-limit gate held
            ),
            patch("ypl.slack_agent_gateway.buffer.discard_buffer", discard_mock),
            patch("ypl.slack_agent_gateway.callbacks.add_reply", add_reply_mock),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="final summary", reply_type=None)
            result = await append_to_reply(req)

        # Overall call succeeded (result mirrors the new-type add_reply).
        assert result.success is True
        assert result.buffered is False

        # Buffer was drained atomically so a later scheduled flush cannot
        # stamp the pending "thinking" content onto the new-type message.
        discard_mock.assert_awaited_once_with("sess-1")

        # add_reply was called TWICE: once to rescue the pending old-type
        # content, once to post the new-type summary.
        assert add_reply_mock.await_count == 2

        rescue_call = add_reply_mock.await_args_list[0].args[0]
        assert rescue_call.text == "pending thinking chunk"
        assert rescue_call.reply_type == "thinking"

        new_call = add_reply_mock.await_args_list[1].args[0]
        assert new_call.text == "final summary"
        assert new_call.reply_type is None

    async def test_type_switch_flush_failure_continues_when_rescue_fails(self) -> None:
        """If the rescue add_reply fails, still try to deliver the new-type text.

        Dropping the rescued old-type chunk is bad, but dropping the incoming
        new-type text on top of that is strictly worse. We keep going.
        """
        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.last_reply_type = "thinking"

        mock_rescue_result = MagicMock()
        mock_rescue_result.success = False
        mock_rescue_result.error = "Slack unreachable"
        mock_rescue_result.message_ts = None

        mock_new_result = MagicMock()
        mock_new_result.success = True
        mock_new_result.error = None
        mock_new_result.message_ts = "new-ts"

        add_reply_mock = AsyncMock(side_effect=[mock_rescue_result, mock_new_result])

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="thinking")),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=42)),
            patch(
                "ypl.slack_agent_gateway.buffer.flush_buffer",
                AsyncMock(return_value=False),
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.discard_buffer",
                AsyncMock(return_value="pending thinking chunk"),
            ),
            patch("ypl.slack_agent_gateway.callbacks.add_reply", add_reply_mock),
        ):
            req = AppendToReplyRequest(session_id="sess-1", text="final summary", reply_type=None)
            result = await append_to_reply(req)

        # The new-type summary still made it out.
        assert result.success is True
        # Both add_reply calls attempted — rescue first, new-type second.
        assert add_reply_mock.await_count == 2
        assert add_reply_mock.await_args_list[1].args[0].text == "final summary"


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
            # Fast-path: empty buffer returns True before any Slack / rate-limit machinery runs.
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=0)),
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
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=5)),
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
                "ypl.slack_agent_gateway.buffer.build_slack_client",
                return_value=mock_slack_client,
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.render_reply_blocks",
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.buffer.record_reply", AsyncMock()),
            # First call (fast-path check) sees content; second (post-flush cleanup) sees empty.
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(side_effect=[5, 0])),
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
                "ypl.slack_agent_gateway.buffer.build_slack_client",
                return_value=mock_slack_client,
            ),
            patch(
                "ypl.slack_agent_gateway.buffer.render_reply_blocks",
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", AsyncMock(return_value=10)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=5)),
        ):
            result = await flush_buffer("sess-1")

        assert result is False

    async def test_rate_limit_deferred_by_wrapper_rebuffers_and_reschedules(self) -> None:
        """When the universal Slack gate is held by another caller, the
        ``RateLimitedSlackClient`` wrapper raises ``RateLimitDeferred`` and
        ``flush_buffer`` re-queues the cleared content + reschedules.  The
        outer rate-limit gate that previously also lived inside ``flush_buffer``
        was removed (it self-deadlocked against the wrapper's gate on the
        same key) — gating now lives entirely in the wrapper."""
        from ypl.slack_agent_gateway.slack_client import RateLimitDeferred

        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.channel_id = "C123"
        mock_session.app_id = "A123"
        mock_session.last_reply_content = ""

        mock_app_config = MagicMock()
        mock_app_config.app_id = "A123"
        mock_app_config.bot_token = "bot-token-123"

        # Wrapper denies the inner acquire → raises RateLimitDeferred(0.0).
        mock_slack_client = AsyncMock()
        mock_slack_client.chat_update = AsyncMock(side_effect=RateLimitDeferred(method="chat_update", retry_after=0.0))

        append = AsyncMock(return_value=10)
        schedule = AsyncMock()

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=5)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="content")),
            patch(
                "ypl.slack_agent_gateway.buffer.get_agent_config_by_app_id",
                AsyncMock(return_value=mock_app_config),
            ),
            patch("ypl.slack_agent_gateway.buffer.build_slack_client", return_value=mock_slack_client),
            patch("ypl.slack_agent_gateway.buffer.render_reply_blocks", return_value=None),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", append),
            patch("ypl.slack_agent_gateway.buffer.schedule_flush", schedule),
        ):
            result = await flush_buffer("sess-1")

        assert result is False
        # Critical: cleared content was re-added so it's not lost.
        append.assert_awaited_once_with("sess-1", "content")
        # And we rescheduled rather than dropping the flush.
        schedule.assert_awaited_once()

    async def test_rate_limit_deferred_on_429_rebufffers_and_honors_retry_after(self) -> None:
        """When chat.update returns 429 despite the gate, the buffer content
        is re-added and the next flush is scheduled at now + Retry-After."""
        from ypl.slack_agent_gateway.slack_client import RateLimitDeferred

        mock_session = MagicMock()
        mock_session.last_reply_ts = "12345.0"
        mock_session.channel_id = "C123"
        mock_session.app_id = "A123"
        mock_session.last_reply_content = ""

        mock_app_config = MagicMock()
        mock_app_config.app_id = "A123"
        mock_app_config.bot_token = "bot-token-123"

        mock_slack_client = AsyncMock()
        mock_slack_client.chat_update = AsyncMock(side_effect=RateLimitDeferred(method="chat_update", retry_after=7.0))

        append = AsyncMock(return_value=10)
        schedule = AsyncMock()

        with (
            patch("ypl.slack_agent_gateway.buffer.get_session", AsyncMock(return_value=mock_session)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_size", AsyncMock(return_value=5)),
            patch("ypl.slack_agent_gateway.buffer.get_buffer_type", AsyncMock(return_value="text")),
            patch("ypl.slack_agent_gateway.buffer.clear_buffer", AsyncMock(return_value="content")),
            patch(
                "ypl.slack_agent_gateway.buffer.get_agent_config_by_app_id",
                AsyncMock(return_value=mock_app_config),
            ),
            patch("ypl.slack_agent_gateway.buffer.build_slack_client", return_value=mock_slack_client),
            patch("ypl.slack_agent_gateway.buffer.render_reply_blocks", return_value=None),
            patch("ypl.slack_agent_gateway.buffer.append_to_buffer", append),
            patch("ypl.slack_agent_gateway.buffer.schedule_flush", schedule),
        ):
            result = await flush_buffer("sess-1")

        assert result is False
        # Re-added the exact content we had cleared — so new appends merge with it.
        append.assert_awaited_once_with("sess-1", "content")
        # Rescheduled (Retry-After honored; exact timestamp is time-dependent so
        # we just check that schedule was called once).
        schedule.assert_awaited_once()


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
