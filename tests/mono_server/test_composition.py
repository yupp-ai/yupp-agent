"""Composition tests for the monolith server.

Verifies that:
- The app creates without errors
- All expected routes / mounts are registered
- The /health endpoint returns 200 OK
- Gateway routes are present / absent based on MonoConfig
"""

from __future__ import annotations
import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_paths(application: FastAPI) -> set[str]:
    """Return the set of path strings for all routes on *application*."""
    paths: set[str] = set()
    for route in application.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    return paths


def _mount_paths(application: FastAPI) -> set[str]:
    """Return the set of path strings for all *mounted* sub-apps on *application*."""
    return {r.path for r in application.routes if isinstance(r, Mount)}


# ---------------------------------------------------------------------------
# Module-level app
# ---------------------------------------------------------------------------


class TestAppCreation:
    """The module-level ``app`` is a valid FastAPI instance."""

    def test_app_is_fastapi(self) -> None:
        from ypl.mono_server.server import app

        assert isinstance(app, FastAPI)

    def test_app_title(self) -> None:
        from ypl.mono_server.server import app

        assert app.title == "Yupp Agent Platform"

    def test_app_has_lifespan(self) -> None:
        """The app was created with a combined lifespan (non-None router)."""
        from ypl.mono_server.server import app

        # FastAPI stores the lifespan on the router; a non-None lifespan means
        # the combined_lifespan context manager was wired in correctly.
        assert app.router.lifespan_context is not None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Expected routes are registered on the default app."""

    def test_health_route_registered(self) -> None:
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        assert "/health" in paths, f"Missing /health in routes: {paths}"

    def test_ahs_routes_present(self) -> None:
        """At least one /ahs/* route is registered."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        ahs_paths = [p for p in paths if p.startswith("/ahs")]
        assert ahs_paths, f"No /ahs routes found. All paths: {paths}"

    def test_harness_mcp_mounted(self) -> None:
        """Harness MCP is mounted at /mcp/harness."""
        from ypl.mono_server.server import app

        mounts = _mount_paths(app)
        assert "/mcp/harness" in mounts, f"Missing /mcp/harness mount. Mounts: {mounts}"

    def test_yuppster_mcp_mounted(self) -> None:
        """Yuppster MCP is mounted at /mcp."""
        from ypl.mono_server.server import app

        mounts = _mount_paths(app)
        assert "/mcp" in mounts, f"Missing /mcp mount. Mounts: {mounts}"

    def test_slack_gateway_routes_present_by_default(self) -> None:
        """SAG routes are present when GATEWAY_SLACK_ENABLED=true (default)."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        gw_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert gw_paths, f"No /gw/slack routes found. All paths: {paths}"


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """GET /health returns 200 OK without running the full lifespan."""

    def test_health_returns_200(self) -> None:
        from ypl.mono_server.server import app

        # Do NOT use `with TestClient(app):` — that would run the lifespan
        # (startup/shutdown) which requires a live DB, Redis, etc.
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_returns_ok_body(self) -> None:
        from ypl.mono_server.server import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/health")
        body = response.json()
        assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# Gateway toggle
# ---------------------------------------------------------------------------


class TestGatewayToggle:
    """Gateway routes are present / absent based on MonoConfig."""

    def test_no_slack_routes_when_disabled(self) -> None:
        """With GATEWAY_SLACK_ENABLED=false, no /gw/slack routes are registered."""
        with patch.dict(os.environ, {"GATEWAY_SLACK_ENABLED": "false"}):
            from ypl.mono_server import server as _srv

            disabled_app = _srv.create_app()

        paths = _route_paths(disabled_app)
        gw_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert not gw_paths, f"Unexpected /gw/slack routes: {gw_paths}"

    def test_slack_routes_present_when_enabled(self) -> None:
        """With GATEWAY_SLACK_ENABLED=true, /gw/slack/* routes are registered."""
        with patch.dict(os.environ, {"GATEWAY_SLACK_ENABLED": "true"}):
            from ypl.mono_server import server as _srv

            enabled_app = _srv.create_app()

        paths = _route_paths(enabled_app)
        gw_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert gw_paths, f"Expected /gw/slack routes but found none. All paths: {paths}"

    def test_github_gateway_not_mounted_by_default(self) -> None:
        """GitHub gateway is off by default — no /gw/github routes."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        github_paths = [p for p in paths if "/gw/github" in p]
        assert not github_paths, f"Unexpected /gw/github routes: {github_paths}"


# ---------------------------------------------------------------------------
# MonoConfig
# ---------------------------------------------------------------------------


class TestMonoConfig:
    """MonoConfig reads values from environment variables."""

    def test_default_port(self) -> None:
        from ypl.mono_server.config import MonoConfig

        cfg = MonoConfig()
        assert cfg.port == 8090

    def test_port_override_via_env(self) -> None:
        with patch.dict(os.environ, {"PORT": "9000"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.port == 9000

    def test_gateway_slack_default_true(self) -> None:
        from ypl.mono_server.config import MonoConfig

        cfg = MonoConfig()
        assert cfg.gateway_slack_enabled is True

    def test_gateway_slack_disabled_via_env(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_SLACK_ENABLED": "0"}):
            from ypl.mono_server.config import MonoConfig

            cfg = MonoConfig()
            assert cfg.gateway_slack_enabled is False

    def test_gateway_github_default_false(self) -> None:
        from ypl.mono_server.config import MonoConfig

        cfg = MonoConfig()
        assert cfg.gateway_github_enabled is False
