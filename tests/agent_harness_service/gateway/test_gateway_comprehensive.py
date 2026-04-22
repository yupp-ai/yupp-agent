"""Comprehensive unit tests for the AHS gateway/ package.

Covers:
- SlackGateway: message delivery, callback registration, dead-session optimisation
- GatewayRegistry: singleton, registration, policy resolution
- init_gateways: idempotency, env-var wiring
- slack_prefetch: pure formatting helpers and fetch_slack_thread_content

All external I/O (httpx, Slack SDK) is mocked.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.gateway.base import Gateway, GatewaySendResult
from ypl.agent_harness_service.gateway.registry import TRIGGER_TO_GATEWAY, GatewayConfig, GatewayRegistry
from ypl.agent_harness_service.gateway.slack import SlackGateway
from ypl.agent_harness_service.gateway.slack_prefetch import (
    _extract_attachment_text,
    _extract_block_text,
    _format_messages,
)

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

BASE_URL = "http://fake-sag:8080"
SESSION_ID = "sess-abc123"


@pytest.fixture
def gateway() -> SlackGateway:
    config = GatewayConfig(
        name="slack",
        gateway_type="slack",
        settings={"base_url": BASE_URL, "api_key": "test-key"},
    )
    return SlackGateway(config)


@pytest.fixture
def gateway_no_key() -> SlackGateway:
    config = GatewayConfig(
        name="slack",
        gateway_type="slack",
        settings={"base_url": BASE_URL},
    )
    return SlackGateway(config)


def _ok_response(extra: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock httpx.Response with success=True."""
    resp = MagicMock()
    data = {"success": True, "message_ts": "1234567890.000001"}
    if extra:
        data.update(extra)
    resp.json.return_value = data
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


def _fail_response(error: str = "Bad request") -> MagicMock:
    """Return a mock httpx.Response with success=False."""
    resp = MagicMock()
    resp.json.return_value = {"success": False, "error": error}
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    raw_resp = httpx.Response(status_code)
    return httpx.HTTPStatusError(
        message=f"HTTP {status_code}",
        request=MagicMock(),
        response=raw_resp,
    )


def _mock_client(response: Any) -> AsyncMock:
    """Return a mock httpx.AsyncClient whose post() returns *response*."""
    client = AsyncMock()
    client.is_closed = False
    client.post = AsyncMock(return_value=response)
    client.is_closed = False
    return client


def _agent_config(allowed_gateways: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        name="test-agent",
        config_dir="/tmp/fake",
        allowed_gateways=allowed_gateways if allowed_gateways is not None else ["*"],
    )


# ===========================================================================
# SlackGateway — headers & client lifecycle
# ===========================================================================


class TestSlackGatewayInit:
    def test_name(self, gateway: SlackGateway) -> None:
        assert gateway.name == "slack"

    def test_base_url(self, gateway: SlackGateway) -> None:
        assert gateway._base_url == BASE_URL

    def test_api_key_present(self, gateway: SlackGateway) -> None:
        assert gateway._api_key == "test-key"

    def test_no_api_key(self, gateway_no_key: SlackGateway) -> None:
        assert gateway_no_key._api_key is None

    def test_dead_sessions_initially_empty(self, gateway: SlackGateway) -> None:
        assert len(gateway._dead_session_ids) == 0

    def test_headers_with_api_key(self, gateway: SlackGateway) -> None:
        headers = gateway._headers()
        assert headers["Content-Type"] == "application/json"
        assert headers["X-API-Key"] == "test-key"

    def test_headers_without_api_key(self, gateway_no_key: SlackGateway) -> None:
        headers = gateway_no_key._headers()
        assert headers["Content-Type"] == "application/json"
        assert "X-API-Key" not in headers

    def test_get_client_creates_instance(self, gateway: SlackGateway) -> None:
        client = gateway._get_client()
        assert isinstance(client, httpx.AsyncClient)

    def test_get_client_reuses_same_instance(self, gateway: SlackGateway) -> None:
        c1 = gateway._get_client()
        c2 = gateway._get_client()
        assert c1 is c2

    def test_get_client_recreates_if_closed(self, gateway: SlackGateway) -> None:
        gateway._get_client()  # create initial client
        # Simulate a closed client by replacing with a mock that reports is_closed=True
        mock_closed = MagicMock()
        mock_closed.is_closed = True
        gateway._client = mock_closed
        c2 = gateway._get_client()
        assert isinstance(c2, httpx.AsyncClient)
        assert c2 is not mock_closed


# ===========================================================================
# SlackGateway — send_reply
# ===========================================================================


class TestSendReply:
    @pytest.mark.asyncio
    async def test_success(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response())
        result = await gateway.send_reply(SESSION_ID, "hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_success_with_reply_type(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        result = await gateway.send_reply(SESSION_ID, "thinking…", reply_type="thinking")
        assert result is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["reply_type"] == "thinking"

    @pytest.mark.asyncio
    async def test_success_with_username(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        result = await gateway.send_reply(SESSION_ID, "hi", username="MyBot")
        assert result is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["username"] == "MyBot"

    @pytest.mark.asyncio
    async def test_payload_excludes_optional_fields_when_none(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_reply(SESSION_ID, "text")
        _, kwargs = client.post.call_args
        assert "reply_type" not in kwargs["json"]
        assert "username" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_gateway_rejected(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("Session not found"))
        result = await gateway.send_reply(SESSION_ID, "hi")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        gateway._client = client
        result = await gateway.send_reply(SESSION_ID, "hi")
        assert result is False

    @pytest.mark.asyncio
    async def test_http_status_error(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(500))
        gateway._client = client
        result = await gateway.send_reply(SESSION_ID, "hi")
        assert result is False

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=RuntimeError("boom"))
        gateway._client = client
        result = await gateway.send_reply(SESSION_ID, "hi")
        assert result is False

    @pytest.mark.asyncio
    async def test_correct_url(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_reply(SESSION_ID, "hi")
        args, _ = client.post.call_args
        assert args[0] == f"{BASE_URL}/slack-agent-gateway/sessions/reply"


# ===========================================================================
# SlackGateway — append_reply
# ===========================================================================


class TestAppendReply:
    @pytest.mark.asyncio
    async def test_success(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response({"buffered": True}))
        result = await gateway.append_reply(SESSION_ID, "more text")
        assert result is True

    @pytest.mark.asyncio
    async def test_success_with_reply_type_and_username(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        result = await gateway.append_reply(SESSION_ID, "text", reply_type="tool_use", username="Bot")
        assert result is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["reply_type"] == "tool_use"
        assert kwargs["json"]["username"] == "Bot"

    @pytest.mark.asyncio
    async def test_gateway_rejected(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response())
        assert await gateway.append_reply(SESSION_ID, "text") is False

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        gateway._client = client
        assert await gateway.append_reply(SESSION_ID, "text") is False

    @pytest.mark.asyncio
    async def test_http_error(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(503))
        gateway._client = client
        assert await gateway.append_reply(SESSION_ID, "text") is False

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=ValueError("bad"))
        gateway._client = client
        assert await gateway.append_reply(SESSION_ID, "text") is False

    @pytest.mark.asyncio
    async def test_correct_url(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.append_reply(SESSION_ID, "hi")
        args, _ = client.post.call_args
        assert args[0] == f"{BASE_URL}/slack-agent-gateway/sessions/reply/append"


# ===========================================================================
# SlackGateway — request_feedback
# ===========================================================================


class TestRequestFeedback:
    @pytest.mark.asyncio
    async def test_success(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response())
        assert await gateway.request_feedback(SESSION_ID) is True

    @pytest.mark.asyncio
    async def test_payload_contains_session_id(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.request_feedback(SESSION_ID)
        _, kwargs = client.post.call_args
        assert kwargs["json"] == {"session_id": SESSION_ID}

    @pytest.mark.asyncio
    async def test_gateway_rejected(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("not ready"))
        assert await gateway.request_feedback(SESSION_ID) is False

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        gateway._client = client
        assert await gateway.request_feedback(SESSION_ID) is False

    @pytest.mark.asyncio
    async def test_http_error(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(502))
        gateway._client = client
        assert await gateway.request_feedback(SESSION_ID) is False

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=OSError("conn refused"))
        gateway._client = client
        assert await gateway.request_feedback(SESSION_ID) is False


# ===========================================================================
# SlackGateway — send_message
# ===========================================================================


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_success_minimal(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response({"channel": "C123"}))
        result = await gateway.send_message("my-agent", "C123", "Hello channel")
        assert isinstance(result, GatewaySendResult)
        assert result.success is True
        assert result.message_id == "1234567890.000001"
        assert result.destination == "C123"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_success_with_thread_id(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response({"channel": "C123"}))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "reply", thread_id="1234.5678")
        assert result.success is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["thread_ts"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_success_with_ahs_session_id(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response({"channel": "C123"}))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "msg", ahs_session_id="sess-uuid")
        assert result.success is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["ahs_session_id"] == "sess-uuid"

    @pytest.mark.asyncio
    async def test_success_with_username(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response({"channel": "C123"}))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "msg", username="CustomBot")
        assert result.success is True
        _, kwargs = client.post.call_args
        assert kwargs["json"]["username"] == "CustomBot"

    @pytest.mark.asyncio
    async def test_thread_id_none_not_in_payload(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response({"channel": "C123"}))
        gateway._client = client
        await gateway.send_message("agent", "C123", "msg")
        _, kwargs = client.post.call_args
        assert "thread_ts" not in kwargs["json"]
        assert "ahs_session_id" not in kwargs["json"]
        assert "username" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_empty_string_thread_id_not_in_payload(self, gateway: SlackGateway) -> None:
        """Verify that falsy-but-not-None values (empty string) are also excluded."""
        client = _mock_client(_ok_response({"channel": "C123"}))
        gateway._client = client
        await gateway.send_message("agent", "C123", "msg", thread_id="", ahs_session_id="", username="")
        _, kwargs = client.post.call_args
        assert "thread_ts" not in kwargs["json"]
        assert "ahs_session_id" not in kwargs["json"]
        assert "username" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_gateway_rejected(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("Channel not found"))
        result = await gateway.send_message("agent", "C999", "msg")
        assert result.success is False
        assert "Channel not found" in (result.error or "")
        assert result.destination == "C999"

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "msg")
        assert result.success is False
        assert result.error == "Timeout"

    @pytest.mark.asyncio
    async def test_http_error(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(400))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "msg")
        assert result.success is False
        assert "400" in (result.error or "")

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=RuntimeError("oops"))
        gateway._client = client
        result = await gateway.send_message("agent", "C123", "msg")
        assert result.success is False
        assert result.error == "Unexpected error"

    @pytest.mark.asyncio
    async def test_destination_fallback_when_channel_absent(self, gateway: SlackGateway) -> None:
        """When SAG doesn't echo channel, destination falls back to the input."""
        resp_data = {"success": True, "message_ts": "123"}
        gateway._client = _mock_client(MagicMock(json=MagicMock(return_value=resp_data), raise_for_status=MagicMock()))
        result = await gateway.send_message("agent", "C123", "msg")
        assert result.destination == "C123"


# ===========================================================================
# SlackGateway — send_questionnaire
# ===========================================================================


class TestSendQuestionnaire:
    CHOICES = [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]

    @pytest.mark.asyncio
    async def test_success(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response())
        result = await gateway.send_questionnaire(SESSION_ID, "q1", "Pick one", self.CHOICES)
        assert result is True

    @pytest.mark.asyncio
    async def test_payload_structure(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_questionnaire(SESSION_ID, "q1", "Pick one", self.CHOICES, allow_free_text=False)
        _, kwargs = client.post.call_args
        payload = kwargs["json"]
        assert payload["session_id"] == SESSION_ID
        assert payload["question_id"] == "q1"
        assert payload["text"] == "Pick one"
        assert payload["choices"] == self.CHOICES
        assert payload["allow_free_text"] is False

    @pytest.mark.asyncio
    async def test_allow_free_text_default_true(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_questionnaire(SESSION_ID, "q1", "Pick", self.CHOICES)
        _, kwargs = client.post.call_args
        assert kwargs["json"]["allow_free_text"] is True

    @pytest.mark.asyncio
    async def test_gateway_rejected(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("invalid"))
        result = await gateway.send_questionnaire(SESSION_ID, "q1", "Pick", self.CHOICES)
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        gateway._client = client
        assert await gateway.send_questionnaire(SESSION_ID, "q1", "Pick", self.CHOICES) is False

    @pytest.mark.asyncio
    async def test_http_error(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(429))
        gateway._client = client
        assert await gateway.send_questionnaire(SESSION_ID, "q1", "Pick", self.CHOICES) is False

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=ConnectionError("refused"))
        gateway._client = client
        assert await gateway.send_questionnaire(SESSION_ID, "q1", "Pick", self.CHOICES) is False


# ===========================================================================
# SlackGateway — send_tool_event
# ===========================================================================


class TestSendToolEvent:
    @pytest.mark.asyncio
    async def test_success_start_event(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response())
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Bash", command="ls /")
        assert result is True

    @pytest.mark.asyncio
    async def test_success_result_event(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_ok_response())
        result = await gateway.send_tool_event(
            SESSION_ID, "result", "tool-123", result_status="done", result_content="file.txt"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_dead_session_skips_http(self, gateway: SlackGateway) -> None:
        gateway._dead_session_ids.add(SESSION_ID)
        client = AsyncMock()
        client.is_closed = False
        gateway._client = client
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Read")
        assert result is False
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_not_found_marks_dead(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("Session not found"))
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Bash")
        assert result is False
        assert SESSION_ID in gateway._dead_session_ids

    @pytest.mark.asyncio
    async def test_other_rejection_does_not_mark_dead(self, gateway: SlackGateway) -> None:
        gateway._client = _mock_client(_fail_response("rate limited"))
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Bash")
        assert result is False
        assert SESSION_ID not in gateway._dead_session_ids

    @pytest.mark.asyncio
    async def test_404_http_error_marks_dead(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(404))
        gateway._client = client
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Bash")
        assert result is False
        assert SESSION_ID in gateway._dead_session_ids

    @pytest.mark.asyncio
    async def test_non_404_http_error_does_not_mark_dead(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=_http_status_error(500))
        gateway._client = client
        result = await gateway.send_tool_event(SESSION_ID, "start", "tool-123", name="Bash")
        assert result is False
        assert SESSION_ID not in gateway._dead_session_ids

    @pytest.mark.asyncio
    async def test_timeout(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
        gateway._client = client
        assert await gateway.send_tool_event(SESSION_ID, "start", "tool-123") is False

    @pytest.mark.asyncio
    async def test_generic_exception(self, gateway: SlackGateway) -> None:
        client = AsyncMock()
        client.is_closed = False
        client.post = AsyncMock(side_effect=RuntimeError("boom"))
        gateway._client = client
        assert await gateway.send_tool_event(SESSION_ID, "start", "tool-123") is False

    @pytest.mark.asyncio
    async def test_payload_optional_fields(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_tool_event(
            SESSION_ID,
            "result",
            "tid",
            name="MyTool",
            command="run it",
            result_status="failed",
            error_msg="oops",
            result_content="partial",
        )
        _, kwargs = client.post.call_args
        payload = kwargs["json"]
        assert payload["name"] == "MyTool"
        assert payload["command"] == "run it"
        assert payload["result_status"] == "failed"
        assert payload["error_msg"] == "oops"
        assert payload["result_content"] == "partial"

    @pytest.mark.asyncio
    async def test_payload_excludes_none_optional_fields(self, gateway: SlackGateway) -> None:
        client = _mock_client(_ok_response())
        gateway._client = client
        await gateway.send_tool_event(SESSION_ID, "start", "tid")
        _, kwargs = client.post.call_args
        payload = kwargs["json"]
        assert "name" not in payload
        assert "command" not in payload
        assert "result_status" not in payload
        assert "error_msg" not in payload
        assert "result_content" not in payload


# ===========================================================================
# GatewayRegistry
# ===========================================================================


class TestGatewayRegistry:
    def setup_method(self) -> None:
        GatewayRegistry.reset()

    def teardown_method(self) -> None:
        GatewayRegistry.reset()

    def _make_registry(self) -> GatewayRegistry:
        return GatewayRegistry()

    def _make_gateway(self, name: str = "slack") -> SlackGateway:
        return SlackGateway(GatewayConfig(name=name, gateway_type="slack", settings={"base_url": BASE_URL}))

    def test_register_and_get(self) -> None:
        registry = self._make_registry()
        gw = self._make_gateway("slack")
        registry.register(gw)
        assert registry.get("slack") is gw

    def test_get_missing_returns_none(self) -> None:
        registry = self._make_registry()
        assert registry.get("nonexistent") is None

    def test_register_multiple(self) -> None:
        registry = self._make_registry()
        gw1 = self._make_gateway("slack")
        gw2 = self._make_gateway("slack2")
        registry.register(gw1)
        registry.register(gw2)
        assert registry.get("slack") is gw1
        assert registry.get("slack2") is gw2

    def test_register_from_config_slack(self) -> None:
        registry = self._make_registry()
        config = GatewayConfig(name="slack", gateway_type="slack", settings={"base_url": BASE_URL})
        registry.register_from_config(config)
        gw = registry.get("slack")
        assert isinstance(gw, SlackGateway)
        assert gw.name == "slack"

    def test_register_from_config_unknown_type(self) -> None:
        registry = self._make_registry()
        config = GatewayConfig(name="web", gateway_type="sse", settings={})
        registry.register_from_config(config)
        assert registry.get("web") is None

    def test_register_from_config_disabled_skips(self) -> None:
        registry = self._make_registry()
        config = GatewayConfig(
            name="slack",
            gateway_type="slack",
            enabled=False,
            settings={"base_url": BASE_URL},
        )
        registry.register_from_config(config)
        assert registry.get("slack") is None

    def test_get_for_session_wildcard_allowed(self) -> None:
        registry = self._make_registry()
        gw = self._make_gateway("slack")
        registry.register(gw)
        agent = _agent_config(["*"])
        result = registry.get_for_session("slack", agent)
        assert result is gw

    def test_get_for_session_explicit_allowed(self) -> None:
        registry = self._make_registry()
        gw = self._make_gateway("slack")
        registry.register(gw)
        agent = _agent_config(["slack"])
        result = registry.get_for_session("slack", agent)
        assert result is gw

    def test_get_for_session_not_in_allowed(self) -> None:
        registry = self._make_registry()
        gw = self._make_gateway("slack")
        registry.register(gw)
        agent = _agent_config(["web"])  # slack not allowed
        result = registry.get_for_session("slack", agent)
        assert result is None

    def test_get_for_session_allowed_but_not_registered(self) -> None:
        registry = self._make_registry()
        agent = _agent_config(["slack"])
        result = registry.get_for_session("slack", agent)
        assert result is None

    def test_get_all_for_agent_wildcard(self) -> None:
        registry = self._make_registry()
        gw1 = self._make_gateway("slack")
        gw2 = self._make_gateway("slack2")
        registry.register(gw1)
        registry.register(gw2)
        agent = _agent_config(["*"])
        result = registry.get_all_for_agent(agent)
        assert len(result) == 2

    def test_get_all_for_agent_specific_list(self) -> None:
        registry = self._make_registry()
        gw1 = self._make_gateway("slack")
        gw2 = self._make_gateway("web")
        registry.register(gw1)
        registry.register(gw2)
        agent = _agent_config(["slack"])
        result = registry.get_all_for_agent(agent)
        assert result == [gw1]

    def test_get_all_for_agent_empty_allowed(self) -> None:
        registry = self._make_registry()
        gw = self._make_gateway("slack")
        registry.register(gw)
        agent = _agent_config([])
        result = registry.get_all_for_agent(agent)
        assert result == []

    def test_reset_clears_singleton(self) -> None:
        # Patch init_gateways to be a no-op so singleton creation doesn't fail on missing env
        with patch("ypl.agent_harness_service.gateway.init_gateways"):
            inst1 = GatewayRegistry.get_instance()
            # Register a gateway so we can verify reset clears it
            gw = self._make_gateway("slack")
            inst1.register(gw)
            assert inst1.get("slack") is gw

            GatewayRegistry.reset()
            inst2 = GatewayRegistry.get_instance()
            assert inst1 is not inst2
            # Verify the new instance has no registered gateways
            assert inst2.get("slack") is None


# ===========================================================================
# TRIGGER_TO_GATEWAY constant
# ===========================================================================


class TestTriggerToGateway:
    def test_slack_maps_to_slack(self) -> None:
        assert TRIGGER_TO_GATEWAY["SLACK"] == "slack"

    def test_api_maps_to_none(self) -> None:
        assert TRIGGER_TO_GATEWAY["API"] is None

    def test_cron_maps_to_none(self) -> None:
        assert TRIGGER_TO_GATEWAY["CRON"] is None

    def test_webhook_maps_to_none(self) -> None:
        assert TRIGGER_TO_GATEWAY["WEBHOOK"] is None


# ===========================================================================
# init_gateways
# ===========================================================================


class TestInitGateways:
    def setup_method(self) -> None:
        GatewayRegistry.reset()
        # Reset the _initialized flag in the __init__ module
        import ypl.agent_harness_service.gateway as gw_module

        gw_module._initialized = False

    def teardown_method(self) -> None:
        GatewayRegistry.reset()
        import ypl.agent_harness_service.gateway as gw_module

        gw_module._initialized = False

    def test_registers_slack_when_base_url_set(self) -> None:
        from ypl.agent_harness_service.gateway import init_gateways

        with patch("ypl.agent_harness_service.gateway.settings") as mock_settings:
            mock_settings.GATEWAY_BASE_URL = "http://sag-host:9000"
            mock_settings.X_API_KEY = "api-key"
            registry = init_gateways()

        gw = registry.get("slack")
        assert isinstance(gw, SlackGateway)
        assert gw._base_url == "http://sag-host:9000"

    def test_no_slack_when_base_url_empty(self) -> None:
        from ypl.agent_harness_service.gateway import init_gateways

        with patch("ypl.agent_harness_service.gateway.settings") as mock_settings:
            mock_settings.GATEWAY_BASE_URL = ""
            mock_settings.X_API_KEY = None
            registry = init_gateways()

        assert registry.get("slack") is None

    def test_idempotent_second_call_is_noop(self) -> None:
        from ypl.agent_harness_service.gateway import init_gateways

        with patch("ypl.agent_harness_service.gateway.settings") as mock_settings:
            mock_settings.GATEWAY_BASE_URL = "http://sag:9000"
            mock_settings.X_API_KEY = None
            r1 = init_gateways()
            # Second call — even if env would produce a different result, registry is unchanged
            mock_settings.GATEWAY_BASE_URL = "http://different-sag:9999"
            r2 = init_gateways()

        assert r1 is r2
        # Verify the gateway still uses the original URL (not the changed one)
        gw = r2.get("slack")
        assert isinstance(gw, SlackGateway)
        assert gw._base_url == "http://sag:9000"


# ===========================================================================
# GatewaySendResult model
# ===========================================================================


class TestGatewaySendResult:
    def test_success_minimal(self) -> None:
        result = GatewaySendResult(success=True)
        assert result.success is True
        assert result.message_id is None
        assert result.destination is None
        assert result.error is None

    def test_failure_with_error(self) -> None:
        result = GatewaySendResult(success=False, error="Timeout", destination="C123")
        assert result.success is False
        assert result.error == "Timeout"
        assert result.destination == "C123"

    def test_full_success(self) -> None:
        result = GatewaySendResult(success=True, message_id="ts123", destination="C456")
        assert result.message_id == "ts123"
        assert result.destination == "C456"


# ===========================================================================
# GatewayConfig model
# ===========================================================================


class TestGatewayConfig:
    def test_defaults(self) -> None:
        config = GatewayConfig(name="test", gateway_type="slack")
        assert config.enabled is True
        assert config.settings == {}

    def test_disabled(self) -> None:
        config = GatewayConfig(name="test", gateway_type="slack", enabled=False)
        assert config.enabled is False

    def test_settings_stored(self) -> None:
        config = GatewayConfig(name="s", gateway_type="slack", settings={"base_url": "http://x"})
        assert config.settings["base_url"] == "http://x"


# ===========================================================================
# Gateway abstract base — default no-op implementations
# ===========================================================================


class ConcreteGateway(Gateway):
    """Minimal concrete subclass for testing base defaults."""

    @property
    def name(self) -> str:
        return "concrete"

    async def send_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        return True

    async def append_reply(
        self, session_id: str, text: str, reply_type: str | None = None, username: str | None = None
    ) -> bool:
        return True

    async def request_feedback(self, session_id: str) -> bool:
        return True

    async def send_message(
        self,
        agent_name: str,
        destination: str,
        text: str,
        thread_id: str | None = None,
        ahs_session_id: str | None = None,
        username: str | None = None,
    ) -> GatewaySendResult:
        return GatewaySendResult(success=True)


class TestGatewayBaseDefaults:
    @pytest.mark.asyncio
    async def test_send_status_update_default_false(self) -> None:
        gw = ConcreteGateway()
        assert await gw.send_status_update("sess", "text") is False

    @pytest.mark.asyncio
    async def test_send_tool_event_default_false(self) -> None:
        gw = ConcreteGateway()
        assert await gw.send_tool_event("sess", "start", "tid") is False

    @pytest.mark.asyncio
    async def test_send_questionnaire_default_false(self) -> None:
        gw = ConcreteGateway()
        result = await gw.send_questionnaire("sess", "q1", "text", [{"label": "A", "value": "a"}])
        assert result is False


# ===========================================================================
# slack_prefetch — pure helper functions
# ===========================================================================


class TestExtractAttachmentText:
    def test_empty_list(self) -> None:
        assert _extract_attachment_text([]) is None

    def test_attachment_with_pretext_title_text(self) -> None:
        att = [{"pretext": "Fwd:", "title": "Meeting", "text": "Let's meet"}]
        result = _extract_attachment_text(att)
        assert result is not None
        assert "Fwd:" in result
        assert "Meeting" in result
        assert "Let's meet" in result

    def test_fallback_only_when_no_richer_content(self) -> None:
        att = [{"fallback": "No rich content here"}]
        result = _extract_attachment_text(att)
        assert result == "No rich content here"

    def test_fallback_ignored_when_richer_content_present(self) -> None:
        att = [{"text": "Rich text", "fallback": "Fallback text"}]
        result = _extract_attachment_text(att)
        assert result is not None
        assert "Rich text" in result
        assert "Fallback text" not in result

    def test_fields_included(self) -> None:
        att = [{"fields": [{"title": "Priority", "value": "High"}, {"title": "Due", "value": "Tomorrow"}]}]
        result = _extract_attachment_text(att)
        assert result is not None
        assert "Priority: High" in result
        assert "Due: Tomorrow" in result

    def test_fields_skipped_when_incomplete(self) -> None:
        att = [{"fields": [{"title": "", "value": "val"}, {"title": "Key", "value": ""}]}]
        result = _extract_attachment_text(att)
        assert result is None

    def test_multiple_attachments_joined(self) -> None:
        att = [{"text": "First"}, {"text": "Second"}]
        result = _extract_attachment_text(att)
        assert result is not None
        assert "First" in result
        assert "Second" in result

    def test_attachment_with_no_content_fields_ignored(self) -> None:
        att = [{"color": "red", "id": 1}]  # no content fields
        result = _extract_attachment_text(att)
        assert result is None


class TestExtractBlockText:
    def test_empty_list(self) -> None:
        assert _extract_block_text([]) is None

    def test_rich_text_block_text_element(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"elements": [{"type": "text", "text": "Hello world"}]}],
            }
        ]
        result = _extract_block_text(blocks)
        assert result == "Hello world"

    def test_rich_text_block_link_element_uses_text(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"elements": [{"type": "link", "url": "http://x.com", "text": "Click here"}]}],
            }
        ]
        result = _extract_block_text(blocks)
        assert result == "Click here"

    def test_rich_text_block_link_fallback_to_url(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"elements": [{"type": "link", "url": "http://fallback.com"}]}],
            }
        ]
        result = _extract_block_text(blocks)
        assert result == "http://fallback.com"

    def test_rich_text_block_user_mention(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"elements": [{"type": "user", "user_id": "U123"}]}],
            }
        ]
        result = _extract_block_text(blocks)
        assert result == "@U123"

    def test_section_block(self) -> None:
        blocks = [{"type": "section", "text": {"text": "Section content"}}]
        result = _extract_block_text(blocks)
        assert result == "Section content"

    def test_header_block(self) -> None:
        blocks = [{"type": "header", "text": {"text": "Header content"}}]
        result = _extract_block_text(blocks)
        assert result == "Header content"

    def test_unknown_block_type_ignored(self) -> None:
        blocks = [{"type": "image", "url": "http://img.com/pic.png"}]
        result = _extract_block_text(blocks)
        assert result is None

    def test_multiple_blocks_combined(self) -> None:
        blocks = [
            {"type": "section", "text": {"text": "First"}},
            {"type": "header", "text": {"text": "Second"}},
        ]
        result = _extract_block_text(blocks)
        assert result is not None
        assert "First" in result
        assert "Second" in result

    def test_section_without_text_field(self) -> None:
        blocks = [{"type": "section", "text": {}}]  # empty text dict
        result = _extract_block_text(blocks)
        assert result is None


class TestFormatMessages:
    def test_basic_user_message(self) -> None:
        msgs = [{"user": "U1", "text": "Hello", "ts": "123.000"}]
        name_map = {"U1": "Alice"}
        result = _format_messages(msgs, name_map)
        assert "[Alice] (123.000): Hello" in result

    def test_user_id_fallback_when_not_in_map(self) -> None:
        msgs = [{"user": "U999", "text": "Hey", "ts": "456.000"}]
        result = _format_messages(msgs, {})
        assert "[U999] (456.000): Hey" in result

    def test_bot_message_uses_username(self) -> None:
        msgs = [{"username": "MyBot", "text": "Automated", "ts": "789.000"}]
        result = _format_messages(msgs, {})
        assert "[MyBot] (789.000): Automated" in result

    def test_no_user_no_username_fallback_unknown(self) -> None:
        msgs = [{"text": "Orphan", "ts": "000.000"}]
        result = _format_messages(msgs, {})
        assert "[unknown] (000.000): Orphan" in result

    def test_attachment_appended(self) -> None:
        msgs = [
            {
                "user": "U1",
                "text": "See attached",
                "ts": "1.0",
                "attachments": [{"text": "Email body"}],
            }
        ]
        result = _format_messages(msgs, {"U1": "Alice"})
        assert "[attachments]: Email body" in result

    def test_blocks_appended_when_text_empty(self) -> None:
        msgs = [
            {
                "user": "U1",
                "text": "",
                "ts": "1.0",
                "blocks": [{"type": "section", "text": {"text": "Block content"}}],
            }
        ]
        result = _format_messages(msgs, {"U1": "Alice"})
        assert "[blocks]: Block content" in result

    def test_blocks_not_appended_when_text_present(self) -> None:
        msgs = [
            {
                "user": "U1",
                "text": "Already has text",
                "ts": "1.0",
                "blocks": [{"type": "section", "text": {"text": "Block content"}}],
            }
        ]
        result = _format_messages(msgs, {"U1": "Alice"})
        assert "[blocks]:" not in result

    def test_multiple_messages_joined_by_newlines(self) -> None:
        msgs = [
            {"user": "U1", "text": "First", "ts": "1.0"},
            {"user": "U2", "text": "Second", "ts": "2.0"},
        ]
        name_map = {"U1": "Alice", "U2": "Bob"}
        result = _format_messages(msgs, name_map)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "Alice" in lines[0]
        assert "Bob" in lines[1]

    def test_empty_messages(self) -> None:
        result = _format_messages([], {})
        assert result == ""


# ===========================================================================
# fetch_slack_thread_content — integration-style with mocked Slack SDK
# ===========================================================================


class TestFetchSlackThreadContent:
    """Slack client + display-name resolution live in ``ypl.slack_common.ops_bot``.

    The prefetch tests here patch the imported references inside
    ``ypl.agent_harness_service.gateway.slack_prefetch`` so they exercise the
    prefetch formatting / truncation / pagination logic without a real Slack
    connection. Client-initialisation behaviour (token env vars, singleton
    reuse) is covered in ``tests/slack_common/test_ops_bot.py``.
    """

    @staticmethod
    def _patches(mock_client: AsyncMock) -> tuple[Any, Any]:
        """Common patch pair: read client + display-name resolver."""
        return (
            patch(
                "ypl.agent_harness_service.gateway.slack_prefetch.get_ops_bot_user_client",
                return_value=mock_client,
            ),
            patch(
                "ypl.agent_harness_service.gateway.slack_prefetch.resolve_display_name",
                AsyncMock(side_effect=lambda uid: "Alice" if uid == "U1" else uid),
            ),
        )

    @pytest.mark.asyncio
    async def test_success_simple(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [{"user": "U1", "text": "Hello", "ts": "1.0"}],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }
        )
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is not None
        assert "Alice" in result
        assert "Hello" in result

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(side_effect=OSError("network error"))
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_ok_false(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(return_value={"ok": False, "error": "channel_not_found"})
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is None

    @pytest.mark.asyncio
    async def test_content_truncated_at_15000_chars(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        long_text = "x" * 20000
        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [{"user": "U1", "text": long_text, "ts": "1.0"}],
                "has_more": False,
                "response_metadata": {},
            }
        )
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is not None
        assert "truncated" in result.lower()
        # The truncation limit is 15000 chars; verify we're within a reasonable margin
        # (some overhead from formatting, metadata, truncation notice)
        assert len(result) <= 16000

    @pytest.mark.asyncio
    async def test_has_more_hint_appended(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [{"user": "U1", "text": "msg", "ts": "1.0"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor-abc"},
            }
        )
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is not None
        assert "additional messages" in result.lower()
        assert "cursor-abc" in result

    @pytest.mark.asyncio
    async def test_user_resolution_failure_falls_back_to_id(self) -> None:
        from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content

        mock_client = AsyncMock()
        mock_client.conversations_replies = AsyncMock(
            return_value={
                "ok": True,
                "messages": [{"user": "U999", "text": "Hi", "ts": "1.0"}],
                "has_more": False,
                "response_metadata": {},
            }
        )
        # Resolver returns the raw ID when it can't look up a name — prefetch
        # should still produce content with the raw ID in place of the name.
        read_patch, resolver_patch = self._patches(mock_client)
        with read_patch, resolver_patch:
            result = await fetch_slack_thread_content("C123", "1.0")

        assert result is not None
        assert "U999" in result
