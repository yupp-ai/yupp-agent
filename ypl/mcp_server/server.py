"""MCP server using FastMCP with Starlette.

This server provides:
- MCP protocol via Streamable HTTP transport (POST /mcp)
- REST convenience endpoints (GET/POST /tools/*)
- Health check endpoint (GET /health)

Authentication mode is controlled by MCP_SERVER_MODE setting:
- DEV_TOKEN: Bearer tokens created via CLI (yupp_dev_* format)
- OAUTH: Google OAuth via FastMCP's GoogleProvider
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime

from fastmcp.exceptions import NotFoundError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from structlog.contextvars import bind_contextvars, clear_contextvars

from ypl.backend.config import settings
from ypl.mcp_server.context_vars import mcp_request_id_var, request_context
from ypl.mcp_server.core import mcp_server
from ypl.mcp_server.lifespan import mcp_shutdown, mcp_startup

# Import mcp_tools to register tools via decorators
from ypl.mcp_server.mcp_tools import execute_tool, format_tool_result
from ypl.structured_logger import get_logger

logger = get_logger()


# --- MCP session tracking ---

# Paths that do not need session-level tracking (lightweight / frequent).
_PATHS_WITHOUT_SESSION_TRACKING: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/healthz",
        "/robots.txt",
        "/tools",
    }
)


class _McpSessionIdFilter(logging.Filter):
    """Injects mcp_request_id into stdlib log records from mcp.server.streamable_http.

    Reads the UUID from mcp_request_id_var (ContextVar) so the value is
    automatically isolated per asyncio task / coroutine chain.  The attribute
    is surfaced as a structured field in GCP log entries alongside the SDK's
    own "Terminating session: None" message, enabling cross-request correlation
    without patching the MCP SDK.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.mcp_request_id = mcp_request_id_var.get()
        return True


# Register once at import time — the filter is stateless and safe to share.
logging.getLogger("mcp.server.streamable_http").addFilter(_McpSessionIdFilter())


class McpSessionMiddleware(BaseHTTPMiddleware):
    """Assigns a UUID to every inbound MCP request for session-level auditing.

    Background
    ----------
    The server uses ``stateless_http=True`` (required for horizontal scaling).
    In this mode the MCP SDK never initialises a persistent session, so
    ``session_id`` remains ``None`` throughout the request and every connection
    produces the log line::

        mcp.server.streamable_http  INFO  Terminating session: None

    This makes replay detection, multi-call correlation, and audit trail
    reconstruction impossible.

    What this middleware does
    -------------------------
    1. Generates a UUID (``mcp_request_id``) at the start of every MCP HTTP
       request and stores it in ``mcp_request_id_var``.
    2. Binds the UUID to structlog context vars (``bind_contextvars``) so it
       appears automatically in every structlog call made during the request,
       including tool-call audit logs and any intermediary logic.
    3. Emits structured "Initializing session" / "Terminating session" log
       messages that bracket the session lifetime with a correlated UUID —
       replacing the SDK's uninformative ``None`` entries.

    Companion
    ---------
    ``_McpSessionIdFilter`` (registered above) propagates the same UUID to the
    MCP SDK's own stdlib logger as a ``mcp_request_id`` extra field on each
    ``LogRecord``.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        # Skip tracking for lightweight utility endpoints to keep logs clean.
        if path in _PATHS_WITHOUT_SESSION_TRACKING or path.startswith("/tools/"):
            return await call_next(request)

        session_id = str(uuid.uuid4())
        cv_token = mcp_request_id_var.set(session_id)
        method = request.method
        bind_contextvars(mcp_request_id=session_id)

        logger.info("Initializing session", http_method=method, http_path=path)
        response: Response | None = None
        try:
            response = await call_next(request)
        finally:
            status_code = response.status_code if response is not None else None
            log_kwargs: dict[str, object] = {
                k: v
                for k, v in {
                    "http_method": method,
                    "http_path": path,
                    "http_status": status_code,
                }.items()
                if v is not None
            }
            # Emit at WARNING level for error conditions so SRE can correlate
            # with Cloud Run platform logs; INFO for successful responses.
            if response is None:
                logger.warning("Session terminated without response (exception in call_next)", **log_kwargs)
            elif 400 <= status_code < 500:  # type: ignore[operator]
                logger.warning("Terminating session with client error", **log_kwargs)
            elif status_code >= 500:  # type: ignore[operator]
                logger.warning("Terminating session with server error", **log_kwargs)
            else:
                logger.info("Terminating session", **log_kwargs)
            clear_contextvars()
            mcp_request_id_var.reset(cv_token)

        # call_next() raises rather than returning None on transport errors,
        # so response is always set when execution reaches this line.
        assert response is not None
        return response


# --- Route handlers ---


async def root(request: Request) -> JSONResponse:
    """Root handler for Cloud Run GFE probes.

    The Cloud Run Google Front End (GFE) periodically probes GET / and logs
    any non-2xx response as a WARNING-severity request log entry with a null
    message payload.  Return a minimal 200 OK to silence those spurious
    WARNING log entries.  Use /health for real liveness checks.
    """
    return JSONResponse({"status": "ok"})


async def robots_txt(request: Request) -> PlainTextResponse:
    """Robots.txt handler for Cloud Run GFE probes.

    The Cloud Run GFE occasionally probes GET /robots.txt; a 404 is logged
    as a WARNING with a null message.  Return a deny-all robots.txt to
    eliminate those entries and keep crawlers out.
    """
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for load balancer."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "mcp-server",
            "mode": settings.MCP_SERVER_MODE,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )


# --- SSE connection drain constants ---

# Cloud Run enforces an absolute per-request timeout (currently --timeout=10m = 600 s).
# MCP clients (Claude Code, Cursor, etc.) open a long-lived GET /mcp SSE connection to
# receive server-initiated messages.  When Cloud Run kills the connection at the hard
# deadline the platform emits:
#   "Truncated response body. Usually implies that the request timed out …"
# and increments the Cloud Run error metric.
#
# Fix: intercept GET /mcp before FastMCP and serve a self-managed SSE stream that:
#   1. Sends keep-alive comments every _SSE_KEEPALIVE_INTERVAL_S seconds (keeps TCP alive
#      and proves to Cloud Run's idle-timeout logic that the connection is active).
#   2. Closes the stream gracefully after _SSE_MAX_DURATION_S seconds (before Cloud Run's
#      10-minute hard limit), allowing the MCP client to reconnect cleanly rather than
#      experiencing an abrupt connection reset.
#
# The server runs with stateless_http=True so it never sends actual server-initiated
# notifications over the SSE channel; the drain stream is semantically equivalent.

_SSE_KEEPALIVE_INTERVAL_S: int = 30  # seconds between SSE keep-alive comments
_SSE_MAX_DURATION_S: int = 480  # must be < Cloud Run --timeout with a ~2 min safety buffer


async def mcp_get_sse_drain(request: Request) -> StreamingResponse:
    """SSE drain handler for GET /mcp — prevents Cloud Run 'Truncated response body' warnings.

    Intercepts GET /mcp before FastMCP so that the long-lived SSE connection is
    managed by us rather than by the MCP SDK.  The handler sends periodic SSE
    keep-alive comments and closes the connection gracefully after
    ``_SSE_MAX_DURATION_S`` seconds, well before Cloud Run's 10-minute hard timeout.

    Background
    ----------
    Cloud Run enforces an absolute per-request timeout (``--timeout=10m``).  MCP
    clients open a persistent ``GET /mcp`` SSE stream to receive server-initiated
    messages.  Because the server runs with ``stateless_http=True`` it never
    actually pushes notifications, so the stream just sits idle.  Cloud Run kills
    it after 10 minutes and logs a WARNING-severity "Truncated response body" entry
    (~4 simultaneous occurrences every ~10 minutes = ~96 platform warnings/day).

    Resolution
    ----------
    By closing the stream ourselves at 8 minutes the client sees a clean EOF,
    resets its reconnect counter, and re-dials immediately.  Cloud Run logs a
    normal 200 response with no truncation warning.  The observable effect to the
    MCP client is identical to a server-initiated graceful stream close.
    """

    async def _sse_generator() -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SSE_MAX_DURATION_S
        while True:
            # Exit early if the client has already disconnected (e.g. IDE closed).
            if await request.is_disconnected():
                logger.info("SSE client disconnected", http_path="/mcp")
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Yield a final drain comment so the client sees an SSE frame,
                # then let the generator return — this closes the stream cleanly.
                yield ": connection-drain\n\n"
                logger.info("SSE connection drained (max duration reached)", http_path="/mcp")
                return
            # SSE comment lines (": …") are not dispatched as events to the
            # application but ARE sent over the wire — perfect for keep-alive.
            yield ": keep-alive\n\n"
            sleep_s = min(_SSE_KEEPALIVE_INTERVAL_S, remaining)
            await asyncio.sleep(sleep_s)

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx/proxy buffering for SSE
        },
    )


async def list_tools(request: Request) -> JSONResponse:
    """List all available MCP tools (REST convenience endpoint)."""
    tools = await mcp_server.get_tools()
    return JSONResponse(
        {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                }
                for tool in tools.values()
            ]
        }
    )


async def invoke_tool(request: Request) -> JSONResponse:
    """Invoke a specific MCP tool (REST convenience endpoint).

    Requires authentication via DevToken. OAuth users should use the MCP protocol endpoint.

    Audit logging is handled by ToolCallLoggingMiddleware (in core.py) which fires
    automatically when execute_tool() delegates to FastMCP's tool dispatch.
    """
    tool_name = request.path_params["tool_name"]
    start_time = time.time()

    # Get authentication context (set by DevTokenAuthMiddleware)
    token = getattr(request.state, "mcp_token", None)
    engineer_email = getattr(request.state, "engineer_email", None)

    if not token or not engineer_email:
        return JSONResponse(
            {"error": "Authentication required. Use DevToken for REST endpoints."},
            status_code=401,
        )

    try:
        body = await request.json()

        # Validate body is a dict (JSON object) - arrays, strings, etc. are not valid
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return JSONResponse(
                {"error": "arguments must be an object"},
                status_code=400,
            )

        logger.info(
            "MCP tool invocation (REST)",
            engineer=engineer_email,
            tool=tool_name,
            arguments=arguments,
        )

        # Execute the tool — delegates to FastMCP's registered tools.
        # ToolCallLoggingMiddleware handles audit logging automatically.
        result = await execute_tool(tool_name, arguments)

        execution_time_ms = int((time.time() - start_time) * 1000)
        result_text = format_tool_result(result)

        return JSONResponse(
            {
                "success": True,
                "result": result_text,
                "execution_time_ms": execution_time_ms,
            }
        )

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            "Invalid JSON in request body",
            tool=tool_name,
            engineer=engineer_email,
            exc_info=True,
        )
        return JSONResponse(
            {"error": f"Invalid JSON in request body: {e}"},
            status_code=400,
        )

    except NotFoundError:
        return JSONResponse(
            {"success": False, "error": f"Unknown tool: {tool_name}"},
            status_code=404,
        )

    except ValidationError as e:
        return JSONResponse(
            {"success": False, "error": f"Invalid arguments: {e}"},
            status_code=422,
        )

    except Exception as err:
        execution_time_ms = int((time.time() - start_time) * 1000)
        logger.error("Error invoking tool", tool=tool_name, exc_info=True, engineer=engineer_email)
        return JSONResponse(
            {"error": str(err)},
            status_code=500,
        )


# --- Lifespan (following MCP SDK docs pattern) ---


# Create the MCP ASGI app - it handles /mcp path internally
mcp_http_app = mcp_server.http_app(
    path="/mcp",
    transport="streamable-http",
    json_response=True,  # Return JSON instead of SSE for responses
    stateless_http=True,  # For horizontal scaling
)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    """Lifespan context manager for the MCP server.

    Combines our app initialization with FastMCP's session manager lifespan.
    Startup and shutdown logic is delegated to :func:`mcp_startup` /
    :func:`mcp_shutdown` (``ypl.mcp_server.lifespan``) so the monolith can
    reuse them without pulling in the standalone Starlette app.
    """
    logger.info("MCP Server starting up...", mode=settings.MCP_SERVER_MODE)
    await mcp_startup()

    # Run FastMCP's lifespan to initialize session manager
    logger.info("APP INIT: Starting FastMCP session manager...")
    async with mcp_http_app.lifespan(mcp_http_app):
        logger.info("APP INIT: MCP Server ready", mode=settings.MCP_SERVER_MODE)
        yield

    await mcp_shutdown()


# --- Mode-based middleware selection ---


def _get_middleware() -> list[Middleware]:
    """Get middleware list based on MCP_SERVER_MODE.

    McpSessionMiddleware is always the outermost layer (first in list) so the
    session UUID is bound to structlog before any auth or tool logic runs.

    Returns:
        - DEV_TOKEN mode: [McpSessionMiddleware, DevTokenAuthMiddleware]
        - OAUTH mode:     [McpSessionMiddleware]
    """
    # Outermost first: session tracking wraps everything so every log call
    # within auth, tool dispatch, and error handling carries mcp_request_id.
    middlewares: list[Middleware] = [Middleware(McpSessionMiddleware)]

    if settings.MCP_SERVER_MODE == "DEV_TOKEN":
        from ypl.mcp_server.auth_dev_token import DevTokenAuthMiddleware

        logger.info("Configuring DevToken authentication middleware")
        middlewares.append(Middleware(DevTokenAuthMiddleware, request_context_var=request_context))
    else:
        # OAUTH mode - FastMCP handles authentication via GoogleProvider
        logger.info("OAuth mode - authentication handled by FastMCP")

    return middlewares


# Create main Starlette app with mode-based middleware
app = Starlette(
    debug=False,
    routes=[
        # GFE probe handlers must come first — Cloud Run's Google Front End
        # probes these paths and emits null-message WARNING log entries for
        # any non-2xx response.  Explicit 200 OK handlers silence those.
        Route("/", root, methods=["GET"]),
        Route("/robots.txt", robots_txt, methods=["GET"]),
        Route("/health", health_check, methods=["GET"]),
        Route("/healthz", health_check, methods=["GET"]),
        Route("/tools", list_tools, methods=["GET"]),
        Route("/tools/{tool_name}", invoke_tool, methods=["POST"]),
        # GET /mcp: intercept SSE stream before FastMCP to prevent Cloud Run
        # 'Truncated response body' warnings.  POST /mcp (tool calls) and
        # DELETE /mcp (session termination) still fall through to mcp_http_app.
        # See mcp_get_sse_drain docstring for full explanation.
        # IMPORTANT: only valid when mcp_http_app is configured with stateless_http=True
        # (see mcp_http_app definition above); a stateful server would need its own
        # SSE channel and this interceptor would break session-level notifications.
        Route("/mcp", mcp_get_sse_drain, methods=["GET"]),
        # Mount MCP at root - FastMCP handles /mcp path internally
        Mount("/", app=mcp_http_app),
    ],
    middleware=_get_middleware(),
    lifespan=lifespan,
)
