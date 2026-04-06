"""Unit tests for ypl/mcp_server/auth_oauth.py.

Covers:
  - is_allowed_email_domain() — domain validation, case insensitivity
  - AllowedDomainsGoogleProvider.verify_token():
      - Parent returns None (invalid token)
      - Token has no email claim
      - Email domain not allowed
      - User lacks USE_MCP permission
      - Successful verification — sets request_context
  - create_oauth_provider() — raises on missing config

All tests run without live OAuth, Redis, or database connections.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.mcp_server.auth_oauth import AllowedDomainsGoogleProvider, is_allowed_email_domain

# ---------------------------------------------------------------------------
# is_allowed_email_domain
# ---------------------------------------------------------------------------


class TestIsAllowedEmailDomain:
    def _patch_settings(self, allowed: list[str]) -> MagicMock:
        mock_settings = MagicMock()
        mock_settings.ALLOWED_MCP_EMAIL_DOMAINS = allowed
        return mock_settings

    def test_allowed_domain(self) -> None:
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            assert is_allowed_email_domain("dev@yupp.ai") is True

    def test_disallowed_domain(self) -> None:
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            assert is_allowed_email_domain("user@gmail.com") is False

    def test_case_insensitive(self) -> None:
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["Yupp.AI"]
            assert is_allowed_email_domain("dev@YUPP.AI") is True

    def test_empty_email(self) -> None:
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            assert is_allowed_email_domain("") is False

    def test_multiple_allowed_domains(self) -> None:
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai", "partner.com"]
            assert is_allowed_email_domain("user@partner.com") is True
            assert is_allowed_email_domain("user@other.com") is False

    def test_subdomain_not_matched(self) -> None:
        """Exact domain match only — sub.yupp.ai is NOT allowed when only yupp.ai is."""
        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            assert is_allowed_email_domain("user@sub.yupp.ai") is False


# ---------------------------------------------------------------------------
# AllowedDomainsGoogleProvider.verify_token
# ---------------------------------------------------------------------------


def _make_provider() -> AllowedDomainsGoogleProvider:
    """Build a provider instance without hitting real OAuth infra."""
    with patch("fastmcp.server.auth.providers.google.GoogleProvider.__init__", return_value=None):
        return AllowedDomainsGoogleProvider.__new__(AllowedDomainsGoogleProvider)


class TestAllowedDomainsGoogleProviderVerifyToken:
    async def test_returns_none_when_parent_returns_none(self) -> None:
        provider = _make_provider()

        with (
            patch.object(AllowedDomainsGoogleProvider, "verify_token", wraps=None),
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "bad-token")

        assert result is None

    async def test_returns_none_when_no_email_claim(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = {}  # no email key

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "token")

        assert result is None

    async def test_returns_none_for_disallowed_domain(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = {"email": "user@external.com"}

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "token")

        assert result is None

    async def test_returns_none_when_no_permission(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = {"email": "dev@yupp.ai"}

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
            patch(
                "ypl.mcp_server.auth_oauth.has_permission_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "token")

        assert result is None

    async def test_successful_verification_returns_token(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = {"email": "dev@yupp.ai"}
        mock_token.client_id = "https://callback.example.com"

        from ypl.mcp_server.context_vars import request_context

        # Reset context before test
        request_context.set(None)

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
            patch(
                "ypl.mcp_server.auth_oauth.has_permission_cached",
                new=AsyncMock(return_value=True),
            ),
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "valid-token")

        assert result is mock_token

    async def test_successful_verification_sets_request_context(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = {"email": "dev@yupp.ai"}
        mock_token.client_id = "https://callback.example.com"

        from ypl.db.mcp import MCPTokenType
        from ypl.mcp_server.context_vars import request_context

        request_context.set(None)

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
            patch(
                "ypl.mcp_server.auth_oauth.has_permission_cached",
                new=AsyncMock(return_value=True),
            ),
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            await AllowedDomainsGoogleProvider.verify_token(provider, "valid-token")

        ctx = request_context.get()
        assert ctx is not None
        assert ctx["email"] == "dev@yupp.ai"
        assert ctx["token_type"] == MCPTokenType.OAUTH
        assert ctx["callback_url"] == "https://callback.example.com"

    async def test_none_claims_treated_as_missing_email(self) -> None:
        provider = _make_provider()
        mock_token = MagicMock()
        mock_token.claims = None  # claims is None

        with (
            patch(
                "fastmcp.server.auth.providers.google.GoogleProvider.verify_token",
                new=AsyncMock(return_value=mock_token),
            ),
            patch("ypl.mcp_server.auth_oauth.settings") as s,
        ):
            s.ALLOWED_MCP_EMAIL_DOMAINS = ["yupp.ai"]
            result = await AllowedDomainsGoogleProvider.verify_token(provider, "token")

        assert result is None


# ---------------------------------------------------------------------------
# create_oauth_provider — config validation
# ---------------------------------------------------------------------------


class TestCreateOAuthProvider:
    def test_raises_when_google_credentials_missing(self) -> None:
        from ypl.mcp_server.auth_oauth import create_oauth_provider

        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.MCP_OAUTH_GOOGLE_CLIENT_ID = ""
            s.MCP_OAUTH_GOOGLE_CLIENT_SECRET = ""
            s.MCP_OAUTH_JWT_SIGNING_KEY = "some-key"
            s.MCP_OAUTH_STORAGE_ENCRYPTION_KEY = "some-enc-key"
            with pytest.raises(ValueError, match="Google OAuth credentials"):
                create_oauth_provider()

    def test_raises_when_jwt_key_missing(self) -> None:
        from ypl.mcp_server.auth_oauth import create_oauth_provider

        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.MCP_OAUTH_GOOGLE_CLIENT_ID = "client-id"
            s.MCP_OAUTH_GOOGLE_CLIENT_SECRET = "client-secret"
            s.MCP_OAUTH_JWT_SIGNING_KEY = ""
            s.MCP_OAUTH_STORAGE_ENCRYPTION_KEY = "some-enc-key"
            with pytest.raises(ValueError, match="JWT signing key"):
                create_oauth_provider()

    def test_raises_when_encryption_key_missing(self) -> None:
        from ypl.mcp_server.auth_oauth import create_oauth_provider

        with patch("ypl.mcp_server.auth_oauth.settings") as s:
            s.MCP_OAUTH_GOOGLE_CLIENT_ID = "client-id"
            s.MCP_OAUTH_GOOGLE_CLIENT_SECRET = "client-secret"
            s.MCP_OAUTH_JWT_SIGNING_KEY = "jwt-key"
            s.MCP_OAUTH_STORAGE_ENCRYPTION_KEY = ""
            with pytest.raises(ValueError, match="storage encryption key"):
                create_oauth_provider()
