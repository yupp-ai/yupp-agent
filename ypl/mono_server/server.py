"""Monolith entrypoint for the Yupp Agent Platform.

Composes AHS, SAG, and MCP into a single FastAPI application with a
combined lifespan, enabling the entire agent platform to run as one process.

Route layout:
  /ahs/*            — Agent Harness Service (router already carries /ahs prefix)
  /mcp/*            — Unified MCP server (FastMCP, agents + developers, single mount)
  /gw/<name>/*      — Gateway plugins (e.g. /gw/slack/*, /gw/github/*)
  /health           — Liveness probe — always 200 OK

Startup order:
  1. AHS (registers orchestration callbacks, warms process pool, etc.)
  2. Unified MCP lifespan (via AHSState._mcp_lifespan_ctx)
  3. register_unified_tools() — imports tools from both FastMCP instances
  4. Yuppster batch-system init (mcp_startup)
  5. Enabled gateway plugins in registration order (see ``discover_plugins``)

Shutdown is in strict reverse order so in-flight AHS tasks can still use
MCP tools while the scheduler drains.

Auth at /mcp:
  - ``x-ahs-token`` header (or ``Bearer <secret>:<session_id>``) -> agent
  - ``Bearer yupp_dev_*`` -> developer (validated against yuppdb; 503 when
    yuppdb is not configured, e.g. one-box mode)
  - Any other request -> 401 Unauthorized

All standalone server entrypoints (AHS, SAG, MCP) remain functional and
unchanged -- the monolith is an *additive* composition, not a replacement.

Gateway plugin model (Section 4.4 of the design doc):
  Gateways implement :class:`~ypl.mono_server.gateway_plugin.GatewayPlugin`
  and are registered in :func:`discover_plugins`.  The monolith calls
  ``plugin.startup()`` / ``plugin.shutdown()`` in its combined lifespan, and
  ``plugin.get_router()`` to mount routes at ``/gw/<name>/``.
"""

from __future__ import annotations
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, Response

# Import mcp_tools to trigger @mcp_server.tool() decorator registration for all
# agcouch MCP tools.  This is a side-effect-only import -- without it the
# agcouch FastMCP instance has an empty tool registry.
import ypl.mcp_server.mcp_tools  # noqa: F401
from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.host_path_guard import HostPathGuardMiddleware
from ypl.agent_harness_service.lifespan import AHSState, ahs_shutdown, ahs_startup
from ypl.agent_harness_service.middleware import AHSRequestLoggingMiddleware
from ypl.agent_harness_service.projects.project_routes import project_router
from ypl.agent_harness_service.routes import router as ahs_router
from ypl.backend.routes.v1.yuppaste import router as yuppaste_router
from ypl.mcp_server.lifespan import mcp_shutdown, mcp_startup
from ypl.mono_server.config import MonoConfig
from ypl.mono_server.gateway_plugin import GatewayPlugin
from ypl.mono_server.plugins.github import GitHubGatewayPlugin
from ypl.mono_server.plugins.slack import SlackGatewayPlugin
from ypl.mono_server.unified_mcp import register_unified_tools, unified_mcp_http_app
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Gateway plugin registry
# ---------------------------------------------------------------------------

# Module-level registry -- the same instances are returned by every
# discover_plugins() call, ensuring lifecycle hooks (startup/shutdown) and
# routing (get_router) always operate on the same object.  This prevents a
# future plugin that keeps instance-local state from silently diverging
# between routing and lifecycle management.
_PLUGIN_REGISTRY: list[GatewayPlugin] = [
    SlackGatewayPlugin(),
    GitHubGatewayPlugin(),
]


def discover_plugins(config: MonoConfig) -> list[GatewayPlugin]:
    """Return the list of gateway plugins that are enabled in *config*.

    Each plugin's :attr:`~ypl.mono_server.gateway_plugin.GatewayPlugin.env_flag`
    maps to a ``MonoConfig`` field by converting it to lower-case
    (e.g. ``GATEWAY_SLACK_ENABLED`` -> ``gateway_slack_enabled``).  A missing
    field raises :exc:`ValueError` rather than silently disabling the plugin --
    a typo in ``env_flag`` would otherwise make the plugin vanish with no
    diagnostic.

    To add a new gateway, create a module under ``ypl/mono_server/plugins/``,
    implement the :class:`~ypl.mono_server.gateway_plugin.GatewayPlugin`
    protocol, and append an instance to :data:`_PLUGIN_REGISTRY` above.
    """
    enabled: list[GatewayPlugin] = []
    for plugin in _PLUGIN_REGISTRY:
        flag_attr = plugin.env_flag.lower()
        if not hasattr(config, flag_attr):
            raise ValueError(
                f"{plugin.name!r} plugin env_flag {plugin.env_flag!r} has no "
                f"matching MonoConfig field -- check for typos"
            )
        if getattr(config, flag_attr):
            enabled.append(plugin)
    return enabled


# ---------------------------------------------------------------------------
# Router setup guard
# ---------------------------------------------------------------------------

# Ensure the AHS router's sub-routers are added exactly once per process.
# The standalone AHS server (server.py) also calls router.include_router()
# inside its own create_app(); this flag prevents double-inclusion if both
# modules are imported in the same process (e.g. during tests).
_ahs_router_setup_done = False


def _setup_ahs_router(config: MonoConfig) -> None:
    """Add sub-routers to the AHS router.  Idempotent -- safe to call multiple times."""
    global _ahs_router_setup_done
    if _ahs_router_setup_done:
        return
    ahs_router.include_router(project_router)
    ahs_router.include_router(
        yuppaste_router,
        tags=["yuppaste"],
        dependencies=[Depends(verify_api_key)],
    )
    _ahs_router_setup_done = True


# ---------------------------------------------------------------------------
# Combined lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def combined_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Orchestrate startup and shutdown of all services in the monolith.

    Startup order:
      1. AHS (wires orchestration callbacks, starts warm process pool,
         launches scheduler, creates unified MCP lifespan context)
      2. Enter unified MCP lifespan (via AHSState._mcp_lifespan_ctx)
      3. register_unified_tools() -- copy all tools from harness + agcouch
         FastMCP instances into the unified instance
      4. Yuppster batch-system init (mcp_startup)
      5. Enabled gateway plugins in registration order (see discover_plugins)

    Shutdown is the mirror image of startup.  AHS shutdown runs *inside* the
    unified MCP lifespan context so that in-flight scheduler tasks can still
    call MCP tools while the drain completes.
    """
    config = MonoConfig()

    # --- 1. AHS startup -------------------------------------------------------
    # ahs_startup() wires orchestration callbacks, starts the warm process pool,
    # launches the scheduler, and returns a state object that carries the unified
    # MCP lifespan context manager (not yet entered).
    ahs_state: AHSState = await ahs_startup(app, unified_mcp_http_app)

    # --- 2. Unified MCP lifespan (FastMCP session manager) --------------------
    # Enter via the unentered context manager stored in ahs_state so real
    # exception info is forwarded to __aexit__ on a crash (same pattern as the
    # standalone AHS server).
    async with ahs_state._mcp_lifespan_ctx:
        try:
            # --- 3. Tool registration ------------------------------------------
            # Both harness and agcouch tool registrations have already fired at
            # module load time via side-effect imports (local_mcp_server import +
            # mcp_tools import at the top of this file).  register_unified_tools()
            # copies the fully-populated tool registries into unified_mcp.
            await register_unified_tools()

            # --- 4. Yuppster batch-system init --------------------------------
            await mcp_startup()
            try:
                # --- 5. Gateway plugins ---------------------------------------
                # Start each enabled plugin in registration order; record
                # (plugin, state) pairs so shutdown runs in reverse order.
                # Startup is wrapped so a failure in plugin N cleanly tears down
                # plugins 0..N-1 before re-raising -- no leaked subsystems.
                plugins = discover_plugins(config)
                gateway_states: list[tuple[GatewayPlugin, Any]] = []
                try:
                    for plugin in plugins:
                        state = await plugin.startup()
                        gateway_states.append((plugin, state))
                except Exception:
                    # Clean up already-started plugins before propagating.
                    for p, s in reversed(gateway_states):
                        try:
                            await p.shutdown(s)
                        except Exception:
                            logger.exception("Plugin %s shutdown failed during startup cleanup", p.name)
                    raise

                try:
                    yield
                finally:
                    # Shutdown plugins in strict reverse startup order.
                    # Each call is individually exception-isolated so a failure
                    # in one plugin does not prevent others from being torn down.
                    for plugin, state in reversed(gateway_states):
                        try:
                            await plugin.shutdown(state)
                        except Exception:
                            logger.exception("Plugin %s shutdown failed", plugin.name)
            finally:
                # Yuppster MCP teardown (batch-system flush, Sentry close, GCP
                # log flush).  The finally block ensures mcp_shutdown runs even
                # if a plugin startup raises or the yield block raises.
                await mcp_shutdown()
        finally:
            # AHS teardown -- inside unified MCP lifespan so in-flight tasks
            # that call MCP tools during scheduler drain can still complete.
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

    # CORS -- same allowed origins as the standalone AHS server
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

    # Host-based path allowlist — restricts scoped subdomains (e.g.
    # mcp.agcouch.com) to specific path prefixes. Reads from HOST_PATH_GUARD
    # env var. Registered BEFORE AHSRequestLoggingMiddleware so requests
    # rejected by the guard don't pollute AHS access logs.
    # Starlette runs middleware in reverse registration order (LIFO), so this
    # still fires *first* at request time.
    if config.host_path_guard:
        application.add_middleware(
            HostPathGuardMiddleware,
            host_allowlist=config.host_path_guard,
        )

    application.add_middleware(AHSRequestLoggingMiddleware)

    # --- AHS routes (/ahs/* prefix already on router) ------------------------
    _setup_ahs_router(config)
    application.include_router(ahs_router)

    # --- Unified MCP (/mcp -- agents and developers share one endpoint) ------
    # Auth is handled by UnifiedMcpAuthMiddleware (already attached to the app):
    #   - x-ahs-token header -> agent context (process-local secret, no DB)
    #   - Bearer yupp_dev_* -> developer context (validated against yuppdb)
    # Mount /mcp/harness BEFORE /mcp — Starlette matches mounts by prefix,
    # so /mcp would intercept /mcp/harness/ requests and pass "/harness/" as
    # the path to the MCP app (which returns 404). The longer prefix must come first.
    application.mount("/mcp/harness", unified_mcp_http_app)
    application.mount("/mcp", unified_mcp_http_app)

    # --- Gateway plugins (all enabled plugins, each at /gw/<name>/) ----------
    # Routers are registered here; lifespan (startup/shutdown) is handled by
    # combined_lifespan via discover_plugins().  Plugins without a router
    # (get_router() returns None) still participate in the lifespan.
    # GitHub gateway is wired in via GitHubGatewayPlugin (Task [7]).
    for plugin in discover_plugins(config):
        if router := plugin.get_router():
            application.include_router(router, prefix=f"/gw/{plugin.name}")
            # Also mount at legacy prefix for backward compatibility with
            # AHS gateway callbacks that use /slack-agent-gateway/ paths.
            if plugin.name == "slack":
                application.include_router(router, prefix="/slack-agent-gateway")

    # --- Health check (always registered, no auth) ---------------------------
    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness probe -- always returns 200 OK."""
        return {"status": "ok"}

    # --- Prometheus metrics stub (no auth) -----------------------------------
    # Returns a minimal valid Prometheus text exposition payload so that a
    # Prometheus scraper can already be pointed at this endpoint.
    # Replace the body with ``prometheus_client.generate_latest()`` once metric
    # instrumentation is wired in (see ``docs/observability.md``).
    @application.get("/metrics", tags=["observability"])
    async def metrics() -> Response:
        """Prometheus metrics stub -- returns a valid (minimal) text payload.

        Upgrade path: install ``prometheus-client``, register collectors, and
        swap the hardcoded string for ``prometheus_client.generate_latest()``.
        The content-type header already matches what Prometheus expects, so no
        scrape-config changes are needed once real metrics are wired in.
        """
        content = "# Prometheus metrics stub -- no instrumentation yet\n"
        return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")

    return application


# Module-level app instance used by uvicorn:
#   uvicorn ypl.mono_server.server:app --port 8090
app = create_app()
