"""Configuration for the monolith server.

All settings can be overridden via environment variables.  Field names map
directly to env-var names (case-insensitive), so ``gateway_slack_enabled``
reads from ``GATEWAY_SLACK_ENABLED``, etc.

Example .env::

    PORT=8090
    GATEWAY_SLACK_ENABLED=true
    GATEWAY_GITHUB_ENABLED=false   # off by default; requires AHS_GITHUB_WEBHOOK_SECRET
    HOST_PATH_GUARD={"mcp.agcouch.com":["/mcp","/health"]}
"""

import json
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = os.environ.get("DOTENV_PATH", ".env")


class MonoConfig(BaseSettings):
    """Runtime configuration for the Yupp Agent Platform monolith.

    Attributes:
        port:                   TCP port for the uvicorn server (default 8090).
        gateway_slack_enabled:  Enable the Slack Agent Gateway and mount its
                                router at ``/gw/slack``.  Default ``True``.
        gateway_github_enabled: Enable the GitHub webhook gateway and mount it
                                at ``/gw/github``.  Default ``False`` — requires
                                ``AHS_GITHUB_WEBHOOK_SECRET`` to be set.
        host_path_guard:        JSON-encoded map of ``{hostname: [allowed_prefix, ...]}``.
                                When set, requests whose ``Host`` header matches
                                a listed hostname are restricted to the allowed
                                path prefixes; everything else 404s.  Hosts not
                                listed pass through untouched.  Default: empty
                                (no restriction, same behaviour as before).
    """

    model_config = SettingsConfigDict(
        # Read from .env (or DOTENV_PATH override) if present, but don't require it
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        # Ignore extra env vars so the monolith shares the same .env as the
        # individual services without errors on unknown keys
        extra="ignore",
    )

    port: int = 8090
    gateway_slack_enabled: bool = True
    gateway_github_enabled: bool = False
    host_path_guard: dict[str, list[str]] = Field(
        default_factory=dict,
        alias="HOST_PATH_GUARD",
    )

    @field_validator("host_path_guard", mode="before")
    @classmethod
    def _parse_host_path_guard(cls, value: object) -> object:
        """Accept either a dict (already parsed) or a JSON string from an env var."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            return json.loads(stripped)
        return value
