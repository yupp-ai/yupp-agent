"""Smoke tests for the mono_server monolith entrypoint.

Verifies end-to-end correctness of the monolith *without* requiring a live
Postgres or Redis instance.  Every test here must pass in CI with
``poetry run pytest tests/mono_server/test_smoke.py``.

Coverage areas:
  1. Health check — always 200, correct body and content-type.
  2. Auth boundary — AHS routes reject unauthenticated requests correctly.
  3. MCP harness auth boundary — rejects requests without a valid token.
  4. SAG route registration — /gw/slack/* routes are wired.
  5. GitHub gateway toggle — /gw/github/* routes controlled by env var.
  6. Combined lifespan ordering — startup / shutdown happen in correct order
     (verified via mocks; no real DB / Redis needed).
  7. Error response format — JSON errors are well-formed.
  8. Integration placeholder — marked ``integration`` so CI skips them;
     run manually with a live stack (``MONOLITH_INTEGRATION=1``).
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(*, raise_server_exceptions: bool = False) -> TestClient:
    """Return a TestClient that does NOT run the lifespan.

    Not using ``with TestClient(app)`` keeps the lifespan (and therefore DB /
    Redis / Slack connections) out of the picture so all smoke tests are
    hermetic.
    """
    from ypl.mono_server.server import app

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _route_paths(application: FastAPI) -> set[str]:
    """Return the set of path strings for all routes registered on *application*."""
    return {getattr(r, "path", "") for r in application.routes}


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------


class TestHealthSmoke:
    """GET /health works without any infra dependencies."""

    def test_health_returns_200(self) -> None:
        """The liveness probe always returns HTTP 200."""
        r = _client().get("/health")
        assert r.status_code == 200

    def test_health_body_is_ok(self) -> None:
        """Body is the canonical ``{"status": "ok"}`` sentinel."""
        r = _client().get("/health")
        assert r.json() == {"status": "ok"}

    def test_health_content_type_is_json(self) -> None:
        """Response carries application/json content-type."""
        r = _client().get("/health")
        assert "application/json" in r.headers.get("content-type", "")

    def test_health_method_not_allowed(self) -> None:
        """POST /health should return 405 (route exists but wrong method)."""
        r = _client().post("/health")
        assert r.status_code == 405

    def test_health_no_auth_required(self) -> None:
        """The /health probe must never require authentication."""
        r = _client().get("/health")
        # Must not be a 401/403/503 auth-gate rejection
        assert r.status_code not in {401, 403, 503}


# ---------------------------------------------------------------------------
# 2. AHS auth boundary
# ---------------------------------------------------------------------------


class TestAHSAuthBoundary:
    """AHS routes return the right auth-related status codes."""

    def test_agents_no_api_key_returns_503(self) -> None:
        """When AGENT_HARNESS_SERVICE_API_KEY is unset all AHS requests → 503."""
        from ypl.backend.config import settings

        with patch.object(settings, "AGENT_HARNESS_SERVICE_API_KEY", ""):
            r = _client().get("/ahs/agents")
        assert r.status_code == 503

    def test_agents_wrong_x_api_key_returns_403(self) -> None:
        """Wrong X-API-Key header → 403 (not 500)."""
        from ypl.backend.config import settings

        with patch.object(settings, "AGENT_HARNESS_SERVICE_API_KEY", "real-key"):
            r = _client().get("/ahs/agents", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 403

    def test_sessions_no_api_key_returns_503(self) -> None:
        """GET /ahs/sessions without API key configured → 503."""
        from ypl.backend.config import settings

        with patch.object(settings, "AGENT_HARNESS_SERVICE_API_KEY", ""):
            r = _client().get("/ahs/sessions")
        assert r.status_code == 503

    def test_agents_wrong_token_not_500(self) -> None:
        """Auth failures must never surface as 500 Internal Server Error."""
        from ypl.backend.config import settings

        with patch.object(settings, "AGENT_HARNESS_SERVICE_API_KEY", "real-key"):
            r = _client().get("/ahs/agents", headers={"X-API-Key": "bad"})
        assert r.status_code != 500

    def test_unknown_ahs_route_returns_404(self) -> None:
        """A non-existent /ahs/* path returns 404, not 500."""
        r = _client().get("/ahs/nonexistent-endpoint-xyz")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. MCP harness auth boundary
# ---------------------------------------------------------------------------


class TestMCPHarnessAuthBoundary:
    """Harness MCP endpoint enforces token authentication."""

    def test_post_mcp_harness_without_token_returns_401(self) -> None:
        """Unauthenticated POST /mcp/harness/ → 401 (McpTokenAuthMiddleware)."""
        r = _client().post("/mcp/harness/")
        assert r.status_code == 401

    def test_get_mcp_harness_without_token_returns_401(self) -> None:
        """Unauthenticated GET /mcp/harness/ → 401."""
        r = _client().get("/mcp/harness/")
        assert r.status_code == 401

    def test_mcp_harness_never_returns_500(self) -> None:
        """Auth rejection must not be a 500 error."""
        r = _client().post("/mcp/harness/")
        assert r.status_code != 500

    def test_mcp_harness_is_mounted(self) -> None:
        """The /mcp/harness mount exists on the app (route discovery)."""
        from starlette.routing import Mount
        from ypl.mono_server.server import app

        mounts = {r.path for r in app.routes if isinstance(r, Mount)}
        assert "/mcp/harness" in mounts, f"Missing /mcp/harness mount. Found: {sorted(mounts)}"


# ---------------------------------------------------------------------------
# 4. SAG route registration
# ---------------------------------------------------------------------------


class TestSAGRouteRegistration:
    """Slack gateway routes are present when GATEWAY_SLACK_ENABLED=true."""

    def test_gw_slack_routes_registered(self) -> None:
        """At least one /gw/slack/* route is registered on the default app."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        sag_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert sag_paths, f"No /gw/slack routes found. All routes: {sorted(paths)}"

    def test_sag_route_count_nonzero(self) -> None:
        """More than one SAG route is registered (router has multiple endpoints)."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        sag_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert len(sag_paths) >= 1

    def test_sag_disabled_no_routes(self) -> None:
        """With GATEWAY_SLACK_ENABLED=false no /gw/slack routes exist."""
        with patch.dict(os.environ, {"GATEWAY_SLACK_ENABLED": "false", "GATEWAY_GITHUB_ENABLED": "false"}):
            from ypl.mono_server import server as _srv

            disabled_app = _srv.create_app()
        paths = _route_paths(disabled_app)
        sag_paths = [p for p in paths if p.startswith("/gw/slack")]
        assert not sag_paths, f"Unexpected /gw/slack routes: {sag_paths}"


# ---------------------------------------------------------------------------
# 5. GitHub gateway toggle
# ---------------------------------------------------------------------------


class TestGitHubGatewayToggle:
    """GitHub gateway routes are controlled by GATEWAY_GITHUB_ENABLED."""

    def test_github_routes_present_by_default(self) -> None:
        """Default config includes /gw/github/* routes."""
        from ypl.mono_server.server import app

        paths = _route_paths(app)
        github_paths = [p for p in paths if "/gw/github" in p]
        assert github_paths, f"Expected /gw/github routes. All routes: {sorted(paths)}"

    def test_github_routes_absent_when_disabled(self) -> None:
        """GATEWAY_GITHUB_ENABLED=false removes /gw/github/* and /ahs/webhook/* routes.

        The _ahs_router_setup_done guard is reset and a fresh ahs_router is injected
        so the test creates a clean app — without this, module-level initialisation
        permanently bakes webhook_router into the shared ahs_router object.
        """
        from fastapi import APIRouter
        from ypl.mono_server import server as _srv

        fresh_ahs_router = APIRouter()
        with (
            patch.dict(os.environ, {"GATEWAY_GITHUB_ENABLED": "false"}),
            patch.object(_srv, "_ahs_router_setup_done", False),
            patch.object(_srv, "ahs_router", fresh_ahs_router),
        ):
            disabled_app = _srv.create_app()
        paths = _route_paths(disabled_app)
        github_paths = [p for p in paths if "/gw/github" in p]
        assert not github_paths, f"Unexpected /gw/github routes: {github_paths}"
        webhook_paths = [p for p in paths if "/webhook" in p]
        assert not webhook_paths, f"Unexpected /webhook routes when GitHub disabled: {webhook_paths}"


# ---------------------------------------------------------------------------
# 6. Combined lifespan ordering
# ---------------------------------------------------------------------------


def _noop_asynccontextmanager() -> Any:
    """Return an async context manager that does nothing."""

    @asynccontextmanager
    async def _noop(app: Any = None) -> AsyncGenerator[None, None]:
        yield

    return _noop()


class TestLifespanOrdering:
    """combined_lifespan starts and stops services in the documented order.

    Startup order:  ahs_startup → harness MCP lifespan → mcp_startup →
                    yuppster MCP lifespan → sag_startup
    Shutdown order: sag_shutdown → yuppster MCP exit → mcp_shutdown →
                    harness MCP exit → ahs_shutdown
    """

    @pytest.mark.asyncio
    async def test_startup_order_ahs_before_mcp(self) -> None:
        """ahs_startup is called before mcp_startup."""
        call_log: list[str] = []

        mock_ahs_state = MagicMock()
        mock_ahs_state._mcp_lifespan_ctx = _noop_asynccontextmanager()

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            call_log.append("ahs_startup")
            return mock_ahs_state

        async def _mcp_startup() -> None:
            call_log.append("mcp_startup")

        async def _mcp_shutdown() -> None:
            call_log.append("mcp_shutdown")

        async def _ahs_shutdown(state: Any) -> None:
            call_log.append("ahs_shutdown")

        mock_sag_state = MagicMock()

        async def _sag_startup() -> Any:
            call_log.append("sag_startup")
            return mock_sag_state

        async def _sag_shutdown(state: Any) -> None:
            call_log.append("sag_shutdown")

        from ypl.mono_server.server import create_app

        with (
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", side_effect=_mcp_shutdown),
            patch("ypl.mono_server.server.ahs_shutdown", side_effect=_ahs_shutdown),
            patch("ypl.mono_server.server.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.server.sag_shutdown", side_effect=_sag_shutdown),
            patch("ypl.mono_server.server.yuppster_mcp_http_app") as mock_yuppster,
        ):
            mock_yuppster.lifespan.return_value = _noop_asynccontextmanager()

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass  # yield point — services should be up

        # Verify order
        assert call_log.index("ahs_startup") < call_log.index("mcp_startup"), (
            f"Expected ahs_startup before mcp_startup. Got: {call_log}"
        )
        assert call_log.index("mcp_startup") < call_log.index("sag_startup"), (
            f"Expected mcp_startup before sag_startup. Got: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_shutdown_order_reverse_of_startup(self) -> None:
        """Shutdown order is the reverse of startup."""
        call_log: list[str] = []

        mock_ahs_state = MagicMock()
        mock_ahs_state._mcp_lifespan_ctx = _noop_asynccontextmanager()

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            call_log.append("ahs_startup")
            return mock_ahs_state

        async def _mcp_startup() -> None:
            call_log.append("mcp_startup")

        async def _mcp_shutdown() -> None:
            call_log.append("mcp_shutdown")

        async def _ahs_shutdown(state: Any) -> None:
            call_log.append("ahs_shutdown")

        mock_sag_state = MagicMock()

        async def _sag_startup() -> Any:
            call_log.append("sag_startup")
            return mock_sag_state

        async def _sag_shutdown(state: Any) -> None:
            call_log.append("sag_shutdown")

        from ypl.mono_server.server import create_app

        with (
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", side_effect=_mcp_shutdown),
            patch("ypl.mono_server.server.ahs_shutdown", side_effect=_ahs_shutdown),
            patch("ypl.mono_server.server.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.server.sag_shutdown", side_effect=_sag_shutdown),
            patch("ypl.mono_server.server.yuppster_mcp_http_app") as mock_yuppster,
        ):
            mock_yuppster.lifespan.return_value = _noop_asynccontextmanager()

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        # Verify shutdown is reverse of startup
        assert call_log.index("sag_shutdown") < call_log.index("mcp_shutdown"), (
            f"Expected sag_shutdown before mcp_shutdown. Got: {call_log}"
        )
        assert call_log.index("mcp_shutdown") < call_log.index("ahs_shutdown"), (
            f"Expected mcp_shutdown before ahs_shutdown. Got: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_ahs_shutdown_called_even_if_mcp_raises(self) -> None:
        """ahs_shutdown is called even if mcp_startup raises an exception."""
        call_log: list[str] = []

        mock_ahs_state = MagicMock()
        mock_ahs_state._mcp_lifespan_ctx = _noop_asynccontextmanager()

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            call_log.append("ahs_startup")
            return mock_ahs_state

        async def _mcp_startup() -> None:
            call_log.append("mcp_startup")
            raise RuntimeError("simulated mcp init failure")

        async def _mcp_shutdown() -> None:
            call_log.append("mcp_shutdown")

        async def _ahs_shutdown(state: Any) -> None:
            call_log.append("ahs_shutdown")

        from ypl.mono_server.server import create_app

        with (
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.mcp_startup", side_effect=_mcp_startup),
            patch("ypl.mono_server.server.mcp_shutdown", side_effect=_mcp_shutdown),
            patch("ypl.mono_server.server.ahs_shutdown", side_effect=_ahs_shutdown),
            patch("ypl.mono_server.server.yuppster_mcp_http_app") as mock_yuppster,
        ):
            mock_yuppster.lifespan.return_value = _noop_asynccontextmanager()

            test_app = create_app()
            with pytest.raises(RuntimeError, match="simulated mcp init failure"):
                async with test_app.router.lifespan_context(test_app):
                    pass  # pragma: no cover

        assert "ahs_shutdown" in call_log, f"ahs_shutdown was not called after mcp_startup failure. Log: {call_log}"

    @pytest.mark.asyncio
    async def test_sag_disabled_sag_startup_not_called(self) -> None:
        """With GATEWAY_SLACK_ENABLED=false, sag_startup is never called."""
        call_log: list[str] = []

        mock_ahs_state = MagicMock()
        mock_ahs_state._mcp_lifespan_ctx = _noop_asynccontextmanager()

        async def _ahs_startup(app: Any, mcp_app: Any) -> Any:
            return mock_ahs_state

        async def _sag_startup() -> Any:
            call_log.append("sag_startup")
            return MagicMock()

        from ypl.mono_server.server import create_app

        with (
            patch("ypl.mono_server.server.ahs_startup", side_effect=_ahs_startup),
            patch("ypl.mono_server.server.mcp_startup", new=AsyncMock()),
            patch("ypl.mono_server.server.mcp_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.ahs_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.sag_startup", side_effect=_sag_startup),
            patch("ypl.mono_server.server.sag_shutdown", new=AsyncMock()),
            patch("ypl.mono_server.server.yuppster_mcp_http_app") as mock_yuppster,
            patch.dict(os.environ, {"GATEWAY_SLACK_ENABLED": "false"}),
        ):
            mock_yuppster.lifespan.return_value = _noop_asynccontextmanager()

            test_app = create_app()
            async with test_app.router.lifespan_context(test_app):
                pass

        assert "sag_startup" not in call_log, "sag_startup should not be called when GATEWAY_SLACK_ENABLED=false"


# ---------------------------------------------------------------------------
# 7. Error response format
# ---------------------------------------------------------------------------


class TestErrorResponseFormat:
    """Error responses are valid JSON with an expected shape."""

    def test_404_is_json(self) -> None:
        """A 404 for an unknown route returns valid JSON."""
        r = _client().get("/this-route-does-not-exist")
        assert r.status_code == 404
        data = r.json()
        assert isinstance(data, dict)

    def test_405_is_json(self) -> None:
        """A 405 Method Not Allowed response returns valid JSON."""
        r = _client().post("/health")
        assert r.status_code == 405
        data = r.json()
        assert isinstance(data, dict)

    def test_401_on_ahs_is_json(self) -> None:
        """A 401 from the AHS auth layer (missing X-API-Key header) returns valid JSON."""
        from ypl.backend.config import settings

        # Provide a valid API key in settings so the middleware can check auth,
        # then make the request without the X-API-Key header to get a 401.
        with patch.object(settings, "AGENT_HARNESS_SERVICE_API_KEY", "key"):
            r = _client().get("/ahs/agents")  # no X-API-Key header → 401
        assert r.status_code == 401
        data = r.json()
        assert isinstance(data, dict)

    def test_401_on_mcp_harness_is_json(self) -> None:
        """A 401 from McpTokenAuthMiddleware returns valid JSON (not plain text)."""
        r = _client().post("/mcp/harness/")
        assert r.status_code == 401
        # McpTokenAuthMiddleware must return JSON so clients get a structured error
        data = r.json()
        assert isinstance(data, dict), f"Expected JSON dict, got: {r.text!r}"
        assert data == {"detail": "Unauthorized"}


# ---------------------------------------------------------------------------
# 8. Integration placeholder (skip in CI; run with MONOLITH_INTEGRATION=1)
# ---------------------------------------------------------------------------

_integration = pytest.mark.skipif(
    not os.environ.get("MONOLITH_INTEGRATION"),
    reason="Set MONOLITH_INTEGRATION=1 and start docker-compose to run integration tests",
)


@_integration
class TestIntegrationLiveStack:
    """End-to-end tests against a real running monolith.

    Prerequisites (run before this class):
      1. ``docker-compose up -d``   (Postgres + Redis)
      2. ``alembic upgrade head``   (apply DB migrations)
      3. ``uvicorn ypl.mono_server.server:app --port 8090``  (monolith)

    Then set MONOLITH_INTEGRATION=1 and run:
      ``poetry run pytest tests/mono_server/test_smoke.py -m integration -v``
    """

    BASE_URL = os.environ.get("MONOLITH_BASE_URL", "http://localhost:8090")

    def _http_client(self) -> Any:
        import httpx

        return httpx.Client(base_url=self.BASE_URL, timeout=10.0)

    def test_health_live(self) -> None:
        """GET /health returns 200 from the live server."""
        with self._http_client() as client:
            r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_ahs_agents_live_401(self) -> None:
        """GET /ahs/agents without auth returns 401 (server is up, auth works)."""
        with self._http_client() as client:
            r = client.get("/ahs/agents")
        # 503 if API key not configured in the live env, 401 if it is
        assert r.status_code in {401, 503}

    def test_mcp_harness_live_401(self) -> None:
        """POST /mcp/harness/ without MCP token returns 401."""
        with self._http_client() as client:
            r = client.post("/mcp/harness/")
        assert r.status_code == 401

    def test_sag_route_live(self) -> None:
        """The SAG Slack-events endpoint exists (400/401/200 are all fine)."""
        with self._http_client() as client:
            r = client.post("/gw/slack/slack/events", content=b"{}")
        # Any response other than connection error or 5xx means the route is up
        assert r.status_code < 500
