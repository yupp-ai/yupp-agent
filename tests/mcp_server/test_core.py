"""Unit tests for ypl/mcp_server/core.py.

Covers:
  - get_authenticated_user_email() — DevToken path, OAuth path, fallback
  - get_requesting_user_id() — from context, missing context
  - get_ahs_agent_name() — present, absent
  - get_ahs_session_id() — present, absent
  - _create_mcp_server() — DEV_TOKEN vs OAUTH mode
  - ToolCallLoggingMiddleware.on_call_tool():
      - Successful tool call logs SUCCESS
      - Failed tool call logs FAILED and re-raises
      - No-context in DEV_TOKEN mode raises PermissionError
      - No-context in OAUTH mode logs warning and skips audit

All tests run without a live database.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.mcp_server.context_vars import request_context
from ypl.mcp_server.core import (
    get_ahs_agent_name,
    get_ahs_session_id,
    get_authenticated_user_email,
    get_requesting_user_id,
)

# ---------------------------------------------------------------------------
# get_authenticated_user_email
# ---------------------------------------------------------------------------


class TestGetAuthenticatedUserEmail:
    def test_dev_token_path(self) -> None:
        mock_token = MagicMock()
        mock_token.email = "dev@example.com"
        request_context.set({"token": mock_token})

        assert get_authenticated_user_email() == "dev@example.com"

    def test_oauth_path(self) -> None:
        request_context.set({"email": "oauthuser@example.com"})

        assert get_authenticated_user_email() == "oauthuser@example.com"

    def test_empty_context_returns_unknown(self) -> None:
        request_context.set(None)

        assert get_authenticated_user_email() == "unknown"

    def test_empty_dict_returns_unknown(self) -> None:
        request_context.set({})

        assert get_authenticated_user_email() == "unknown"

    def test_oauth_empty_string_falls_through(self) -> None:
        """Email key present but empty string → returns unknown."""
        request_context.set({"email": ""})

        assert get_authenticated_user_email() == "unknown"

    def test_token_takes_precedence_over_email(self) -> None:
        """DevToken context wins even if 'email' key is also present."""
        mock_token = MagicMock()
        mock_token.email = "token@example.com"
        request_context.set({"token": mock_token, "email": "other@example.com"})

        assert get_authenticated_user_email() == "token@example.com"


# ---------------------------------------------------------------------------
# get_requesting_user_id
# ---------------------------------------------------------------------------


class TestGetRequestingUserId:
    def test_present(self) -> None:
        request_context.set({"requesting_user_id": "user-abc-123"})
        assert get_requesting_user_id() == "user-abc-123"

    def test_absent(self) -> None:
        request_context.set({})
        assert get_requesting_user_id() is None

    def test_none_context(self) -> None:
        request_context.set(None)
        assert get_requesting_user_id() is None


# ---------------------------------------------------------------------------
# get_ahs_agent_name
# ---------------------------------------------------------------------------


class TestGetAhsAgentName:
    def test_present(self) -> None:
        request_context.set({"ahs_agent_name": "eng-raccoon"})
        assert get_ahs_agent_name() == "eng-raccoon"

    def test_absent(self) -> None:
        request_context.set({})
        assert get_ahs_agent_name() is None

    def test_none_context(self) -> None:
        request_context.set(None)
        assert get_ahs_agent_name() is None


# ---------------------------------------------------------------------------
# get_ahs_session_id
# ---------------------------------------------------------------------------


class TestGetAhsSessionId:
    def test_present(self) -> None:
        request_context.set({"ahs_session_id": "sess-uuid-1234"})
        assert get_ahs_session_id() == "sess-uuid-1234"

    def test_absent(self) -> None:
        request_context.set({})
        assert get_ahs_session_id() is None

    def test_none_context(self) -> None:
        request_context.set(None)
        assert get_ahs_session_id() is None


# ---------------------------------------------------------------------------
# _create_mcp_server
# ---------------------------------------------------------------------------


class TestCreateMcpServer:
    def test_dev_token_mode_creates_server_without_auth(self) -> None:
        from fastmcp import FastMCP
        from ypl.mcp_server.core import _create_mcp_server

        with patch("ypl.mcp_server.core.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            mock_settings.AGCOUCH_MCP_SERVER_NAME = "agcouch-mcp-server"
            server = _create_mcp_server()

        assert isinstance(server, FastMCP)
        assert server.name == "agcouch-mcp-server"

    def test_oauth_mode_creates_server_with_auth(self) -> None:
        from fastmcp import FastMCP
        from ypl.mcp_server.core import _create_mcp_server

        mock_provider = MagicMock()
        mock_create = MagicMock(return_value=mock_provider)

        with patch("ypl.mcp_server.core.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            mock_settings.AGCOUCH_MCP_SERVER_NAME = "agcouch-mcp-server"
            with patch.dict(
                "sys.modules",
                {
                    "ypl.mcp_server.auth_oauth": MagicMock(create_oauth_provider=mock_create),
                },
            ):
                server = _create_mcp_server()

        assert isinstance(server, FastMCP)
        # Verify the OAuth provider factory was called
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# ToolCallLoggingMiddleware
# ---------------------------------------------------------------------------


def _make_middleware_context(tool_name: str = "test_tool", arguments: dict | None = None) -> MagicMock:
    """Build a mock MiddlewareContext for tool call tests."""
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = tool_name
    ctx.message.arguments = arguments or {"param": "value"}
    return ctx


class TestToolCallLoggingMiddleware:
    async def test_dev_token_no_context_raises_permission_error(self) -> None:
        """In DEV_TOKEN mode, missing request_context raises PermissionError."""
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        request_context.set(None)  # no context

        mock_ctx = _make_middleware_context()
        call_next = AsyncMock(return_value=MagicMock())

        with patch("ypl.mcp_server.core.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            with pytest.raises(PermissionError, match="DevToken authentication is required"):
                await middleware.on_call_tool(mock_ctx, call_next)

    async def test_successful_tool_call_logs_success(self) -> None:
        """Successful tool call triggers audit log with SUCCESS status."""
        from ypl.db.mcp import MCPTokenType
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()

        mock_token = MagicMock()
        mock_token.email = "dev@example.com"
        request_context.set({"token": mock_token, "ip_address": "1.2.3.4", "user_agent": "pytest"})

        mock_ctx = _make_middleware_context()
        mock_result = MagicMock()
        call_next = AsyncMock(return_value=mock_result)

        with (
            patch("ypl.mcp_server.core.settings") as mock_settings,
            patch("ypl.mcp_server.core.log_tool_call", new=AsyncMock()) as mock_log,
        ):
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            result = await middleware.on_call_tool(mock_ctx, call_next)

        assert result is mock_result
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args.kwargs
        from ypl.db.mcp import MCPAuditLogStatus

        assert call_kwargs["status"] == MCPAuditLogStatus.SUCCESS
        assert call_kwargs["token_type"] == MCPTokenType.DEV_TOKEN

    async def test_failed_tool_call_logs_failure_and_reraises(self) -> None:
        """Exception in tool call is logged as FAILED and then re-raised."""
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()

        mock_token = MagicMock()
        mock_token.email = "dev@example.com"
        request_context.set({"token": mock_token, "ip_address": "1.2.3.4", "user_agent": "pytest"})

        mock_ctx = _make_middleware_context()
        error = RuntimeError("tool exploded")
        call_next = AsyncMock(side_effect=error)

        with (
            patch("ypl.mcp_server.core.settings") as mock_settings,
            patch("ypl.mcp_server.core.log_tool_call", new=AsyncMock()) as mock_log,
        ):
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            with pytest.raises(RuntimeError, match="tool exploded"):
                await middleware.on_call_tool(mock_ctx, call_next)

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args.kwargs
        from ypl.db.mcp import MCPAuditLogStatus

        assert call_kwargs["status"] == MCPAuditLogStatus.FAILED
        assert call_kwargs["error_message"] == "tool exploded"
        assert call_kwargs["error_type"] == "RuntimeError"

    async def test_oauth_no_context_logs_warning_and_skips_audit(self) -> None:
        """In OAUTH mode with no context, tool call completes but audit is skipped."""
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        request_context.set(None)

        mock_ctx = _make_middleware_context()
        mock_result = MagicMock()
        call_next = AsyncMock(return_value=mock_result)

        with (
            patch("ypl.mcp_server.core.settings") as mock_settings,
            patch("ypl.mcp_server.core.log_tool_call", new=AsyncMock()) as mock_log,
            patch("ypl.mcp_server.core.get_access_token", side_effect=Exception("no token")),
        ):
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            result = await middleware.on_call_tool(mock_ctx, call_next)

        assert result is mock_result
        # No email → audit log should NOT be called
        mock_log.assert_not_called()
