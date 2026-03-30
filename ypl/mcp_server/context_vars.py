"""Shared context variables for MCP server request handling.

This module provides ContextVars that carry per-request state through the
middleware stack.  It lives in its own module to avoid circular imports between
core.py (which creates the FastMCP server) and auth_oauth.py (which is imported
by core.py during server creation).

request_context
  Set by:
    - DevTokenAuthMiddleware.dispatch() for DEV_TOKEN mode
    - AllowedDomainsGoogleProvider.verify_token() for OAUTH mode
  Read by:
    - ToolCallLoggingMiddleware.on_call_tool() (core.py)
    - get_authenticated_user_email() (core.py)
    - get_requesting_user_id() (core.py)

mcp_request_id_var
  Set by:
    - McpSessionMiddleware.dispatch() for every inbound MCP request
  Read by:
    - _McpSessionIdFilter (server.py) to inject the ID into stdlib log records
      emitted by mcp.server.streamable_http
  Note: structlog propagation happens via bind_contextvars, not this var.
"""

from contextvars import ContextVar
from typing import Any

# Dict shape for DEV_TOKEN mode:
#   {"token": MCPDevToken, "token_type": MCPTokenType.DEV_TOKEN,
#    "ip_address": str|None, "user_agent": str|None, "requesting_user_id": str|None}
#
# Dict shape for OAUTH mode:
#   {"email": str, "token_type": MCPTokenType.OAUTH,
#    "ip_address": None, "user_agent": None, "callback_url": str | None}
request_context: ContextVar[dict[str, Any] | None] = ContextVar("request_context", default=None)

# UUID assigned by McpSessionMiddleware for every inbound HTTP request.
# Enables correlation across all log calls within a single MCP session
# (one stateless_http request = one session).  Set to None outside of an
# active MCP request.
mcp_request_id_var: ContextVar[str | None] = ContextVar("mcp_request_id", default=None)
