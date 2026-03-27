"""Core MCP server components.

This module provides:
- FastMCP server instance with mode-based authentication
- Request context for passing auth info between middleware layers
- Audit logging for tool calls
- Tool call logging middleware
"""

import time
import traceback
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp import types as mcp_types

from ypl.backend.config import settings
from ypl.backend.db import get_async_session, retry_db
from ypl.db.mcp import MCPAuditLog, MCPAuditLogStatus, MCPDevToken, MCPTokenType
from ypl.mcp_server.context_vars import request_context
from ypl.structured_logger import get_logger
from ypl.utils import maybe_truncate

logger = get_logger()


def get_authenticated_user_email() -> str:
    """Get the authenticated user's email from request context or OAuth token.

    Works with both DevToken and OAuth authentication modes:
    - DevToken: Gets email from request_context["token"].email (set by DevTokenAuthMiddleware)
    - OAuth: Gets email from request_context["email"] (set by AllowedDomainsGoogleProvider)

    Returns:
        The authenticated user's email, or "unknown" if not available.
    """
    req_ctx = request_context.get() or {}

    # DevToken: email comes from the MCPDevToken DB record stored in context
    token = req_ctx.get("token")
    if token:
        return str(token.email)

    # OAuth: email is stored directly after verify_token() succeeds
    email = req_ctx.get("email")
    if isinstance(email, str) and email:
        return email

    return "unknown"


def get_requesting_user_id() -> str | None:
    """Get the requesting user's user_id from request context.

    Checks the X-User-ID header first (injected by AHS or other callers
    that forward user identity). Returns None if not available — callers
    should fall back to resolving from the authenticated email.

    Returns:
        The requesting user's user_id string, or None if not set.
    """
    req_ctx = request_context.get() or {}
    return req_ctx.get("requesting_user_id")


def get_ahs_agent_name() -> str | None:
    """Get the AHS agent name from request context.

    Set from the X-AHS-Agent-Name header injected by the AHS runner into
    the session's .mcp.json. This header is tamper-proof: the runner writes
    the config file in a sandboxed workspace that the agent cannot modify.

    Returns:
        The agent name string (e.g. 'eng-raccoon'), or None if not set.
    """
    req_ctx = request_context.get() or {}
    return req_ctx.get("ahs_agent_name")


def get_ahs_session_id() -> str | None:
    """Get the AHS session ID from request context.

    Set from the X-AHS-Session-ID header injected by the AHS runner.

    Returns:
        The session ID string (UUID), or None if not set.
    """
    req_ctx = request_context.get() or {}
    return req_ctx.get("ahs_session_id")


def _create_mcp_server() -> FastMCP:
    """Create FastMCP server with mode-based authentication.

    Returns:
        FastMCP server instance configured based on MCP_SERVER_MODE:
        - DEV_TOKEN: No FastMCP auth (auth handled by Starlette middleware)
        - OAUTH: FastMCP GoogleProvider auth
    """
    if settings.MCP_SERVER_MODE == "OAUTH":
        # Import here to avoid loading OAuth dependencies in DEV_TOKEN mode
        from ypl.mcp_server.auth_oauth import create_oauth_provider

        oauth_provider = create_oauth_provider()
        logger.info("Creating MCP server with OAuth authentication")
        return FastMCP(name="yuppster-mcp-server", auth=oauth_provider)

    # DEV_TOKEN mode - auth handled by Starlette middleware
    logger.info("Creating MCP server with DevToken authentication")
    return FastMCP(name="yuppster-mcp-server", auth=None)


# Create FastMCP server instance based on mode
# Tools and routes are registered in mcp_tools.py after import
mcp_server = _create_mcp_server()


@retry_db
async def log_tool_call(
    tool_name: str,
    tool_parameters: dict[str, Any],
    status: MCPAuditLogStatus,
    error_message: str | None,
    result_summary: str | None,
    execution_time_ms: int,
    email: str,
    token_type: MCPTokenType,
    token: MCPDevToken | None = None,
    callback_url: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    error_type: str | None = None,
    stack_trace: str | None = None,
) -> None:
    """Log a tool call to the audit log.

    Supports both DevToken and OAuth authentication:
    - DevToken: token is provided, mcp_dev_token_id is set
    - OAuth: token is None, only email and callback_url are set

    For FAILED calls, error_type (exception class name) and stack_trace
    (full formatted traceback) are stored to aid root cause analysis.
    """
    try:
        async with get_async_session() as session:
            audit_log = MCPAuditLog(
                mcp_dev_token_id=token.mcp_dev_token_id if token else None,
                email=email,
                callback_url=callback_url,
                token_type=token_type,
                tool_name=tool_name,
                tool_parameters=tool_parameters,
                status=status,
                error_message=error_message,
                error_type=error_type,
                stack_trace=stack_trace,
                result_summary=result_summary,
                execution_time_ms=execution_time_ms,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            session.add(audit_log)
            await session.commit()

            logger.info(
                "MCP tool call logged",
                engineer=email.split("@")[0],
                mcp_dev_token_id=str(token.mcp_dev_token_id) if token else None,
                token_type=token_type.value,
                tool=tool_name,
                status=status.value,
                execution_time_ms=execution_time_ms,
                error_type=error_type,
            )

    except Exception as e:
        logger.error("Error logging tool call", error=str(e), tool_name=tool_name)


class ToolCallLoggingMiddleware(Middleware):
    """Middleware to log all MCP tool calls to the audit log.

    Works with both DEV_TOKEN and OAUTH modes:
    - DEV_TOKEN: Gets auth info from request_context (set by DevTokenAuthMiddleware)
    - OAUTH: Gets auth info from request_context (set by AllowedDomainsGoogleProvider.verify_token)
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Log tool calls with timing and result information."""
        start_time = time.time()
        tool_name = context.message.name
        arguments = context.message.arguments or {}

        # Both auth modes populate request_context before the tool call:
        #   DEV_TOKEN: DevTokenAuthMiddleware.dispatch() sets {"token": MCPDevToken, ...}
        #   OAUTH:     AllowedDomainsGoogleProvider.verify_token() sets {"email": str, ...}
        req_ctx = request_context.get() or {}
        token: MCPDevToken | None = req_ctx.get("token")

        # For OAuth, email and callback_url are stored directly in request_context by verify_token()
        oauth_email: str | None = req_ctx.get("email")
        callback_url: str | None = req_ctx.get("callback_url")

        if not token and settings.MCP_SERVER_MODE == "DEV_TOKEN":
            # should never happen, but add a check to be safe
            logger.error("DevToken authentication is required, but no token was found")
            raise PermissionError("DevToken authentication is required")

        if not token and settings.MCP_SERVER_MODE == "OAUTH" and not oauth_email:
            # Fallback: request_context wasn't set (shouldn't happen after the fix, but
            # provides a safety net for unexpected FastMCP internals)
            try:
                access_token = get_access_token()
                if access_token and access_token.claims:
                    oauth_email = access_token.claims.get("email")
                    if hasattr(access_token, "client_id"):
                        callback_url = access_token.client_id
            except Exception as e:
                logger.warning(
                    "OAuth tool call: could not retrieve email from request_context or access token",
                    tool=tool_name,
                    error=str(e),
                )

        result: ToolResult | None = None
        err: Exception | None = None
        err_traceback: str | None = None
        try:
            # Execute the tool
            result = await call_next(context)
            return result  # noqa: RET504, I need it in the finally block
        except Exception as e:
            err = e
            err_traceback = maybe_truncate(traceback.format_exc(), 10000)
            raise
        finally:
            execution_time_ms = int((time.time() - start_time) * 1000)

            # Determine email and token type
            email: str | None = None
            token_type: MCPTokenType

            if token:
                # DevToken authentication
                email = token.email
                token_type = MCPTokenType.DEV_TOKEN
            elif oauth_email:
                # OAuth authentication
                email = oauth_email
                token_type = MCPTokenType.OAUTH
            else:
                # No authentication context - log warning but don't fail
                if not err:
                    logger.warning("MCP tool call without authentication context", tool=tool_name)
                email = None

            if email:
                status = MCPAuditLogStatus.FAILED if err else MCPAuditLogStatus.SUCCESS
                error_message = str(err) if err else None
                error_type = type(err).__name__ if err else None
                result_summary = None
                if not err:
                    result_summary = maybe_truncate(str(result), 500) if result else None

                await log_tool_call(
                    tool_name=tool_name,
                    tool_parameters=arguments,
                    status=status,
                    error_message=error_message,
                    error_type=error_type,
                    stack_trace=err_traceback,
                    result_summary=result_summary,
                    execution_time_ms=execution_time_ms,
                    email=email,
                    token_type=token_type,
                    token=token,
                    callback_url=callback_url,
                    ip_address=req_ctx.get("ip_address"),
                    user_agent=req_ctx.get("user_agent"),
                )


# Register the logging middleware
mcp_server.add_middleware(ToolCallLoggingMiddleware())
