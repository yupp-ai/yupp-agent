"""Core agcouch MCP server: FastMCP instance + audit logging middleware.

The legacy per-field accessors (``get_authenticated_user_email``,
``get_requesting_user_id``, ``get_ahs_agent_name``, ``get_ahs_session_id``)
are gone — tools now consume identity through the typed
:class:`~ypl.mcp_common.auth_context.RequestContext` published by the auth
middleware. See :mod:`ypl.mcp_common.auth_context`.

This module:

- Constructs the FastMCP server based on ``MCP_SERVER_MODE``
  (``DEV_TOKEN`` vs ``OAUTH``).
- Defines :class:`ToolCallLoggingMiddleware`, which writes one
  ``MCPAuditLog`` row per tool call. The middleware reads identity from
  the typed :class:`RequestContext` and the transitional DevToken
  audit var (:data:`ypl.mcp_server.auth_dev_token._devtoken_audit_var`)
  to keep ``MCPAuditLog.mcp_dev_token_id`` populated until phase 5b.
"""

from __future__ import annotations
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
from ypl.mcp_common.auth_context import current_request_context
from ypl.structured_logger import get_logger
from ypl.utils import maybe_truncate

logger = get_logger()


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
        return FastMCP(name=settings.AGCOUCH_MCP_SERVER_NAME, auth=oauth_provider)

    # DEV_TOKEN mode - auth handled by Starlette middleware
    logger.info("Creating MCP server with DevToken authentication")
    return FastMCP(name=settings.AGCOUCH_MCP_SERVER_NAME, auth=None)


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

    Reads identity off the unified
    :class:`~ypl.mcp_common.auth_context.RequestContext` published by
    each mount's auth middleware:

    - ``DEV_TOKEN`` mode: ``DevTokenAuthMiddleware`` populates the
      context plus the transitional DevToken audit var.
    - ``OAUTH`` mode: ``AllowedDomainsGoogleProvider.verify_token``
      populates the context.
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

        ctx = current_request_context()

        # Lazy import: ``auth_dev_token`` pulls in DB modules, and not
        # every deployment loads it (e.g. OAuth-only). The transitional
        # var defaults to ``None`` outside DevToken middleware paths.
        token: MCPDevToken | None = None
        try:
            from ypl.mcp_server.auth_dev_token import current_devtoken_for_audit

            token = current_devtoken_for_audit()
        except Exception:
            token = None

        # In DEV_TOKEN mode the middleware MUST have populated context;
        # if not, fail loudly so the misconfiguration is obvious.
        if token is None and ctx is None and settings.MCP_SERVER_MODE == "DEV_TOKEN":
            logger.error("DevToken authentication is required, but no context was set")
            raise PermissionError("DevToken authentication is required")

        # OAuth fallback: if FastMCP somehow processed the request
        # without going through verify_token (shouldn't happen, but
        # defensive), reach into the access token directly.
        oauth_email: str | None = ctx.audit_email if ctx else None
        callback_url: str | None = None
        if token is None and oauth_email is None and settings.MCP_SERVER_MODE == "OAUTH":
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

            # Determine email and token type for the audit log row.
            email: str | None = None
            token_type: MCPTokenType

            if token:
                # DevToken — phase-5 deprecation window. Token row is
                # what makes ``mcp_dev_token_id`` populate.
                email = token.email
                token_type = MCPTokenType.DEV_TOKEN
            elif oauth_email:
                email = oauth_email
                token_type = MCPTokenType.OAUTH
            else:
                # No auth context — log a warning unless we already
                # have an exception in flight (which logs its own info).
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
                    ip_address=ctx.ip_address if ctx else None,
                    user_agent=ctx.user_agent if ctx else None,
                )


# Register the logging middleware
mcp_server.add_middleware(ToolCallLoggingMiddleware())
