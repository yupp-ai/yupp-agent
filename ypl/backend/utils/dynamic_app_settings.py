"""Stub of dynamic_app_settings for yupp-agent.

The full implementation in yupp-mind has deep imports into LLM routing,
abuse, payment, etc. This stub provides only the functions used by
AHS, SAG, and MCP services, loading settings from YAML files.
"""

import enum
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ypl.structured_logger import get_logger

logger = get_logger()

_settings_cache: dict[str, Any] = {}


class AppSettingsName(enum.Enum):
    SLACK_AGENT_GATEWAY_SETTINGS = "slack_agent_gateway_settings"
    MCP_TOOLS_SETTINGS = "mcp_tools_settings"


class SlackAgentConfig(BaseModel):
    """Configuration for a single Slack agent in the gateway."""

    name: str = Field(..., description="Agent name (e.g., 'giladovski')")
    display_name: str = Field(default="", description="Display name")
    agent_name: str | None = Field(default=None, description="Agent name in AHS")

    def model_post_init(self, __context: Any) -> None:
        if not self.display_name and self.name:
            self.display_name = self.name.title()
        if not self.agent_name and self.name:
            self.agent_name = self.name


class SlackAgentGatewaySettings(BaseModel):
    """Runtime settings for Slack Agent Gateway."""

    agents: list[SlackAgentConfig] = Field(default_factory=list)
    channel_denylist: list[str] = Field(default_factory=list)
    channel_allowlist: list[str] = Field(default_factory=lambda: [".*"])


class MCPToolsSettings(BaseModel):
    """Runtime settings for MCP tools."""

    bigquery_max_bytes_processed_gb: float = 200.0
    bigquery_expensive_max_bytes_processed_gb: float = 10000.0


_SETTINGS_TYPE_MAP: dict[str, type[BaseModel]] = {
    "SlackAgentGatewaySettings": SlackAgentGatewaySettings,
    "MCPToolsSettings": MCPToolsSettings,
}


def _load_settings_from_yaml() -> dict[str, Any]:
    """Load settings from environment-specific YAML file."""
    global _settings_cache
    if _settings_cache:
        return _settings_cache

    env = os.environ.get("ENVIRONMENT", "local")
    # Try environment-specific file, fall back to staging
    for candidate in [f"data/dynamic_app_settings_{env}.yml", "data/dynamic_app_settings_staging.yml"]:
        path = Path(candidate)
        if not path.is_absolute():
            # Try relative to /app (Docker) then cwd
            for base in [Path("/app"), Path.cwd()]:
                full = base / candidate
                if full.exists():
                    path = full
                    break
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            for item in data.get("app_settings", []):
                name = item.get("name")
                type_name = item.get("type")
                value = item.get("value", {})
                model_cls = _SETTINGS_TYPE_MAP.get(type_name)
                if name and model_cls:
                    _settings_cache[name] = model_cls(**value)
            logger.info(f"Loaded dynamic app settings from {path}", settings_count=len(_settings_cache))
            return _settings_cache

    logger.info("No dynamic_app_settings YAML found, using defaults")
    return _settings_cache


async def get_slack_agent_gateway_settings() -> SlackAgentGatewaySettings:
    """Return Slack Agent Gateway settings from YAML or defaults."""
    settings = _load_settings_from_yaml()
    return settings.get("slack_agent_gateway_settings", SlackAgentGatewaySettings())


async def get_mcp_tools_settings() -> MCPToolsSettings:
    """Return MCP tools settings from YAML or defaults."""
    settings = _load_settings_from_yaml()
    return settings.get("mcp_tools_settings", MCPToolsSettings())


async def refresh_dynamic_app_settings_in_redis(yaml_path: str | None = None, always_log: bool = False) -> None:
    """No-op stub — yupp-agent doesn't use Redis-backed dynamic settings yet."""
    logger.info("refresh_dynamic_app_settings_in_redis called (no-op in yupp-agent)")
