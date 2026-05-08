"""Split MCP sub-apps for monolith mode.

Exposes the harness MCP and the agcouch MCP as *two independent* Starlette
ASGI sub-apps, each with its own authentication middleware and its own URL
prefix:

* ``/mcp/harness`` — agent tools. Only accepts ``x-ahs-token`` (or the
  Codex-CLI fallback ``Authorization: Bearer <secret>:<session_id>``).
  Validated in-process against ``AHS_MCP_SECRET``; no DB round-trip.
  Publishes a typed
  :class:`~ypl.mcp_common.auth_context.RequestContext` so harness tools
  see one shape regardless of mount.

* ``/mcp/agcouch`` — developer/product tools. Only accepts
  ``Authorization: Bearer yupp_dev_*``. Validated against yuppdb;
  publishes the same :class:`RequestContext`. Returns HTTP 503 when
  yuppdb is unavailable (one-box deployments without the product
  database).

Any request that does not match the expected auth for its path is rejected
with HTTP 401. Tool sets are **never merged** — a yupp_dev token on the
externally-exposed ``/mcp/agcouch`` physically cannot reach harness tools,
and vice versa. This replaces the earlier "unified" single-endpoint design.

This module is used exclusively by :mod:`ypl.mono_server.server`. The
standalone servers (``ypl.agent_harness_service.server`` and
``ypl.mcp_server.server``) are unaffected — they manage their own FastMCP
instances independently.
"""

from __future__ import annotations
import hmac
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET
from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
from ypl.mcp_common.auth_context import RequestContext, mcp_session_id_var, request_context
from ypl.mcp_server.core import mcp_server as agcouch_mcp
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Harness auth — x-ahs-token only (rejects yupp_dev_* tokens)
# ---------------------------------------------------------------------------


class HarnessMcpAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests to ``/mcp/harness``.

    Accepts either:

    1. ``x-ahs-token: <AHS_MCP_SECRET>`` header (primary agent path), or
    2. ``Authorization: Bearer <AHS_MCP_SECRET>:<session_id>`` (Codex-CLI
       fallback; Codex only supports bearer-token env vars, so both the
       secret and the session ID are encoded into one token).

    Developer tokens (``Bearer yupp_dev_*``) are explicitly rejected — they
    belong on the ``/mcp/agcouch`` mount. All secret comparisons use
    :func:`hmac.compare_digest` to avoid timing side channels.

    On success, publishes a typed
    :class:`~ypl.mcp_common.auth_context.RequestContext` with
    ``auth_kind="agent_secret"`` and the AHS identity headers
    (``X-User-ID`` / ``X-AHS-Agent-Name`` / ``X-AHS-Session-ID``) the
    runner injects into the sandboxed ``.mcp.json``. These headers are
    tamper-proof inside the sandbox.

    Rejects all requests with 503 when ``AHS_MCP_SECRET`` is unset (an empty
    secret would make every request succeed, collapsing the boundary).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not AHS_MCP_SECRET:
            logger.error("AHS_MCP_SECRET is not configured — rejecting all /mcp/harness requests")
            return JSONResponse(content={"detail": "Service not configured"}, status_code=503)

        token = request.headers.get("x-ahs-token", "")
        session_id = request.headers.get("x-ahs-session-id", "")
        auth_header = request.headers.get("authorization", "")

        if not hmac.compare_digest(token, AHS_MCP_SECRET) and auth_header.startswith("Bearer "):
            bearer = auth_header[7:]
            # Reject dev tokens explicitly — they should go to /mcp/agcouch.
            if bearer.startswith("yupp_dev_"):
                return JSONResponse(
                    content={"detail": "Developer tokens are not accepted on /mcp/harness — use /mcp/agcouch"},
                    status_code=401,
                )
            if ":" in bearer:
                bearer_secret, bearer_session_id = bearer.split(":", 1)
                if hmac.compare_digest(bearer_secret, AHS_MCP_SECRET):
                    token = bearer_secret
                    session_id = bearer_session_id

        if not hmac.compare_digest(token, AHS_MCP_SECRET):
            return JSONResponse(content={"detail": "Unauthorized"}, status_code=401)

        # Build the typed context from the AHS runner's tamper-proof
        # headers. ``X-User-ID`` is the user the agent is acting on
        # behalf of; ``X-AHS-Agent-Name`` and ``X-AHS-Session-ID`` are
        # the agent's own identity. None of these are inferred from the
        # secret alone.
        ctx = RequestContext(
            auth_kind="agent_secret",
            requesting_user_id=request.headers.get("x-user-id") or None,
            ahs_session_id=session_id or None,
            ahs_agent_name=request.headers.get("x-ahs-agent-name") or None,
            audit_email=None,  # agent_secret callers have no audit email
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        ctx_token = request_context.set(ctx)
        # Mirror the session id on the legacy ContextVar for tools
        # (e.g. ``new_task``) that haven't migrated to the typed context
        # yet. Removed once those callers are migrated.
        legacy_token = mcp_session_id_var.set(session_id)
        try:
            return await call_next(request)
        finally:
            mcp_session_id_var.reset(legacy_token)
            request_context.reset(ctx_token)


# ---------------------------------------------------------------------------
# Agcouch auth — Bearer yupp_dev_* only (rejects agent tokens)
# ---------------------------------------------------------------------------


class AgcouchMcpAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests to ``/mcp/agcouch``.

    Accepts only ``Authorization: Bearer yupp_dev_*`` tokens, validated
    against yuppdb's ``MCPDevToken`` table. Agent tokens (``x-ahs-token`` or
    ``Bearer <secret>:<session>``) are explicitly rejected — they belong on
    ``/mcp/harness``.

    Returns HTTP 503 when yuppdb is unavailable (one-box mode without the
    product database) so callers can distinguish configuration from logic
    errors.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Validate dev-token before processing request.

        Wraps :meth:`_dispatch_authenticated` so every response — accepted
        or rejected — carries the
        :data:`~ypl.mcp_server.auth_dev_token.DEPRECATION_HEADER`. The
        ``/mcp/agcouch`` mount is dedicated to dev-token traffic, so even
        the no-bearer 401 is, in effect, telling the caller "your future
        dev-token request will not be honoured".
        """
        # Lazy import to keep the constants available even when the dev-token
        # machinery is removed in phase 5b — at that point this whole class
        # disappears with it, so the import is harmless.
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER, DEPRECATION_NOTICE

        response = await self._dispatch_authenticated(request, call_next)
        response.headers[DEPRECATION_HEADER] = DEPRECATION_NOTICE
        return response

    async def _dispatch_authenticated(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        auth_header = request.headers.get("authorization", "")

        if not auth_header.startswith("Bearer yupp_dev_"):
            return JSONResponse(content={"detail": "Unauthorized"}, status_code=401)

        token = auth_header[7:]  # strip "Bearer "

        # Lazy import: yuppdb may not be configured in one-box mode.
        try:
            from ypl.backend.utils.soul_utils import has_permission_cached
            from ypl.db.mcp import MCPTokenStatus
            from ypl.db.rbac import Permission
            from ypl.mcp_server.auth_dev_token import _devtoken_audit_var, build_request_context, validate_token
        except ImportError as exc:
            logger.error(
                "yuppdb not configured — developer token auth unavailable",
                error=str(exc),
            )
            return JSONResponse(
                content={"detail": "Developer token authentication is not available in this deployment"},
                status_code=503,
            )

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

        # Mirror the standalone ``DevTokenAuthMiddleware`` USE_MCP gate
        # (auth_dev_token.py:429-437). Without this, an active token
        # whose owner has lost ``USE_MCP`` would still be accepted on
        # ``/mcp/agcouch`` here while being rejected on the standalone
        # MCP server — a security boundary mismatch.
        if not await has_permission_cached(db_token.email, Permission.USE_MCP):
            logger.warning(
                "DevToken authentication rejected - user lacks USE_MCP permission",
                email_local_part=db_token.email.split("@")[0],
            )
            return JSONResponse(
                content={
                    "detail": "User does not have permission to use MCP. Please contact your TLM to add the permission."
                },
                status_code=403,
            )

        # ``build_request_context`` does a fresh DB lookup
        # (email → user_id). Treat any failure the same way the OAuth
        # path treats it: degrade to ``requesting_user_id=None`` rather
        # than surfacing an unhandled 500. ``build_request_context``
        # itself already wraps the lookup in try/except, so this is
        # belt-and-suspenders against future regressions.
        try:
            ctx = await build_request_context(db_token, request)
        except Exception:
            logger.exception(
                "build_request_context failed — yuppdb may be unavailable",
                email_local_part=db_token.email.split("@")[0],
            )
            return JSONResponse(
                content={"detail": "Token validation failed — yuppdb may not be configured"},
                status_code=503,
            )
        ctx_token = request_context.set(ctx)
        audit_token = _devtoken_audit_var.set(db_token)
        try:
            return await call_next(request)
        finally:
            request_context.reset(ctx_token)
            _devtoken_audit_var.reset(audit_token)


# ---------------------------------------------------------------------------
# HTTP apps (module-level — auth middleware attached here)
# ---------------------------------------------------------------------------

#: ASGI app for agent-facing harness tools. Mount at ``/mcp/harness``.
harness_mcp_http_app = harness_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)
harness_mcp_http_app.add_middleware(HarnessMcpAuthMiddleware)

#: ASGI app for developer-facing agcouch tools. Mount at ``/mcp/agcouch``.
agcouch_mcp_http_app = agcouch_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)
agcouch_mcp_http_app.add_middleware(AgcouchMcpAuthMiddleware)
