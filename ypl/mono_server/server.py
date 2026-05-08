"""Monolith entrypoint for the Yupp Agent Platform.

Composes AHS, SAG, and MCP into a single FastAPI application with a
combined lifespan, enabling the entire agent platform to run as one process.

Two master flags in :class:`~ypl.mono_server.config.MonoConfig` gate the
optional surfaces of the monolith:

  ``AHS_MONO_ENABLE_GATEWAY_SERVICE`` (default ``false``) — mount any
      ``/gw/<name>/`` gateway plugin routers and run their startup/shutdown
      hooks.  When ``false`` no plugin lifespan hooks fire and the
      per-plugin sub-flags (``GATEWAY_SLACK_ENABLED``,
      ``GATEWAY_GITHUB_ENABLED``) are ignored.
  ``AHS_MONO_ENABLE_MCP`` (default ``false``) — mount the agcouch MCP at
      ``/mcp/agcouch`` and run the agcouch FastMCP session manager + the
      yuppster ``mcp_startup``/``mcp_shutdown`` pair.

The harness MCP at ``/mcp/harness`` is **always** mounted regardless of the
flags — AHS agent sessions always have a tool surface (shared and
external-data tools dual-register on harness MCP via
``ypl.mcp_common.shared_tool``, see PR #300).

Route layout (everything turned on):
  /ahs/*            — Agent Harness Service (router already carries /ahs prefix)
  /mcp/harness      — Harness MCP (agents; x-ahs-token or Bearer <secret>:<session>)
  /mcp/agcouch      — Agcouch MCP (developers; Bearer yupp_dev_*) — gated on
                      AHS_MONO_ENABLE_MCP
  /gw/<name>/*      — Gateway plugins (e.g. /gw/slack/*, /gw/github/*) —
                      gated on AHS_MONO_ENABLE_GATEWAY_SERVICE
  /health           — Liveness probe — always 200 OK

Startup order (when both master flags are on):
  1. AHS (registers orchestration callbacks, warms process pool, etc.)
  2. Harness MCP lifespan (via AHSState._mcp_lifespan_ctx)
  3. Agcouch MCP lifespan
  4. Yuppster batch-system init (mcp_startup)
  5. Enabled gateway plugins in registration order (see ``discover_plugins``)

Shutdown is in strict reverse order so in-flight AHS tasks can still use
MCP tools while the scheduler drains.

When ``AHS_MONO_ENABLE_MCP=false`` steps 3 and 4 are skipped (the inner block
runs directly inside the harness MCP lifespan).  When
``AHS_MONO_ENABLE_GATEWAY_SERVICE=false`` step 5 is skipped (the gateway
plugin loop is not entered at all).

The two MCP mounts expose *disjoint* tool sets — agent tools are reachable
only via ``/mcp/harness`` and developer tools only via ``/mcp/agcouch``.
Path is the enforcement boundary; there is no shared tool registry.

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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, Response

# Import mcp_tools to trigger @mcp_server.tool() decorator registration for all
# agcouch MCP tools.  This is a side-effect-only import -- without it the
# agcouch FastMCP instance has an empty tool registry.
import ypl.mcp_server.mcp_tools  # noqa: F401
from ypl.agent_harness_service.artifact_routes import artifact_router
from ypl.agent_harness_service.host_path_guard import HostPathGuardMiddleware
from ypl.agent_harness_service.lifespan import AHSState, ahs_shutdown, ahs_startup
from ypl.agent_harness_service.middleware import AHSRequestLoggingMiddleware
from ypl.agent_harness_service.projects.project_routes import project_router
from ypl.agent_harness_service.routes import router as ahs_router
from ypl.mcp_server.lifespan import mcp_shutdown, mcp_startup
from ypl.mono_server.config import MonoConfig
from ypl.mono_server.gateway_plugin import GatewayPlugin
from ypl.mono_server.plugins.github import GitHubGatewayPlugin
from ypl.mono_server.plugins.slack import SlackGatewayPlugin
from ypl.mono_server.runtime import set_monolith_mode
from ypl.mono_server.unified_mcp import agcouch_mcp_http_app, harness_mcp_http_app
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

    Honours the master ``ahs_mono_enable_gateway_service`` flag: if it is
    ``False`` this function returns an empty list **unconditionally**, no
    matter what the per-plugin sub-flags say.  This keeps the whole gateway
    surface (lifespan + routes) behind a single deployment switch.

    When the master flag is ``True``, each plugin's
    :attr:`~ypl.mono_server.gateway_plugin.GatewayPlugin.env_flag` maps to a
    ``MonoConfig`` field by converting it to lower-case (e.g.
    ``GATEWAY_SLACK_ENABLED`` -> ``gateway_slack_enabled``).  A missing
    field raises :exc:`ValueError` rather than silently disabling the plugin --
    a typo in ``env_flag`` would otherwise make the plugin vanish with no
    diagnostic.

    To add a new gateway, create a module under ``ypl/mono_server/plugins/``,
    implement the :class:`~ypl.mono_server.gateway_plugin.GatewayPlugin`
    protocol, and append an instance to :data:`_PLUGIN_REGISTRY` above.
    """
    if not config.ahs_mono_enable_gateway_service:
        return []
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
    ahs_router.include_router(artifact_router)
    _ahs_router_setup_done = True


# ---------------------------------------------------------------------------
# Combined lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _maybe_agcouch_lifespan(enabled: bool) -> AsyncGenerator[None, None]:
    """Enter the agcouch MCP lifespan when ``enabled``; otherwise no-op.

    Encapsulates the master ``AHS_MONO_ENABLE_MCP`` gate so the body of
    :func:`combined_lifespan` reads as a single linear flow regardless of
    flag state.
    """
    if enabled:
        async with agcouch_mcp_http_app.lifespan(agcouch_mcp_http_app):
            yield
    else:
        yield


@asynccontextmanager
async def combined_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Orchestrate startup and shutdown of all services in the monolith.

    Both master flags from :class:`~ypl.mono_server.config.MonoConfig` are
    honoured here:

    * ``ahs_mono_enable_mcp=False``  →  skip the agcouch MCP lifespan ctx and
      skip ``mcp_startup`` / ``mcp_shutdown``.  The inner block (gateway
      plugins + yield + AHS shutdown) runs directly inside the harness MCP
      lifespan.
    * ``ahs_mono_enable_gateway_service=False``  →  skip the gateway plugin
      startup/shutdown loop entirely (``discover_plugins`` already returns an
      empty list in that case, but we don't even iterate it for clarity).

    Startup order when both flags are on:
      1. AHS (wires orchestration callbacks, starts warm process pool,
         launches scheduler, creates harness MCP lifespan context)
      2. Enter harness MCP lifespan (via AHSState._mcp_lifespan_ctx)
      3. Enter agcouch MCP lifespan (its own FastMCP session manager)
      4. Yuppster batch-system init (mcp_startup)
      5. Enabled gateway plugins in registration order (see discover_plugins)

    Shutdown is the mirror image of startup.  AHS shutdown runs *inside* the
    harness MCP lifespan so in-flight scheduler tasks can still call MCP
    tools while the drain completes.
    """
    config = MonoConfig()

    # Mark the process as monolith *before* AHS startup so that
    # _recover_stale_sessions (which fires from inside ahs_startup) can use the
    # in-process Slack courtesy path. The HTTP listener isn't open yet, so an
    # HTTP loopback would silently 4xx/timeout.  Reset on exit so repeated
    # lifespan entries (e.g. in tests) don't leak the flag across runs.
    set_monolith_mode(True)
    try:
        # --- 1. AHS startup ---------------------------------------------------
        # ahs_startup() wires orchestration callbacks, starts the warm process pool,
        # launches the scheduler, and returns a state object that carries the harness
        # MCP lifespan context manager (not yet entered).
        ahs_state: AHSState = await ahs_startup(app, harness_mcp_http_app)

        # --- 2. Harness MCP lifespan (FastMCP session manager) ---------------
        # Enter via the unentered context manager stored in ahs_state so real
        # exception info is forwarded to __aexit__ on a crash (same pattern as the
        # standalone AHS server).  The harness MCP is ALWAYS up — agents always
        # need a tool surface, regardless of the master flags.
        async with ahs_state._mcp_lifespan_ctx:
            try:
                # --- 3. Agcouch MCP lifespan (gated on ahs_mono_enable_mcp) -
                # Separate FastMCP instance with its own session manager.
                # When the flag is OFF this is a noop async with — the inner
                # block runs directly inside the harness MCP lifespan only.
                async with _maybe_agcouch_lifespan(config.ahs_mono_enable_mcp):
                    # --- 4. Yuppster batch-system init ---------------------
                    # Tied to the agcouch MCP — when that mount is off, we
                    # have no batch system to initialise.
                    if config.ahs_mono_enable_mcp:
                        await mcp_startup()
                    try:
                        # --- 5. Gateway plugins (gated on master flag) -----
                        # discover_plugins() already returns [] when the
                        # master flag is off, but we also short-circuit here
                        # so the startup-failure cleanup path never runs in
                        # the disabled case.
                        gateway_states: list[tuple[GatewayPlugin, Any]] = []
                        if config.ahs_mono_enable_gateway_service:
                            plugins = discover_plugins(config)
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
                            # Each call is individually exception-isolated so a
                            # failure in one plugin does not prevent others
                            # from being torn down.  When the master flag is
                            # off ``gateway_states`` is empty and this loop
                            # is a noop.
                            for plugin, state in reversed(gateway_states):
                                try:
                                    await plugin.shutdown(state)
                                except Exception:
                                    logger.exception("Plugin %s shutdown failed", plugin.name)
                    finally:
                        # Yuppster MCP teardown (batch-system flush, Sentry close,
                        # GCP log flush). The finally block ensures mcp_shutdown
                        # runs even if a plugin startup raises or the yield
                        # block raises.  Skipped when the agcouch mount is off.
                        if config.ahs_mono_enable_mcp:
                            await mcp_shutdown()
            finally:
                # AHS teardown -- inside the harness MCP lifespan so in-flight
                # tasks that call MCP tools during scheduler drain can still
                # complete.  Stays in monolith mode so send_slack_shutdown_courtesy
                # uses the in-process Slack callback (the HTTP listener has
                # already closed by the time this finally runs).
                await ahs_shutdown(ahs_state)
    finally:
        set_monolith_mode(False)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and configure the monolith FastAPI application.

    Returns a fully-wired FastAPI app suitable for ``uvicorn
    ypl.mono_server.server:app``.  The standalone AHS, SAG, and MCP server
    entrypoints remain unaffected.

    Two master flags from :class:`~ypl.mono_server.config.MonoConfig` gate
    the optional mounts:

    * ``ahs_mono_enable_mcp=False``  →  the ``/mcp/agcouch`` mount is skipped.
      ``/mcp/harness`` is always mounted.
    * ``ahs_mono_enable_gateway_service=False``  →  no ``/gw/<name>/`` routers
      are included; ``discover_plugins()`` returns an empty list.
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

    # --- Split MCP mounts (disjoint tool sets; path is the enforcement) ------
    # /mcp/harness: agent tools. Auth via HarnessMcpAuthMiddleware
    #   (x-ahs-token or Bearer <secret>:<session_id>). Dev tokens rejected.
    #   ALWAYS mounted — AHS sessions need a tool surface.
    # /mcp/agcouch: developer tools. Auth via AgcouchMcpAuthMiddleware
    #   (Bearer yupp_dev_*). Agent tokens rejected.  Gated on
    #   ``ahs_mono_enable_mcp`` — off by default for pure-AHS deployments.
    # There is intentionally no catch-all /mcp mount — each tool set is
    # reachable only at its own path.
    application.mount("/mcp/harness", harness_mcp_http_app)
    if config.ahs_mono_enable_mcp:
        application.mount("/mcp/agcouch", agcouch_mcp_http_app)

    # --- Gateway plugins (all enabled plugins, each at /gw/<name>/) ----------
    # Routers are registered here; lifespan (startup/shutdown) is handled by
    # combined_lifespan via discover_plugins().  Plugins without a router
    # (get_router() returns None) still participate in the lifespan.
    # GitHub gateway is wired in via GitHubGatewayPlugin (Task [7]).
    # The whole loop is gated on ``ahs_mono_enable_gateway_service``;
    # discover_plugins() returns [] when that master flag is off, so
    # iterating it would be a noop, but we skip the call entirely so
    # disabled-state behaviour is obvious from the code path.
    if config.ahs_mono_enable_gateway_service:
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
