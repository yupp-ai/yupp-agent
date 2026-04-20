"""FastAPI server for Agent Harness Service.

Standalone service that hosts AI agents with persistent identity,
memory, and sandboxed repo access. Communicates with the Slack
gateway (and other callers) via REST + SSE.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.github_webhook import webhook_router
from ypl.agent_harness_service.lifespan import AHSState, ahs_shutdown, ahs_startup
from ypl.agent_harness_service.middleware import AHSRequestLoggingMiddleware, McpTokenAuthMiddleware
from ypl.agent_harness_service.projects.project_routes import project_router
from ypl.agent_harness_service.routes import router
from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
from ypl.backend.config import settings
from ypl.backend.routes.v1.yuppaste import router as yuppaste_router

# Create the MCP sub-app once so we can wire its lifespan into the main app.
# json_response=True: return application/json instead of SSE-wrapped responses.
# stateless_http=True: no server-side session tracking (session_id is a tool param).
# Matches the yuppster-mcp-server config in ypl/mcp_server/server.py.
mcp_app = harness_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)

mcp_app.add_middleware(McpTokenAuthMiddleware)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle."""
    state: AHSState = await ahs_startup(app, mcp_app)
    # Use `async with` so that real exception info (exc_type, exc_val, exc_tb)
    # is forwarded to the MCP lifespan's __aexit__, rather than always passing
    # (None, None, None).  ahs_shutdown() does NOT call __aexit__ itself.
    # TODO: If state._mcp_lifespan_ctx.__aenter__() raises (extremely unlikely),
    # ahs_shutdown() will not be called and startup resources (scheduler task,
    # auto-stale sweep, DB connections) will leak.  This is a pre-existing
    # non-regression edge case; harden if FastMCP's lifespan becomes more complex.
    async with state._mcp_lifespan_ctx:
        try:
            yield
        finally:
            await ahs_shutdown(state)


def create_app() -> FastAPI:
    """Create and configure the AHS FastAPI application."""
    application = FastAPI(
        title="Agent Harness Service",
        description="Hosts AI agents with persistent identity, memory, and sandboxed repo access",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )

    # CORS: always allow the loopback ports used by local dev; additional
    # origins come from ``AHS_CORS_ALLOW_ORIGINS`` (JSON list) and an optional
    # regex via ``AHS_CORS_ALLOW_ORIGIN_REGEX``. Deployers set these in .env.
    cors_origins = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:8080",
        *settings.AHS_CORS_ALLOW_ORIGINS,
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=settings.AHS_CORS_ALLOW_ORIGIN_REGEX or None,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_middleware(AHSRequestLoggingMiddleware)
    router.include_router(project_router)
    router.include_router(yuppaste_router, tags=["yuppaste"], dependencies=[Depends(verify_api_key)])
    router.include_router(webhook_router)
    application.include_router(router)

    # Mount the MCP server as an HTTP sub-app (streamable-http transport).
    # Agents connect to this endpoint instead of spawning a stdio subprocess.
    application.mount("/mcp/harness", mcp_app)

    return application


app = create_app()


# Health check
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
