"""Unit tests for SAG callbacks and callbacks_rendering modules.

Covers:
- render_reply_blocks (pure, callbacks_rendering)
- _escape_mrkdwn, _render_tool_cluster (pure, callbacks)
- _build_survey_blocks, _build_questionnaire_blocks (pure, callbacks)
- add_reply, update_reply, send_message, request_feedback,
  send_questionnaire, handle_tool_event (async, mocked Slack + Redis)
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError
from ypl.slack_agent_gateway.callbacks import (
    _build_questionnaire_blocks,
    _build_survey_blocks,
    _escape_mrkdwn,
    _render_tool_cluster,
    add_reply,
    handle_tool_event,
    request_feedback,
    send_message,
    send_questionnaire,
    update_reply,
)
from ypl.slack_agent_gateway.callbacks_rendering import render_reply_blocks
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
# Fixtures
# ---------------------------------------------------------------------------


def _make_session(
    session_id: str = "C123:1234567890.000:A001",
    channel_id: str = "C123",
    thread_ts: str = "1234567890.000",
    app_id: str = "A001",
    last_reply_ts: str | None = None,
    placeholder_ts: str | None = None,
    status_message_ts: str | None = None,
    show_tool_calls: bool = True,
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
        show_tool_calls=show_tool_calls,
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


def _slack_api_error(code: str = "message_too_long") -> SlackApiError:
    resp = MagicMock()
    resp.get = lambda key, default=None: code if key == "error" else default
    return SlackApiError(message=code, response=resp)  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# callbacks_rendering — render_reply_blocks
# ---------------------------------------------------------------------------


class TestRenderReplyBlocks:
    def test_thinking_returns_context_block(self) -> None:
        result = render_reply_blocks("I am thinking...", "thinking")
        assert result is not None
        assert len(result) == 1
        block = result[0]
        assert block["type"] == "context"
        assert block["elements"][0]["text"] == "I am thinking..."

    def test_none_reply_type_returns_none(self) -> None:
        assert render_reply_blocks("Hello!", None) is None

    def test_unknown_reply_type_returns_none(self) -> None:
        assert render_reply_blocks("Hello!", "tool_use") is None

    def test_long_thinking_text_is_truncated(self) -> None:
        long_text = "x" * 3100
        result = render_reply_blocks(long_text, "thinking")
        assert result is not None
        rendered_text = result[0]["elements"][0]["text"]
        assert len(rendered_text) <= 3000
        assert rendered_text.endswith("...")

    def test_text_at_limit_not_truncated(self) -> None:
        text = "y" * 3000
        result = render_reply_blocks(text, "thinking")
        assert result is not None
        assert result[0]["elements"][0]["text"] == text


# ---------------------------------------------------------------------------
# Pure helpers — callbacks.py
# ---------------------------------------------------------------------------


class TestEscapeMrkdwn:
    def test_ampersand_escaped(self) -> None:
        assert _escape_mrkdwn("a & b") == "a &amp; b"

    def test_less_than_escaped(self) -> None:
        assert _escape_mrkdwn("<@U123>") == "&lt;@U123&gt;"

    def test_greater_than_escaped(self) -> None:
        assert _escape_mrkdwn("value > 10") == "value &gt; 10"

    def test_backtick_not_escaped(self) -> None:
        assert _escape_mrkdwn("`code`") == "`code`"

    def test_plain_text_unchanged(self) -> None:
        assert _escape_mrkdwn("hello world") == "hello world"

    def test_all_special_chars(self) -> None:
        result = _escape_mrkdwn("&<>")
        assert result == "&amp;&lt;&gt;"


class TestBuildSurveyBlocks:
    def test_returns_list_of_blocks(self) -> None:
        blocks = _build_survey_blocks("session-123")
        assert isinstance(blocks, list)
        assert len(blocks) >= 2

    def test_contains_survey_buttons(self) -> None:
        blocks = _build_survey_blocks("session-123")
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1
        elements = action_blocks[0]["elements"]
        action_ids = [e["action_id"] for e in elements]
        assert "survey_bad" in action_ids
        assert "survey_ok" in action_ids
        assert "survey_good" in action_ids

    def test_session_id_embedded_in_button_values(self) -> None:
        blocks = _build_survey_blocks("my-session-id")
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        elements = action_blocks[0]["elements"]
        for elem in elements:
            assert "my-session-id" in elem["value"]

    def test_custom_prompt_used(self) -> None:
        blocks = _build_survey_blocks("s1", prompt="Was this helpful?")
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        assert any("Was this helpful?" in str(b) for b in section_blocks)

    def test_default_prompt_used_when_none(self) -> None:
        blocks = _build_survey_blocks("s1", prompt=None)
        section_blocks = [b for b in blocks if b.get("type") == "section"]
        assert len(section_blocks) >= 1


class TestBuildQuestionnaireBlocks:
    def _make_request(
        self,
        choices: list[str] | None = None,
        allow_free_text: bool = True,
    ) -> SendQuestionnaireRequest:
        choices_list = choices or ["Option A", "Option B"]
        return SendQuestionnaireRequest(
            session_id="sess-123",
            question_id="q1",
            text="Which do you prefer?",
            choices=[QuestionChoice(label=c, value=c) for c in choices_list],
            allow_free_text=allow_free_text,
        )

    def test_returns_list_of_blocks(self) -> None:
        req = self._make_request()
        blocks = _build_questionnaire_blocks(req)
        assert isinstance(blocks, list)
        assert len(blocks) >= 2

    def test_question_text_in_section_block(self) -> None:
        req = self._make_request()
        blocks = _build_questionnaire_blocks(req)
        section = next(b for b in blocks if b["type"] == "section")
        assert "Which do you prefer?" in section["text"]["text"]

    def test_choice_buttons_created(self) -> None:
        req = self._make_request(choices=["Yes", "No", "Maybe"])
        blocks = _build_questionnaire_blocks(req)
        action_blocks = [b for b in blocks if b["type"] == "actions"]
        assert len(action_blocks) == 1
        elements = action_blocks[0]["elements"]
        labels = [e["text"]["text"] for e in elements]
        assert "Yes" in labels
        assert "No" in labels
        assert "Maybe" in labels

    def test_button_value_is_session_id(self) -> None:
        req = self._make_request()
        blocks = _build_questionnaire_blocks(req)
        action_blocks = [b for b in blocks if b["type"] == "actions"]
        for elem in action_blocks[0]["elements"]:
            assert elem["value"] == "sess-123"

    def test_free_text_hint_added_when_allowed(self) -> None:
        req = self._make_request(allow_free_text=True)
        blocks = _build_questionnaire_blocks(req)
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert len(context_blocks) >= 1

    def test_free_text_hint_absent_when_disabled(self) -> None:
        req = self._make_request(allow_free_text=False)
        blocks = _build_questionnaire_blocks(req)
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert len(context_blocks) == 0

    def test_question_id_sanitized_in_action_id(self) -> None:
        """Special chars in question_id should be replaced with underscores."""
        req = SendQuestionnaireRequest(
            session_id="s1",
            question_id="q1 with spaces!",
            text="Q?",
            choices=[QuestionChoice(label="A", value="a")],
        )
        blocks = _build_questionnaire_blocks(req)
        action_blocks = [b for b in blocks if b["type"] == "actions"]
        action_id = action_blocks[0]["elements"][0]["action_id"]
        assert " " not in action_id
        assert "!" not in action_id


class TestRenderToolCluster:
    def _make_entry(
        self,
        name: str = "Bash",
        command: str = "ls -la",
        status: ToolResultStatus = ToolResultStatus.RUNNING,
        error_msg: str | None = None,
        result_content: str | None = None,
    ) -> ToolUseEntry:
        return ToolUseEntry(
            tool_use_id=f"tu-{name}",
            name=name,
            command=command,
            result_status=status,
            error_msg=error_msg,
            result_content=result_content,
        )

    def test_single_running_entry(self) -> None:
        entries = [self._make_entry("Bash", "ls -la", ToolResultStatus.RUNNING)]
        result = _render_tool_cluster(entries)
        assert "Bash" in result
        assert "ls -la" in result
        assert "_..._" in result

    def test_done_entry_shows_content(self) -> None:
        entries = [self._make_entry("Grep", "grep foo", ToolResultStatus.DONE, result_content="found it")]
        result = _render_tool_cluster(entries)
        assert "[DONE]" in result
        assert "found it" in result

    def test_empty_entry(self) -> None:
        entries = [self._make_entry("Read", "file.py", ToolResultStatus.EMPTY)]
        result = _render_tool_cluster(entries)
        assert "[EMPTY]" in result

    def test_failed_entry_shows_error(self) -> None:
        entries = [self._make_entry("Bash", "rm -rf /", ToolResultStatus.FAILED, error_msg="Permission denied")]
        result = _render_tool_cluster(entries)
        assert "[FAILED]" in result
        assert "Permission denied" in result

    def test_only_last_two_entries_shown(self) -> None:
        entries = [self._make_entry(f"Tool{i}", f"cmd{i}", ToolResultStatus.DONE) for i in range(5)]
        result = _render_tool_cluster(entries)
        # Only last 2 should appear in the live display
        assert "Tool3" in result
        assert "Tool4" in result
        assert "Tool0" not in result

    def test_footer_appended_for_more_than_two(self) -> None:
        entries = [self._make_entry(f"T{i}", f"c{i}") for i in range(4)]
        result = _render_tool_cluster(entries)
        assert "_4 tools used_" in result

    def test_no_footer_for_two_or_fewer(self) -> None:
        entries = [self._make_entry("T1", "c1"), self._make_entry("T2", "c2")]
        result = _render_tool_cluster(entries)
        assert "tools used" not in result

    def test_long_command_truncated(self) -> None:
        long_cmd = "x" * 200
        entries = [self._make_entry("Bash", long_cmd, ToolResultStatus.RUNNING)]
        result = _render_tool_cluster(entries)
        assert "..." in result

    def test_mrkdwn_special_chars_in_output_escaped(self) -> None:
        entries = [
            self._make_entry(
                "Bash",
                "cmd",
                ToolResultStatus.DONE,
                result_content="<@U123> mentioned &amp; more",
            )
        ]
        result = _render_tool_cluster(entries)
        assert "&lt;@U123&gt;" in result


# ---------------------------------------------------------------------------
# Async callbacks — add_reply
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_app_config() -> AgentAppConfig:
    return _make_app_config()


def _make_slack_response(ts: str = "1111111111.000") -> MagicMock:
    resp = MagicMock()
    resp.get = lambda key, default=None: ts if key == "ts" else (None if key != "channel" else "C123")
    return resp


class TestAddReply:
    @pytest.mark.asyncio
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=None):
            request = AddReplyRequest(session_id="missing", text="Hello")
            result = await add_reply(request)
        assert result.success is False
        assert "Session not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_returns_error_when_no_app_config(self) -> None:
        session = _make_session()
        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=None),
        ):
            request = AddReplyRequest(session_id=session.session_id, text="Hello")
            result = await add_reply(request)
        assert result.success is False
        assert "Failed to get Slack client" in (result.error or "")

    @pytest.mark.asyncio
    async def test_posts_new_message_successfully(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = _make_slack_response("9999999.000")

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.callbacks.record_reply", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.store_reply_mapping", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.get_tool_entries", return_value=[]),
            patch("ypl.slack_agent_gateway.callbacks.clear_tool_entries", new_callable=AsyncMock),
            patch(
                "ypl.slack_agent_gateway.callbacks.remove_from_status_flush_schedule",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.callbacks.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
            ),
        ):
            request = AddReplyRequest(session_id=session.session_id, text="Hello there!")
            result = await add_reply(request)

        assert result.success is True
        assert result.message_ts == "9999999.000"
        mock_client.chat_postMessage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_placeholder_when_set(self) -> None:
        session = _make_session(placeholder_ts="8888.000")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        # chat_update returns a response with ts
        update_resp = MagicMock()
        update_resp.get = lambda key, default=None: "8888.000" if key == "ts" else default
        mock_client.chat_update.return_value = update_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.callbacks.save_session", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.record_reply", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.store_reply_mapping", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.get_tool_entries", return_value=[]),
            patch("ypl.slack_agent_gateway.callbacks.clear_tool_entries", new_callable=AsyncMock),
            patch(
                "ypl.slack_agent_gateway.callbacks.remove_from_status_flush_schedule",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.callbacks.get_and_clear_tool_cluster_pending",
                new_callable=AsyncMock,
            ),
        ):
            request = AddReplyRequest(session_id=session.session_id, text="Done!")
            result = await add_reply(request)

        assert result.success is True
        mock_client.chat_update.assert_awaited()
        mock_client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_api_error_returns_failure(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = _slack_api_error("channel_not_found")

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = AddReplyRequest(session_id=session.session_id, text="Hi")
            result = await add_reply(request)

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# Async callbacks — update_reply
# ---------------------------------------------------------------------------


class TestUpdateReply:
    @pytest.mark.asyncio
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=None):
            result = await update_reply(UpdateReplyRequest(session_id="x", text="y"))
        assert result.success is False
        assert "Session not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_returns_error_when_no_last_reply_ts(self) -> None:
        session = _make_session(last_reply_ts=None)
        with patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session):
            result = await update_reply(UpdateReplyRequest(session_id=session.session_id, text="y"))
        assert result.success is False
        assert "No previous reply" in (result.error or "")

    @pytest.mark.asyncio
    async def test_updates_message_successfully(self) -> None:
        session = _make_session(last_reply_ts="7777.000")
        app_config = _make_app_config()
        mock_client = AsyncMock()
        update_resp = MagicMock()
        update_resp.get = lambda key, default=None: "7777.000" if key == "ts" else default
        mock_client.chat_update.return_value = update_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.callbacks.discard_buffer", new_callable=AsyncMock),
            patch("ypl.slack_agent_gateway.callbacks.record_reply", new_callable=AsyncMock),
        ):
            result = await update_reply(UpdateReplyRequest(session_id=session.session_id, text="Updated!"))

        assert result.success is True
        mock_client.chat_update.assert_awaited_once()
        call_kwargs = mock_client.chat_update.call_args[1]
        assert call_kwargs["text"] == "Updated!"


# ---------------------------------------------------------------------------
# Async callbacks — send_message (proactive)
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_agent(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=None):
            request = SendMessageRequest(agent_name="unknown-agent", channel="C999", text="hello")
            result = await send_message(request)
        assert result.success is False
        assert "does not have a Slack presence" in (result.error or "")

    @pytest.mark.asyncio
    async def test_sends_message_successfully(self) -> None:
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: (
            "5555.000" if key == "ts" else ("C999" if key == "channel" else default)
        )
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = SendMessageRequest(agent_name="test-agent", channel="C999", text="Hello channel!")
            result = await send_message(request)

        assert result.success is True
        assert result.message_ts == "5555.000"
        mock_client.chat_postMessage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stores_thread_mapping_for_top_level_with_session(self) -> None:
        """When ahs_session_id is provided and message is top-level, thread mapping is stored."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: (
            "6666.000" if key == "ts" else ("C100" if key == "channel" else default)
        )
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch(
                "ypl.slack_agent_gateway.callbacks.store_thread_session_mapping",
                new_callable=AsyncMock,
            ) as mock_store,
        ):
            request = SendMessageRequest(
                agent_name="test-agent",
                channel="C100",
                text="Hi",
                ahs_session_id="ahs-session-uuid",
            )
            result = await send_message(request)

        assert result.success is True
        mock_store.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_thread_mapping_for_in_thread_message(self) -> None:
        """No thread mapping stored when message is already in a thread."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: (
            "7777.000" if key == "ts" else ("C100" if key == "channel" else default)
        )
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch(
                "ypl.slack_agent_gateway.callbacks.store_thread_session_mapping",
                new_callable=AsyncMock,
            ) as mock_store,
        ):
            # Providing thread_ts means it's already in a thread
            request = SendMessageRequest(
                agent_name="test-agent",
                channel="C100",
                text="Hi",
                thread_ts="1234.000",
                ahs_session_id="ahs-session-uuid",
            )
            await send_message(request)

        mock_store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_slack_error_returns_failure(self) -> None:
        app_config = _make_app_config()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = _slack_api_error("channel_not_found")

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = SendMessageRequest(agent_name="test-agent", channel="CBAD", text="Hi")
            result = await send_message(request)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_forwards_unfurl_flags_to_slack(self) -> None:
        """unfurl_links / unfurl_media on the request should be forwarded to chat.postMessage."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: (
            "8888.000" if key == "ts" else ("C999" if key == "channel" else default)
        )
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = SendMessageRequest(
                agent_name="test-agent",
                channel="C999",
                text="digest",
                unfurl_links=False,
                unfurl_media=False,
            )
            result = await send_message(request)

        assert result.success is True
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["unfurl_links"] is False
        assert call_kwargs["unfurl_media"] is False

    @pytest.mark.asyncio
    async def test_unfurl_flags_default_to_true_on_chat_postmessage(self) -> None:
        """Omitting unfurl_* on the request should pass True (Slack default) to chat.postMessage."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: (
            "8889.000" if key == "ts" else ("C999" if key == "channel" else default)
        )
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_name", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = SendMessageRequest(agent_name="test-agent", channel="C999", text="hi")
            await send_message(request)

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["unfurl_links"] is True
        assert call_kwargs["unfurl_media"] is True

    @pytest.mark.asyncio
    async def test_base_name_fallback_for_personal_agent(self) -> None:
        """Personal agents like 'yuppclaw-alice' should fall back to 'yuppclaw' config."""
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: "1234.000" if key == "ts" else ("C1" if key == "channel" else default)
        mock_client.chat_postMessage.return_value = post_resp

        # First call (exact name) returns None; second call (base name) returns config
        with (
            patch(
                "ypl.slack_agent_gateway.callbacks.get_agent_config_by_name",
                side_effect=[None, app_config],
            ),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
        ):
            request = SendMessageRequest(agent_name="test-agent-alice", channel="C1", text="Hi")
            result = await send_message(request)

        assert result.success is True


# ---------------------------------------------------------------------------
# Async callbacks — request_feedback
# ---------------------------------------------------------------------------


class TestRequestFeedback:
    @pytest.mark.asyncio
    async def test_skips_if_already_claimed(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.try_claim_feedback_request", return_value=False):
            result = await request_feedback(RequestFeedbackRequest(session_id="s1"))
        assert result.success is True
        assert "already requested" in (result.error or "")

    @pytest.mark.asyncio
    async def test_returns_error_when_session_not_found(self) -> None:
        with (
            patch("ypl.slack_agent_gateway.callbacks.try_claim_feedback_request", return_value=True),
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=None),
            patch("ypl.slack_agent_gateway.callbacks.release_feedback_claim", new_callable=AsyncMock),
        ):
            result = await request_feedback(RequestFeedbackRequest(session_id="s1"))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_posts_survey_successfully(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: "2222.000" if key == "ts" else default
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.try_claim_feedback_request", return_value=True),
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.callbacks.store_reply_mapping", new_callable=AsyncMock),
        ):
            result = await request_feedback(RequestFeedbackRequest(session_id=session.session_id))

        assert result.success is True
        assert result.message_ts == "2222.000"
        # Verify survey blocks were included
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs
        assert len(call_kwargs["blocks"]) > 0


# ---------------------------------------------------------------------------
# Async callbacks — send_questionnaire
# ---------------------------------------------------------------------------


class TestSendQuestionnaire:
    @pytest.mark.asyncio
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=None):
            req = SendQuestionnaireRequest(
                session_id="missing",
                question_id="q1",
                text="Q?",
                choices=[QuestionChoice(label="A", value="a")],
            )
            result = await send_questionnaire(req)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_posts_questionnaire_successfully(self) -> None:
        session = _make_session()
        app_config = _make_app_config()
        mock_client = AsyncMock()
        post_resp = MagicMock()
        post_resp.get = lambda key, default=None: "3333.000" if key == "ts" else default
        mock_client.chat_postMessage.return_value = post_resp

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.get_agent_config_by_app_id", return_value=app_config),
            patch("ypl.slack_agent_gateway.callbacks.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.callbacks.store_reply_mapping", new_callable=AsyncMock),
        ):
            req = SendQuestionnaireRequest(
                session_id=session.session_id,
                question_id="deploy_env",
                text="Which environment?",
                choices=[
                    QuestionChoice(label="Staging", value="staging"),
                    QuestionChoice(label="Production", value="production"),
                ],
            )
            result = await send_questionnaire(req)

        assert result.success is True
        assert result.message_ts == "3333.000"
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs


# ---------------------------------------------------------------------------
# Async callbacks — handle_tool_event
# ---------------------------------------------------------------------------


class TestHandleToolEvent:
    @pytest.mark.asyncio
    async def test_returns_error_when_session_not_found(self) -> None:
        with patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=None):
            req = SendToolEventRequest(
                session_id="missing",
                kind=ToolEventKind.START,
                tool_use_id="t1",
                name="Bash",
                command="ls",
            )
            result = await handle_tool_event(req)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_start_event_appends_entry(self) -> None:
        session = _make_session()

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.append_tool_entry", new_callable=AsyncMock) as mock_append,
            patch("ypl.slack_agent_gateway.callbacks.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(
                "ypl.slack_agent_gateway.callbacks.try_acquire_status_ratelimit",
                return_value=False,
            ),
            patch("ypl.slack_agent_gateway.callbacks.schedule_status_flush", new_callable=AsyncMock),
        ):
            req = SendToolEventRequest(
                session_id=session.session_id,
                kind=ToolEventKind.START,
                tool_use_id="t1",
                name="Bash",
                command="echo hello",
            )
            result = await handle_tool_event(req)

        assert result.success is True
        mock_append.assert_awaited_once()
        appended_entry = mock_append.call_args[0][1]
        assert appended_entry.name == "Bash"
        assert appended_entry.result_status == ToolResultStatus.RUNNING

    @pytest.mark.asyncio
    async def test_result_event_updates_entry(self) -> None:
        session = _make_session()

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.update_tool_result", new_callable=AsyncMock) as mock_update,
            patch("ypl.slack_agent_gateway.callbacks.set_tool_cluster_pending", new_callable=AsyncMock),
            patch(
                "ypl.slack_agent_gateway.callbacks.try_acquire_status_ratelimit",
                return_value=False,
            ),
            patch("ypl.slack_agent_gateway.callbacks.schedule_status_flush", new_callable=AsyncMock),
        ):
            req = SendToolEventRequest(
                session_id=session.session_id,
                kind=ToolEventKind.RESULT,
                tool_use_id="t1",
                result_status="done",
                result_content="output line 1",
            )
            result = await handle_tool_event(req)

        assert result.success is True
        mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quiet_session_suppresses_tool_events(self) -> None:
        """When /quiet is active on a session, tool events are dropped without
        buffering, rendering, or flushing to Slack."""
        session = _make_session(show_tool_calls=False, status_message_ts="1111.222")

        with (
            patch("ypl.slack_agent_gateway.callbacks.get_session", return_value=session),
            patch("ypl.slack_agent_gateway.callbacks.append_tool_entry", new_callable=AsyncMock) as mock_append,
            patch("ypl.slack_agent_gateway.callbacks.update_tool_result", new_callable=AsyncMock) as mock_update,
            patch(
                "ypl.slack_agent_gateway.callbacks.set_tool_cluster_pending",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "ypl.slack_agent_gateway.callbacks.try_acquire_status_ratelimit",
                return_value=True,
            ) as mock_ratelimit,
            patch(
                "ypl.slack_agent_gateway.callbacks.flush_status_update",
                new_callable=AsyncMock,
            ) as mock_flush,
        ):
            start_req = SendToolEventRequest(
                session_id=session.session_id,
                kind=ToolEventKind.START,
                tool_use_id="t1",
                name="Bash",
                command="ls",
            )
            start_result = await handle_tool_event(start_req)

            result_req = SendToolEventRequest(
                session_id=session.session_id,
                kind=ToolEventKind.RESULT,
                tool_use_id="t1",
                result_status="done",
                result_content="ok",
            )
            result_result = await handle_tool_event(result_req)

        # Both events return success so AHS doesn't retry.
        assert start_result.success is True
        assert result_result.success is True
        # Nothing was buffered, rendered, or posted to Slack.
        mock_append.assert_not_called()
        mock_update.assert_not_called()
        mock_pending.assert_not_called()
        mock_ratelimit.assert_not_called()
        mock_flush.assert_not_called()
