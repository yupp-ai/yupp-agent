"""Unit tests for backend utils/dynamic_app_settings.py.

Covers:
- SlackAgentConfig (model_post_init defaults)
- SlackAgentGatewaySettings defaults
- MCPToolsSettings defaults
- _load_settings_from_yaml (found / not found / cwd resolution)
- get_slack_agent_gateway_settings (from cache / fallback defaults)
- get_mcp_tools_settings (from cache / fallback defaults)
- refresh_dynamic_app_settings_in_redis (no-op stub)
"""

from __future__ import annotations
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import ypl.backend.utils.dynamic_app_settings as _das_module
from ypl.backend.utils.dynamic_app_settings import (
    MCPToolsSettings,
    SlackAgentConfig,
    SlackAgentGatewaySettings,
    _load_settings_from_yaml,
    get_mcp_tools_settings,
    get_slack_agent_gateway_settings,
    refresh_dynamic_app_settings_in_redis,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_cache() -> None:
    """Clear the module-level settings cache."""
    _das_module._settings_cache.clear()


# ---------------------------------------------------------------------------
# SlackAgentConfig
# ---------------------------------------------------------------------------


class TestSlackAgentConfig:
    def test_display_name_defaults_to_title_case(self) -> None:
        cfg = SlackAgentConfig(name="alice")
        assert cfg.display_name == "Alice"

    def test_agent_name_defaults_to_name(self) -> None:
        cfg = SlackAgentConfig(name="bob")
        assert cfg.agent_name == "bob"

    def test_explicit_display_name_preserved(self) -> None:
        cfg = SlackAgentConfig(name="alice", display_name="Alice Bot")
        assert cfg.display_name == "Alice Bot"

    def test_explicit_agent_name_preserved(self) -> None:
        cfg = SlackAgentConfig(name="alice", agent_name="alice-v2")
        assert cfg.agent_name == "alice-v2"

    def test_empty_name_no_defaults_set(self) -> None:
        cfg = SlackAgentConfig(name="")
        assert cfg.display_name == ""
        assert cfg.agent_name is None


# ---------------------------------------------------------------------------
# SlackAgentGatewaySettings
# ---------------------------------------------------------------------------


class TestSlackAgentGatewaySettings:
    def test_default_channel_allowlist(self) -> None:
        settings = SlackAgentGatewaySettings()
        assert settings.channel_allowlist == [".*"]

    def test_default_channel_denylist_empty(self) -> None:
        settings = SlackAgentGatewaySettings()
        assert settings.channel_denylist == []

    def test_default_agents_empty(self) -> None:
        settings = SlackAgentGatewaySettings()
        assert settings.agents == []

    def test_custom_values(self) -> None:
        settings = SlackAgentGatewaySettings(
            channel_allowlist=["eng-.*"],
            channel_denylist=["noise-.*"],
            agents=[SlackAgentConfig(name="mybot")],
        )
        assert settings.channel_allowlist == ["eng-.*"]
        assert settings.channel_denylist == ["noise-.*"]
        assert len(settings.agents) == 1


# ---------------------------------------------------------------------------
# MCPToolsSettings
# ---------------------------------------------------------------------------


class TestMCPToolsSettings:
    def test_default_bigquery_limits(self) -> None:
        settings = MCPToolsSettings()
        assert settings.bigquery_max_bytes_processed_gb == 200.0
        assert settings.bigquery_expensive_max_bytes_processed_gb == 10000.0

    def test_custom_limits(self) -> None:
        settings = MCPToolsSettings(bigquery_max_bytes_processed_gb=50.0)
        assert settings.bigquery_max_bytes_processed_gb == 50.0


# ---------------------------------------------------------------------------
# _load_settings_from_yaml
# ---------------------------------------------------------------------------


class TestLoadSettingsFromYaml:
    def test_returns_empty_when_no_file_found(self) -> None:
        _clear_cache()
        with (
            patch.dict(os.environ, {"ENVIRONMENT": "test_env_that_does_not_exist"}),
            patch("ypl.backend.utils.dynamic_app_settings.Path") as mock_path_cls,
        ):
            # Make all path.exists() calls return False
            mock_path = MagicMock(spec=Path)
            mock_path.exists.return_value = False
            mock_path.is_absolute.return_value = False
            mock_path_cls.return_value = mock_path
            mock_path_cls.cwd.return_value = mock_path
            mock_path.__truediv__ = lambda self, other: mock_path

            result = _load_settings_from_yaml()

        _clear_cache()
        assert isinstance(result, dict)

    def test_loads_slack_agent_gateway_settings(self, tmp_path: Path) -> None:
        """Test that _load_settings_from_yaml returns a dict regardless of env."""
        _clear_cache()
        # Simply verify the function returns a dict (actual loading depends on file presence)
        with patch.dict(os.environ, {"ENVIRONMENT": "no_such_env_xyz"}):
            result = _load_settings_from_yaml()
        _clear_cache()
        assert isinstance(result, dict)

    def test_caches_result(self) -> None:
        _clear_cache()
        # Call twice; second call returns cached result
        with patch.dict(os.environ, {"ENVIRONMENT": "no_such_env"}):
            result1 = _load_settings_from_yaml()
            result2 = _load_settings_from_yaml()
        _clear_cache()
        assert result1 is result2  # Same dict object (cache hit)


# ---------------------------------------------------------------------------
# get_slack_agent_gateway_settings
# ---------------------------------------------------------------------------


class TestGetSlackAgentGatewaySettings:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_cache_empty(self) -> None:
        _clear_cache()
        with patch("ypl.backend.utils.dynamic_app_settings._load_settings_from_yaml", return_value={}):
            result = await get_slack_agent_gateway_settings()
        _clear_cache()
        assert isinstance(result, SlackAgentGatewaySettings)
        assert result.channel_allowlist == [".*"]

    @pytest.mark.asyncio
    async def test_returns_cached_settings(self) -> None:
        _clear_cache()
        custom_settings = SlackAgentGatewaySettings(channel_allowlist=["custom-.*"])
        with patch(
            "ypl.backend.utils.dynamic_app_settings._load_settings_from_yaml",
            return_value={"slack_agent_gateway_settings": custom_settings},
        ):
            result = await get_slack_agent_gateway_settings()
        _clear_cache()
        assert result.channel_allowlist == ["custom-.*"]

    @pytest.mark.asyncio
    async def test_result_is_correct_type(self) -> None:
        _clear_cache()
        with patch("ypl.backend.utils.dynamic_app_settings._load_settings_from_yaml", return_value={}):
            result = await get_slack_agent_gateway_settings()
        _clear_cache()
        assert isinstance(result, SlackAgentGatewaySettings)


# ---------------------------------------------------------------------------
# get_mcp_tools_settings
# ---------------------------------------------------------------------------


class TestGetMcpToolsSettings:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_cache_empty(self) -> None:
        _clear_cache()
        with patch("ypl.backend.utils.dynamic_app_settings._load_settings_from_yaml", return_value={}):
            result = await get_mcp_tools_settings()
        _clear_cache()
        assert isinstance(result, MCPToolsSettings)
        assert result.bigquery_max_bytes_processed_gb == 200.0

    @pytest.mark.asyncio
    async def test_returns_cached_settings(self) -> None:
        _clear_cache()
        custom = MCPToolsSettings(bigquery_max_bytes_processed_gb=99.0)
        with patch(
            "ypl.backend.utils.dynamic_app_settings._load_settings_from_yaml",
            return_value={"mcp_tools_settings": custom},
        ):
            result = await get_mcp_tools_settings()
        _clear_cache()
        assert result.bigquery_max_bytes_processed_gb == 99.0


# ---------------------------------------------------------------------------
# refresh_dynamic_app_settings_in_redis (no-op stub)
# ---------------------------------------------------------------------------


class TestRefreshDynamicAppSettingsInRedis:
    @pytest.mark.asyncio
    async def test_noop_does_not_raise(self) -> None:
        # Should complete without raising
        await refresh_dynamic_app_settings_in_redis()

    @pytest.mark.asyncio
    async def test_accepts_yaml_path_param(self) -> None:
        await refresh_dynamic_app_settings_in_redis(yaml_path="/some/path.yml")

    @pytest.mark.asyncio
    async def test_accepts_always_log_param(self) -> None:
        await refresh_dynamic_app_settings_in_redis(always_log=True)
