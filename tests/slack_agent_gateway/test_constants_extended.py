"""Extended unit tests for ypl/slack_agent_gateway/constants.py.

Covers:
- Module-level constants (values/types)
- _load_agent_configs_from_env (env var parsing)
- _fetch_agent_secrets (concurrent fetch)
- get_agent_config_by_app_id / get_agent_config_by_name
- get_all_signing_secrets
- get_bot_father_config
- get_agent_service_url
- clear_config_cache
"""

from __future__ import annotations
import os
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
    _load_agent_configs_from_env,
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
# _load_agent_configs_from_env
# ---------------------------------------------------------------------------


class TestLoadAgentConfigsFromEnv:
    def test_returns_empty_dict_when_no_agents_configured(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = ""
        with patch(f"{MODULE}.settings", mock_settings):
            result = _load_agent_configs_from_env()
        assert result == {}

    def test_returns_empty_dict_when_agents_is_none(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = None
        with patch(f"{MODULE}.settings", mock_settings):
            result = _load_agent_configs_from_env()
        assert result == {}

    def test_loads_single_agent_from_env_vars(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = "giladovski"
        # Simulate missing settings attrs (env var takes priority)
        mock_settings.SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_GILADOVSKI_DISPLAY_NAME = ""

        env_vars = {
            "SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID": "A123",
            "SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN": "xoxb-tok",
            "SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET": "signing-sec",
            "SLACK_AGENT_GATEWAY_GILADOVSKI_DISPLAY_NAME": "Giladovski",
        }

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars),
        ):
            result = _load_agent_configs_from_env()

        assert "A123" in result
        config = result["A123"]
        assert config.bot_token == "xoxb-tok"
        assert config.signing_secret == "signing-sec"
        assert config.display_name == "Giladovski"

    def test_skips_agent_with_incomplete_config(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = "incomplete"
        mock_settings.SLACK_AGENT_GATEWAY_INCOMPLETE_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_INCOMPLETE_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_INCOMPLETE_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_INCOMPLETE_DISPLAY_NAME = ""

        # Only APP_ID is set, missing bot_token and signing_secret
        env_vars = {"SLACK_AGENT_GATEWAY_INCOMPLETE_APP_ID": "A999"}

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars, clear=False),
        ):
            # Remove the vars that would complete config
            os.environ.pop("SLACK_AGENT_GATEWAY_INCOMPLETE_BOT_TOKEN", None)
            os.environ.pop("SLACK_AGENT_GATEWAY_INCOMPLETE_SIGNING_SECRET", None)
            result = _load_agent_configs_from_env()

        assert result == {}

    def test_uses_agent_name_title_case_as_default_display_name(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = "mybot"
        mock_settings.SLACK_AGENT_GATEWAY_MYBOT_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYBOT_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYBOT_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYBOT_DISPLAY_NAME = ""

        env_vars = {
            "SLACK_AGENT_GATEWAY_MYBOT_APP_ID": "A001",
            "SLACK_AGENT_GATEWAY_MYBOT_BOT_TOKEN": "xoxb-tok",
            "SLACK_AGENT_GATEWAY_MYBOT_SIGNING_SECRET": "sec",
        }
        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars, clear=False),
        ):
            os.environ.pop("SLACK_AGENT_GATEWAY_MYBOT_DISPLAY_NAME", None)
            result = _load_agent_configs_from_env()

        assert "A001" in result
        assert result["A001"].display_name == "Mybot"

    def test_loads_multiple_agents(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = "agent1,agent2"
        for agent in ["AGENT1", "AGENT2"]:
            setattr(mock_settings, f"SLACK_AGENT_GATEWAY_{agent}_APP_ID", "")
            setattr(mock_settings, f"SLACK_AGENT_GATEWAY_{agent}_BOT_TOKEN", "")
            setattr(mock_settings, f"SLACK_AGENT_GATEWAY_{agent}_SIGNING_SECRET", "")
            setattr(mock_settings, f"SLACK_AGENT_GATEWAY_{agent}_DISPLAY_NAME", "")

        env_vars = {
            "SLACK_AGENT_GATEWAY_AGENT1_APP_ID": "A001",
            "SLACK_AGENT_GATEWAY_AGENT1_BOT_TOKEN": "xoxb-1",
            "SLACK_AGENT_GATEWAY_AGENT1_SIGNING_SECRET": "sec1",
            "SLACK_AGENT_GATEWAY_AGENT2_APP_ID": "A002",
            "SLACK_AGENT_GATEWAY_AGENT2_BOT_TOKEN": "xoxb-2",
            "SLACK_AGENT_GATEWAY_AGENT2_SIGNING_SECRET": "sec2",
        }

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars),
        ):
            result = _load_agent_configs_from_env()

        assert "A001" in result
        assert "A002" in result

    def test_strips_whitespace_from_agent_names(self) -> None:
        mock_settings = MagicMock()
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = " myagent , otheragent "
        # Only myagent has complete config
        mock_settings.SLACK_AGENT_GATEWAY_MYAGENT_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYAGENT_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYAGENT_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_MYAGENT_DISPLAY_NAME = ""
        mock_settings.SLACK_AGENT_GATEWAY_OTHERAGENT_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_OTHERAGENT_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_OTHERAGENT_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_OTHERAGENT_DISPLAY_NAME = ""

        env_vars = {
            "SLACK_AGENT_GATEWAY_MYAGENT_APP_ID": "AXX",
            "SLACK_AGENT_GATEWAY_MYAGENT_BOT_TOKEN": "xoxb-yy",
            "SLACK_AGENT_GATEWAY_MYAGENT_SIGNING_SECRET": "sec-yy",
        }
        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars, clear=False),
        ):
            os.environ.pop("SLACK_AGENT_GATEWAY_OTHERAGENT_APP_ID", None)
            result = _load_agent_configs_from_env()

        assert "AXX" in result


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
# get_agent_configs — local env path
# ---------------------------------------------------------------------------


class TestGetAgentConfigs:
    async def test_local_env_uses_env_vars(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = "localbot"
        mock_settings.SLACK_AGENT_GATEWAY_LOCALBOT_APP_ID = ""
        mock_settings.SLACK_AGENT_GATEWAY_LOCALBOT_BOT_TOKEN = ""
        mock_settings.SLACK_AGENT_GATEWAY_LOCALBOT_SIGNING_SECRET = ""
        mock_settings.SLACK_AGENT_GATEWAY_LOCALBOT_DISPLAY_NAME = ""

        env_vars = {
            "SLACK_AGENT_GATEWAY_LOCALBOT_APP_ID": "ALOC",
            "SLACK_AGENT_GATEWAY_LOCALBOT_BOT_TOKEN": "xoxb-local",
            "SLACK_AGENT_GATEWAY_LOCALBOT_SIGNING_SECRET": "local-sec",
        }

        with (
            patch(f"{MODULE}.settings", mock_settings),
            patch.dict(os.environ, env_vars),
        ):
            clear_config_cache()
            result = await get_agent_configs()

        assert "ALOC" in result
        # Reset cache after test
        clear_config_cache()

    async def test_test_env_uses_env_vars(self) -> None:
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "test"
        mock_settings.SLACK_AGENT_GATEWAY_AGENTS = ""

        with patch(f"{MODULE}.settings", mock_settings):
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
