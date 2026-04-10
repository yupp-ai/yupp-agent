"""Extended tests for SAG callbacks module — covering previously-uncovered paths.

Covers:
- add_reply: placeholder_ts update path (chat_update), fallback to new post on error
- add_reply: tool cluster summary rendered after real reply
- add_reply: store_reply_mapping failure is non-fatal
- update_reply: success, missing last_reply_ts, Slack error
- send_message: agent name fallback (base prefix), thread registration, Slack error
- request_feedback: already-claimed skip, session-not-found after claim, Slack error
- send_questionnaire: success, session not found, Slack error
- flush_status_update: post new status msg, edit existing, no-op when no pending,
  message_not_found reset, other Slack error
- handle_tool_event: START event, RESULT event, rate-limit deferred path
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from slack_sdk.errors import SlackApiError
from ypl.slack_agent_gateway.callbacks import (
    add_reply,
    flush_status_update,
    handle_tool_event,
    request_feedback,
    send_message,
    send_questionnaire,
    update_reply,
)
from ypl.slack_agent_gateway.types import (
    AddReplyRequest,
    AgentAppConfig,
    AgentSession,
    QuestionChoice,
    RequestFeedbackRequest,
    SendMessageRequest,
    SendQuestionnaireRequest,
    SendToolEventRequest,
    ToolEventKind,
    ToolResultStatus,
    ToolUseEntry,
    UpdateReplyRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "C123:1234567890.000:A001",
    channel_id: str = "C123",
    thread_ts: str = "1234567890.000",
    app_id: str = "A001",
    last_reply_ts: str | None = "1111.000",
    placeholder_ts: str | None = None,
    status_message_ts: str | None = None,
) -> AgentSession:
    now = datetime.now(UTC)
    return AgentSession(
        session_id=session_id,
        channel_id=channel_id,
        channel_name="general",
        thread_ts=thread_ts,
        creator_slack_user_id="U999",
        creator_slack_username="testuser",
        app_id=app_id,
        agent_name="test-agent",
        last_reply_ts=last_reply_ts,
        placeholder_ts=placeholder_ts,
        status_message_ts=status_message_ts,
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(hours=8),
    )


def _make_app_config(app_id: str = "A001") -> AgentAppConfig:
    return AgentAppConfig(
        app_id=app_id,
        agent_name="test-agent",
        slack_name="testbot",
        bot_token="xoxb-test-token",
        signing_secret="test-secret",
        display_name="Test Bot",
    )


def _slack_api_error(code: str = "channel_not_found") -> SlackApiError:
    resp = MagicMock()
    resp.get = lambda key, default=None: code if key == "error" else default
    return SlackApiError(message=code, response=resp)  # type: ignore[no-untyped-call]


def _make_slack_response(ts: str = "9999.000", channel: str = "C123") -> MagicMock:
    resp = MagicMock()
    resp.get = lambda key, default=None: {"ts": ts, "channel": channel}.get(key, default)
    return resp


# ---------------------------------------------------------------------------
# add_reply — placeholder_ts path
# ---------------------------------------------------------------------------

_CALLBACKS_MODULE = "ypl.slack_agent_gateway.callbacks"


class TestAddReplyWithPlaceholder:
    async def test_uses_chat_update_when_placeholder_ts_set(self) -> None:
        session = _make_session(placeholder_ts="OLD_TS")
        app_config = _make_app_config()

        mock_client = AsyncMock()
        mock_client.chat_update.return_value = _make_slack_response(ts="OLD_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.save_session", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.record_reply", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.store_reply_mapping", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.get_tool_entries", new_callable=AsyncMock, return_value=[]),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.clear_tool_entries", new_callable=AsyncMock),
        ):
            req = AddReplyRequest(session_id="C123:1234567890.000:A001", text="Hello!")
            resp = await add_reply(req)

        assert resp.success is True
        assert resp.message_ts == "OLD_TS"
        mock_client.chat_update.assert_awaited_once()

    async def test_falls_back_to_new_post_when_update_fails(self) -> None:
        session = _make_session(placeholder_ts="OLD_TS")
        app_config = _make_app_config()

        mock_client = AsyncMock()
        mock_client.chat_update.side_effect = _slack_api_error("message_not_found")
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="NEW_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.save_session", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.record_reply", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.store_reply_mapping", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.get_tool_entries", new_callable=AsyncMock, return_value=[]),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.clear_tool_entries", new_callable=AsyncMock),
        ):
            req = AddReplyRequest(session_id="C123:1234567890.000:A001", text="Hello!")
            resp = await add_reply(req)

        assert resp.success is True
        assert resp.message_ts == "NEW_TS"

    async def test_returns_error_when_new_post_missing_ts(self) -> None:
        session = _make_session(placeholder_ts="OLD_TS")
        app_config = _make_app_config()

        mock_client = AsyncMock()
        mock_client.chat_update.side_effect = _slack_api_error("message_not_found")
        # Response with empty ts
        resp_mock = MagicMock()
        resp_mock.get = lambda key, default=None: "" if key == "ts" else default
        mock_client.chat_postMessage.return_value = resp_mock

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.save_session", new_callable=AsyncMock),
        ):
            req = AddReplyRequest(session_id="C123:1234567890.000:A001", text="Hello!")
            resp = await add_reply(req)

        assert resp.success is False
        assert "timestamp" in (resp.error or "").lower()

    async def test_store_reply_mapping_failure_is_non_fatal(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.record_reply", new_callable=AsyncMock),
            patch(
                f"{_CALLBACKS_MODULE}.store_reply_mapping",
                new_callable=AsyncMock,
                side_effect=Exception("redis down"),
            ),
            patch(f"{_CALLBACKS_MODULE}.get_tool_entries", new_callable=AsyncMock, return_value=[]),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.clear_tool_entries", new_callable=AsyncMock),
        ):
            req = AddReplyRequest(session_id="C123:1234567890.000:A001", text="Real reply")
            resp = await add_reply(req)

        # Should still succeed — redis mapping failure is non-fatal
        assert resp.success is True

    async def test_thinking_reply_does_not_clear_tool_cluster(self) -> None:
        session = _make_session(status_message_ts="STATUS_TS")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="THINKING_TS")

        clear_entries_mock = AsyncMock()
        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.record_reply", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.store_reply_mapping", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.clear_tool_entries", clear_entries_mock),
        ):
            req = AddReplyRequest(session_id="C123:1234567890.000:A001", text="Thinking...", reply_type="thinking")
            resp = await add_reply(req)

        assert resp.success is True
        # clear_tool_entries should NOT be called for "thinking" reply type
        clear_entries_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_reply
# ---------------------------------------------------------------------------


class TestUpdateReply:
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=None):
            req = UpdateReplyRequest(session_id="NOPE", text="Updated")
            resp = await update_reply(req)
        assert resp.success is False
        assert "not found" in (resp.error or "").lower()

    async def test_returns_error_when_no_last_reply_ts(self) -> None:
        session = _make_session(last_reply_ts=None)
        with patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session):
            req = UpdateReplyRequest(session_id="C123:1234567890.000:A001", text="Updated")
            resp = await update_reply(req)
        assert resp.success is False
        assert "no previous reply" in (resp.error or "").lower()

    async def test_returns_error_when_no_slack_client(self) -> None:
        session = _make_session(last_reply_ts="1111.000")
        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=None),
            patch(f"{_CALLBACKS_MODULE}.discard_buffer", new_callable=AsyncMock),
        ):
            req = UpdateReplyRequest(session_id="C123:1234567890.000:A001", text="Updated")
            resp = await update_reply(req)
        assert resp.success is False

    async def test_successful_update(self) -> None:
        session = _make_session(last_reply_ts="1111.000")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_update.return_value = _make_slack_response(ts="1111.000")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.discard_buffer", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.record_reply", new_callable=AsyncMock),
        ):
            req = UpdateReplyRequest(session_id="C123:1234567890.000:A001", text="Updated text")
            resp = await update_reply(req)

        assert resp.success is True
        mock_client.chat_update.assert_awaited_once()

    async def test_slack_error_returns_failure(self) -> None:
        session = _make_session(last_reply_ts="1111.000")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_update.side_effect = _slack_api_error("not_in_channel")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.discard_buffer", new_callable=AsyncMock),
        ):
            req = UpdateReplyRequest(session_id="C123:1234567890.000:A001", text="Updated")
            resp = await update_reply(req)

        assert resp.success is False
        assert resp.error is not None


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_sends_message_successfully(self) -> None:
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
        ):
            req = SendMessageRequest(agent_name="test-agent", channel="C456", text="Hello from agent!")
            resp = await send_message(req)

        assert resp.success is True
        assert resp.message_ts == "MSG_TS"

    async def test_falls_back_to_base_prefix_name(self) -> None:
        """'yuppclaw-alice' should fall back to 'yuppclaw' for token lookup."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS")

        # First call (full name) returns None, second call (base prefix) returns config
        with (
            patch(
                f"{_CALLBACKS_MODULE}.get_agent_config_by_name",
                new_callable=AsyncMock,
                side_effect=[None, app_config],
            ),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
        ):
            req = SendMessageRequest(agent_name="test-agent-alice", channel="C456", text="Hi!")
            resp = await send_message(req)

        assert resp.success is True

    async def test_returns_error_when_agent_not_found(self) -> None:
        with patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=None):
            req = SendMessageRequest(agent_name="unknown-agent", channel="C456", text="Hi!")
            resp = await send_message(req)
        assert resp.success is False
        assert "does not have a Slack presence" in (resp.error or "")

    async def test_registers_thread_session_mapping_for_top_level_message(self) -> None:
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS", channel="C123")
        store_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.store_thread_session_mapping", store_mock),
        ):
            req = SendMessageRequest(
                agent_name="test-agent",
                channel="C123",
                text="Hello!",
                ahs_session_id="sess-abc-123",
            )
            resp = await send_message(req)

        assert resp.success is True
        store_mock.assert_awaited_once_with("C123", "MSG_TS", "sess-abc-123")

    async def test_no_thread_mapping_for_threaded_message(self) -> None:
        """If thread_ts is provided, no session mapping should be registered."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS")
        store_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.store_thread_session_mapping", store_mock),
        ):
            req = SendMessageRequest(
                agent_name="test-agent",
                channel="C123",
                text="Reply!",
                thread_ts="PARENT_TS",
                ahs_session_id="sess-abc-123",
            )
            await send_message(req)

        store_mock.assert_not_awaited()

    async def test_empty_thread_ts_treated_as_none(self) -> None:
        """Empty string thread_ts should behave like None (top-level message)."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="MSG_TS")
        store_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.store_thread_session_mapping", store_mock),
        ):
            req = SendMessageRequest(
                agent_name="test-agent",
                channel="C123",
                text="Top level!",
                thread_ts="",  # empty string
                ahs_session_id="sess-xyz",
            )
            resp = await send_message(req)

        assert resp.success is True
        store_mock.assert_awaited_once()

    async def test_slack_error_returns_failure(self) -> None:
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = _slack_api_error("channel_not_found")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_name", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
        ):
            req = SendMessageRequest(agent_name="test-agent", channel="CBAD", text="Hi!")
            resp = await send_message(req)

        assert resp.success is False
        assert resp.error is not None


# ---------------------------------------------------------------------------
# request_feedback
# ---------------------------------------------------------------------------


class TestRequestFeedback:
    async def test_skips_when_already_claimed(self) -> None:
        with patch(
            f"{_CALLBACKS_MODULE}.try_claim_feedback_request",
            new_callable=AsyncMock,
            return_value=False,
        ):
            req = RequestFeedbackRequest(session_id="C123:1234567890.000:A001")
            resp = await request_feedback(req)

        assert resp.success is True
        assert "already requested" in (resp.error or "")

    async def test_releases_claim_when_session_not_found(self) -> None:
        release_mock = AsyncMock()
        with (
            patch(f"{_CALLBACKS_MODULE}.try_claim_feedback_request", new_callable=AsyncMock, return_value=True),
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=None),
            patch(f"{_CALLBACKS_MODULE}.release_feedback_claim", release_mock),
        ):
            req = RequestFeedbackRequest(session_id="GONE")
            resp = await request_feedback(req)

        assert resp.success is False
        release_mock.assert_awaited_once_with("GONE")

    async def test_releases_claim_when_no_client(self) -> None:
        session = _make_session()
        release_mock = AsyncMock()
        with (
            patch(f"{_CALLBACKS_MODULE}.try_claim_feedback_request", new_callable=AsyncMock, return_value=True),
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=None),
            patch(f"{_CALLBACKS_MODULE}.release_feedback_claim", release_mock),
        ):
            req = RequestFeedbackRequest(session_id="C123:1234567890.000:A001")
            resp = await request_feedback(req)

        assert resp.success is False
        release_mock.assert_awaited_once()

    async def test_successful_feedback_post(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="SURVEY_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.try_claim_feedback_request", new_callable=AsyncMock, return_value=True),
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.store_reply_mapping", new_callable=AsyncMock),
        ):
            req = RequestFeedbackRequest(session_id="C123:1234567890.000:A001")
            resp = await request_feedback(req)

        assert resp.success is True
        assert resp.message_ts == "SURVEY_TS"

    async def test_releases_claim_on_slack_error(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = Exception("network error")
        release_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.try_claim_feedback_request", new_callable=AsyncMock, return_value=True),
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.release_feedback_claim", release_mock),
        ):
            req = RequestFeedbackRequest(session_id="C123:1234567890.000:A001")
            resp = await request_feedback(req)

        assert resp.success is False
        release_mock.assert_awaited_once()

    async def test_missing_ts_in_response_releases_claim(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        resp_mock = MagicMock()
        resp_mock.get = lambda key, default=None: "" if key == "ts" else default
        mock_client.chat_postMessage.return_value = resp_mock
        release_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.try_claim_feedback_request", new_callable=AsyncMock, return_value=True),
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.release_feedback_claim", release_mock),
        ):
            req = RequestFeedbackRequest(session_id="C123:1234567890.000:A001")
            resp = await request_feedback(req)

        assert resp.success is False
        release_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# send_questionnaire
# ---------------------------------------------------------------------------


class TestSendQuestionnaire:
    def _make_req(self, session_id: str = "C123:1234567890.000:A001") -> SendQuestionnaireRequest:
        return SendQuestionnaireRequest(
            session_id=session_id,
            question_id="q1",
            text="What's next?",
            choices=[QuestionChoice(label="A", value="a"), QuestionChoice(label="B", value="b")],
        )

    async def test_returns_error_when_session_not_found(self) -> None:
        with patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=None):
            resp = await send_questionnaire(self._make_req(session_id="NOPE"))
        assert resp.success is False

    async def test_returns_error_when_no_client(self) -> None:
        session = _make_session()
        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=None),
        ):
            resp = await send_questionnaire(self._make_req())
        assert resp.success is False

    async def test_successful_post(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="Q_TS")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.store_reply_mapping", new_callable=AsyncMock),
        ):
            resp = await send_questionnaire(self._make_req())

        assert resp.success is True
        assert resp.message_ts == "Q_TS"

    async def test_slack_error_returns_failure(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = Exception("timeout")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
        ):
            resp = await send_questionnaire(self._make_req())

        assert resp.success is False


# ---------------------------------------------------------------------------
# flush_status_update
# ---------------------------------------------------------------------------


class TestFlushStatusUpdate:
    def _make_entry(self, name: str = "Bash") -> ToolUseEntry:
        return ToolUseEntry(
            tool_use_id=f"tu-{name}",
            name=name,
            command="ls -la",
            result_status=ToolResultStatus.RUNNING,
        )

    async def test_noop_when_no_pending_flag(self) -> None:
        session = _make_session(status_message_ts="STATUS_TS")
        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(
                f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
        ):
            resp = await flush_status_update("C123:1234567890.000:A001")

        assert resp.success is True
        assert resp.message_ts == "STATUS_TS"

    async def test_posts_new_status_message_when_no_existing(self) -> None:
        session = _make_session(status_message_ts=None)
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response(ts="NEW_STATUS_TS")
        fresh_session = _make_session(status_message_ts=None)

        with (
            patch(
                f"{_CALLBACKS_MODULE}.get_session",
                new_callable=AsyncMock,
                side_effect=[session, fresh_session],
            ),
            patch(
                f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(
                f"{_CALLBACKS_MODULE}.get_tool_entries",
                new_callable=AsyncMock,
                return_value=[self._make_entry()],
            ),
            patch(f"{_CALLBACKS_MODULE}.save_session", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.peek_tool_cluster_pending", new_callable=AsyncMock, return_value=False),
        ):
            resp = await flush_status_update("C123:1234567890.000:A001")

        assert resp.success is True
        mock_client.chat_postMessage.assert_awaited_once()

    async def test_edits_existing_status_message(self) -> None:
        session = _make_session(status_message_ts="EXISTING_TS")
        app_config = _make_app_config()
        mock_client = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(
                f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(
                f"{_CALLBACKS_MODULE}.get_tool_entries",
                new_callable=AsyncMock,
                return_value=[self._make_entry("Grep")],
            ),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.peek_tool_cluster_pending", new_callable=AsyncMock, return_value=False),
        ):
            resp = await flush_status_update("C123:1234567890.000:A001")

        assert resp.success is True
        assert resp.message_ts == "EXISTING_TS"
        mock_client.chat_update.assert_awaited_once()
        mock_client.chat_postMessage.assert_not_awaited()

    async def test_handles_message_not_found_by_resetting_ts(self) -> None:
        """If status message was deleted externally, reset status_message_ts."""
        session = _make_session(status_message_ts="DELETED_TS")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_update.side_effect = _slack_api_error("message_not_found")

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(
                f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(
                f"{_CALLBACKS_MODULE}.get_tool_entries",
                new_callable=AsyncMock,
                return_value=[self._make_entry()],
            ),
            patch(f"{_CALLBACKS_MODULE}.save_session", new_callable=AsyncMock) as save_mock,
            patch(f"{_CALLBACKS_MODULE}.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.schedule_status_flush", new_callable=AsyncMock),
        ):
            resp = await flush_status_update("C123:1234567890.000:A001")

        assert resp.success is False
        # session.status_message_ts should be cleared
        assert session.status_message_ts is None
        save_mock.assert_awaited()

    async def test_returns_error_when_session_not_found(self) -> None:
        with patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=None):
            resp = await flush_status_update("NOPE")
        assert resp.success is False

    async def test_no_op_when_entries_cleared_between_flag_set_and_flush(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(
                f"{_CALLBACKS_MODULE}.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(f"{_CALLBACKS_MODULE}.get_agent_config_by_app_id", new_callable=AsyncMock, return_value=app_config),
            patch(f"{_CALLBACKS_MODULE}.AsyncWebClient", return_value=mock_client),
            patch(f"{_CALLBACKS_MODULE}.get_tool_entries", new_callable=AsyncMock, return_value=[]),
            patch(f"{_CALLBACKS_MODULE}.remove_from_status_flush_schedule", new_callable=AsyncMock),
        ):
            resp = await flush_status_update("C123:1234567890.000:A001")

        assert resp.success is True
        mock_client.chat_postMessage.assert_not_awaited()
        mock_client.chat_update.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_tool_event
# ---------------------------------------------------------------------------


class TestHandleToolEvent:
    async def test_start_event_appends_entry(self) -> None:
        session = _make_session()
        append_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.append_tool_entry", append_mock),
            patch(f"{_CALLBACKS_MODULE}.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.try_acquire_status_ratelimit", new_callable=AsyncMock, return_value=True),
            patch(
                f"{_CALLBACKS_MODULE}.flush_status_update",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, message_ts="TS", error=None),
            ),
        ):
            req = SendToolEventRequest(
                session_id="C123:1234567890.000:A001",
                kind=ToolEventKind.START,
                tool_use_id="tu-001",
                name="Bash",
                command="ls -la",
            )
            resp = await handle_tool_event(req)

        assert resp.success is True
        append_mock.assert_awaited_once()

    async def test_result_event_updates_entry(self) -> None:
        session = _make_session()
        update_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.update_tool_result", update_mock),
            patch(f"{_CALLBACKS_MODULE}.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.try_acquire_status_ratelimit", new_callable=AsyncMock, return_value=True),
            patch(
                f"{_CALLBACKS_MODULE}.flush_status_update",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, message_ts="TS", error=None),
            ),
        ):
            req = SendToolEventRequest(
                session_id="C123:1234567890.000:A001",
                kind=ToolEventKind.RESULT,
                tool_use_id="tu-001",
                result_status="done",
                result_content="output here",
            )
            resp = await handle_tool_event(req)

        assert resp.success is True
        update_mock.assert_awaited_once()

    async def test_rate_limited_defers_flush(self) -> None:
        session = _make_session(status_message_ts="STATUS_TS")
        schedule_mock = AsyncMock()

        with (
            patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=session),
            patch(f"{_CALLBACKS_MODULE}.append_tool_entry", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(f"{_CALLBACKS_MODULE}.try_acquire_status_ratelimit", new_callable=AsyncMock, return_value=False),
            patch(f"{_CALLBACKS_MODULE}.schedule_status_flush", schedule_mock),
        ):
            req = SendToolEventRequest(
                session_id="C123:1234567890.000:A001",
                kind=ToolEventKind.START,
                tool_use_id="tu-002",
                name="Read",
                command="file.py",
            )
            resp = await handle_tool_event(req)

        assert resp.success is True
        assert resp.message_ts == "STATUS_TS"
        schedule_mock.assert_awaited_once()

    async def test_returns_error_when_session_not_found(self) -> None:
        with patch(f"{_CALLBACKS_MODULE}.get_session", new_callable=AsyncMock, return_value=None):
            req = SendToolEventRequest(
                session_id="NOPE",
                kind=ToolEventKind.START,
                tool_use_id="tu-003",
            )
            resp = await handle_tool_event(req)

        assert resp.success is False
        assert "not found" in (resp.error or "").lower()
