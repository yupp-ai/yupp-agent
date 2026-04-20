"""Extended unit tests for ypl/slack_agent_gateway/constants.py.

Covers:
- Module-level constants (values/types)
- _fetch_agent_secrets (concurrent fetch)
- get_agent_configs (DB-backed; empty + non-empty paths)
- get_agent_config_by_app_id / get_agent_config_by_name
- get_all_signing_secrets
- get_bot_father_config
- get_agent_service_url
- clear_config_cache
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.slack_agent_gateway.constants import (
    BUFFER_TTL_SECONDS,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_SESSION_EXPIRATION_SECONDS,
    EVENT_DEDUP_TTL_SECONDS,
    FEEDBACK_REQUESTED_TTL_SECONDS,
    MAX_BUFFER_SIZE_CHARS,
    MAX_QUEUE_LENGTH,
    QUEUE_TTL_SECONDS,
    REDIS_KEY_PREFIX_BUFFER,
    REDIS_KEY_PREFIX_EVENT,
    REDIS_KEY_PREFIX_QUEUE,
    REDIS_KEY_PREFIX_SESSION,
    REPLY_MAPPING_TTL_SECONDS,
    SESSION_REDIS_TTL_SECONDS,
    STATUS_PENDING_TTL_SECONDS,
    STATUS_RATELIMIT_SECONDS,
    SURVEY_RESPONSE_TTL_SECONDS,
    THREAD_SESSION_MAPPING_TTL_SECONDS,
    TOOL_ENTRIES_TTL_SECONDS,
    _fetch_agent_secrets,
    clear_config_cache,
    get_agent_config_by_app_id,
    get_agent_config_by_name,
    get_agent_configs,
    get_agent_service_url,
    get_all_signing_secrets,
    get_bot_father_config,
)

MODULE = "ypl.slack_agent_gateway.constants"


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_session_expiration_is_30_minutes(self) -> None:
        assert DEFAULT_SESSION_EXPIRATION_SECONDS == 30 * 60

    def test_session_redis_ttl_is_24_hours(self) -> None:
        assert SESSION_REDIS_TTL_SECONDS == 24 * 60 * 60

    def test_event_dedup_ttl_is_5_minutes(self) -> None:
        assert EVENT_DEDUP_TTL_SECONDS == 5 * 60

    def test_buffer_ttl_is_5_minutes(self) -> None:
        assert BUFFER_TTL_SECONDS == 5 * 60

    def test_queue_ttl_is_1_hour(self) -> None:
        assert QUEUE_TTL_SECONDS == 60 * 60

    def test_max_queue_length_is_positive(self) -> None:
        assert MAX_QUEUE_LENGTH > 0

    def test_default_flush_interval_is_positive(self) -> None:
        assert DEFAULT_FLUSH_INTERVAL_SECONDS > 0

    def test_max_buffer_size_chars_is_positive(self) -> None:
        assert MAX_BUFFER_SIZE_CHARS > 0

    def test_redis_key_prefixes_have_namespace(self) -> None:
        for prefix in [
            REDIS_KEY_PREFIX_SESSION,
            REDIS_KEY_PREFIX_EVENT,
            REDIS_KEY_PREFIX_BUFFER,
            REDIS_KEY_PREFIX_QUEUE,
        ]:
            assert prefix.startswith("slack_agent_gw:")

    def test_thread_session_mapping_ttl_matches_session_ttl(self) -> None:
        assert THREAD_SESSION_MAPPING_TTL_SECONDS == SESSION_REDIS_TTL_SECONDS

    def test_reply_mapping_ttl_matches_session_ttl(self) -> None:
        assert REPLY_MAPPING_TTL_SECONDS == SESSION_REDIS_TTL_SECONDS

    def test_feedback_requested_ttl_is_24_hours(self) -> None:
        assert FEEDBACK_REQUESTED_TTL_SECONDS == 24 * 60 * 60

    def test_survey_response_ttl_is_1_hour(self) -> None:
        assert SURVEY_RESPONSE_TTL_SECONDS == 60 * 60

    def test_status_ratelimit_is_positive(self) -> None:
        assert STATUS_RATELIMIT_SECONDS > 0

    def test_status_pending_ttl_is_positive(self) -> None:
        assert STATUS_PENDING_TTL_SECONDS > 0

    def test_tool_entries_ttl_is_24_hours(self) -> None:
        assert TOOL_ENTRIES_TTL_SECONDS == 24 * 60 * 60


# ---------------------------------------------------------------------------
# _fetch_agent_secrets
# ---------------------------------------------------------------------------


class TestFetchAgentSecrets:
    async def test_returns_bot_token_and_signing_secret(self) -> None:
        async def _mock_fetch(agent_name: str, secret_type: str) -> str | None:
            if secret_type == "bot-token":
                return "xoxb-tok"
            if secret_type == "signing-secret":
                return "signing-sec"
            return None

        with patch(f"{MODULE}.fetch_agent_secret", side_effect=_mock_fetch):
            bot_token, signing_secret = await _fetch_agent_secrets("giladovski")

        assert bot_token == "xoxb-tok"
        assert signing_secret == "signing-sec"

    async def test_returns_none_when_secret_not_found(self) -> None:
        with patch(f"{MODULE}.fetch_agent_secret", new_callable=AsyncMock, return_value=None):
            bot_token, signing_secret = await _fetch_agent_secrets("noagent")

        assert bot_token is None
        assert signing_secret is None


# ---------------------------------------------------------------------------
# get_agent_configs — DB-backed single path
# ---------------------------------------------------------------------------


class TestGetAgentConfigs:
    async def test_returns_configs_from_database(self) -> None:
        from ypl.slack_agent_gateway.types import AgentAppConfig

        fake = {
            "A001": AgentAppConfig(
                app_id="A001",
                agent_name="examplebot",
                slack_name="examplebot",
                bot_token="xoxb-tok",
                signing_secret="sec",
                display_name="ExampleBot",
            )
        }
        with patch(f"{MODULE}._load_agent_configs_from_database", new_callable=AsyncMock, return_value=fake):
            clear_config_cache()
            result = await get_agent_configs()

        assert result == fake
        clear_config_cache()

    async def test_returns_empty_when_database_empty(self) -> None:
        with patch(f"{MODULE}._load_agent_configs_from_database", new_callable=AsyncMock, return_value={}):
            clear_config_cache()
            result = await get_agent_configs()

        assert result == {}
        clear_config_cache()


# ---------------------------------------------------------------------------
# get_agent_config_by_app_id / get_agent_config_by_name
# ---------------------------------------------------------------------------


class TestGetAgentConfigLookups:
    async def test_get_by_app_id_returns_config(self) -> None:
        from ypl.slack_agent_gateway.types import AgentAppConfig

        mock_config = AgentAppConfig(
            app_id="A111",
            agent_name="testbot",
            slack_name="testbot",
            bot_token="xoxb-test",
            signing_secret="sec",
            display_name="Test Bot",
        )

        with patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={"A111": mock_config}):
            result = await get_agent_config_by_app_id("A111")

        assert result is not None
        assert result.app_id == "A111"

    async def test_get_by_app_id_returns_none_for_unknown_id(self) -> None:
        with patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={}):
            result = await get_agent_config_by_app_id("UNKNOWN")

        assert result is None

    async def test_get_by_name_returns_config(self) -> None:
        from ypl.slack_agent_gateway.types import AgentAppConfig

        mock_config = AgentAppConfig(
            app_id="A222",
            agent_name="giladovski",
            slack_name="giladovski",
            bot_token="xoxb-g",
            signing_secret="gsec",
            display_name="Giladovski",
        )

        with patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={"A222": mock_config}):
            result = await get_agent_config_by_name("giladovski")

        assert result is not None
        assert result.agent_name == "giladovski"

    async def test_get_by_name_is_case_insensitive(self) -> None:
        from ypl.slack_agent_gateway.types import AgentAppConfig

        mock_config = AgentAppConfig(
            app_id="A333",
            agent_name="myagent",
            slack_name="myagent",
            bot_token="xoxb-m",
            signing_secret="msec",
            display_name="My Agent",
        )

        with patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={"A333": mock_config}):
            result = await get_agent_config_by_name("MYAGENT")

        assert result is not None

    async def test_get_by_name_returns_none_for_unknown_name(self) -> None:
        with patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={}):
            result = await get_agent_config_by_name("unknown-agent")

        assert result is None


# ---------------------------------------------------------------------------
# get_all_signing_secrets
# ---------------------------------------------------------------------------


class TestGetAllSigningSecrets:
    async def test_includes_agent_secrets(self) -> None:
        from ypl.slack_agent_gateway.types import AgentAppConfig

        mock_config = AgentAppConfig(
            app_id="A001",
            agent_name="bot",
            slack_name="bot",
            bot_token="xoxb-tok",
            signing_secret="agent-secret",
            display_name="Bot",
        )
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = "father-secret"

        with (
            patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={"A001": mock_config}),
            patch(f"{MODULE}.settings", mock_settings),
        ):
            secrets = await get_all_signing_secrets()

        assert "agent-secret" in secrets
        assert "father-secret" in secrets

    async def test_includes_bot_father_secret(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = "bf-signing-sec"

        with (
            patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={}),
            patch(f"{MODULE}.settings", mock_settings),
        ):
            secrets = await get_all_signing_secrets()

        assert "bf-signing-sec" in secrets

    async def test_excludes_bot_father_when_not_configured(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = ""

        with (
            patch(f"{MODULE}.get_agent_configs", new_callable=AsyncMock, return_value={}),
            patch(f"{MODULE}.settings", mock_settings),
        ):
            secrets = await get_all_signing_secrets()

        assert "" not in secrets

    async def test_raises_when_get_agent_configs_fails(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = ""

        with (
            patch(
                f"{MODULE}.get_agent_configs",
                new_callable=AsyncMock,
                side_effect=Exception("DB down"),
            ),
            patch(f"{MODULE}.settings", mock_settings),
            pytest.raises(Exception, match="DB down"),
        ):
            await get_all_signing_secrets()


# ---------------------------------------------------------------------------
# get_bot_father_config
# ---------------------------------------------------------------------------


class TestGetBotFatherConfig:
    def test_returns_dict_with_required_keys(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_BOT_TOKEN = "xoxb-bf"
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = "bf-sec"
        mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = "xoxe-1-refresh"
        mock_settings.SLACK_BOT_FATHER_APPROVAL_CHANNEL = "C-approval"

        with patch(f"{MODULE}.settings", mock_settings):
            config = get_bot_father_config()

        assert "bot_token" in config
        assert "signing_secret" in config
        assert "app_config_refresh_token" in config
        assert "approval_channel" in config

    def test_returns_correct_values(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_BOT_FATHER_BOT_TOKEN = "xoxb-father-token"
        mock_settings.SLACK_BOT_FATHER_SIGNING_SECRET = "father-signing"
        mock_settings.SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN = "xoxe-1-father-refresh"
        mock_settings.SLACK_BOT_FATHER_APPROVAL_CHANNEL = "C0APPROVAL"

        with patch(f"{MODULE}.settings", mock_settings):
            config = get_bot_father_config()

        assert config["bot_token"] == "xoxb-father-token"
        assert config["signing_secret"] == "father-signing"
        assert config["app_config_refresh_token"] == "xoxe-1-father-refresh"
        assert config["approval_channel"] == "C0APPROVAL"


# ---------------------------------------------------------------------------
# get_agent_service_url
# ---------------------------------------------------------------------------


class TestGetAgentServiceUrl:
    def test_returns_configured_url(self) -> None:
        mock_settings = MagicMock()
        mock_settings.AGENT_HARNESS_SERVICE_BASE_URL = "http://localhost:8090"

        with patch(f"{MODULE}.settings", mock_settings):
            url = get_agent_service_url()

        assert url == "http://localhost:8090"

    def test_returns_empty_string_when_not_configured(self) -> None:
        mock_settings = MagicMock()
        mock_settings.AGENT_HARNESS_SERVICE_BASE_URL = ""

        with patch(f"{MODULE}.settings", mock_settings):
            url = get_agent_service_url()

        assert url == ""


# ---------------------------------------------------------------------------
# clear_config_cache
# ---------------------------------------------------------------------------


class TestClearConfigCache:
    def test_clear_config_cache_does_not_raise(self) -> None:
        # Just ensure it can be called without error
        clear_config_cache()
