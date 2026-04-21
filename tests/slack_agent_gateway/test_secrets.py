"""Unit tests for the env-var fallback helpers in
``ypl.slack_agent_gateway.secrets``. The DB-backed secret path is covered by
``test_constants_extended.py::Test_resolve_agent_secrets`` (and the loader
integration tests nearby).
"""

from __future__ import annotations
import os
from unittest.mock import patch

from ypl.slack_agent_gateway.secrets import _build_env_var_name, get_env_var_secret


class TestBuildEnvVarName:
    def test_bot_token_name(self) -> None:
        assert _build_env_var_name("giladovski", "bot-token") == "SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN"

    def test_signing_secret_name(self) -> None:
        assert _build_env_var_name("giladovski", "signing-secret") == "SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET"

    def test_uppercases_bot_name(self) -> None:
        assert _build_env_var_name("myagent", "bot-token").startswith("SLACK_AGENT_GATEWAY_MYAGENT_")

    def test_dashes_in_bot_name_become_underscores(self) -> None:
        # Env var names can't contain dashes.
        assert _build_env_var_name("eng-raccoon", "bot-token") == "SLACK_AGENT_GATEWAY_ENG_RACCOON_BOT_TOKEN"


class TestGetEnvVarSecret:
    def test_reads_from_os_environ_first(self) -> None:
        env = {"SLACK_AGENT_GATEWAY_LOCALBOT_BOT_TOKEN": "xoxb-env"}
        with patch.dict(os.environ, env, clear=False):
            assert get_env_var_secret("localbot", "bot-token") == "xoxb-env"

    def test_returns_none_when_missing(self) -> None:
        # Clear any pre-existing value from the test harness.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLACK_AGENT_GATEWAY_NOSUCHBOT_BOT_TOKEN", None)
            assert get_env_var_secret("nosuchbot", "bot-token") is None

    def test_falls_back_to_settings_attr(self) -> None:
        # Simulate a value supplied via pydantic-settings .env loading rather
        # than process env.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLACK_AGENT_GATEWAY_ATTRBOT_BOT_TOKEN", None)
            with patch(
                "ypl.slack_agent_gateway.secrets.settings",
                type("S", (), {"SLACK_AGENT_GATEWAY_ATTRBOT_BOT_TOKEN": "xoxb-from-settings"})(),
            ):
                assert get_env_var_secret("attrbot", "bot-token") == "xoxb-from-settings"
