"""Tests for ypl/mono_server/unified_mcp.py (split-MCP design).

Verifies:
  1. Module imports and exposes the two apps + two auth middleware classes.
  2. HarnessMcpAuthMiddleware:
       - Valid x-ahs-token → 200 (agent path)
       - Bearer <secret>:<session_id> Codex-CLI fallback → 200
       - Wrong / missing token → 401
       - Bearer yupp_dev_* → 401 (explicitly rejected on harness path)
  3. AgcouchMcpAuthMiddleware:
       - Missing / non-dev token → 401
       - Bearer yupp_dev_* without yuppdb → 503 (graceful degradation)
       - DB error → 503
  4. Mount wiring: the two http_apps are distinct objects and each carries
     its own auth middleware class.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_harness_app() -> FastAPI:
    from ypl.mono_server.unified_mcp import HarnessMcpAuthMiddleware

    app = FastAPI()
    app.add_middleware(HarnessMcpAuthMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    return app


def _make_agcouch_app() -> FastAPI:
    from ypl.mono_server.unified_mcp import AgcouchMcpAuthMiddleware

    app = FastAPI()
    app.add_middleware(AgcouchMcpAuthMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    return app


# ---------------------------------------------------------------------------
# 1. Module-level symbols
# ---------------------------------------------------------------------------


class TestSplitMcpModule:
    def test_apps_importable(self) -> None:
        from ypl.mono_server.unified_mcp import agcouch_mcp_http_app, harness_mcp_http_app

        assert harness_mcp_http_app is not None
        assert agcouch_mcp_http_app is not None
        assert harness_mcp_http_app is not agcouch_mcp_http_app

    def test_middlewares_importable(self) -> None:
        from ypl.mono_server.unified_mcp import AgcouchMcpAuthMiddleware, HarnessMcpAuthMiddleware

        assert HarnessMcpAuthMiddleware is not None
        assert AgcouchMcpAuthMiddleware is not None

    def test_harness_app_carries_harness_middleware(self) -> None:
        from ypl.mono_server.unified_mcp import harness_mcp_http_app

        names = {getattr(m.cls, "__name__", "") for m in harness_mcp_http_app.user_middleware}
        assert "HarnessMcpAuthMiddleware" in names, f"got {names}"
        assert "AgcouchMcpAuthMiddleware" not in names, f"got {names}"

    def test_agcouch_app_carries_agcouch_middleware(self) -> None:
        from ypl.mono_server.unified_mcp import agcouch_mcp_http_app

        names = {getattr(m.cls, "__name__", "") for m in agcouch_mcp_http_app.user_middleware}
        assert "AgcouchMcpAuthMiddleware" in names, f"got {names}"
        assert "HarnessMcpAuthMiddleware" not in names, f"got {names}"


# ---------------------------------------------------------------------------
# 2. HarnessMcpAuthMiddleware
# ---------------------------------------------------------------------------


class TestHarnessAuth:
    def test_no_auth_returns_401(self) -> None:
        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        assert client.get("/ping").status_code == 401

    def test_wrong_ahs_token_returns_401(self) -> None:
        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": "definitely-wrong"})
        assert r.status_code == 401

    def test_correct_ahs_token_returns_200(self) -> None:
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": AHS_MCP_SECRET})
        assert r.status_code == 200
        assert r.json() == {"pong": True}

    def test_bearer_secret_colon_session_returns_200(self) -> None:
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": f"Bearer {AHS_MCP_SECRET}:some-session"})
        assert r.status_code == 200

    def test_bearer_wrong_secret_colon_session_returns_401(self) -> None:
        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer wrong-secret:some-session"})
        assert r.status_code == 401

    def test_dev_token_rejected_on_harness_path(self) -> None:
        """yupp_dev_* tokens are for /mcp/agcouch; harness must reject them."""
        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. AgcouchMcpAuthMiddleware
# ---------------------------------------------------------------------------


class TestAgcouchAuth:
    def test_no_auth_returns_401(self) -> None:
        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        assert client.get("/ping").status_code == 401

    def test_ahs_token_rejected_on_agcouch_path(self) -> None:
        """x-ahs-token is for /mcp/harness; agcouch must reject it."""
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": AHS_MCP_SECRET})
        assert r.status_code == 401

    def test_bearer_secret_colon_session_rejected(self) -> None:
        """Agent-style Bearer is rejected on agcouch path."""
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": f"Bearer {AHS_MCP_SECRET}:sess"})
        assert r.status_code == 401

    def test_dev_token_without_yuppdb_never_returns_200_or_500(self) -> None:
        """In one-box / CI mode yuppdb is unavailable — must be 401 or 503."""
        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"})
        assert r.status_code in {401, 503}, f"got {r.status_code}: {r.text}"

    def test_dev_token_db_error_returns_503(self) -> None:
        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            r = client.get("/ping", headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"})
        assert r.status_code == 503, f"got {r.status_code}: {r.text}"
        body = r.json()
        assert isinstance(body, dict) and "detail" in body

    def test_dev_token_without_use_mcp_returns_403(self) -> None:
        """Mono ``AgcouchMcpAuthMiddleware`` must enforce ``USE_MCP``,
        matching the standalone ``DevTokenAuthMiddleware`` behavior. An
        active token whose owner has lost ``USE_MCP`` is rejected here
        with 403 — same as on the standalone server.
        """
        from unittest.mock import MagicMock

        from ypl.db.mcp import MCPTokenStatus

        db_token = MagicMock()
        db_token.email = "engineer@example.com"
        db_token.mcp_dev_token_id = "test-token-uuid"

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        with (
            patch(
                "ypl.mcp_server.auth_dev_token.validate_token",
                new=AsyncMock(return_value=(db_token, MCPTokenStatus.ACTIVE)),
            ),
            patch(
                "ypl.backend.utils.soul_utils.has_permission_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            r = client.get("/ping", headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"})

        assert r.status_code == 403, f"got {r.status_code}: {r.text}"
        body = r.json()
        assert "permission" in body["detail"].lower()


# ---------------------------------------------------------------------------
# 4. Deprecation header on /mcp/agcouch
# ---------------------------------------------------------------------------


class TestAgcouchDeprecationHeader:
    """``/mcp/agcouch`` stamps ``X-Auth-Deprecation`` only on responses
    where the caller actually presented a ``Bearer yupp_dev_*`` token.
    Non-dev-token rejections (no auth header at all, OAuth JWTs misrouted
    here, agent-token misroutes) do NOT carry the header — telling those
    callers to "switch to OAuth" would be confusing or actively wrong.
    Removed in phase 5b together with the dev-token branch of
    ``AgcouchMcpAuthMiddleware``.
    """

    def test_header_absent_on_no_auth_header(self) -> None:
        """No ``Authorization`` header → 401, but no deprecation header
        (the caller never claimed to be using a dev token)."""
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401
        assert r.headers.get(DEPRECATION_HEADER) is None

    def test_header_absent_on_non_dev_token_bearer(self) -> None:
        """OAuth JWT misrouted to /mcp/agcouch → 401, but no deprecation
        header. OAuth callers are already on the recommended path; telling
        them to "switch to OAuth" is wrong."""
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer eyJ.fake.oauth-jwt"})
        assert r.status_code == 401
        assert r.headers.get(DEPRECATION_HEADER) is None

    def test_header_absent_on_ahs_token_misroute(self) -> None:
        """``x-ahs-token`` agent-secret on agcouch → 401 with no deprecation
        header (agent callers never used dev tokens)."""
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": AHS_MCP_SECRET})
        assert r.status_code == 401
        assert r.headers.get(DEPRECATION_HEADER) is None

    def test_header_present_on_dev_token_unauthorized(self) -> None:
        """``Bearer yupp_dev_*`` (invalid / no-yuppdb) → header is set so the
        caller can self-detect they're on the deprecated path."""
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER, DEPRECATION_NOTICE

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        r = client.get(
            "/ping",
            headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"},
        )
        # 401 (invalid token) or 503 (yuppdb unavailable) — both must carry the header
        assert r.status_code in {401, 503}, f"got {r.status_code}: {r.text}"
        assert r.headers.get(DEPRECATION_HEADER) == DEPRECATION_NOTICE

    def test_header_present_on_db_error(self) -> None:
        """503 (yuppdb error) on a dev-token request → deprecation header
        still set."""
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER, DEPRECATION_NOTICE

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"},
            )
        assert r.status_code == 503
        assert r.headers.get(DEPRECATION_HEADER) == DEPRECATION_NOTICE

    def test_header_present_on_no_permission_403(self) -> None:
        """403 (no USE_MCP) on a dev-token request → deprecation header
        still set."""
        from unittest.mock import MagicMock

        from ypl.db.mcp import MCPTokenStatus
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER, DEPRECATION_NOTICE

        db_token = MagicMock()
        db_token.email = "engineer@example.com"
        db_token.mcp_dev_token_id = "test-token-uuid"

        client = TestClient(_make_agcouch_app(), raise_server_exceptions=False)
        with (
            patch(
                "ypl.mcp_server.auth_dev_token.validate_token",
                new=AsyncMock(return_value=(db_token, MCPTokenStatus.ACTIVE)),
            ),
            patch(
                "ypl.backend.utils.soul_utils.has_permission_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 403
        assert r.headers.get(DEPRECATION_HEADER) == DEPRECATION_NOTICE


# ---------------------------------------------------------------------------
# 5. /mcp/harness MUST NOT carry the deprecation header
# ---------------------------------------------------------------------------


class TestHarnessNoDeprecationHeader:
    """Negative test for the harness mount. ``X-Auth-Deprecation`` is a
    dev-token concept; AHS agent tokens never used dev tokens. A future
    refactor that hoists the dispatch wrapper into a shared base class —
    or copy-pastes it onto ``HarnessMcpAuthMiddleware`` — would silently
    start stamping every harness response with a "switch to OAuth" hint
    that is meaningless (and confusing) for agent callers. Pin the
    invariant here.
    """

    def test_harness_unauthorized_does_not_carry_header(self) -> None:
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401
        assert r.headers.get(DEPRECATION_HEADER) is None

    def test_harness_authorized_does_not_carry_header(self) -> None:
        from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get("/ping", headers={"x-ahs-token": AHS_MCP_SECRET})
        assert r.status_code == 200
        assert r.headers.get(DEPRECATION_HEADER) is None

    def test_harness_dev_token_misroute_does_not_carry_header(self) -> None:
        """A ``yupp_dev_*`` bearer misrouted to /mcp/harness is rejected with
        401 and explicitly does NOT carry the deprecation header — the
        rejection itself already tells the caller to use /mcp/agcouch.
        Stamping the deprecation hint here would falsely suggest the caller
        should "switch to OAuth" when the actual fix is to fix the URL.
        """
        from ypl.mcp_server.auth_dev_token import DEPRECATION_HEADER

        client = TestClient(_make_harness_app(), raise_server_exceptions=False)
        r = client.get(
            "/ping",
            headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXX"},
        )
        assert r.status_code == 401
        assert r.headers.get(DEPRECATION_HEADER) is None
