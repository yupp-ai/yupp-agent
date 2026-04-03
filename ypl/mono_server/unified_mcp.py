"""Unified MCP server for monolith mode.

A single :class:`fastmcp.FastMCP` instance that exposes all tools from both
the *harness MCP* (for agents) and the *yuppster MCP* (for developers),
mounted at a single ``/mcp`` endpoint.

Authentication is header-based — callers identify themselves via one of two
mutually exclusive paths:

* **Agent path** — ``x-ahs-token: <secret>`` header, or the Codex CLI
  fallback ``Authorization: Bearer <secret>:<session_id>``.  Validated
  against the process-local ``AHS_MCP_SECRET`` (no database round-trip).
  Sets ``mcp_session_id_var`` so harness tools can identify the session.

* **Developer path** — ``Authorization: Bearer yupp_dev_*``.  Validates
  the dev-token against yuppdb (``MCPDevToken`` table) and populates
  ``request_context`` so yuppster tools can identify the caller.  If yuppdb
  is not configured (one-box mode without the product database), returns
  HTTP 503 with a descriptive message rather than crashing.

Any request that does not match either pattern is rejected with HTTP 401.

This module is used exclusively by :mod:`ypl.mono_server.server`.  The
standalone servers (``ypl.agent_harness_service.server`` and
``ypl.mcp_server.server``) are unaffected — they continue to create and
manage their own :class:`~fastmcp.FastMCP` instances independently.

Tool registration
-----------------
Tools from both FastMCP instances are **not** copied at import time.
:func:`register_unified_tools` must be called exactly once during the
monolith's combined lifespan, *after* all side-effect tool-module imports
have fired (i.e. after ``import ypl.mcp_server.mcp_tools`` and
``from ypl.agent_harness_service.tools.local_mcp_server import mcp``).
The function is idempotent — subsequent calls are no-ops.
"""

from __future__ import annotations
from collections.abc import Awaitable, Callable

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET, mcp_session_id_var
from ypl.mcp_server.context_vars import request_context
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Unified FastMCP instance
# ---------------------------------------------------------------------------

#: Single FastMCP instance that combines all harness and yuppster tools.
#: Tools are populated by :func:`register_unified_tools` during startup.
unified_mcp: FastMCP = FastMCP("unified-mcp")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class UnifiedMcpAuthMiddleware(BaseHTTPMiddleware):
    """Unified authentication middleware for the ``/mcp`` endpoint.

    Accepts two distinct auth paths:

    1. **Agent path**: ``x-ahs-token: <secret>`` header, or
       ``Authorization: Bearer <secret>:<session_id>`` (Codex CLI compat).
       Validated in-process against ``AHS_MCP_SECRET``; no DB round-trip.
       Sets ``mcp_session_id_var`` so harness tools can identify the session.

    2. **Developer path**: ``Authorization: Bearer yupp_dev_*``.
       Validated against yuppdb.  Sets ``request_context`` so yuppster tools
       can identify the caller.  Returns HTTP 503 when yuppdb is unavailable
       (e.g. one-box mode without the product database).

    Any request that satisfies neither condition is rejected with HTTP 401.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # ------------------------------------------------------------------
        # 1. Agent path: x-ahs-token header
        # ------------------------------------------------------------------
        token = request.headers.get("x-ahs-token", "")
        session_id = request.headers.get("x-ahs-session-id", "")

        if token != AHS_MCP_SECRET:
            # Fallback: Bearer <secret>:<session_id> format.
            # Codex CLI only supports bearer_token_env_var for MCP auth, so
            # both the secret and the session ID are encoded into one token.
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                bearer = auth_header[7:]
                # Must contain ":" and must NOT look like a yupp_dev_* token
                # so developer tokens (which contain no ":") are not confused
                # with the <secret>:<session_id> format.
                if ":" in bearer and not bearer.startswith("yupp_dev_"):
                    bearer_secret, bearer_session_id = bearer.split(":", 1)
                    if bearer_secret == AHS_MCP_SECRET:
                        token = bearer_secret
                        session_id = bearer_session_id

        if token == AHS_MCP_SECRET:
            # Agent authenticated — bind harness session context and dispatch.
            cv_token = mcp_session_id_var.set(session_id)
            try:
                return await call_next(request)
            finally:
                mcp_session_id_var.reset(cv_token)

        # ------------------------------------------------------------------
        # 2. Developer path: Bearer yupp_dev_*
        # ------------------------------------------------------------------
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer yupp_dev_"):
            dev_token_str = auth_header[7:]  # strip "Bearer " prefix
            return await self._handle_dev_token(dev_token_str, request, call_next)

        # ------------------------------------------------------------------
        # 3. No valid auth
        # ------------------------------------------------------------------
        return JSONResponse(content={"detail": "Unauthorized"}, status_code=401)

    async def _handle_dev_token(
        self,
        token: str,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Validate a ``yupp_dev_*`` dev-token and populate ``request_context``.

        Returns HTTP 503 (not 500) when yuppdb is unavailable so callers can
        distinguish a configuration issue from an application logic error.

        Args:
            token:      The raw ``yupp_dev_*`` token string (without the
                        ``Bearer `` prefix).
            request:    Incoming Starlette request.
            call_next:  Next ASGI handler in the middleware chain.

        Returns:
            The response from downstream handlers, or an error JSONResponse.
        """
        # Lazy import: yuppdb may not be configured in one-box mode.
        # If the import fails we surface a 503 rather than a 500.
        try:
            from ypl.db.mcp import MCPTokenStatus
            from ypl.mcp_server.auth_dev_token import create_request_context, validate_token
        except Exception as exc:
            logger.error(
                "yuppdb not configured — developer token auth unavailable",
                error=str(exc),
            )
            return JSONResponse(
                content={"detail": "Developer token authentication is not available in this deployment"},
                status_code=503,
            )

        # Validate token against yuppdb.  A DB connection error also → 503.
        try:
            db_token, token_status = await validate_token(token)
        except Exception as exc:
            logger.error(
                "Token validation failed — yuppdb may not be configured",
                error=str(exc),
            )
            return JSONResponse(
                content={"detail": "Token validation failed — yuppdb may not be configured"},
                status_code=503,
            )

        if db_token is None:
            if token_status == MCPTokenStatus.REVOKED:
                detail = "Token has been revoked"
            elif token_status == MCPTokenStatus.EXPIRED:
                detail = "Token has expired"
            else:
                detail = "Invalid token"
            return JSONResponse(content={"detail": detail}, status_code=401)

        # Valid token — populate request_context for yuppster tools.
        ctx = create_request_context(db_token, request)
        cv_token = request_context.set(ctx)
        try:
            return await call_next(request)
        finally:
            request_context.reset(cv_token)


# ---------------------------------------------------------------------------
# HTTP app (module-level — auth middleware attached here)
# ---------------------------------------------------------------------------

#: ASGI app for the unified MCP endpoint.  Mount this at ``/mcp`` in the
#: monolith FastAPI application.  Auth is handled by
#: :class:`UnifiedMcpAuthMiddleware` (already attached).
unified_mcp_http_app = unified_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)
unified_mcp_http_app.add_middleware(UnifiedMcpAuthMiddleware)


# ---------------------------------------------------------------------------
# Tool registration (called once during monolith lifespan startup)
# ---------------------------------------------------------------------------

_tools_registered: bool = False


async def register_unified_tools() -> None:
    """Import all tools from both FastMCP instances into :data:`unified_mcp`.

    Must be called once during the monolith's combined lifespan *after* all
    side-effect tool-module imports have fired so that:

    * ``harness_mcp`` (from :mod:`ypl.agent_harness_service.tools.local_mcp_server`)
      has all harness tools registered via ``@mcp.tool()`` decorators.
    * ``yuppster_mcp_server`` (from :mod:`ypl.mcp_server.core`) has all
      yuppster tools registered via ``@mcp_server.tool()`` decorators.

    Uses :meth:`fastmcp.FastMCP.import_server` which copies tool *definitions*
    from the source servers into :data:`unified_mcp`.  Middleware and
    server-level lifespans from the source servers are **not** imported;
    authentication and session management are handled by
    :class:`UnifiedMcpAuthMiddleware` and the unified lifespan instead.

    This function is idempotent — subsequent calls are no-ops so it is safe
    to call from ``create_app()`` in tests without worrying about double
    registration.
    """
    global _tools_registered
    if _tools_registered:
        return

    from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
    from ypl.mcp_server.core import mcp_server as yuppster_mcp

    await unified_mcp.import_server(harness_mcp)
    logger.info("Unified MCP: imported harness tools")

    await unified_mcp.import_server(yuppster_mcp)
    logger.info("Unified MCP: imported yuppster tools")

    tools = await unified_mcp.get_tools()
    logger.info("Unified MCP ready", tool_count=len(tools))

    _tools_registered = True
