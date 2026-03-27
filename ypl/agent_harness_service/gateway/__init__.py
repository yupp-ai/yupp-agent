"""AHS Gateway package — abstract gateway layer for multi-transport support.

Call ``init_gateways()`` once at startup to populate the registry from
environment variables.  Future iterations may load configs from a file
or dynamic settings.
"""

from ypl.agent_harness_service.gateway.base import Gateway, GatewaySendResult
from ypl.agent_harness_service.gateway.registry import (
    TRIGGER_TO_GATEWAY,
    GatewayConfig,
    GatewayRegistry,
)
from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()

__all__ = [
    "Gateway",
    "GatewayConfig",
    "GatewayRegistry",
    "GatewaySendResult",
    "TRIGGER_TO_GATEWAY",
    "init_gateways",
]


_initialized = False


def init_gateways() -> GatewayRegistry:
    """Build gateway instances from environment configuration and register them.

    Called at service startup and lazily by the registry on first access.
    Idempotent — subsequent calls are no-ops.
    """
    global _initialized
    if _initialized:
        return GatewayRegistry.get_instance()
    _initialized = True

    registry = GatewayRegistry.get_instance()

    # Slack gateway (from existing env vars)
    base_url = settings.GATEWAY_BASE_URL
    if base_url:
        slack_config = GatewayConfig(
            name="slack",
            gateway_type="slack",
            settings={"base_url": base_url, "api_key": settings.X_API_KEY},
        )
        registry.register_from_config(slack_config)
    else:
        logger.warning("GATEWAY_BASE_URL not configured, Slack gateway will not be registered")

    return registry
