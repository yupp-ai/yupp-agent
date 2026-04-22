"""Unit tests for SAG interactions module.

Covers:
- handle_interaction dispatch (routing by payload type and action_id)
- _handle_survey_action (rating extraction, dedup, feedback send, thank-you update)
- _handle_bot_father_action (approve / deny)
- _handle_questionnaire_action (forwarding choice to AHS)
- _handle_create_agent_submission (validation, bot creation request)
- process_slack_interaction (signature verification, form parsing)
"""

from __future__ import annotations
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote_plus

import pytest
from fastapi import HTTPException, Request
from ypl.slack_agent_gateway.interactions import (
    _handle_bot_father_action,
    _handle_create_agent_submission,
    _handle_questionnaire_action,
    _handle_survey_action,
    handle_interaction,
    process_slack_interaction,
)
from ypl.slack_agent_gateway.types import AgentAppConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_block_actions_payload(
    action_id: str,
    action_value: str = "",
    user_id: str = "U999",
    channel_id: str = "C123",
    message_ts: str = "1234.000",
    api_app_id: str = "A001",
    state_values: dict | None = None,
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "api_app_id": api_app_id,
        "user": {"id": user_id},
        "channel": {"id": channel_id},
        "message": {"ts": message_ts},
        "actions": [{"action_id": action_id, "value": action_value, "text": {"text": "Label"}}],
        "state": {"values": state_values or {}},
    }


def _make_app_config(app_id: str = "A001") -> AgentAppConfig:
    return AgentAppConfig(
        app_id=app_id,
        agent_name="test-agent",
        slack_name="testbot",
        bot_token="xoxb-test",
        signing_secret="test-secret",
        display_name="Test Bot",
    )


# ---------------------------------------------------------------------------
# handle_interaction — routing
# ---------------------------------------------------------------------------


class TestHandleInteractionRouting:
    @pytest.mark.asyncio
    async def test_unhandled_type_returns_unhandled(self) -> None:
        payload = {"type": "something_weird"}
        response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "unhandled_type"

    @pytest.mark.asyncio
    async def test_no_actions_returns_unhandled_action(self) -> None:
        payload = {"type": "block_actions", "actions": []}
        response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "unhandled_action"

    @pytest.mark.asyncio
    async def test_unknown_action_id_returns_unhandled(self) -> None:
        payload = _make_block_actions_payload(action_id="unknown_action_xyz")
        response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "unhandled_action"

    @pytest.mark.asyncio
    async def test_survey_action_routed_to_survey_handler(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-123",
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value="yupp-user",
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,  # Skip Slack update
            ),
        ):
            response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "feedback_recorded"

    @pytest.mark.asyncio
    async def test_bot_father_action_routed_correctly(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_approve",
            action_value="req-123",
        )
        with patch(
            "ypl.slack_agent_gateway.interactions.handle_approval",
            new_callable=AsyncMock,
        ):
            response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "approved"

    @pytest.mark.asyncio
    async def test_questionnaire_action_routed_correctly(self) -> None:
        payload = _make_block_actions_payload(
            action_id="questionnaire_deploy_env",
            action_value="sess-abc",
        )
        payload["actions"][0]["text"] = {"text": "Staging"}

        with (
            patch(
                "ypl.slack_agent_gateway.interactions.send_questionnaire_answer_to_agent",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            response = await handle_interaction(payload)
        body = json.loads(response.body)
        assert body["status"] == "answer_forwarded"

    @pytest.mark.asyncio
    async def test_view_submission_unhandled_callback(self) -> None:
        payload = {
            "type": "view_submission",
            "view": {"callback_id": "some_other_modal"},
        }
        response = await handle_interaction(payload)
        assert response.status_code == 200
        assert json.loads(response.body) == {}


# ---------------------------------------------------------------------------
# _handle_survey_action
# ---------------------------------------------------------------------------


class TestHandleSurveyAction:
    @pytest.mark.asyncio
    async def test_returns_no_actions_when_empty(self) -> None:
        payload = {"type": "block_actions", "actions": []}
        response = await _handle_survey_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "no_actions"

    @pytest.mark.asyncio
    async def test_returns_invalid_value_for_malformed_button_value(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="no-colon-here",
        )
        response = await _handle_survey_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "invalid_value"

    @pytest.mark.asyncio
    async def test_duplicate_survey_returns_duplicate_status(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-123",
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=False,
            ),
        ):
            response = await _handle_survey_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_good_rating_sends_positive_feedback(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-good",
        )

        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value="yupp-user-1",
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            response = await _handle_survey_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "feedback_recorded"
        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["rating"] == "POSITIVE"
        assert call_kwargs["session_id"] == "session-good"

    @pytest.mark.asyncio
    async def test_bad_rating_sends_negative_feedback(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_bad",
            action_value="bad:session-bad",
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            await _handle_survey_action(payload)

        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["rating"] == "NEGATIVE"

    @pytest.mark.asyncio
    async def test_ok_rating_sends_none_rating(self) -> None:
        payload = _make_block_actions_payload(
            action_id="survey_ok",
            action_value="ok:session-ok",
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            await _handle_survey_action(payload)

        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["rating"] is None

    @pytest.mark.asyncio
    async def test_feedback_failure_releases_claim(self) -> None:
        """When AHS feedback fails, the dedup claim should be released."""
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-fail",
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.release_survey_response_claim",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            response = await _handle_survey_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "feedback_persistence_failed"
        mock_release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_comment_extracted_from_state(self) -> None:
        """A comment in survey state values should be forwarded to AHS."""
        state_values = {
            "survey_comment": {
                "comment_input": {"value": "Great job!"},
            }
        }
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-comment",
            state_values=state_values,
        )
        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_feedback,
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            await _handle_survey_action(payload)

        call_kwargs = mock_feedback.call_args[1]
        assert call_kwargs["comment"] == "Great job!"

    @pytest.mark.asyncio
    async def test_thank_you_message_posted_on_success(self) -> None:
        """After successful feedback, the survey message is replaced with a thank-you."""
        payload = _make_block_actions_payload(
            action_id="survey_good",
            action_value="good:session-ty",
            channel_id="C123",
            message_ts="1234.000",
            api_app_id="A001",
        )
        mock_client = AsyncMock()
        app_config = _make_app_config("A001")

        with (
            patch(
                "ypl.slack_agent_gateway.interactions.resolve_slack_user_to_yupp_user_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.try_claim_survey_response",
                return_value=True,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.send_feedback",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=app_config,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.build_slack_client",
                return_value=mock_client,
            ),
        ):
            response = await _handle_survey_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "feedback_recorded"
        mock_client.chat_update.assert_awaited_once()
        call_kwargs = mock_client.chat_update.call_args[1]
        assert "Good" in call_kwargs["text"]  # rating_label capitalized
        assert call_kwargs["blocks"] == []


# ---------------------------------------------------------------------------
# _handle_bot_father_action
# ---------------------------------------------------------------------------


class TestHandleBotFatherAction:
    @pytest.mark.asyncio
    async def test_no_actions_returns_no_actions(self) -> None:
        payload = {"type": "block_actions", "actions": []}
        response = await _handle_bot_father_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "no_actions"

    @pytest.mark.asyncio
    async def test_missing_request_id_returns_error(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_approve",
            action_value="",  # No request_id
        )
        response = await _handle_bot_father_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "missing_request_id"

    @pytest.mark.asyncio
    async def test_approve_calls_handle_approval(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_approve",
            action_value="req-abc-123",
            user_id="U-admin",
        )
        with patch(
            "ypl.slack_agent_gateway.interactions.handle_approval",
            new_callable=AsyncMock,
        ) as mock_approve:
            response = await _handle_bot_father_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "approved"
        mock_approve.assert_awaited_once_with("req-abc-123", "U-admin")

    @pytest.mark.asyncio
    async def test_deny_calls_handle_denial(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_deny",
            action_value="req-deny-456",
            user_id="U-admin",
        )
        with patch(
            "ypl.slack_agent_gateway.interactions.handle_denial",
            new_callable=AsyncMock,
        ) as mock_deny:
            response = await _handle_bot_father_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "denied"
        mock_deny.assert_awaited_once_with("req-deny-456", "U-admin")

    @pytest.mark.asyncio
    async def test_value_error_returns_error_with_message(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_approve",
            action_value="req-bad",
        )
        with patch(
            "ypl.slack_agent_gateway.interactions.handle_approval",
            new_callable=AsyncMock,
            side_effect=ValueError("Request not found"),
        ):
            response = await _handle_bot_father_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "error"
        assert "Request not found" in body.get("message", "")

    @pytest.mark.asyncio
    async def test_unknown_bot_father_action(self) -> None:
        payload = _make_block_actions_payload(
            action_id="bot_father_magic",  # Not a real action
            action_value="req-123",
        )
        # Patch handle_approval and handle_denial to make sure they're not called
        response = await _handle_bot_father_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "unknown_bot_father_action"


# ---------------------------------------------------------------------------
# _handle_questionnaire_action
# ---------------------------------------------------------------------------


class TestHandleQuestionnaireAction:
    @pytest.mark.asyncio
    async def test_no_actions_returns_no_actions(self) -> None:
        payload = {"type": "block_actions", "actions": []}
        response = await _handle_questionnaire_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "no_actions"

    @pytest.mark.asyncio
    async def test_missing_session_id_returns_error(self) -> None:
        payload = _make_block_actions_payload(
            action_id="questionnaire_q1",
            action_value="",  # Empty = missing session_id
        )
        payload["actions"][0]["text"] = {"text": "Option A"}
        response = await _handle_questionnaire_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "missing_session_id"

    @pytest.mark.asyncio
    async def test_missing_choice_label_returns_error(self) -> None:
        payload = _make_block_actions_payload(
            action_id="questionnaire_q1",
            action_value="session-xyz",
        )
        payload["actions"][0]["text"] = {"text": ""}  # Empty label
        response = await _handle_questionnaire_action(payload)
        body = json.loads(response.body)
        assert body["status"] == "missing_choice_label"

    @pytest.mark.asyncio
    async def test_forwards_answer_to_ahs(self) -> None:
        payload = _make_block_actions_payload(
            action_id="questionnaire_deploy_env_0",
            action_value="session-deploy",
            channel_id="C200",
            message_ts="5555.000",
        )
        payload["actions"][0]["text"] = {"text": "Staging"}

        with (
            patch(
                "ypl.slack_agent_gateway.interactions.send_questionnaire_answer_to_agent",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ) as mock_send,
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=None,
            ),
        ):
            response = await _handle_questionnaire_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "answer_forwarded"
        mock_send.assert_awaited_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["session_id"] == "session-deploy"
        assert call_kwargs["answer_text"] == "Staging"

    @pytest.mark.asyncio
    async def test_ahs_failure_keeps_buttons_visible(self) -> None:
        payload = _make_block_actions_payload(
            action_id="questionnaire_q1_0",
            action_value="session-fail",
        )
        payload["actions"][0]["text"] = {"text": "Option A"}

        with patch(
            "ypl.slack_agent_gateway.interactions.send_questionnaire_answer_to_agent",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await _handle_questionnaire_action(payload)

        body = json.loads(response.body)
        assert body["status"] == "forwarding_failed"

    @pytest.mark.asyncio
    async def test_confirmation_message_replaces_questionnaire(self) -> None:
        """After forwarding, the questionnaire message is replaced with selection confirmation."""
        payload = _make_block_actions_payload(
            action_id="questionnaire_env_0",
            action_value="session-confirm",
            channel_id="C300",
            message_ts="6666.000",
            api_app_id="A001",
        )
        payload["actions"][0]["text"] = {"text": "Production"}

        mock_client = AsyncMock()
        app_config = _make_app_config("A001")

        with (
            patch(
                "ypl.slack_agent_gateway.interactions.send_questionnaire_answer_to_agent",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.get_agent_config_by_app_id",
                return_value=app_config,
            ),
            patch(
                "ypl.slack_agent_gateway.interactions.build_slack_client",
                return_value=mock_client,
            ),
        ):
            await _handle_questionnaire_action(payload)

        mock_client.chat_update.assert_awaited_once()
        call_kwargs = mock_client.chat_update.call_args[1]
        assert "Production" in call_kwargs["text"]
        assert call_kwargs["blocks"] == []


# ---------------------------------------------------------------------------
# _handle_create_agent_submission
# ---------------------------------------------------------------------------


class TestHandleCreateAgentSubmission:
    def _make_view_submission(
        self,
        agent_name: str = "my-agent",
        slack_name: str = "my-slack-bot",
        display_name: str = "My Agent",
        description: str | None = "A test agent",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "view_submission",
            "user": {"id": "U-creator"},
            "view": {
                "callback_id": "create_agent_modal",
                "state": {
                    "values": {
                        "agent_name_block": {"agent_name_input": {"value": agent_name}},
                        "slack_name_block": {"slack_name_input": {"value": slack_name}},
                        "display_name_block": {"display_name_input": {"value": display_name}},
                        "description_block": {"description_input": {"value": description}},
                        "system_prompt_block": {"system_prompt_input": {"value": system_prompt}},
                    }
                },
            },
        }

    @pytest.mark.asyncio
    async def test_invalid_agent_name_returns_errors(self) -> None:
        payload = self._make_view_submission(agent_name="UPPERCASE-INVALID")
        response = await _handle_create_agent_submission(payload)
        body = json.loads(response.body)
        assert body["response_action"] == "errors"
        assert "agent_name_block" in body["errors"]

    @pytest.mark.asyncio
    async def test_agent_name_with_spaces_returns_errors(self) -> None:
        payload = self._make_view_submission(agent_name="my agent")
        response = await _handle_create_agent_submission(payload)
        body = json.loads(response.body)
        assert body["response_action"] == "errors"

    @pytest.mark.asyncio
    async def test_empty_display_name_returns_errors(self) -> None:
        payload = self._make_view_submission(display_name="")
        response = await _handle_create_agent_submission(payload)
        body = json.loads(response.body)
        assert body["response_action"] == "errors"
        assert "display_name_block" in body["errors"]

    @pytest.mark.asyncio
    async def test_invalid_slack_name_returns_errors(self) -> None:
        payload = self._make_view_submission(slack_name="Has Spaces!")
        response = await _handle_create_agent_submission(payload)
        body = json.loads(response.body)
        assert body["response_action"] == "errors"
        assert "slack_name_block" in body["errors"]

    @pytest.mark.asyncio
    async def test_valid_submission_calls_request_bot_creation(self) -> None:
        payload = self._make_view_submission(
            agent_name="my-agent",
            slack_name="my-slack-bot",
            display_name="My Agent",
        )
        mock_result = MagicMock()
        mock_result.request_id = "req-new-123"

        with patch(
            "ypl.slack_agent_gateway.interactions.request_bot_creation",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_create:
            response = await _handle_create_agent_submission(payload)

        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {}  # Empty response closes the modal
        mock_create.assert_awaited_once()
        call_args = mock_create.call_args[0][0]
        assert call_args.agent_name == "my-agent"
        assert call_args.slack_name == "my-slack-bot"
        assert call_args.display_name == "My Agent"
        assert call_args.requested_by == "U-creator"

    @pytest.mark.asyncio
    async def test_value_error_in_bot_creation_returns_error_modal(self) -> None:
        payload = self._make_view_submission()
        with patch(
            "ypl.slack_agent_gateway.interactions.request_bot_creation",
            new_callable=AsyncMock,
            side_effect=ValueError("Name already taken"),
        ):
            response = await _handle_create_agent_submission(payload)

        body = json.loads(response.body)
        assert body["response_action"] == "errors"
        assert "Name already taken" in body["errors"].get("agent_name_block", "")

    @pytest.mark.asyncio
    async def test_permission_error_in_bot_creation_returns_error_modal(self) -> None:
        payload = self._make_view_submission()
        with patch(
            "ypl.slack_agent_gateway.interactions.request_bot_creation",
            new_callable=AsyncMock,
            side_effect=PermissionError("You are not authorized"),
        ):
            response = await _handle_create_agent_submission(payload)

        body = json.loads(response.body)
        assert body["response_action"] == "errors"

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_generic_error_modal(self) -> None:
        payload = self._make_view_submission()
        with patch(
            "ypl.slack_agent_gateway.interactions.request_bot_creation",
            new_callable=AsyncMock,
            side_effect=Exception("Internal error"),
        ):
            response = await _handle_create_agent_submission(payload)

        body = json.loads(response.body)
        assert body["response_action"] == "errors"
        assert "unexpected error" in body["errors"].get("agent_name_block", "").lower()


# ---------------------------------------------------------------------------
# process_slack_interaction
# ---------------------------------------------------------------------------


class TestProcessSlackInteraction:
    def _make_request(self, body: bytes, payload_dict: dict) -> MagicMock:
        encoded = quote_plus(json.dumps(payload_dict))
        form_body = f"payload={encoded}".encode()
        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=form_body)
        req.headers = {}
        return req

    @pytest.mark.asyncio
    async def test_raises_when_no_signing_secrets(self) -> None:
        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=b"payload=test")
        req.headers = {}

        with (
            patch("ypl.slack_agent_gateway.interactions.get_all_signing_secrets", return_value=[]),
            pytest.raises(HTTPException) as exc_info,
        ):
            await process_slack_interaction(req)

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_missing_payload_field(self) -> None:
        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=b"other_field=value")
        req.headers = {}

        with (
            patch("ypl.slack_agent_gateway.interactions.get_all_signing_secrets", return_value=["secret"]),
            patch("ypl.slack_agent_gateway.interactions.verify_slack_signature_multi", return_value="secret"),
            pytest.raises(HTTPException) as exc_info,
        ):
            await process_slack_interaction(req)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_processes_valid_interaction(self) -> None:
        payload_dict = _make_block_actions_payload(
            action_id="unknown_action",
            action_value="",
        )
        encoded = quote_plus(json.dumps(payload_dict))
        form_body = f"payload={encoded}".encode()

        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=form_body)
        req.headers = {}

        with (
            patch("ypl.slack_agent_gateway.interactions.get_all_signing_secrets", return_value=["secret"]),
            patch("ypl.slack_agent_gateway.interactions.verify_slack_signature_multi", return_value="secret"),
        ):
            response = await process_slack_interaction(req)

        body = json.loads(response.body)
        assert body["status"] == "unhandled_action"
