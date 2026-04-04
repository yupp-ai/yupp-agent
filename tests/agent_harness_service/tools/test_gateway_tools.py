"""Tests for gateway_tools.py — Slack integration MCP tools."""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.tools.gateway_tools import (
    ask_question as _ask_question_tool,
)
from ypl.agent_harness_service.tools.gateway_tools import (
    request_feedback as _request_feedback_tool,
)
from ypl.agent_harness_service.tools.gateway_tools import (
    send_slack_message as _send_slack_message_tool,
)

# Unwrap FunctionTool to get raw callables
ask_question = _ask_question_tool.fn
request_feedback = _request_feedback_tool.fn
send_slack_message = _send_slack_message_tool.fn

VALID_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SLACK_SESSION = "C123ABC:1234567890.123456:A99XYZ"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway_registry(gateway: MagicMock | None) -> MagicMock:
    """Create a mock GatewayRegistry class where get_instance().get('slack') returns gateway."""
    registry_instance = MagicMock()
    registry_instance.get.return_value = gateway
    registry_class = MagicMock()
    registry_class.get_instance.return_value = registry_instance
    return registry_class


def _make_slack_gateway(success: bool = True, error: str | None = None) -> MagicMock:
    gw = AsyncMock()
    gw.request_feedback.return_value = success
    gw.send_questionnaire.return_value = success
    send_result = MagicMock()
    send_result.success = success
    send_result.error = error
    send_result.destination = "C123ABC"
    send_result.message_id = "1234567890.123456"
    gw.send_message.return_value = send_result
    return gw


# ---------------------------------------------------------------------------
# request_feedback
# ---------------------------------------------------------------------------


class TestRequestFeedback:
    async def test_success(self) -> None:
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await request_feedback(VALID_SESSION)

        assert result == {"status": "ok"}

    async def test_no_slack_session_returns_error(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
            new=AsyncMock(return_value=None),
        ):
            result = await request_feedback(VALID_SESSION)

        assert result["status"] == "error"
        assert "No Slack session" in result["error"]

    async def test_no_gateway_returns_error(self) -> None:
        registry_class = _make_gateway_registry(None)  # No gateway registered

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await request_feedback(VALID_SESSION)

        assert result["status"] == "error"
        assert "not registered" in result["error"]

    async def test_gateway_returns_false(self) -> None:
        gateway = _make_slack_gateway(success=False)
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await request_feedback(VALID_SESSION)

        assert result["status"] == "error"
        assert "rejected" in result["error"]

    async def test_gateway_raises(self) -> None:
        gateway = AsyncMock()
        gateway.request_feedback.side_effect = Exception("connection lost")
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await request_feedback(VALID_SESSION)

        assert result["status"] == "error"
        assert "connection lost" in result["error"]

    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            await request_feedback("not-a-uuid")


# ---------------------------------------------------------------------------
# ask_question
# ---------------------------------------------------------------------------


class TestAskQuestion:
    async def test_success(self) -> None:
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await ask_question(
                session_id=VALID_SESSION,
                question_id="deploy_env",
                text="Which environment?",
                choices=["prod", "staging", "dev"],
            )

        assert result["status"] == "ok"

    async def test_empty_choices_returns_error(self) -> None:
        result = await ask_question(
            session_id=VALID_SESSION,
            question_id="q1",
            text="What?",
            choices=[],
        )
        assert result["status"] == "error"
        assert "empty" in result["error"]

    async def test_too_many_choices_returns_error(self) -> None:
        result = await ask_question(
            session_id=VALID_SESSION,
            question_id="q1",
            text="What?",
            choices=["a", "b", "c", "d", "e", "f"],  # 6 items exceeds max 5
        )
        assert result["status"] == "error"
        assert "5" in result["error"]

    async def test_exactly_5_choices_allowed(self) -> None:
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await ask_question(
                session_id=VALID_SESSION,
                question_id="q1",
                text="Pick one",
                choices=["a", "b", "c", "d", "e"],
            )

        assert result["status"] == "ok"

    async def test_no_slack_session_returns_error(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
            new=AsyncMock(return_value=None),
        ):
            result = await ask_question(
                session_id=VALID_SESSION,
                question_id="q1",
                text="Q?",
                choices=["a"],
            )

        assert result["status"] == "error"
        assert "No Slack session" in result["error"]

    async def test_choices_converted_to_label_value_dicts(self) -> None:
        """Choices are converted to {label, value} dicts before sending to gateway."""
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)

        with (
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_slack_session_id",
                new=AsyncMock(return_value=SLACK_SESSION),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            await ask_question(
                session_id=VALID_SESSION,
                question_id="q1",
                text="Q?",
                choices=["yes", "no"],
            )

        call_kwargs = gateway.send_questionnaire.call_args[1]
        assert call_kwargs["choices"] == [{"label": "yes", "value": "yes"}, {"label": "no", "value": "no"}]

    async def test_invalid_session_raises(self) -> None:
        with pytest.raises(ValueError):
            await ask_question(
                session_id="bad-uuid",
                question_id="q1",
                text="Q?",
                choices=["a"],
            )


# ---------------------------------------------------------------------------
# send_slack_message
# ---------------------------------------------------------------------------


class TestSendSlackMessage:
    async def test_success_with_explicit_channel(self) -> None:
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)
        mock_db = AsyncMock()
        mock_db_cm = AsyncMock()
        mock_db_cm.__aenter__.return_value = mock_db

        mock_var = MagicMock()
        mock_var.get.return_value = VALID_SESSION
        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": "sre"}),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
            patch("ypl.agent_harness_service.tools.gateway_tools.get_async_session", return_value=mock_db_cm),
        ):
            result = await send_slack_message(text="Hello!", channel="alert-backend")

        assert result["status"] == "ok"
        assert result["channel"] == "C123ABC"

    async def test_no_session_id_returns_error(self) -> None:
        mock_var = MagicMock()
        mock_var.get.return_value = None

        with patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var):
            result = await send_slack_message(text="Hello!", channel="test-channel")

        assert result["status"] == "error"
        assert "session_id is required" in result["error"]

    async def test_no_channel_no_project_returns_error(self) -> None:
        mock_var = MagicMock()
        mock_var.get.return_value = VALID_SESSION

        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": "sre"}),
            ),
        ):
            result = await send_slack_message(text="Hello!")

        assert result["status"] == "error"
        assert "channel is required" in result["error"]

    async def test_gateway_not_registered_returns_error(self) -> None:
        registry_class = _make_gateway_registry(None)
        mock_var = MagicMock()
        mock_var.get.return_value = VALID_SESSION

        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": "sre"}),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await send_slack_message(text="Hi", channel="general")

        assert result["status"] == "error"
        assert "not registered" in result["error"]

    async def test_agent_name_not_found_returns_error(self) -> None:
        mock_var = MagicMock()
        mock_var.get.return_value = VALID_SESSION

        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": None}),
            ),
        ):
            result = await send_slack_message(text="Hi", channel="general")

        assert result["status"] == "error"
        assert "agent name" in result["error"].lower()

    async def test_gateway_send_failure(self) -> None:
        gateway = _make_slack_gateway(success=False, error="Slack API error")
        registry_class = _make_gateway_registry(gateway)
        mock_var = MagicMock()
        mock_var.get.return_value = VALID_SESSION

        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": "sre"}),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
        ):
            result = await send_slack_message(text="Hi", channel="general")

        assert result["status"] == "error"
        assert "Slack API error" in result["error"]

    async def test_session_id_from_tool_parameter_as_fallback(self) -> None:
        """Falls back to session_id parameter when context var not set."""
        gateway = _make_slack_gateway(success=True)
        registry_class = _make_gateway_registry(gateway)
        mock_db = AsyncMock()
        mock_db_cm = AsyncMock()
        mock_db_cm.__aenter__.return_value = mock_db
        mock_var = MagicMock()
        mock_var.get.return_value = None  # Context var not set

        with (
            patch("ypl.agent_harness_service.tools.gateway_tools.mcp_session_id_var", mock_var),
            patch(
                "ypl.agent_harness_service.tools.gateway_tools._resolve_parent_session",
                new=AsyncMock(return_value={"agent_name": "sre"}),
            ),
            patch("ypl.agent_harness_service.gateway.GatewayRegistry", registry_class),
            patch("ypl.agent_harness_service.tools.gateway_tools.get_async_session", return_value=mock_db_cm),
        ):
            result = await send_slack_message(text="Hi", channel="general", session_id=VALID_SESSION)

        assert result["status"] == "ok"
