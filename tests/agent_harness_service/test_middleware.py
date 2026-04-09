"""Unit tests for ypl/agent_harness_service/middleware.py.

Tests helper functions and middleware dispatch logic.
"""

from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request
from starlette.responses import Response
from ypl.agent_harness_service.middleware import (
    AHSRequestLoggingMiddleware,
    McpTokenAuthMiddleware,
    _get_media_type,
    _is_json_content_type,
    _maybe_truncate,
    _truncate_payload,
)

# ===========================================================================
# Tests: _get_media_type
# ===========================================================================


class TestGetMediaType:
    """Tests for _get_media_type."""

    def test_returns_none_for_none_input(self) -> None:
        assert _get_media_type(None) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _get_media_type("") is None

    def test_returns_base_type_without_params(self) -> None:
        assert _get_media_type("application/json") == "application/json"

    def test_strips_charset_param(self) -> None:
        result = _get_media_type("application/json; charset=utf-8")
        assert result == "application/json"

    def test_lowercases_media_type(self) -> None:
        result = _get_media_type("Application/JSON")
        assert result == "application/json"

    def test_strips_whitespace(self) -> None:
        result = _get_media_type("  application/json  ")
        assert result == "application/json"

    def test_returns_none_for_whitespace_only(self) -> None:
        assert _get_media_type("   ") is None


# ===========================================================================
# Tests: _is_json_content_type
# ===========================================================================


class TestIsJsonContentType:
    """Tests for _is_json_content_type."""

    def test_returns_false_for_none(self) -> None:
        assert _is_json_content_type(None) is False

    def test_returns_true_for_application_json(self) -> None:
        assert _is_json_content_type("application/json") is True

    def test_returns_true_with_charset(self) -> None:
        assert _is_json_content_type("application/json; charset=utf-8") is True

    def test_returns_true_for_vendor_json_types(self) -> None:
        assert _is_json_content_type("application/vnd.api+json") is True

    def test_returns_false_for_form_data(self) -> None:
        assert _is_json_content_type("multipart/form-data") is False

    def test_returns_false_for_plain_text(self) -> None:
        assert _is_json_content_type("text/plain") is False

    def test_returns_false_for_empty_string(self) -> None:
        assert _is_json_content_type("") is False


# ===========================================================================
# Tests: _maybe_truncate
# ===========================================================================


class TestMaybeTruncate:
    """Tests for _maybe_truncate."""

    def test_short_string_unchanged(self) -> None:
        assert _maybe_truncate("short") == "short"

    def test_long_string_truncated(self) -> None:
        long_str = "x" * 100
        result = _maybe_truncate(long_str)
        assert isinstance(result, str)
        assert "chars" in result
        assert len(result) < len(long_str)

    def test_non_string_passthrough(self) -> None:
        assert _maybe_truncate(42) == 42
        assert _maybe_truncate(None) is None
        assert _maybe_truncate(True) is True

    def test_exactly_at_limit_unchanged(self) -> None:
        from ypl.agent_harness_service.middleware import _MSG_TRUNCATE_LEN

        boundary = "a" * _MSG_TRUNCATE_LEN
        result = _maybe_truncate(boundary)
        assert result == boundary

    def test_one_over_limit_truncated(self) -> None:
        from ypl.agent_harness_service.middleware import _MSG_TRUNCATE_LEN

        over = "a" * (_MSG_TRUNCATE_LEN + 1)
        result = _maybe_truncate(over)
        assert "chars" in result


# ===========================================================================
# Tests: _truncate_payload
# ===========================================================================


class TestTruncatePayload:
    """Tests for _truncate_payload."""

    def test_truncates_message_field(self) -> None:
        data = {"message": "x" * 100, "other": "y" * 100}
        result = _truncate_payload(data)
        assert len(result["message"]) < 100
        assert result["other"] == "y" * 100  # non-truncate field preserved

    def test_non_message_fields_unchanged(self) -> None:
        data = {"agent_id": "my-agent", "session_id": "abc-123"}
        result = _truncate_payload(data)
        assert result["agent_id"] == "my-agent"
        assert result["session_id"] == "abc-123"

    def test_empty_dict_returns_empty_dict(self) -> None:
        assert _truncate_payload({}) == {}

    def test_preserves_non_string_values(self) -> None:
        data = {"message": 42, "count": [1, 2, 3]}
        result = _truncate_payload(data)
        assert result["message"] == 42  # int, not truncated
        assert result["count"] == [1, 2, 3]


# ===========================================================================
# Tests: McpTokenAuthMiddleware
# ===========================================================================


class TestMcpTokenAuthMiddleware:
    """Tests for McpTokenAuthMiddleware.dispatch."""

    def _make_mock_request(
        self,
        headers: dict[str, str] | None = None,
    ) -> MagicMock:
        req = MagicMock(spec=Request)
        req.headers = headers or {}
        return req

    async def test_rejects_request_without_token(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=Response(content="ok"))

        req = MagicMock(spec=Request)
        req.headers = {}  # No token

        with patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"):
            response = await middleware.dispatch(req, call_next)
            assert response.status_code == 401

    async def test_accepts_correct_x_ahs_token_header(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.headers = {"x-ahs-token": "test-secret", "x-ahs-session-id": "sess-123"}

        with (
            patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"),
            patch("ypl.agent_harness_service.middleware.mcp_session_id_var"),
        ):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response
            call_next.assert_called_once()

    async def test_accepts_bearer_token_format(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.headers = {
            "x-ahs-session-id": "",
            "authorization": "Bearer test-secret:sess-abc",
        }

        with (
            patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"),
            patch("ypl.agent_harness_service.middleware.mcp_session_id_var"),
        ):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response
            call_next.assert_called_once()

    async def test_rejects_wrong_bearer_secret(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=Response(content="ok"))

        req = MagicMock(spec=Request)
        req.headers = {
            "x-ahs-session-id": "",
            "authorization": "Bearer wrong-secret:sess-abc",
        }

        with patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"):
            response = await middleware.dispatch(req, call_next)
            assert response.status_code == 401

    async def test_rejects_bearer_without_colon(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=Response(content="ok"))

        req = MagicMock(spec=Request)
        req.headers = {
            "x-ahs-session-id": "",
            "authorization": "Bearer test-secret",  # no colon
        }

        with patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"):
            response = await middleware.dispatch(req, call_next)
            assert response.status_code == 401

    async def test_sets_session_id_context_var(self) -> None:
        middleware = McpTokenAuthMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.headers = {"x-ahs-token": "test-secret", "x-ahs-session-id": "my-sess"}

        with (
            patch("ypl.agent_harness_service.middleware.AHS_MCP_SECRET", "test-secret"),
            patch("ypl.agent_harness_service.middleware.mcp_session_id_var") as mock_var,
        ):
            await middleware.dispatch(req, call_next)
            mock_var.set.assert_called_once_with("my-sess")


# ===========================================================================
# Tests: AHSRequestLoggingMiddleware helper patterns
# ===========================================================================


class TestAHSRequestLoggingMiddleware:
    """Tests for AHSRequestLoggingMiddleware dispatch logic."""

    async def test_non_ahs_routes_pass_through_without_logging(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.url.path = "/health"
        req.method = "GET"

        response = await middleware.dispatch(req, call_next)
        assert response is ok_response

    async def test_ahs_get_route_logged(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        session_uuid = "12345678-1234-1234-1234-123456789012"
        req.url.path = f"/ahs/session/{session_uuid}"
        req.method = "GET"
        req.query_params = {}

        with patch("ypl.agent_harness_service.middleware._logger"):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response

    async def test_ahs_post_with_json_body_parsed(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        body = json.dumps({"session_id": "abc-123", "message": "hello"}).encode()

        req = MagicMock(spec=Request)
        req.url.path = "/ahs/session/create"
        req.method = "POST"
        req.query_params = {}
        req.headers = {"content-type": "application/json"}
        req.body = AsyncMock(return_value=body)

        with (
            patch("ypl.agent_harness_service.middleware._logger"),
            patch("ypl.agent_harness_service.middleware.bind_contextvars"),
            patch("ypl.agent_harness_service.middleware.unbind_contextvars"),
        ):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response

    async def test_ahs_post_with_invalid_json_doesnt_crash(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.url.path = "/ahs/session/create"
        req.method = "POST"
        req.query_params = {}
        req.headers = {"content-type": "application/json"}
        req.body = AsyncMock(return_value=b"not-json-{{{")

        with patch("ypl.agent_harness_service.middleware._logger"):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response

    async def test_ahs_post_with_non_json_content_type_skips_body(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.url.path = "/ahs/session/create"
        req.method = "POST"
        req.query_params = {}
        req.headers = {"content-type": "multipart/form-data"}

        with patch("ypl.agent_harness_service.middleware._logger"):
            response = await middleware.dispatch(req, call_next)
            assert response is ok_response

    async def test_session_id_extracted_from_path_for_get(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        session_uuid = "12345678-1234-1234-1234-123456789012"
        req = MagicMock(spec=Request)
        req.url.path = f"/ahs/session/{session_uuid}/history"
        req.method = "GET"
        req.query_params = {}

        with (
            patch("ypl.agent_harness_service.middleware._logger"),
            patch("ypl.agent_harness_service.middleware.bind_contextvars") as mock_bind,
            patch("ypl.agent_harness_service.middleware.unbind_contextvars"),
        ):
            await middleware.dispatch(req, call_next)
            mock_bind.assert_called_once_with(session_id=session_uuid)

    async def test_session_id_not_bound_for_non_session_path(self) -> None:
        middleware = AHSRequestLoggingMiddleware(app=MagicMock())
        ok_response = Response(content="ok")
        call_next = AsyncMock(return_value=ok_response)

        req = MagicMock(spec=Request)
        req.url.path = "/ahs/agents"
        req.method = "GET"
        req.query_params = {}

        with (
            patch("ypl.agent_harness_service.middleware._logger"),
            patch("ypl.agent_harness_service.middleware.bind_contextvars") as mock_bind,
        ):
            await middleware.dispatch(req, call_next)
            mock_bind.assert_not_called()
