"""Tests for the GatewayPlugin protocol and built-in plugin implementations.

Coverage areas:
  1. Protocol definition — GatewayPlugin is importable and @runtime_checkable.
  2. SlackGatewayPlugin — satisfies the protocol; delegates to SAG helpers.
  3. discover_plugins   — respects MonoConfig enable/disable flags.
  4. Route registration — /gw/slack/* present/absent based on flag.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter

# ---------------------------------------------------------------------------
# 1. Protocol
# ---------------------------------------------------------------------------


class TestGatewayPluginProtocol:
    """GatewayPlugin is a well-formed, runtime-checkable Protocol."""

    def test_protocol_importable(self) -> None:
        from ypl.mono_server.gateway_plugin import GatewayPlugin

        assert GatewayPlugin is not None

    def test_slack_plugin_satisfies_protocol(self) -> None:
        """isinstance(SlackGatewayPlugin(), GatewayPlugin) is True."""
        from ypl.mono_server.gateway_plugin import GatewayPlugin
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        assert isinstance(SlackGatewayPlugin(), GatewayPlugin)

    def test_bare_object_does_not_satisfy_protocol(self) -> None:
        """A plain object missing the required attrs is not a GatewayPlugin."""
        from ypl.mono_server.gateway_plugin import GatewayPlugin

        assert not isinstance(object(), GatewayPlugin)

    def test_minimal_conforming_object_satisfies_protocol(self) -> None:
        """Any object with name, env_flag, get_router, startup, shutdown satisfies."""
        from ypl.mono_server.gateway_plugin import GatewayPlugin

        class _MinimalPlugin:
            name = "test"
            env_flag = "TEST_ENABLED"

            def get_router(self) -> APIRouter | None:
                return None

            async def startup(self) -> None:
                return None

            async def shutdown(self, state: object) -> None:
                pass

        assert isinstance(_MinimalPlugin(), GatewayPlugin)


# ---------------------------------------------------------------------------
# 2. SlackGatewayPlugin
# ---------------------------------------------------------------------------


class TestSlackGatewayPlugin:
    """SlackGatewayPlugin wraps SAG correctly."""

    def test_name_is_slack(self) -> None:
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        assert SlackGatewayPlugin().name == "slack"

    def test_env_flag(self) -> None:
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        assert SlackGatewayPlugin().env_flag == "GATEWAY_SLACK_ENABLED"

    def test_get_router_returns_api_router(self) -> None:
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        plugin = SlackGatewayPlugin()
        router = plugin.get_router()
        assert isinstance(router, APIRouter)

    def test_get_router_is_sag_router(self) -> None:
        """The returned router is the exact same SAG router object."""
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin
        from ypl.slack_agent_gateway.routes import router as sag_router

        assert SlackGatewayPlugin().get_router() is sag_router

    def test_sag_router_has_slack_events_route(self) -> None:
        """The SAG router exposes /slack/events (mounted at /gw/slack/slack/events)."""
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        router = SlackGatewayPlugin().get_router()
        assert router is not None
        paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
        assert "/slack/events" in paths, f"Missing /slack/events. Found: {sorted(paths)}"

    async def test_startup_delegates_to_sag_startup(self) -> None:
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        mock_state = MagicMock()
        with patch("ypl.mono_server.plugins.slack.sag_startup", new_callable=AsyncMock, return_value=mock_state):
            state = await SlackGatewayPlugin().startup()
        assert state is mock_state

    async def test_shutdown_delegates_to_sag_shutdown(self) -> None:
        from ypl.mono_server.plugins.slack import SlackGatewayPlugin

        mock_state = MagicMock()
        with patch("ypl.mono_server.plugins.slack.sag_shutdown", new_callable=AsyncMock) as mock_shutdown:
            await SlackGatewayPlugin().shutdown(mock_state)
            mock_shutdown.assert_awaited_once_with(mock_state)


# ---------------------------------------------------------------------------
# 3. discover_plugins
# ---------------------------------------------------------------------------


class TestDiscoverPlugins:
    """discover_plugins() respects MonoConfig enable/disable flags."""

    def test_slack_included_when_enabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=True, gateway_github_enabled=False)
        plugins = discover_plugins(config)
        assert any(p.name == "slack" for p in plugins)

    def test_slack_excluded_when_disabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=False, gateway_github_enabled=False)
        plugins = discover_plugins(config)
        assert not any(p.name == "slack" for p in plugins)

    def test_empty_when_all_disabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=False, gateway_github_enabled=False)
        assert discover_plugins(config) == []

    def test_all_returned_plugins_satisfy_protocol(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.gateway_plugin import GatewayPlugin
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=True)
        for plugin in discover_plugins(config):
            assert isinstance(plugin, GatewayPlugin), f"{plugin!r} does not satisfy GatewayPlugin"


# ---------------------------------------------------------------------------
# 4. Route registration
# ---------------------------------------------------------------------------


class TestGatewayRouteRegistration:
    """/gw/slack/* routes present iff GATEWAY_SLACK_ENABLED=true."""

    def _route_paths(self, app: object) -> set[str]:
        from fastapi import FastAPI

        assert isinstance(app, FastAPI)
        return {getattr(r, "path", "") for r in app.routes}

    def test_gw_slack_routes_present_when_enabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=True)
        plugins = discover_plugins(config)
        slack_plugin = next((p for p in plugins if p.name == "slack"), None)
        assert slack_plugin is not None, "SlackGatewayPlugin not in discover_plugins output"

        router = slack_plugin.get_router()
        assert router is not None
        paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
        assert "/slack/events" in paths

    def test_gw_slack_routes_absent_when_disabled(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        config = MonoConfig(gateway_slack_enabled=False)
        plugins = discover_plugins(config)
        assert not any(p.name == "slack" for p in plugins)

    def test_create_app_mounts_gw_slack_prefix(self) -> None:
        """create_app() includes /gw/slack/* routes on the top-level app."""
        from ypl.mono_server.server import app

        paths = self._route_paths(app)
        slack_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert slack_paths, f"No /gw/slack routes found. All routes: {sorted(paths)}"
