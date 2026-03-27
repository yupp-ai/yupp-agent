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
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastmcp.exceptions import NotFoundError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ypl.backend.config import settings
from ypl.backend.utils.batch_utils import initialize_batch_system, stop_batch_system
from ypl.logger import flush_and_close_google_cloud_logging
from ypl.loggers.config import flush_and_close_google_logging_client
from ypl.mcp_server.context_vars import request_context
from ypl.mcp_server.core import mcp_server

# Import mcp_tools to register tools via decorators
from ypl.mcp_server.mcp_tools import execute_tool, format_tool_result
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()


# --- Route handlers ---


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
    """
    logger.info("MCP Server starting up...", mode=settings.MCP_SERVER_MODE)

    setup_asyncio_logging()

    # Initialize batch system
    logger.info("APP INIT: Initializing batch system...")
    await initialize_batch_system()

    # Run FastMCP's lifespan to initialize session manager
    logger.info("APP INIT: Starting FastMCP session manager...")
    async with mcp_http_app.lifespan(mcp_http_app):
        logger.info("APP INIT: MCP Server ready", mode=settings.MCP_SERVER_MODE)
        yield

    # Cleanup on shutdown
    logger.info("MCP Server shutting down...")

    # Ensure buffers are flushed within 5 seconds during shutdown
    try:
        async with asyncio.timeout(5):
            await stop_batch_system()
    except TimeoutError:
        logger.warning("Timed out waiting for buffers flush during shutdown")
    except Exception:
        logger.warning("Error flushing buffers during shutdown", exc_info=True)

    # Close Sentry aiohttp session
    from ypl.mcp_server.tools.sentry import close_sentry_session

    await close_sentry_session()

    # Flush and close Google Cloud logging
    flush_and_close_google_cloud_logging()
    flush_and_close_google_logging_client()

    logger.info("MCP Server shut down complete.")


# --- Mode-based middleware selection ---


def _get_middleware() -> list[Middleware]:
    """Get middleware list based on MCP_SERVER_MODE.

    Returns:
        - DEV_TOKEN mode: [DevTokenAuthMiddleware] - validates yupp_dev_* tokens
        - OAUTH mode: [] - FastMCP handles OAuth authentication
    """
    if settings.MCP_SERVER_MODE == "DEV_TOKEN":
        from ypl.mcp_server.auth_dev_token import DevTokenAuthMiddleware

        logger.info("Configuring DevToken authentication middleware")
        return [Middleware(DevTokenAuthMiddleware, request_context_var=request_context)]

    # OAUTH mode - FastMCP handles authentication via GoogleProvider
    logger.info("OAuth mode - authentication handled by FastMCP")
    return []


# Create main Starlette app with mode-based middleware
app = Starlette(
    debug=False,
    routes=[
        Route("/health", health_check, methods=["GET"]),
        Route("/healthz", health_check, methods=["GET"]),
        Route("/tools", list_tools, methods=["GET"]),
        Route("/tools/{tool_name}", invoke_tool, methods=["POST"]),
        # Mount MCP at root - FastMCP handles /mcp path internally
        Mount("/", app=mcp_http_app),
    ],
    middleware=_get_middleware(),
    lifespan=lifespan,
)
