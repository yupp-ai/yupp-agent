"""Stub of dynamic_app_settings for yupp-agent.

The full implementation in yupp-mind has deep imports into LLM routing,
abuse, payment, etc. This stub provides only the functions used by
AHS, SAG, and MCP services, with sensible defaults.
"""

import enum
from typing import Any

from pydantic import BaseModel, Field

from ypl.structured_logger import get_logger

logger = get_logger()


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
    channel_allowlist: list[str] = Field(default_factory=list)


class MCPToolsSettings(BaseModel):
    """Runtime settings for MCP tools."""

    bigquery_max_bytes_processed_gb: float = 200.0
    bigquery_expensive_max_bytes_processed_gb: float = 10000.0


async def get_slack_agent_gateway_settings() -> SlackAgentGatewaySettings:
    """Return default Slack Agent Gateway settings."""
    return SlackAgentGatewaySettings()


async def get_mcp_tools_settings() -> MCPToolsSettings:
    """Return default MCP tools settings."""
    return MCPToolsSettings()


async def refresh_dynamic_app_settings_in_redis(yaml_path: str | None = None, always_log: bool = False) -> None:
    """No-op stub — yupp-agent doesn't use Redis-backed dynamic settings yet."""
    logger.info("refresh_dynamic_app_settings_in_redis called (no-op in yupp-agent)")
