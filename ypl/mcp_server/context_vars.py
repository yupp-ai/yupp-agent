"""Context vars for the platform MCP server.

The per-request **identity** context (``request_context``) is now defined
in :mod:`ypl.mcp_common.auth_context` so it can be shared with the harness
MCP and any future external MCP — see the docstring there for the typed
:class:`~ypl.mcp_common.auth_context.RequestContext` shape and accessors.

This module retains :data:`mcp_request_id_var`, an HTTP-request-scoped UUID
used by ``server.py``'s ``_McpSessionIdFilter`` to correlate stdlib log
records emitted from inside ``mcp.server.streamable_http``. It is a
log-correlation concern, not an identity one, and lives outside the typed
auth context for that reason.

For backwards compatibility this module also re-exports
:data:`request_context` from :mod:`ypl.mcp_common.auth_context`. New code
should import directly from there::

    from ypl.mcp_common.auth_context import current_request_context, request_context
"""

from __future__ import annotations
from contextvars import ContextVar

# Re-exported from the canonical home in ypl.mcp_common.
from ypl.mcp_common.auth_context import request_context

# UUID assigned by McpSessionMiddleware for every inbound HTTP request.
# Enables correlation across all log calls within a single MCP session
# (one stateless_http request = one session).  Set to None outside of an
# active MCP request.
mcp_request_id_var: ContextVar[str | None] = ContextVar("mcp_request_id", default=None)

__all__ = ["mcp_request_id_var", "request_context"]
