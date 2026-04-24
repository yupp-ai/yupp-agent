"""Unit tests for ypl/mcp_server/auth_dev_token.py.

Covers:
  - Token generation format and uniqueness
  - Lookup key extraction (edge cases)
  - Hash and verify helpers (pure crypto operations)
  - _is_service_token() access control
  - create_request_context() — header handling, service vs regular token
  - DevTokenAuthMiddleware.dispatch() — auth flows via Starlette TestClient
  - validate_token() — mocked DB (active, revoked, expired, not found)
  - create_token() — domain check, permission check, DB write
  - revoke_token() — marks token REVOKED in DB

All tests run without a live database or Redis — all I/O is mocked.
"""

from __future__ import annotations
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from ypl.mcp_server.auth_dev_token import (
    TOKEN_LENGTH,
    DevTokenAuthMiddleware,
    _can_assert_user_identity,
    create_request_context,
    generate_token,
    get_token_lookup_key,
    hash_token,
    verify_token_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_token(
    *,
    email: str = "dev@example.com",
    status_val: str = "ACTIVE",
    expires_at: datetime | None = None,
) -> MagicMock:
    """Return a mock MCPDevToken-like object."""
    from ypl.db.mcp import MCPTokenStatus

    token = MagicMock()
    token.email = email
    token.status = MCPTokenStatus[status_val]
    token.expires_at = expires_at
    token.mcp_dev_token_id = "test-token-uuid"
    return token


def _make_starlette_app() -> Starlette:
    """Minimal Starlette app with DevTokenAuthMiddleware for integration tests."""
    from ypl.mcp_server.context_vars import request_context

    async def ping(request: Request) -> JSONResponse:
        return JSONResponse({"pong": True})

    app = Starlette(routes=[Route("/ping", ping)])
    app.add_middleware(DevTokenAuthMiddleware, request_context_var=request_context)
    return app


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


class TestGenerateToken:
    def test_format(self) -> None:
        token = generate_token()
        assert token.startswith("yupp_dev_")

    def test_suffix_length(self) -> None:
        token = generate_token()
        suffix = token[len("yupp_dev_") :]
        assert len(suffix) == TOKEN_LENGTH

    def test_suffix_alphanumeric_only(self) -> None:
        token = generate_token()
        suffix = token[len("yupp_dev_") :]
        assert re.match(r"^[A-Za-z0-9]+$", suffix), f"Non-alphanumeric chars: {suffix}"

    def test_uniqueness(self) -> None:
        tokens = {generate_token() for _ in range(10)}
        assert len(tokens) == 10, "Expected all 10 tokens to be unique"


# ---------------------------------------------------------------------------
# Lookup key extraction
# ---------------------------------------------------------------------------


class TestGetTokenLookupKey:
    def test_standard_token(self) -> None:
        token = "yupp_dev_AbCdXxXxXxXxXxXxXxXxXxXxXxEfGh"
        key = get_token_lookup_key(token)
        # first 4 + last 4 of the suffix
        assert key == "AbCd" + "EfGh"

    def test_no_prefix(self) -> None:
        # Token without yupp_dev_ prefix — treated as raw suffix
        token = "AbCdXxXxXxXxXxXxXxXxXxXxXxEfGh"
        key = get_token_lookup_key(token)
        assert key == "AbCd" + "EfGh"

    def test_short_token_fallback(self) -> None:
        # Suffix < 8 chars — returns suffix as-is
        key = get_token_lookup_key("yupp_dev_ABC")
        assert key == "ABC"

    def test_exactly_8_chars(self) -> None:
        key = get_token_lookup_key("yupp_dev_12345678")
        assert key == "1234" + "5678"

    def test_real_token(self) -> None:
        token = generate_token()
        key = get_token_lookup_key(token)
        assert len(key) == 8
        assert re.match(r"^[A-Za-z0-9]+$", key)


# ---------------------------------------------------------------------------
# Hash and verify
# ---------------------------------------------------------------------------


class TestHashAndVerify:
    def test_hash_is_bcrypt_string(self) -> None:
        h = hash_token("yupp_dev_TestToken1234")
        assert h.startswith("$2b$"), f"Expected bcrypt hash, got: {h!r}"

    def test_verify_correct_token(self) -> None:
        token = generate_token()
        h = hash_token(token)
        assert verify_token_hash(token, h) is True

    def test_verify_wrong_token(self) -> None:
        token = generate_token()
        h = hash_token(token)
        assert verify_token_hash("yupp_dev_WrongToken000000000000000000", h) is False

    def test_verify_empty_token(self) -> None:
        h = hash_token(generate_token())
        assert verify_token_hash("", h) is False

    def test_verify_bad_hash(self) -> None:
        # verify_token_hash must not raise on malformed hash
        assert verify_token_hash(generate_token(), "not-a-valid-hash") is False

    def test_different_tokens_different_hashes(self) -> None:
        t1, t2 = generate_token(), generate_token()
        assert hash_token(t1) != hash_token(t2)

    def test_hash_is_deterministic_in_verify(self) -> None:
        token = generate_token()
        h = hash_token(token)
        # bcrypt is non-deterministic (different salt each time), but verify
        # must always succeed for the correct token.
        for _ in range(3):
            assert verify_token_hash(token, h)


# ---------------------------------------------------------------------------
# _can_assert_user_identity
# ---------------------------------------------------------------------------


class TestCanAssertUserIdentity:
    """The middleware's X-User-ID / X-AHS-* trust gate is now driven by RBAC:
    the token owner must hold MANAGE_AGENT_SESSIONS. This replaces the
    legacy AHS_SERVICE_TOKEN_EMAILS env-var allowlist.
    """

    async def test_permission_granted_trusts_token(self) -> None:
        token = _make_db_token(email="admin@example.com")
        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=True),
        ):
            assert await _can_assert_user_identity(token) is True

    async def test_permission_missing_does_not_trust(self) -> None:
        token = _make_db_token(email="engineer@example.com")
        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=False),
        ):
            assert await _can_assert_user_identity(token) is False


# ---------------------------------------------------------------------------
# create_request_context
# ---------------------------------------------------------------------------


class TestCreateRequestContext:
    def _make_request(
        self,
        *,
        client_host: str = "127.0.0.1",
        headers: dict[str, str] | None = None,
    ) -> MagicMock:
        req = MagicMock(spec=Request)
        req.client = MagicMock()
        req.client.host = client_host
        req.headers = headers or {}
        return req

    async def test_basic_fields_present(self) -> None:
        db_token = _make_db_token()
        request = self._make_request(headers={"user-agent": "test-agent/1.0"})

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=True),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["token"] is db_token
        assert ctx["ip_address"] == "127.0.0.1"
        assert ctx["user_agent"] == "test-agent/1.0"

    async def test_privileged_token_trusts_x_user_id(self) -> None:
        db_token = _make_db_token(email="admin@example.com")
        request = self._make_request(headers={"x-user-id": "user-abc-123"})

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=True),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["requesting_user_id"] == "user-abc-123"

    async def test_non_privileged_token_ignores_x_user_id(self) -> None:
        db_token = _make_db_token(email="engineer@example.com")
        request = self._make_request(headers={"x-user-id": "user-abc-123"})

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=False),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["requesting_user_id"] is None

    async def test_privileged_token_reads_ahs_headers(self) -> None:
        db_token = _make_db_token(email="admin@example.com")
        request = self._make_request(
            headers={
                "x-ahs-agent-name": "test-raccoon",
                "x-ahs-session-id": "session-uuid-1234",
            }
        )

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=True),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["ahs_agent_name"] == "test-raccoon"
        assert ctx["ahs_session_id"] == "session-uuid-1234"

    async def test_non_privileged_token_ignores_ahs_headers(self) -> None:
        db_token = _make_db_token(email="engineer@example.com")
        request = self._make_request(
            headers={
                "x-ahs-agent-name": "malicious-agent",
                "x-ahs-session-id": "fake-session",
            }
        )

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=False),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["ahs_agent_name"] is None
        assert ctx["ahs_session_id"] is None

    async def test_no_client_host(self) -> None:
        db_token = _make_db_token()
        request = self._make_request()
        request.client = None

        with patch(
            "ypl.mcp_server.auth_dev_token.has_permission_cached",
            new=AsyncMock(return_value=False),
        ):
            ctx = await create_request_context(db_token, request)

        assert ctx["ip_address"] is None


# ---------------------------------------------------------------------------
# DevTokenAuthMiddleware — integration with Starlette TestClient
# ---------------------------------------------------------------------------


class TestDevTokenAuthMiddleware:
    def test_unauthenticated_request_returns_401(self) -> None:
        """An unauthenticated request to a non-public path returns 401."""
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401

    def test_missing_auth_header_returns_401(self) -> None:
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 401
        data = r.json()
        assert "Missing Authorization header" in data["error"]

    def test_malformed_bearer_returns_401(self) -> None:
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Token not-bearer-format"})
        assert r.status_code == 401
        assert "Invalid Authorization header format" in r.json()["error"]

    def test_wrong_token_format_returns_401(self) -> None:
        """Token that doesn't start with yupp_dev_ is rejected."""
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping", headers={"authorization": "Bearer sk-some-other-token"})
        assert r.status_code == 401
        assert "yupp_dev_" in r.json()["error"]

    def test_invalid_token_returns_401(self) -> None:
        """A yupp_dev_ token that fails DB lookup returns 401."""
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(return_value=(None, None)),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 401
        assert "Invalid token" in r.json()["error"]

    def test_revoked_token_returns_401(self) -> None:
        from ypl.db.mcp import MCPTokenStatus

        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(return_value=(None, MCPTokenStatus.REVOKED)),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 401
        assert "revoked" in r.json()["error"].lower()

    def test_expired_token_returns_401(self) -> None:
        from ypl.db.mcp import MCPTokenStatus

        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "ypl.mcp_server.auth_dev_token.validate_token",
            new=AsyncMock(return_value=(None, MCPTokenStatus.EXPIRED)),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 401
        assert "expired" in r.json()["error"].lower()

    def test_no_permission_returns_403(self) -> None:
        """Valid token but user lacks USE_MCP → 403."""
        db_token = _make_db_token()
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch(
                "ypl.mcp_server.auth_dev_token.validate_token",
                new=AsyncMock(return_value=(db_token, None)),
            ),
            patch(
                "ypl.mcp_server.auth_dev_token.has_permission_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 403
        assert "permission" in r.json()["error"].lower()

    def test_valid_token_passes_through(self) -> None:
        """Valid token with USE_MCP permission → request passes to handler."""
        db_token = _make_db_token()
        app = _make_starlette_app()
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch(
                "ypl.mcp_server.auth_dev_token.validate_token",
                new=AsyncMock(return_value=(db_token, None)),
            ),
            patch(
                "ypl.mcp_server.auth_dev_token.has_permission_cached",
                new=AsyncMock(return_value=True),
            ),
        ):
            r = client.get(
                "/ping",
                headers={"authorization": "Bearer yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"},
            )

        assert r.status_code == 200
        assert r.json() == {"pong": True}


# ---------------------------------------------------------------------------
# validate_token (mocked DB session)
# ---------------------------------------------------------------------------


def _make_mock_session(candidates: list[MagicMock]) -> MagicMock:
    """Build a mock async session that returns *candidates* from exec().all()."""
    mock_result = MagicMock()
    mock_result.all.return_value = candidates

    mock_session = AsyncMock()
    mock_session.exec.return_value = mock_result
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestValidateToken:
    async def test_returns_none_when_no_candidates(self) -> None:
        from ypl.mcp_server.auth_dev_token import validate_token

        with patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=_make_mock_session([])):
            db_token, status = await validate_token("yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")

        assert db_token is None
        assert status is None

    async def test_returns_none_for_hash_mismatch(self) -> None:
        from ypl.mcp_server.auth_dev_token import validate_token

        # Token in DB has a hash that doesn't match
        bad_db_token = _make_db_token()
        bad_db_token.token_hash = hash_token(generate_token())  # different token's hash

        with patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=_make_mock_session([bad_db_token])):
            db_token, status = await validate_token("yupp_dev_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")

        assert db_token is None
        assert status is None

    async def test_returns_active_token(self) -> None:
        from ypl.mcp_server.auth_dev_token import validate_token

        plaintext = generate_token()
        db_token = _make_db_token()
        db_token.token_hash = hash_token(plaintext)
        db_token.expires_at = None

        with patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=_make_mock_session([db_token])):
            result_token, status = await validate_token(plaintext)

        from ypl.db.mcp import MCPTokenStatus

        assert result_token is db_token
        assert status == MCPTokenStatus.ACTIVE

    async def test_returns_revoked_status(self) -> None:
        from ypl.db.mcp import MCPTokenStatus
        from ypl.mcp_server.auth_dev_token import validate_token

        plaintext = generate_token()
        db_token = _make_db_token(status_val="REVOKED")
        db_token.token_hash = hash_token(plaintext)
        db_token.expires_at = None

        with patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=_make_mock_session([db_token])):
            result_token, status = await validate_token(plaintext)

        assert result_token is None
        assert status == MCPTokenStatus.REVOKED

    async def test_marks_expired_token(self) -> None:
        from ypl.db.mcp import MCPTokenStatus
        from ypl.mcp_server.auth_dev_token import validate_token

        plaintext = generate_token()
        db_token = _make_db_token(status_val="ACTIVE")
        db_token.token_hash = hash_token(plaintext)
        db_token.expires_at = datetime.now(UTC) - timedelta(days=1)  # already expired

        with patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=_make_mock_session([db_token])):
            result_token, status = await validate_token(plaintext)

        assert result_token is None
        assert status == MCPTokenStatus.EXPIRED


# ---------------------------------------------------------------------------
# create_token (mocked DB + settings + permissions)
# ---------------------------------------------------------------------------


class TestCreateToken:
    async def test_raises_for_disallowed_domain(self) -> None:
        from ypl.mcp_server.auth_dev_token import create_token

        with patch("ypl.mcp_server.auth_dev_token.settings") as mock_settings:
            mock_settings.ALLOWED_MCP_EMAIL_DOMAINS = ["example.com"]
            with pytest.raises(ValueError, match="not in allowed domains"):
                await create_token("user@external.com", "test", None)

    async def test_raises_for_missing_permission(self) -> None:
        from ypl.mcp_server.auth_dev_token import create_token

        with (
            patch("ypl.mcp_server.auth_dev_token.settings") as mock_settings,
            patch(
                "ypl.mcp_server.auth_dev_token.has_permission_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            mock_settings.ALLOWED_MCP_EMAIL_DOMAINS = ["example.com"]
            with pytest.raises(PermissionError, match="USE_MCP"):
                await create_token("dev@example.com", "test", None)

    async def test_creates_token_record(self) -> None:
        from ypl.db.mcp import MCPDevToken
        from ypl.mcp_server.auth_dev_token import create_token

        added: list[object] = []
        mock_session = AsyncMock()
        mock_session.add = MagicMock(side_effect=added.append)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.auth_dev_token.settings") as mock_settings,
            patch(
                "ypl.mcp_server.auth_dev_token.has_permission_cached",
                new=AsyncMock(return_value=True),
            ),
            patch("ypl.mcp_server.auth_dev_token.get_async_session", return_value=ctx),
        ):
            mock_settings.ALLOWED_MCP_EMAIL_DOMAINS = ["example.com"]
            plaintext, db_record = await create_token("dev@example.com", "my token", None)

        assert plaintext.startswith("yupp_dev_")
        token_records = [o for o in added if isinstance(o, MCPDevToken)]
        assert len(token_records) == 1
        assert token_records[0].email == "dev@example.com"
