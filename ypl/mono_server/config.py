"""Configuration for the monolith server.

All settings can be overridden via environment variables.  Field names map
directly to env-var names (case-insensitive), so ``gateway_slack_enabled``
reads from ``GATEWAY_SLACK_ENABLED``, etc.

Example .env::

    PORT=8090
    GATEWAY_SLACK_ENABLED=true
    GATEWAY_GITHUB_ENABLED=true
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class MonoConfig(BaseSettings):
    """Runtime configuration for the Yupp Agent Platform monolith.

    Attributes:
        port:                   TCP port for the uvicorn server (default 8090).
        gateway_slack_enabled:  Enable the Slack Agent Gateway and mount its
                                router at ``/gw/slack``.  Default ``True``.
        gateway_github_enabled: Enable the GitHub webhook gateway and mount it
                                at ``/gw/github``.  Default ``True``.
    """

    model_config = SettingsConfigDict(
        # Read from .env if present, but don't require it
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore extra env vars so the monolith shares the same .env as the
        # individual services without errors on unknown keys
        extra="ignore",
    )

    port: int = 8090
    gateway_slack_enabled: bool = True
    gateway_github_enabled: bool = True
