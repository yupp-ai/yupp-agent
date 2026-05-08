"""Configuration for the monolith server.

All settings can be overridden via environment variables.  Field names map
directly to env-var names (case-insensitive), so ``gateway_slack_enabled``
reads from ``GATEWAY_SLACK_ENABLED``, etc.

Two layers of feature flags gate the optional surfaces of the monolith:

  Master flags (default OFF — pure-AHS deployment shape):
    AHS_MONO_ENABLE_GATEWAY_SERVICE  Mount any ``/gw/<name>/`` plugin routers
                                     and run their startup/shutdown hooks.
                                     When OFF the per-plugin flags below
                                     are ignored.
    AHS_MONO_ENABLE_MCP              Mount the agcouch MCP at ``/mcp/agcouch``
                                     and run the agcouch FastMCP session
                                     manager + ``mcp_startup``/``mcp_shutdown``.
                                     The harness MCP at ``/mcp/harness`` is
                                     ALWAYS mounted regardless of this flag —
                                     AHS agents always have a tool surface.

  Per-plugin sub-flags (only consulted when AHS_MONO_ENABLE_GATEWAY_SERVICE=true):
    GATEWAY_SLACK_ENABLED            Mount the Slack Agent Gateway at
                                     ``/gw/slack``. Default: ``True``.
    GATEWAY_GITHUB_ENABLED           Mount the GitHub webhook gateway at
                                     ``/gw/github``. Default: ``False`` —
                                     requires ``AHS_GITHUB_WEBHOOK_SECRET``.

Operators upgrading from a previous release that defaulted to
"AHS + SAG + agcouch MCP" must explicitly opt in by setting both master
flags to ``true``. See ``DEPLOYMENT.md`` for deployment-shape recipes.

Example .env::

    PORT=8090
    AHS_MONO_ENABLE_GATEWAY_SERVICE=true   # opt-in: keep gateway plugins
    AHS_MONO_ENABLE_MCP=true               # opt-in: keep agcouch MCP mount
    GATEWAY_SLACK_ENABLED=true
    GATEWAY_GITHUB_ENABLED=false           # off by default; needs AHS_GITHUB_WEBHOOK_SECRET
    HOST_PATH_GUARD={"mcp.agcouch.com":["/mcp/agcouch","/health"]}
"""

import json
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = os.environ.get("DOTENV_PATH", ".env")


class MonoConfig(BaseSettings):
    """Runtime configuration for the Yupp Agent Platform monolith.

    Attributes:
        port:                            TCP port for the uvicorn server (default 8090).
        ahs_mono_enable_gateway_service: Master flag for gateway plugins.  When
                                         ``False`` (default) **no** plugin
                                         routers are mounted and **no**
                                         plugin lifespan hooks fire — the
                                         per-plugin ``gateway_*_enabled``
                                         flags are ignored.  When ``True``
                                         each per-plugin flag is consulted to
                                         decide whether that specific gateway
                                         is enabled.
        ahs_mono_enable_mcp:             Master flag for the agcouch MCP mount.
                                         When ``False`` (default) the
                                         ``/mcp/agcouch`` mount, the agcouch
                                         FastMCP lifespan, and the yuppster
                                         batch-system init/shutdown
                                         (``mcp_startup``/``mcp_shutdown``) are
                                         all skipped.  The harness MCP at
                                         ``/mcp/harness`` is always mounted
                                         regardless — AHS agent sessions
                                         always have a tool surface.
        gateway_slack_enabled:           Sub-control under
                                         ``ahs_mono_enable_gateway_service``.
                                         Mounts the Slack Agent Gateway at
                                         ``/gw/slack`` when both this and the
                                         master flag are ``True``.  Default
                                         ``True``.
        gateway_github_enabled:          Sub-control under
                                         ``ahs_mono_enable_gateway_service``.
                                         Mounts the GitHub webhook gateway at
                                         ``/gw/github`` when both this and the
                                         master flag are ``True``.  Default
                                         ``False`` — requires
                                         ``AHS_GITHUB_WEBHOOK_SECRET`` to be
                                         set.
        host_path_guard:                 JSON-encoded map of ``{hostname: [allowed_prefix, ...]}``.
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
    # Master flags — default OFF.  See module docstring for the upgrade-from-
    # previous-default story (operators must explicitly opt in to keep the
    # legacy AHS + SAG + agcouch MCP shape).
    ahs_mono_enable_gateway_service: bool = False
    ahs_mono_enable_mcp: bool = False
    # Per-plugin sub-flags — only honoured when ``ahs_mono_enable_gateway_service``
    # is True.  Their defaults match the legacy "monolith with everything on"
    # shape so flipping just the master flag is sufficient to restore today's
    # behaviour.
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
