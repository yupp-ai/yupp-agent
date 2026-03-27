"""Gateway registry — singleton that maps gateway names to instances.

At startup, `init_gateways()` (in __init__.py) builds GatewayConfig
objects from env vars and registers concrete Gateway instances here.
Callers resolve a gateway by name or by agent config.
"""

from __future__ import annotations
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.gateway.base import Gateway
from ypl.structured_logger import get_logger

logger = get_logger()


class GatewayConfig(BaseModel):
    """Configuration for a single gateway instance.

    Intentionally minimal — each Gateway subclass reads what it needs
    from ``settings``.
    """

    name: str  # e.g., "slack", "web"
    gateway_type: str  # maps to a Gateway subclass (e.g., "slack", "sse")
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


# Trigger → gateway-name mapping.
# The trigger field on a session tells us which gateway originated it,
# so we can route replies back through the same transport.
TRIGGER_TO_GATEWAY: dict[str, str | None] = {
    "SLACK": "slack",
    "API": None,  # no outgoing gateway
    "CRON": None,  # no outgoing gateway (for now)
    "WEBHOOK": None,  # no outgoing gateway (for now)
}


class GatewayRegistry:
    """Singleton registry of all available gateways."""

    _instance: GatewayRegistry | None = None
    _gateways: dict[str, Gateway]

    def __init__(self) -> None:
        self._gateways = {}

    @classmethod
    def get_instance(cls) -> GatewayRegistry:
        if cls._instance is None:
            cls._instance = cls()
            # Lazy init: ensure gateways are registered even outside FastAPI lifecycle
            # (e.g., standalone MCP server via stdio transport).
            from ypl.agent_harness_service.gateway import init_gateways

            init_gateways()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton. Useful for testing."""
        cls._instance = None

    def register(self, gateway: Gateway) -> None:
        self._gateways[gateway.name] = gateway
        logger.info("Registered gateway", gateway_name=gateway.name)

    def register_from_config(self, config: GatewayConfig) -> None:
        """Instantiate and register a gateway from its config."""
        from ypl.agent_harness_service.gateway.slack import SlackGateway

        gateway_type_map: dict[str, Callable[[GatewayConfig], Gateway]] = {
            "slack": SlackGateway,
        }

        factory = gateway_type_map.get(config.gateway_type)
        if factory is None:
            logger.error("Unknown gateway type", gateway_type=config.gateway_type)
            return

        if not config.enabled:
            logger.info("Gateway disabled, skipping", gateway_name=config.name)
            return

        gateway = factory(config)
        self.register(gateway)

    def get(self, name: str) -> Gateway | None:
        return self._gateways.get(name)

    def get_for_session(
        self,
        gateway_name: str,
        agent_config: AgentConfig,
    ) -> Gateway | None:
        """Get the gateway for a session, respecting agent's allowed_gateways."""
        if "*" not in agent_config.allowed_gateways and gateway_name not in agent_config.allowed_gateways:
            return None
        return self.get(gateway_name)

    def get_all_for_agent(self, agent_config: AgentConfig) -> list[Gateway]:
        """Get all enabled gateways allowed for this agent. For fan-out use cases."""
        if "*" in agent_config.allowed_gateways:
            return list(self._gateways.values())
        return [gw for name, gw in self._gateways.items() if name in agent_config.allowed_gateways]
