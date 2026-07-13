"""Tests for the two master deployment flags on ``MonoConfig``:

  AHS_MONO_ENABLE_GATEWAY_SERVICE  master flag for /gw/<name>/ plugin mounts
  AHS_MONO_ENABLE_MCP              master flag for /mcp/platform mount + lifespan

Both default to ``False`` in production — the monolith boots as a pure AHS
process. These tests exercise the four flag combinations and verify that:

  * The harness MCP at ``/mcp/harness`` is mounted in EVERY configuration.
  * AHS routes (``/ahs/*``) are present in EVERY configuration.
  * ``/health`` is reachable in EVERY configuration.
  * ``/mcp/platform`` is mounted IFF ``AHS_MONO_ENABLE_MCP=true``.
  * ``/gw/<name>/`` routers are mounted IFF
    ``AHS_MONO_ENABLE_GATEWAY_SERVICE=true`` AND the per-plugin sub-flag is on.
  * ``mcp_startup`` / ``mcp_shutdown`` only run when ``ahs_mono_enable_mcp`` is on.
  * Plugin ``startup`` / ``shutdown`` hooks only run when
    ``ahs_mono_enable_gateway_service`` is on.

The mono-server ``conftest.py`` sets both master flags ON at import time so
the rest of the test suite sees the legacy "everything mounted" shape.
These tests explicitly override one or both flags via ``patch.dict`` and
build a fresh app with ``create_app()`` — they do NOT use the module-level
``app`` because that's locked in at import time.
"""

from __future__ import annotations
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_paths(application: FastAPI) -> set[str]:
    return {getattr(r, "path", "") for r in application.routes}


def _mount_paths(application: FastAPI) -> set[str]:
    return {r.path for r in application.routes if isinstance(r, Mount)}


def _build_app(*, gateway_service: bool, mcp: bool) -> FastAPI:
    """Build a fresh app with the two master flags set explicitly.

    Always passes ``GATEWAY_SLACK_ENABLED=true`` so the master gateway flag
    has at least one default-on sub-plugin to discover when on. GitHub stays
    off (its per-plugin default).
    """
    env = {
        "AHS_MONO_ENABLE_GATEWAY_SERVICE": "true" if gateway_service else "false",
        "AHS_MONO_ENABLE_MCP": "true" if mcp else "false",
        "GATEWAY_SLACK_ENABLED": "true",
        "GATEWAY_GITHUB_ENABLED": "false",
    }
    with patch.dict(os.environ, env):
        # Re-import inside the patch so MonoConfig() reads the desired env.
        from ypl.mono_server import server as _srv

        return _srv.create_app()


# ---------------------------------------------------------------------------
# 1. MonoConfig defaults — both master flags off
# ---------------------------------------------------------------------------


class TestMonoConfigMasterFlagDefaults:
    """The two master flags default to ``False``."""

    def test_gateway_service_default_false(self) -> None:
        """``AHS_MONO_ENABLE_GATEWAY_SERVICE`` defaults to False.

        The mono-server conftest sets this to ``true`` for the legacy test
        baseline, so we patch it back out here to verify the production
        default.
        """
        from ypl.mono_server.config import MonoConfig

        with patch.dict(os.environ, {"AHS_MONO_ENABLE_GATEWAY_SERVICE": ""}, clear=False):
            # Empty string -> pydantic falls back to the field default
            os.environ.pop("AHS_MONO_ENABLE_GATEWAY_SERVICE", None)
            cfg = MonoConfig()
        assert cfg.ahs_mono_enable_gateway_service is False

    def test_mcp_default_false(self) -> None:
        """``AHS_MONO_ENABLE_MCP`` defaults to False."""
        from ypl.mono_server.config import MonoConfig

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AHS_MONO_ENABLE_MCP", None)
            cfg = MonoConfig()
        assert cfg.ahs_mono_enable_mcp is False

    def test_gateway_service_enabled_via_env(self) -> None:
        with patch.dict(os.environ, {"AHS_MONO_ENABLE_GATEWAY_SERVICE": "true"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.ahs_mono_enable_gateway_service is True

    def test_mcp_enabled_via_env(self) -> None:
        with patch.dict(os.environ, {"AHS_MONO_ENABLE_MCP": "true"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.ahs_mono_enable_mcp is True

    def test_gateway_service_numeric_true(self) -> None:
        with patch.dict(os.environ, {"AHS_MONO_ENABLE_GATEWAY_SERVICE": "1"}):
            from ypl.mono_server.config import MonoConfig

            assert MonoConfig().ahs_mono_enable_gateway_service is True

    def test_mcp_numeric_true(self) -> None:
        with patch.dict(os.environ, {"AHS_MONO_ENABLE_MCP": "1"}):
            from ypl.mono_server.config import MonoConfig

            assert MonoConfig().ahs_mono_enable_mcp is True


# ---------------------------------------------------------------------------
# 2. App composition for all four flag combinations
# ---------------------------------------------------------------------------


class TestPureAhsDefaults:
    """Both master flags off → only AHS routes, harness MCP, /health."""

    def test_harness_mcp_always_mounted(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        mounts = _mount_paths(app)
        assert "/mcp/harness" in mounts, f"/mcp/harness must always be mounted. Mounts: {mounts}"

    def test_platform_mcp_not_mounted(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        mounts = _mount_paths(app)
        assert "/mcp/platform" not in mounts, f"/mcp/platform must not be mounted. Mounts: {mounts}"

    def test_no_gateway_routes(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        paths = _route_paths(app)
        gw_paths = [p for p in paths if p.startswith(("/gw/", "/slack-agent-gateway"))]
        assert not gw_paths, f"No /gw/* routes should be mounted. Found: {gw_paths}"

    def test_ahs_routes_present(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        paths = _route_paths(app)
        ahs_paths = [p for p in paths if p.startswith("/ahs")]
        assert ahs_paths, f"AHS routes must be mounted. All paths: {sorted(paths)}"

    def test_health_route_present(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        assert "/health" in _route_paths(app)

    def test_health_endpoint_returns_200(self) -> None:
        """The /health probe responds 200 on the pure-AHS build."""
        app = _build_app(gateway_service=False, mcp=False)
        client = TestClient(app, raise_server_exceptions=True)
        assert client.get("/health").status_code == 200


class TestBothFlagsOn:
    """Both master flags on → all mounts present (legacy "everything on" shape)."""

    def test_harness_mcp_mounted(self) -> None:
        app = _build_app(gateway_service=True, mcp=True)
        assert "/mcp/harness" in _mount_paths(app)

    def test_platform_mcp_mounted(self) -> None:
        app = _build_app(gateway_service=True, mcp=True)
        assert "/mcp/platform" in _mount_paths(app)

    def test_slack_gateway_routes_present(self) -> None:
        app = _build_app(gateway_service=True, mcp=True)
        paths = _route_paths(app)
        slack_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert slack_paths, f"Expected /gw/slack routes. All paths: {sorted(paths)}"

    def test_ahs_routes_present(self) -> None:
        app = _build_app(gateway_service=True, mcp=True)
        paths = _route_paths(app)
        assert any(p.startswith("/ahs") for p in paths)

    def test_health_route_present(self) -> None:
        app = _build_app(gateway_service=True, mcp=True)
        assert "/health" in _route_paths(app)


class TestGatewayOnlyMcpOff:
    """``AHS_MONO_ENABLE_GATEWAY_SERVICE=true`` only — gateway routes but no /mcp/platform."""

    def test_harness_mcp_mounted(self) -> None:
        app = _build_app(gateway_service=True, mcp=False)
        assert "/mcp/harness" in _mount_paths(app)

    def test_platform_mcp_not_mounted(self) -> None:
        app = _build_app(gateway_service=True, mcp=False)
        assert "/mcp/platform" not in _mount_paths(app)

    def test_slack_gateway_routes_present(self) -> None:
        app = _build_app(gateway_service=True, mcp=False)
        paths = _route_paths(app)
        slack_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert slack_paths, f"Expected /gw/slack routes. All paths: {sorted(paths)}"

    def test_ahs_routes_present(self) -> None:
        app = _build_app(gateway_service=True, mcp=False)
        assert any(p.startswith("/ahs") for p in _route_paths(app))


class TestMcpOnlyGatewayOff:
    """``AHS_MONO_ENABLE_MCP=true`` only — /mcp/platform mounted but no /gw/* routes."""

    def test_harness_mcp_mounted(self) -> None:
        app = _build_app(gateway_service=False, mcp=True)
        assert "/mcp/harness" in _mount_paths(app)

    def test_platform_mcp_mounted(self) -> None:
        app = _build_app(gateway_service=False, mcp=True)
        assert "/mcp/platform" in _mount_paths(app)

    def test_no_gateway_routes(self) -> None:
        app = _build_app(gateway_service=False, mcp=True)
        paths = _route_paths(app)
        gw_paths = [p for p in paths if p.startswith(("/gw/", "/slack-agent-gateway"))]
        assert not gw_paths, f"No /gw/* routes should be mounted. Found: {gw_paths}"

    def test_ahs_routes_present(self) -> None:
        app = _build_app(gateway_service=False, mcp=True)
        assert any(p.startswith("/ahs") for p in _route_paths(app))


# ---------------------------------------------------------------------------
# 3. Invariants across every flag combination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gateway_service", [False, True])
@pytest.mark.parametrize("mcp", [False, True])
class TestAlwaysPresent:
    """Routes / mounts that MUST be present regardless of master flags."""

    def test_harness_mcp_always_mounted(self, gateway_service: bool, mcp: bool) -> None:
        app = _build_app(gateway_service=gateway_service, mcp=mcp)
        assert "/mcp/harness" in _mount_paths(app)

    def test_ahs_routes_always_present(self, gateway_service: bool, mcp: bool) -> None:
        app = _build_app(gateway_service=gateway_service, mcp=mcp)
        assert any(p.startswith("/ahs") for p in _route_paths(app))

    def test_health_route_always_present(self, gateway_service: bool, mcp: bool) -> None:
        app = _build_app(gateway_service=gateway_service, mcp=mcp)
        assert "/health" in _route_paths(app)

    def test_metrics_route_always_present(self, gateway_service: bool, mcp: bool) -> None:
        app = _build_app(gateway_service=gateway_service, mcp=mcp)
        assert "/metrics" in _route_paths(app)


# ---------------------------------------------------------------------------
# 4. discover_plugins respects the master flag
# ---------------------------------------------------------------------------


class TestDiscoverPluginsMasterFlag:
    """``discover_plugins`` returns [] when the master gateway flag is off."""

    def test_returns_empty_when_master_flag_off(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        cfg = MonoConfig(
            ahs_mono_enable_gateway_service=False,
            gateway_slack_enabled=True,
            gateway_github_enabled=True,
        )
        assert discover_plugins(cfg) == [], (
            "discover_plugins must return [] when AHS_MONO_ENABLE_GATEWAY_SERVICE=false, "
            "regardless of per-plugin sub-flag values"
        )

    def test_returns_plugins_when_master_flag_on(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        cfg = MonoConfig(
            ahs_mono_enable_gateway_service=True,
            gateway_slack_enabled=True,
            gateway_github_enabled=False,
        )
        plugins = discover_plugins(cfg)
        names = [p.name for p in plugins]
        assert "slack" in names

    def test_master_on_sub_off_returns_empty(self) -> None:
        from ypl.mono_server.config import MonoConfig
        from ypl.mono_server.server import discover_plugins

        cfg = MonoConfig(
            ahs_mono_enable_gateway_service=True,
            gateway_slack_enabled=False,
            gateway_github_enabled=False,
        )
        assert discover_plugins(cfg) == []


# ---------------------------------------------------------------------------
# 5. Lifespan behaviour for each flag combination
# ---------------------------------------------------------------------------


def _noop_acm() -> Any:
    @asynccontextmanager
    async def _noop(app: Any = None) -> AsyncGenerator[None, None]:
        yield

    return _noop()


def _stub_platform() -> Any:
    """Replace platform_mcp_http_app with a stub so its FastMCP session manager
    doesn't try to start (and crash on repeat lifespan entries)."""
    stub = MagicMock()
    stub.lifespan = lambda _app: _noop_acm()
    return patch("ypl.mono_server.server.platform_mcp_http_app", new=stub)


class TestLifespanFlagBehaviour:
    """``combined_lifespan`` skips the right hooks for each flag combination."""

    @pytest.mark.asyncio
    async def test_pure_ahs_skips_mcp_startup_and_plugins(self) -> None:
        """Both master flags off → no mcp_startup, no plugin startup."""
        called: list[str] = []

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            called.append("ahs_startup")
            state = MagicMock()
            state._mcp_lifespan_ctx = _noop_acm()
            return state

        async def _ahs_shutdown(state: Any) -> None:
            called.append("ahs_shutdown")

        async def _mcp_startup() -> None:
            called.append("mcp_startup")

        async def _mcp_shutdown() -> None:
            called.append("mcp_shutdown")

        async def _sag_startup() -> Any:
            called.append("sag_startup")
            return MagicMock()

        async def _sag_shutdown(state: Any) -> None:
            called.append("sag_shutdown")

        with (
            patch.dict(
                os.environ,
                {"AHS_MONO_ENABLE_GATEWAY_SERVICE": "false", "AHS_MONO_ENABLE_MCP": "false"},
            ),
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.ahs_shutdown", side_effect=_ahs_shutdown),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", side_effect=_mcp_shutdown),
            _stub_platform(),
            patch("ypl.mono_server.plugins.slack.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.plugins.slack.sag_shutdown", side_effect=_sag_shutdown),
        ):
            from ypl.mono_server.server import create_app

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        assert "ahs_startup" in called
        assert "ahs_shutdown" in called
        assert "mcp_startup" not in called, f"mcp_startup must NOT run when master MCP flag off. Log: {called}"
        assert "mcp_shutdown" not in called, f"mcp_shutdown must NOT run when master MCP flag off. Log: {called}"
        assert "sag_startup" not in called, f"sag_startup must NOT run when master gateway flag off. Log: {called}"
        assert "sag_shutdown" not in called, f"sag_shutdown must NOT run when master gateway flag off. Log: {called}"

    @pytest.mark.asyncio
    async def test_gateway_only_runs_plugins_skips_mcp(self) -> None:
        """Gateway on, MCP off → sag_startup runs but mcp_startup does not."""
        called: list[str] = []

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            state = MagicMock()
            state._mcp_lifespan_ctx = _noop_acm()
            return state

        async def _mcp_startup() -> None:
            called.append("mcp_startup")

        async def _sag_startup() -> Any:
            called.append("sag_startup")
            return MagicMock()

        with (
            patch.dict(
                os.environ,
                {
                    "AHS_MONO_ENABLE_GATEWAY_SERVICE": "true",
                    "AHS_MONO_ENABLE_MCP": "false",
                    "GATEWAY_SLACK_ENABLED": "true",
                    "GATEWAY_GITHUB_ENABLED": "false",
                },
            ),
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.ahs_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", new=AsyncMock()),
            _stub_platform(),
            patch("ypl.mono_server.plugins.slack.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.plugins.slack.sag_shutdown", new=AsyncMock()),
        ):
            from ypl.mono_server.server import create_app

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        assert "sag_startup" in called, f"sag_startup must run when master gateway on. Log: {called}"
        assert "mcp_startup" not in called, f"mcp_startup must NOT run when master MCP off. Log: {called}"

    @pytest.mark.asyncio
    async def test_mcp_only_runs_mcp_skips_plugins(self) -> None:
        """MCP on, gateway off → mcp_startup runs but sag_startup does not."""
        called: list[str] = []

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            state = MagicMock()
            state._mcp_lifespan_ctx = _noop_acm()
            return state

        async def _mcp_startup() -> None:
            called.append("mcp_startup")

        async def _sag_startup() -> Any:
            called.append("sag_startup")
            return MagicMock()

        with (
            patch.dict(
                os.environ,
                {"AHS_MONO_ENABLE_GATEWAY_SERVICE": "false", "AHS_MONO_ENABLE_MCP": "true"},
            ),
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.ahs_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", new=AsyncMock()),
            _stub_platform(),
            patch("ypl.mono_server.plugins.slack.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.plugins.slack.sag_shutdown", new=AsyncMock()),
        ):
            from ypl.mono_server.server import create_app

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        assert "mcp_startup" in called, f"mcp_startup must run when master MCP on. Log: {called}"
        assert "sag_startup" not in called, f"sag_startup must NOT run when master gateway off. Log: {called}"

    @pytest.mark.asyncio
    async def test_both_on_runs_everything(self) -> None:
        """Both master flags on → mcp_startup AND sag_startup both run."""
        called: list[str] = []

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            state = MagicMock()
            state._mcp_lifespan_ctx = _noop_acm()
            return state

        async def _mcp_startup() -> None:
            called.append("mcp_startup")

        async def _sag_startup() -> Any:
            called.append("sag_startup")
            return MagicMock()

        with (
            patch.dict(
                os.environ,
                {
                    "AHS_MONO_ENABLE_GATEWAY_SERVICE": "true",
                    "AHS_MONO_ENABLE_MCP": "true",
                    "GATEWAY_SLACK_ENABLED": "true",
                    "GATEWAY_GITHUB_ENABLED": "false",
                },
            ),
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.ahs_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", new=AsyncMock()),
            _stub_platform(),
            patch("ypl.mono_server.plugins.slack.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.plugins.slack.sag_shutdown", new=AsyncMock()),
        ):
            from ypl.mono_server.server import create_app

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        assert "mcp_startup" in called
        assert "sag_startup" in called


# ---------------------------------------------------------------------------
# 6. /mcp/platform is not reachable when master MCP flag is off
# ---------------------------------------------------------------------------


class TestPlatformUnreachableWhenOff:
    """When ``AHS_MONO_ENABLE_MCP=false``, requests to /mcp/platform return 404."""

    def test_platform_returns_404_when_off(self) -> None:
        app = _build_app(gateway_service=False, mcp=False)
        client = TestClient(app, raise_server_exceptions=False)
        # No mount → no auth middleware → not even a 401, just 404.
        assert client.post("/mcp/platform/").status_code == 404
        assert client.get("/mcp/platform/").status_code == 404

    def test_harness_still_returns_401_when_platform_off(self) -> None:
        """The harness mount is unaffected by the platform flag."""
        app = _build_app(gateway_service=False, mcp=False)
        client = TestClient(app, raise_server_exceptions=False)
        # Harness is mounted; auth middleware rejects no-token requests as 401.
        assert client.post("/mcp/harness/").status_code == 401
