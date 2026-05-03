"""Unit tests for ypl/mcp_server/core.py.

Covers:
  - _create_mcp_server() — DEV_TOKEN vs OAUTH mode
  - ToolCallLoggingMiddleware.on_call_tool():
      - Successful tool call logs SUCCESS with the typed RequestContext
      - Failed tool call logs FAILED and re-raises
      - DEV_TOKEN mode without context raises PermissionError
      - OAuth-mode no-context logs warning and skips audit

The legacy per-field accessors (``get_authenticated_user_email`` etc.)
are gone — tools read identity from the typed
:class:`~ypl.mcp_common.auth_context.RequestContext` directly. The
tests below exercise the audit middleware against that typed shape.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.mcp_common.auth_context import RequestContext, request_context


def _set_devtoken_request(email: str = "dev@example.com", **extra: object) -> MagicMock:
    """Set up a typed RequestContext + DevToken audit row for a "DevToken" request.

    Mirrors what ``DevTokenAuthMiddleware`` does in production: publishes
    the typed context AND stashes a DevToken row on the transitional
    audit ContextVar so the audit middleware can populate
    ``MCPAuditLog.mcp_dev_token_id``.
    """
    request_context.set(
        RequestContext(
            auth_kind="dev_token",
            requesting_user_id=str(extra.get("user_id", "user-abc-123")),
            principal_user_id=str(extra.get("user_id", "user-abc-123")),
            audit_email=email,
            ip_address=str(extra.get("ip_address", "1.2.3.4")),
            user_agent=str(extra.get("user_agent", "pytest")),
        )
    )
    mock_token = MagicMock()
    mock_token.email = email
    mock_token.mcp_dev_token_id = MagicMock()
    from ypl.mcp_server.auth_dev_token import _devtoken_audit_var

    _devtoken_audit_var.set(mock_token)
    return mock_token


def _set_oauth_request(
    email: str = "oauth@example.com",
    callback_url: str | None = None,
) -> None:
    """Publish a typed OAuth-style RequestContext."""
    request_context.set(
        RequestContext(
            auth_kind="oauth_user",
            requesting_user_id=None,
            audit_email=email,
            callback_url=callback_url,
        )
    )
    from ypl.mcp_server.auth_dev_token import _devtoken_audit_var

    _devtoken_audit_var.set(None)


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
        """In DEV_TOKEN mode, missing context raises PermissionError."""
        from ypl.mcp_server.auth_dev_token import _devtoken_audit_var
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        request_context.set(None)
        _devtoken_audit_var.set(None)

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
        _set_devtoken_request()

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
        # The typed context exposes IP / UA on the typed RequestContext;
        # the audit middleware threads them through unchanged.
        assert call_kwargs["ip_address"] == "1.2.3.4"
        assert call_kwargs["user_agent"] == "pytest"

    async def test_failed_tool_call_logs_failure_and_reraises(self) -> None:
        """Exception in tool call is logged as FAILED and then re-raised."""
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        _set_devtoken_request()

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
        from ypl.mcp_server.auth_dev_token import _devtoken_audit_var
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        request_context.set(None)
        _devtoken_audit_var.set(None)

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

    async def test_oauth_with_typed_context_logs_audit(self) -> None:
        """When OAuth middleware populated the typed context, audit emits OAUTH row."""
        from ypl.db.mcp import MCPAuditLogStatus, MCPTokenType
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        _set_oauth_request("oauthuser@example.com")

        mock_ctx = _make_middleware_context()
        mock_result = MagicMock()
        call_next = AsyncMock(return_value=mock_result)

        with (
            patch("ypl.mcp_server.core.settings") as mock_settings,
            patch("ypl.mcp_server.core.log_tool_call", new=AsyncMock()) as mock_log,
        ):
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            await middleware.on_call_tool(mock_ctx, call_next)

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["status"] == MCPAuditLogStatus.SUCCESS
        assert call_kwargs["token_type"] == MCPTokenType.OAUTH
        assert call_kwargs["email"] == "oauthuser@example.com"

    async def test_oauth_audit_carries_callback_url_from_typed_context(self) -> None:
        """Regression: ``MCPAuditLog.callback_url`` must be sourced from
        the typed ``RequestContext.callback_url`` populated at OAuth
        verify time. Previously the typed context dropped the field, so
        every OAuth-authenticated audit row silently wrote ``NULL``.
        """
        from ypl.mcp_server.core import ToolCallLoggingMiddleware

        middleware = ToolCallLoggingMiddleware()
        _set_oauth_request(
            "oauthuser@example.com",
            callback_url="https://callback.example.com",
        )

        mock_ctx = _make_middleware_context()
        mock_result = MagicMock()
        call_next = AsyncMock(return_value=mock_result)

        with (
            patch("ypl.mcp_server.core.settings") as mock_settings,
            patch("ypl.mcp_server.core.log_tool_call", new=AsyncMock()) as mock_log,
        ):
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            await middleware.on_call_tool(mock_ctx, call_next)

        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs["callback_url"] == "https://callback.example.com"


# ---------------------------------------------------------------------------
# require_caller_user_id (the new tool-facing accessor)
# ---------------------------------------------------------------------------


class TestRequireCallerUserId:
    def test_returns_user_id(self) -> None:
        from ypl.mcp_common.auth_context import require_caller_user_id

        request_context.set(
            RequestContext(
                auth_kind="agent_secret",
                requesting_user_id="user-xyz",
            )
        )
        assert require_caller_user_id() == "user-xyz"

    def test_raises_when_no_context(self) -> None:
        from ypl.mcp_common.auth_context import require_caller_user_id

        request_context.set(None)
        with pytest.raises(PermissionError, match="Authentication required"):
            require_caller_user_id()

    def test_raises_when_user_id_is_none(self) -> None:
        from ypl.mcp_common.auth_context import require_caller_user_id

        request_context.set(
            RequestContext(
                auth_kind="agent_secret",
                requesting_user_id=None,
            )
        )
        with pytest.raises(PermissionError, match="Authentication required"):
            require_caller_user_id()
