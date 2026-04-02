"""Monolith entrypoint for the Yupp Agent Platform.

Composes AHS, SAG, and MCP into a single FastAPI application with a
combined lifespan, enabling the entire agent platform to run as one process.

Route layout:
  /ahs/*          — Agent Harness Service (router already carries /ahs prefix)
  /mcp/harness/*  — Harness MCP server (FastMCP, for agents)
  /mcp/*          — Yuppster MCP server (FastMCP, for developers)
  /gw/slack/*     — Slack gateway (SAG; toggled by GATEWAY_SLACK_ENABLED)
  /health         — Liveness probe — always 200 OK

Startup order:
  1. AHS (registers orchestration callbacks, warms process pool, etc.)
  2. Harness MCP lifespan (via AHSState._mcp_lifespan_ctx)
  3. Yuppster MCP (batch-system init + FastMCP session-manager)
  4. Slack gateway (if GATEWAY_SLACK_ENABLED=true)

Shutdown is in strict reverse order so in-flight AHS tasks can still use
MCP tools while the scheduler drains.

All standalone server entrypoints (AHS, SAG, MCP) remain functional and
unchanged — the monolith is an *additive* composition, not a replacement.
"""

from __future__ import annotations
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

# Import mcp_tools to trigger @mcp_server.tool() decorator registration for all
# yuppster MCP tools.  This is a side-effect-only import — without it the
# yuppster FastMCP instance has an empty tool registry.
import ypl.mcp_server.mcp_tools  # noqa: F401
from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.github_webhook import webhook_router
from ypl.agent_harness_service.lifespan import AHSState, ahs_shutdown, ahs_startup
from ypl.agent_harness_service.middleware import AHSRequestLoggingMiddleware, McpTokenAuthMiddleware
from ypl.agent_harness_service.projects.project_routes import project_router
from ypl.agent_harness_service.routes import router as ahs_router
from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
from ypl.backend.routes.v1.yuppaste import router as yuppaste_router
from ypl.mcp_server.core import mcp_server as yuppster_mcp_server
from ypl.mcp_server.lifespan import mcp_shutdown, mcp_startup
from ypl.mono_server.config import MonoConfig
from ypl.slack_agent_gateway.lifespan import SAGState, sag_shutdown, sag_startup
from ypl.slack_agent_gateway.routes import router as sag_router

# ---------------------------------------------------------------------------
# Sub-app creation (module level — shared across all requests)
# ---------------------------------------------------------------------------

# Harness MCP app — exactly the same configuration as the standalone AHS server
# (path="/", transport=streamable-http, json_response, stateless).
# McpTokenAuthMiddleware is applied here so agents authenticating via the
# x-ahs-token header pass through correctly.
harness_mcp_app = harness_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)
harness_mcp_app.add_middleware(McpTokenAuthMiddleware)

# Yuppster MCP app — created with path="/" so it can be cleanly mounted at
# /mcp in the parent FastAPI app (Starlette strips the /mcp prefix before
# dispatching to the sub-app).  Tools are already registered above via the
# mcp_tools side-effect import.
yuppster_mcp_http_app = yuppster_mcp_server.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)

# ---------------------------------------------------------------------------
# Router setup guard
# ---------------------------------------------------------------------------

# Ensure the AHS router's sub-routers are added exactly once per process.
# The standalone AHS server (server.py) also calls router.include_router()
# inside its own create_app(); this flag prevents double-inclusion if both
# modules are imported in the same process (e.g. during tests).
_ahs_router_setup_done = False


def _setup_ahs_router() -> None:
    """Add sub-routers to the AHS router.  Idempotent — safe to call multiple times."""
    global _ahs_router_setup_done
    if _ahs_router_setup_done:
        return
    ahs_router.include_router(project_router)
    ahs_router.include_router(
        yuppaste_router,
        tags=["yuppaste"],
        dependencies=[Depends(verify_api_key)],
    )
    ahs_router.include_router(webhook_router)
    _ahs_router_setup_done = True


# ---------------------------------------------------------------------------
# Combined lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def combined_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Orchestrate startup and shutdown of all services in the monolith.

    Startup order:
      1. AHS (creates harness MCP lifespan ctx internally)
      2. Enter harness MCP lifespan (via AHSState._mcp_lifespan_ctx)
      3. Yuppster MCP batch-system init
      4. Enter yuppster MCP session-manager lifespan
      5. Slack gateway (if enabled)

    Shutdown is the mirror image of startup.  AHS shutdown runs *inside* the
    harness MCP lifespan context so that in-flight scheduler tasks can still
    call MCP tools while the drain completes.
    """
    config = MonoConfig()

    # --- 1. AHS startup -------------------------------------------------------
    # ahs_startup() wires orchestration callbacks, starts the warm process pool,
    # launches the scheduler, and returns a state object that carries the harness
    # MCP lifespan context manager (not yet entered).
    ahs_state: AHSState = await ahs_startup(app, harness_mcp_app)

    # --- 2. Harness MCP lifespan (FastMCP session manager) --------------------
    # Enter via the unentered context manager stored in ahs_state so real
    # exception info is forwarded to __aexit__ on a crash (same pattern as the
    # standalone AHS server).
    async with ahs_state._mcp_lifespan_ctx:
        # --- 3 & 4. Yuppster MCP ----------------------------------------------
        await mcp_startup()
        async with yuppster_mcp_http_app.lifespan(yuppster_mcp_http_app):
            # --- 5. Gateways --------------------------------------------------
            sag_state: SAGState | None = None
            if config.gateway_slack_enabled:
                sag_state = await sag_startup()

            try:
                yield
            finally:
                # Shutdown in reverse order
                if sag_state is not None:
                    await sag_shutdown(sag_state)

        # Yuppster MCP teardown (batch-system flush, Sentry close, GCP log flush)
        await mcp_shutdown()

        # AHS teardown — inside harness MCP lifespan so in-flight tasks that
        # call MCP tools during scheduler drain can still complete
        await ahs_shutdown(ahs_state)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and configure the monolith FastAPI application.

    Returns a fully-wired FastAPI app suitable for ``uvicorn
    ypl.mono_server.server:app``.  The standalone AHS, SAG, and MCP server
    entrypoints remain unaffected.
    """
    config = MonoConfig()

    application = FastAPI(
        title="Yupp Agent Platform",
        description="One-box deployment: AHS + SAG + MCP in a single process",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=combined_lifespan,
    )

    # CORS — same allowed origins as the standalone AHS server
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://yupp-soul.vercel.app",
            "https://chaos-soul.vercel.app",
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:8080",
        ],
        allow_origin_regex=r"https://.*\.yupp\.ai",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_middleware(AHSRequestLoggingMiddleware)

    # --- AHS routes (/ahs/* prefix already on router) ------------------------
    _setup_ahs_router()
    application.include_router(ahs_router)

    # --- Harness MCP (agents connect here) -----------------------------------
    application.mount("/mcp/harness", harness_mcp_app)

    # --- Yuppster MCP (developers/IDEs connect here) -------------------------
    application.mount("/mcp", yuppster_mcp_http_app)

    # --- Slack gateway (pluggable) -------------------------------------------
    if config.gateway_slack_enabled:
        application.include_router(sag_router, prefix="/gw/slack")

    # --- Health check (always registered, no auth) ---------------------------
    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness probe — always returns 200 OK."""
        return {"status": "ok"}

    return application


# Module-level app instance used by uvicorn:
#   uvicorn ypl.mono_server.server:app --port 8090
app = create_app()
