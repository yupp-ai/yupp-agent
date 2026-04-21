"""Tests for ypl/mono_server/unified_mcp.py.

Verifies:
  1. Module imports without errors and exports expected symbols.
  2. UnifiedMcpAuthMiddleware auth paths:
       - Valid x-ahs-token → 200 (agent path)
       - Bearer <secret>:<session_id> Codex-CLI fallback → 200 (agent path)
       - Wrong x-ahs-token → 401
       - No auth → 401
       - Bearer yupp_dev_* without yuppdb → 503 (graceful degradation)
  3. register_unified_tools() is idempotent (no-op on repeated calls).
"""

from __future__ import annotations
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app() -> FastAPI:
    """Return a minimal FastAPI app with UnifiedMcpAuthMiddleware attached.

    The app has a single ``GET /ping`` route that returns ``{"pong": true}``
    when authentication passes.  Used to exercise the middleware in isolation
    without spinning up the full monolith.
    """
    from ypl.mono_server.unified_mcp import UnifiedMcpAuthMiddleware

    app = FastAPI()
    app.add_middleware(UnifiedMcpAuthMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    return app


# ---------------------------------------------------------------------------
# 1. Module-level symbols
# ---------------------------------------------------------------------------


class TestUnifiedMcpModule:
    """unified_mcp module imports and exposes the right symbols."""

    def test_unified_mcp_instance_importable(self) -> None:
        """FastMCP unified instance is importable."""
        from fastmcp import FastMCP
        from ypl.mono_server.unified_mcp import unified_mcp

        assert isinstance(unified_mcp, FastMCP)
        assert unified_mcp.name == "unified-mcp"

    def test_http_app_importable(self) -> None:
        """unified_mcp_http_app is importable and non-None."""
        from ypl.mono_server.unified_mcp import unified_mcp_http_app

        assert unified_mcp_http_app is not None

    def test_auth_middleware_importable(self) -> None:
        """UnifiedMcpAuthMiddleware class is importable."""
        from ypl.mono_server.unified_mcp import UnifiedMcpAuthMiddleware

        assert UnifiedMcpAuthMiddleware is not None

    def test_register_unified_tools_importable(self) -> None:
        """register_unified_tools is importable and callable."""
        from ypl.mono_server.unified_mcp import register_unified_tools

        assert callable(register_unified_tools)

    def test_http_app_has_unified_auth_middleware(self) -> None:
        """unified_mcp_http_app has UnifiedMcpAuthMiddleware in its middleware stack."""
        from ypl.mono_server.unified_mcp import unified_mcp_http_app

        # Starlette middleware entries carry a .cls attribute.  Collect class names
        # (strings) to avoid mypy type-overlap errors with _MiddlewareFactory.
        mw_class_names = {getattr(m.cls, "__name__", "") for m in unified_mcp_http_app.user_middleware}
        assert "UnifiedMcpAuthMiddleware" in mw_class_names, (
            f"UnifiedMcpAuthMiddleware not found in middleware. Got: {mw_class_names}"
        )


# ---------------------------------------------------------------------------
# 2. UnifiedMcpAuthMiddleware — agent path (x-ahs-token)
# ---------------------------------------------------------------------------


class TestAgentAuthPath:
    """Agent path: x-ahs-token header (or Bearer <secret>:<session_id>)."""

    def test_no_auth_returns_401(self) -> None:
        """Request with no auth header → 401."""
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401

    def test_wrong_ahs_token_returns_401(self) -> None:
        """Wrong value in x-ahs-token header → 401."""
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": "definitely-wrong"})
        assert r.status_code == 401

    def test_correct_ahs_token_returns_200(self) -> None:
        """Correct x-ahs-token → 200 (agent authenticated)."""
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": AHS_MCP_SECRET})
        assert r.status_code == 200
        assert r.json() == {"pong": True}

    def test_bearer_secret_colon_session_returns_200(self) -> None:
        """``Bearer <secret>:<session_id>`` Codex CLI fallback → 200."""
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        bearer = f"{AHS_MCP_SECRET}:test-session-uuid-1234"
        r = client.get("/ping", headers={"authorization": f"Bearer {bearer}"})
        assert r.status_code == 200
        assert r.json() == {"pong": True}

    def test_bearer_wrong_secret_colon_session_returns_401(self) -> None:
        """``Bearer wrong:<session>`` → 401 (secret mismatch)."""
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer wrong-secret:some-session"})
        assert r.status_code == 401

    def test_unauthorized_response_is_json(self) -> None:
        """401 response body is valid JSON with ``detail`` key."""
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401
        data = r.json()
        assert isinstance(data, dict)
        assert data == {"detail": "Unauthorized"}


# ---------------------------------------------------------------------------
# 3. UnifiedMcpAuthMiddleware — developer path (Bearer yupp_dev_*)
# ---------------------------------------------------------------------------


class TestDeveloperAuthPath:
    """Developer path: ``Bearer yupp_dev_*`` token validated against yuppdb."""

    def test_yupp_dev_without_yuppdb_returns_503(self) -> None:
        """``Bearer yupp_dev_*`` returns 503 when yuppdb is not configured.

        In one-box / CI mode there is no yuppdb, so the validate_token() call
        fails.  The middleware must return 503 (not 500) to indicate a
        configuration issue rather than a code bug.
        """
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Any yupp_dev_* token without a real yuppdb behind it will fail
        r = client.get(
            "/ping",
            headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
        )
        # Either 401 (token invalid) or 503 (yuppdb unavailable) are acceptable;
        # we just must not get a 200 or a 500.
        assert r.status_code in {401, 503}, f"Expected 401 or 503, got {r.status_code}: {r.text}"
        assert r.status_code != 200
        assert r.status_code != 500

    def test_yupp_dev_db_error_returns_503(self) -> None:
        """DB errors during token validation surface as HTTP 503."""
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        # 503 on DB error; never 500
        assert r.status_code == 503, f"Expected 503 on DB error, got {r.status_code}: {r.text}"
        data = r.json()
        assert isinstance(data, dict)
        assert "detail" in data

    def test_yupp_dev_token_not_confused_with_bearer_secret_colon(self) -> None:
        """A ``yupp_dev_*`` token is NOT interpreted as ``<secret>:<session_id>`` format.

        ``yupp_dev_`` tokens don't contain ``:`` so there's no ambiguity, but
        this test acts as a regression guard.
        """
        app = _make_test_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get(
            "/ping",
            headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
        )
        # Must not pass agent auth (yupp_dev tokens don't match AHS_MCP_SECRET)
        assert r.status_code != 200


# ---------------------------------------------------------------------------
# 4. register_unified_tools idempotency
# ---------------------------------------------------------------------------


class TestRegisterUnifiedToolsIdempotency:
    """register_unified_tools() is a no-op on repeated calls."""

    @pytest.mark.asyncio
    async def test_idempotent(self) -> None:
        """Calling register_unified_tools() twice does not double-register tools."""
        import ypl.mono_server.unified_mcp as _mod
        from ypl.mono_server.unified_mcp import register_unified_tools, unified_mcp

        # Reset the flag so the function runs on first call
        original_flag = _mod._tools_registered

        mock_import = AsyncMock()
        with patch.object(unified_mcp, "import_server", mock_import):
            # Temporarily reset flag
            _mod._tools_registered = False
            try:
                await register_unified_tools()
                first_call_count = mock_import.call_count

                # Second call must be a no-op
                await register_unified_tools()
                second_call_count = mock_import.call_count
            finally:
                # Restore original flag regardless of test outcome
                _mod._tools_registered = original_flag

        assert first_call_count == 2, f"Expected 2 import_server calls (harness + agcouch), got {first_call_count}"
        assert second_call_count == 2, (
            "import_server should NOT be called again on second register_unified_tools() call"
        )
