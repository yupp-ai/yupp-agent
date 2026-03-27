"""Shared MCP configuration for harnessed executors (Claude Code, Codex, etc.).

Resolves the set of MCP servers a session should have access to, then writes
the result in the format each CLI expects:
  - Claude Code: .mcp.json (custom headers)
  - Codex CLI:   -c flags  (bearer_token_env_var)
"""

import json
import os
from typing import Any

from ypl.agent_harness_service.common.constants import (
    AHS_MCP_BASE_URL,
    AHS_MCP_SECRET,
    AHS_REPOS_DIR,
    ALL_MCP_SERVERS,
)
from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.structured_logger import get_logger

logger = get_logger()

# Env var name injected into the Codex subprocess carrying "<secret>:<session_id>".
# The MCP auth middleware accepts this as a Bearer token fallback.
CODEX_HARNESS_BEARER_ENV = "AHS_MCP_BEARER"


def resolve_mcp_servers(
    session_id: str = "",
    session_context: dict[str, Any] | None = None,
    is_slack: bool = False,
    agent_name: str = "",
) -> dict[str, Any]:
    """Resolve the set of MCP servers a session should have access to.

    Reads the base .mcp.json from the repo, applies permission filtering,
    injects the harness MCP server, and returns the final server dict.
    """
    # Read the base config from the repo (contains yuppster-mcp-server, etc.)
    base_mcp_path = os.path.join(AHS_REPOS_DIR, "yupp-agent", ".mcp.json")
    base_servers: dict[str, Any] = {}
    try:
        with open(base_mcp_path) as f:
            base_servers = json.load(f).get("mcpServers", {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Base .mcp.json not found or invalid", path=base_mcp_path)

    # Resolve permissions from context (with fail-secure defaults).
    ctx = session_context or {}
    if "permissions" in ctx:
        perms = SessionPermissions.from_context(ctx)
    elif is_slack:
        # Slack session without any permission info - fail secure
        perms = SessionPermissions.restricted()
        logger.warning(
            "Slack session missing permissions, defaulting to restricted",
            session_id=session_id,
        )
    else:
        # Non-Slack sessions (API, cron, webhook) - allow by default
        perms = SessionPermissions()

    # Remove MCP servers not in the allowed list; third-party servers are kept.
    known_servers = set(ALL_MCP_SERVERS)
    allowed = set(perms.allowed_servers)
    servers_to_remove = [k for k in base_servers if k in known_servers and k not in allowed]
    for server_name in servers_to_remove:
        del base_servers[server_name]
    if servers_to_remove:
        logger.info(
            "Excluded MCP servers (not in allowed_servers)",
            session_id=session_id,
            excluded_servers=servers_to_remove,
            allowed_servers=perms.allowed_servers,
        )

    # Harness MCP server is always present (tool-level filtering is done
    # via --allowedTools in _build_args based on permissions).
    base_servers["harness"] = {
        "type": "http",
        "url": f"{AHS_MCP_BASE_URL}/mcp/harness/",
        "headers": {
            "X-AHS-Token": AHS_MCP_SECRET,
            "X-AHS-Session-ID": session_id,
        },
    }

    # Inject session identity headers into yuppster-mcp-server.
    # These headers are written by the AHS runner into the sandbox workspace's
    # .mcp.json and cannot be modified by the agent — they are tamper-proof.
    if "yuppster-mcp-server" in base_servers:
        base_servers["yuppster-mcp-server"].setdefault("headers", {})

        # X-User-ID: creator attribution (prefer current_turn_user_id over session user_id).
        # Without this, all resources created by AHS agents are attributed to the shared
        # YUPPSTER_MCP_TOKEN owner instead of the session's actual requesting user.
        user_id = ctx.get("current_turn_user_id") or ctx.get("user_id")
        if user_id:
            base_servers["yuppster-mcp-server"]["headers"]["X-User-ID"] = user_id

        # X-AHS-Agent-Name: the agent's canonical name (e.g. "eng-raccoon").
        # Used by report_security_incident to attribute incidents to the correct agent
        # without trusting the agent itself to supply its own name.
        if agent_name:
            base_servers["yuppster-mcp-server"]["headers"]["X-AHS-Agent-Name"] = agent_name

        # X-AHS-Session-ID: the current session UUID.
        # Allows MCP tools to link incidents to the originating session.
        if session_id:
            base_servers["yuppster-mcp-server"]["headers"]["X-AHS-Session-ID"] = session_id

    # Drop disabled servers
    servers = {k: v for k, v in base_servers.items() if not v.get("disabled")}

    logger.info(
        "Resolved MCP servers",
        session_id=session_id,
        mcp_servers=list(servers.keys()),
        allowed_servers=perms.allowed_servers,
        has_full_tool_access=perms.has_full_tool_access,
    )
    return servers


def ensure_workspace_mcp_config(
    workspace: str,
    session_id: str = "",
    session_context: dict[str, Any] | None = None,
    is_slack: bool = False,
    agent_name: str = "",
) -> None:
    """Write .mcp.json to the workspace root for Claude Code MCP server discovery.

    Claude Code reads .mcp.json at the project root to discover MCP servers.
    This format supports arbitrary headers for auth.
    """
    if not workspace:
        return

    servers = resolve_mcp_servers(session_id, session_context, is_slack, agent_name=agent_name)

    mcp_json_path = os.path.join(workspace, ".mcp.json")
    tmp_path = mcp_json_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"mcpServers": servers}, f, indent=2)
    os.replace(tmp_path, mcp_json_path)

    logger.info("Wrote workspace .mcp.json", session_id=session_id, path=mcp_json_path)


def build_codex_mcp_args(
    session_id: str = "",
    session_context: dict[str, Any] | None = None,
    is_slack: bool = False,
) -> list[str]:
    """Build -c flags for Codex CLI to inject MCP servers at launch time.

    Codex CLI discovers MCP servers from config.toml [mcp_servers.<name>] sections,
    not from .mcp.json. The -c flag can override config values at launch:
      -c 'mcp_servers.<name>.url="<url>"'
      -c 'mcp_servers.<name>.bearer_token_env_var="<env_var>"'

    The harness MCP server uses a Bearer token in format "<secret>:<session_id>"
    via the CODEX_HARNESS_BEARER_ENV env var (set by build_codex_mcp_env).
    The yuppster MCP server uses YUPPSTER_MCP_TOKEN (already in the subprocess env).
    """
    servers = resolve_mcp_servers(session_id, session_context, is_slack)
    args: list[str] = []

    for name, server in servers.items():
        url = server.get("url", "")
        if not url:
            continue

        args += ["-c", f'mcp_servers.{name}.url="{url}"']

        if name == "harness":
            # Harness auth: Bearer token = "<secret>:<session_id>" via env var.
            args += ["-c", f'mcp_servers.{name}.bearer_token_env_var="{CODEX_HARNESS_BEARER_ENV}"']
        elif name == "yuppster-mcp-server":
            # Yuppster auth: Bearer token from YUPPSTER_MCP_TOKEN env var.
            args += ["-c", f'mcp_servers.{name}.bearer_token_env_var="YUPPSTER_MCP_TOKEN"']

    return args


def build_codex_mcp_env(session_id: str) -> dict[str, str]:
    """Return extra env vars needed for Codex MCP auth.

    Sets the bearer token env var that Codex reads for the harness MCP server.
    Format: "<secret>:<session_id>" — parsed by the MCP auth middleware.
    """
    return {CODEX_HARNESS_BEARER_ENV: f"{AHS_MCP_SECRET}:{session_id}"}
