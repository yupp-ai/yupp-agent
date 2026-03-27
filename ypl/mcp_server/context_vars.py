"""Shared context variables for MCP server request handling.

This module provides the request_context ContextVar that carries per-request
auth information through the middleware stack. It lives in its own module to
avoid circular imports between core.py (which creates the FastMCP server) and
auth_oauth.py (which is imported by core.py during server creation).

Set by:
  - DevTokenAuthMiddleware.dispatch() for DEV_TOKEN mode
  - AllowedDomainsGoogleProvider.verify_token() for OAUTH mode

Read by:
  - ToolCallLoggingMiddleware.on_call_tool() (core.py)
  - get_authenticated_user_email() (core.py)
  - get_requesting_user_id() (core.py)
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
