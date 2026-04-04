"""Unit tests for SAG secrets module.

Tests cover naming helpers, env-var caching, GCP Secret Manager fetching,
fetch_agent_secret precedence logic, and write operations (create_agent_secret,
update_bot_father_refresh_token). GCP client is mocked via AsyncMock.
"""

from __future__ import annotations
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as core_exceptions
from ypl.slack_agent_gateway.secrets import (
    _build_env_var_name,
    _build_secret_name,
    _cache_secret_in_env,
    _fetch_gcp_secret,
    _get_env_var_secret,
    create_agent_secret,
    fetch_agent_secret,
    update_bot_father_refresh_token,
)

# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


class TestBuildSecretName:
    def test_builds_correct_name(self) -> None:
        result = _build_secret_name("giladovski", "bot-token", "staging")
        assert result == "ym-slack-agent-gateway-giladovski-bot-token-staging"

    def test_lowercases_agent_name(self) -> None:
        result = _build_secret_name("GILADOVSKI", "app-id", "production")
        assert result == "ym-slack-agent-gateway-giladovski-app-id-production"

    def test_different_secret_types(self) -> None:
        for secret_type in ("app-id", "bot-token", "signing-secret"):
            result = _build_secret_name("myagent", secret_type, "staging")
            assert secret_type in result


class TestBuildEnvVarName:
    def test_bot_token_name(self) -> None:
        result = _build_env_var_name("giladovski", "bot-token")
        assert result == "SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN"

    def test_app_id_name(self) -> None:
        result = _build_env_var_name("giladovski", "app-id")
        assert result == "SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID"

    def test_signing_secret_name(self) -> None:
        result = _build_env_var_name("giladovski", "signing-secret")
        assert result == "SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET"

    def test_uppercases_agent_name(self) -> None:
        result = _build_env_var_name("myagent", "bot-token")
        assert result.startswith("SLACK_AGENT_GATEWAY_MYAGENT_")


class TestGetEnvVarSecret:
    def test_returns_env_var_value(self) -> None:
        with patch.dict(os.environ, {"SLACK_AGENT_GATEWAY_TESTAGENT_BOT_TOKEN": "test-token"}):
            result = _get_env_var_secret("testagent", "bot-token")
        assert result == "test-token"

    def test_returns_none_when_not_set(self) -> None:
        env_var = "SLACK_AGENT_GATEWAY_NOAGENT_BOT_TOKEN"
        with patch.dict(os.environ, {}, clear=True):
            # Ensure it's not in env
            os.environ.pop(env_var, None)
            result = _get_env_var_secret("noagent", "bot-token")
        assert result is None

    def test_returns_settings_value_as_fallback(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_MYAGENT_BOT_TOKEN = "settings-token"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
        ):
            # Make sure env var is not set
            os.environ.pop("SLACK_AGENT_GATEWAY_MYAGENT_BOT_TOKEN", None)
            result = _get_env_var_secret("myagent", "bot-token")
        assert result == "settings-token"


class TestCacheSecretInEnv:
    def test_sets_env_var(self) -> None:
        _cache_secret_in_env("newagent", "bot-token", "xoxb-12345")
        assert os.environ.get("SLACK_AGENT_GATEWAY_NEWAGENT_BOT_TOKEN") == "xoxb-12345"
        # Cleanup
        os.environ.pop("SLACK_AGENT_GATEWAY_NEWAGENT_BOT_TOKEN", None)

    def test_overwrites_existing_value(self) -> None:
        with patch.dict(os.environ, {"SLACK_AGENT_GATEWAY_OVERWRITE_BOT_TOKEN": "old-value"}):
            _cache_secret_in_env("overwrite", "bot-token", "new-value")
            assert os.environ.get("SLACK_AGENT_GATEWAY_OVERWRITE_BOT_TOKEN") == "new-value"


# ---------------------------------------------------------------------------
# _fetch_gcp_secret
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_gcp_client() -> AsyncMock:
    client = AsyncMock()
    response = MagicMock()
    response.payload.data = b"super-secret-value"
    client.access_secret_version.return_value = response
    return client


class TestFetchGcpSecret:
    async def test_returns_none_when_project_id_not_set(self) -> None:
        mock_settings = MagicMock()
        mock_settings.GCP_PROJECT_ID = None

        with patch("ypl.slack_agent_gateway.secrets.settings", mock_settings):
            result = await _fetch_gcp_secret("some-secret")

        assert result is None

    async def test_fetches_secret_successfully(self, mock_gcp_client: AsyncMock) -> None:
        mock_settings = MagicMock()
        mock_settings.GCP_PROJECT_ID = "my-project"

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_gcp_client),
        ):
            result = await _fetch_gcp_secret("some-secret")

        assert result == "super-secret-value"
        mock_gcp_client.access_secret_version.assert_awaited_once()
        # Verify correct secret path
        call_kwargs = mock_gcp_client.access_secret_version.call_args
        req = call_kwargs[1].get("request") or call_kwargs[0][0]
        assert "my-project" in req["name"]
        assert "some-secret" in req["name"]

    async def test_returns_none_on_not_found(self, mock_gcp_client: AsyncMock) -> None:
        mock_settings = MagicMock()
        mock_settings.GCP_PROJECT_ID = "my-project"
        mock_gcp_client.access_secret_version.side_effect = core_exceptions.NotFound("404")  # type: ignore[no-untyped-call]

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_gcp_client),
        ):
            result = await _fetch_gcp_secret("missing-secret")

        assert result is None

    async def test_returns_none_on_generic_error(self, mock_gcp_client: AsyncMock) -> None:
        mock_settings = MagicMock()
        mock_settings.GCP_PROJECT_ID = "my-project"
        mock_gcp_client.access_secret_version.side_effect = Exception("unexpected")

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_gcp_client),
        ):
            result = await _fetch_gcp_secret("failing-secret")

        assert result is None


# ---------------------------------------------------------------------------
# fetch_agent_secret
# ---------------------------------------------------------------------------


class TestFetchAgentSecret:
    async def test_returns_env_var_value_immediately(self) -> None:
        """Env var hit: should return without GCP call."""
        with (
            patch.dict(os.environ, {"SLACK_AGENT_GATEWAY_TESTAGENT_BOT_TOKEN": "env-token"}),
            patch("ypl.slack_agent_gateway.secrets._fetch_gcp_secret", new_callable=AsyncMock) as mock_gcp,
        ):
            result = await fetch_agent_secret("testagent", "bot-token")

        assert result == "env-token"
        mock_gcp.assert_not_awaited()

    async def test_returns_none_in_local_env_when_no_env_var(self) -> None:
        """In local/test env, GCP should never be called."""
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        mock_settings.SLACK_AGENT_GATEWAY_NOAGENT_BOT_TOKEN = None

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch.dict(os.environ, {}, clear=True),
            patch("ypl.slack_agent_gateway.secrets._fetch_gcp_secret", new_callable=AsyncMock) as mock_gcp,
        ):
            os.environ.pop("SLACK_AGENT_GATEWAY_NOAGENT_BOT_TOKEN", None)
            result = await fetch_agent_secret("noagent", "bot-token")

        assert result is None
        mock_gcp.assert_not_awaited()

    async def test_fetches_from_gcp_in_staging(self) -> None:
        """In staging, should fall through to GCP when no env var."""
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.GCP_PROJECT_ID = "yupp-project"
        mock_settings.SLACK_AGENT_GATEWAY_STAGEAGENT_BOT_TOKEN = None

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ypl.slack_agent_gateway.secrets._fetch_gcp_secret",
                new_callable=AsyncMock,
                return_value="gcp-token",
            ) as mock_gcp,
        ):
            os.environ.pop("SLACK_AGENT_GATEWAY_STAGEAGENT_BOT_TOKEN", None)
            result = await fetch_agent_secret("stageagent", "bot-token")

            assert result == "gcp-token"
            mock_gcp.assert_awaited_once()
            # Should cache the fetched value in env var
            assert os.environ.get("SLACK_AGENT_GATEWAY_STAGEAGENT_BOT_TOKEN") == "gcp-token"

    async def test_returns_none_when_gcp_has_no_secret(self) -> None:
        """GCP returns None: result should be None."""
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.SLACK_AGENT_GATEWAY_PROD_AGENT_BOT_TOKEN = None

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch.dict(os.environ, {}, clear=True),
            patch(
                "ypl.slack_agent_gateway.secrets._fetch_gcp_secret",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            os.environ.pop("SLACK_AGENT_GATEWAY_PROD_AGENT_BOT_TOKEN", None)
            result = await fetch_agent_secret("prod_agent", "bot-token")

        assert result is None


# ---------------------------------------------------------------------------
# create_agent_secret
# ---------------------------------------------------------------------------


class TestCreateAgentSecret:
    async def test_noop_in_local_env(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client") as mock_factory,
        ):
            await create_agent_secret("testagent", "bot-token", "xoxb-123")

        mock_factory.assert_not_called()

    async def test_noop_in_test_env(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "test"

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client") as mock_factory,
        ):
            await create_agent_secret("testagent", "bot-token", "xoxb-123")

        mock_factory.assert_not_called()

    async def test_raises_when_no_project_id(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.GCP_PROJECT_ID = None

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            pytest.raises(RuntimeError, match="GCP_PROJECT_ID not set"),
        ):
            await create_agent_secret("testagent", "bot-token", "xoxb-123")

    async def test_creates_secret_in_gcp(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.GCP_PROJECT_ID = "yupp-project"

        mock_client = AsyncMock()

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_client),
        ):
            await create_agent_secret("newagent", "signing-secret", "abcdef")

        mock_client.create_secret.assert_awaited_once()
        mock_client.add_secret_version.assert_awaited_once()
        # Verify payload
        add_call_kwargs = mock_client.add_secret_version.call_args[1] or mock_client.add_secret_version.call_args[0][0]
        payload_data = add_call_kwargs.get("request", {}).get("payload", {}).get("data", b"")
        assert payload_data == b"abcdef"
        # Cleanup env var
        os.environ.pop("SLACK_AGENT_GATEWAY_NEWAGENT_SIGNING_SECRET", None)

    async def test_handles_already_exists_error_gracefully(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.GCP_PROJECT_ID = "yupp-project"

        mock_client = AsyncMock()
        mock_client.create_secret.side_effect = core_exceptions.AlreadyExists("already exists")  # type: ignore[no-untyped-call]

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_client),
        ):
            # Should not raise; AlreadyExists is handled
            await create_agent_secret("existingagent", "bot-token", "xoxb-existing")

        mock_client.add_secret_version.assert_awaited_once()
        os.environ.pop("SLACK_AGENT_GATEWAY_EXISTINGAGENT_BOT_TOKEN", None)

    async def test_raises_on_create_error(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.GCP_PROJECT_ID = "yupp-project"

        mock_client = AsyncMock()
        mock_client.create_secret.side_effect = Exception("permission denied")

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_client),
            pytest.raises(RuntimeError, match="Failed to create GCP secret"),
        ):
            await create_agent_secret("failagent", "bot-token", "xoxb-fail")


# ---------------------------------------------------------------------------
# update_bot_father_refresh_token
# ---------------------------------------------------------------------------


class TestUpdateBotFatherRefreshToken:
    async def test_noop_in_local_env(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client") as mock_factory,
        ):
            await update_bot_father_refresh_token("new-token")

        mock_factory.assert_not_called()

    async def test_noop_in_test_env(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "test"

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client") as mock_factory,
        ):
            await update_bot_father_refresh_token("new-token")

        mock_factory.assert_not_called()

    async def test_raises_when_no_project_id(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.GCP_PROJECT_ID = None

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            pytest.raises(RuntimeError, match="GCP_PROJECT_ID not set"),
        ):
            await update_bot_father_refresh_token("new-token")

    async def test_updates_secret_in_gcp(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.GCP_PROJECT_ID = "yupp-project"
        mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = "old-token"

        mock_client = AsyncMock()

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_client),
        ):
            await update_bot_father_refresh_token("xoxe-1-new-token")

        mock_client.add_secret_version.assert_awaited_once()
        # Verify payload
        call_kwargs = mock_client.add_secret_version.call_args
        req = call_kwargs[1].get("request") or call_kwargs[0][0]
        assert req["payload"]["data"] == b"xoxe-1-new-token"
        # Verify in-memory settings updated
        assert mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN == "xoxe-1-new-token"

    async def test_raises_on_gcp_error(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "production"
        mock_settings.GCP_PROJECT_ID = "yupp-project"

        mock_client = AsyncMock()
        mock_client.add_secret_version.side_effect = Exception("write failed")

        with (
            patch("ypl.slack_agent_gateway.secrets.settings", mock_settings),
            patch("ypl.slack_agent_gateway.secrets._get_async_secret_manager_client", return_value=mock_client),
            pytest.raises(RuntimeError, match="Failed to update Bot Father refresh token"),
        ):
            await update_bot_father_refresh_token("new-token")
