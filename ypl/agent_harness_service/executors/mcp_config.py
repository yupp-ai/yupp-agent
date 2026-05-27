"""Shared MCP configuration for harnessed executors (Claude Code, Codex, etc.).

Resolves the set of MCP servers a session should have access to, then writes
the result in the format each CLI expects:
  - Claude Code: .mcp.json (custom headers)
  - Codex CLI:   -c flags  (bearer_token_env_var)

Currently only the harness MCP is injected. Shared (AHS-system) and
external-data tools that previously required the agent to also connect to
``/mcp/agcouch`` with ``AGCOUCH_MCP_TOKEN`` now register on the harness
mount via ``@shared_tool`` (see :mod:`ypl.mcp_common.shared_tool`), so
agents reach every tool through ``AHS_MCP_SECRET`` and the AHS executor
does not handle ``AGCOUCH_MCP_TOKEN`` at all.

When the DB-backed external-MCP registry lands (phase-9), additional
servers will plug into :func:`_build_base_servers` here and inherit the
same permission filtering logic.
"""

import json
import os
from typing import Any

from ypl.agent_harness_service.common.constants import (
    AHS_MCP_BASE_URL,
    AHS_MCP_SECRET,
    ALL_MCP_SERVERS,
)
from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.structured_logger import get_logger

logger = get_logger()

# Env var name injected into the Codex subprocess carrying "<secret>:<session_id>".
# The MCP auth middleware accepts this as a Bearer token fallback.
CODEX_HARNESS_BEARER_ENV = "AHS_MCP_BEARER"


def _build_base_servers() -> dict[str, Any]:
    """Return the baseline set of MCP servers available to any session.

    Currently empty — the harness server is injected later in
    :func:`resolve_mcp_servers` because it needs per-session headers
    (``X-AHS-Session-ID``). When we add additional first-party MCP
    servers (or a DB-backed registry of external ones in phase-9), they
    plug in here.
    """
    return {}


def resolve_mcp_servers(
    session_id: str = "",
    session_context: dict[str, Any] | None = None,
    is_slack: bool = False,
    agent_name: str = "",
    external_mcps: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve the set of MCP servers a session should have access to.

    Builds the baseline set, applies permission filtering, injects the
    harness MCP server, and returns the final server dict.
    """
    base_servers = _build_base_servers()

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
    #
    # Identity headers (``X-User-ID`` / ``X-AHS-Agent-Name``) are written
    # into the agent's sandboxed ``.mcp.json`` here — the AHS runner
    # writes the file and the agent cannot modify it. The harness auth
    # middleware validates the secret then trusts these headers, so
    # tools (project_tasks, agent_artifacts, memory_artifacts,
    # report_security_incident, …) attribute calls to the right
    # principal without trusting the agent's process.
    harness_headers: dict[str, str] = {
        "X-AHS-Token": AHS_MCP_SECRET,
        "X-AHS-Session-ID": session_id,
    }
    user_id = ctx.get("current_turn_user_id") or ctx.get("user_id")
    if user_id:
        harness_headers["X-User-ID"] = user_id
    if agent_name:
        harness_headers["X-AHS-Agent-Name"] = agent_name
    base_servers["harness"] = {
        "type": "http",
        "url": f"{AHS_MCP_BASE_URL}/mcp/harness/",
        "headers": harness_headers,
    }

    # Drop disabled servers
    servers = {k: v for k, v in base_servers.items() if not v.get("disabled")}

    # Merge external MCPs (Gmail, Drive, Calendar, …).  Import is local so
    # the runtime path doesn't pay for SQLAlchemy + Fernet on every resolve
    # when no agent declares any external_mcps.
    if external_mcps and user_id:
        import asyncio
        import uuid as _uuid

        from ypl.external_mcp.resolver import build_external_mcp_entries

        agent_id_raw = ctx.get("agent_id")
        try:
            agent_uuid = _uuid.UUID(agent_id_raw) if agent_id_raw else None
        except (TypeError, ValueError):
            agent_uuid = None
        try:
            sess_uuid = _uuid.UUID(session_id) if session_id else None
        except (TypeError, ValueError):
            sess_uuid = None

        # The async resolver uses `ypl.backend.db.get_async_session()`, whose
        # AsyncEngine pool is bound to whichever asyncio loop first touches
        # it. Calling it via `asyncio.run()` from this sync code path spins
        # up a fresh loop, and any cached connection in the pool then errors
        # with "Future attached to a different loop" (or asyncpg's variant).
        # Run the resolver inside a fresh worker thread so each call lands
        # on a brand-new loop, and treat any failure as "no external MCPs
        # this session" rather than killing the agent runner.
        ext_servers: dict[str, Any] = {}
        unavailable: list[dict[str, str]] = []
        import concurrent.futures

        # Run the async resolver on a fresh worker-thread loop so it doesn't
        # inherit connections that asyncpg cached on a different event loop.
        #
        # IMPORTANT: do NOT use `with ThreadPoolExecutor(...) as pool:` here.
        # The context manager calls shutdown(wait=True) on __exit__, so even
        # when future.result(timeout=20) raises TimeoutError the process blocks
        # until the worker thread actually finishes — making the timeout
        # effectively cosmetic.  We instead call shutdown(wait=False) in a
        # finally block to let the worker die in the background while the
        # session continues without external MCPs.
        #
        # TODO: The AsyncEngine pool is a process-level singleton; connections
        # checked out on the worker thread's loop are returned to the pool when
        # the thread exits and can cause "Future attached to a different loop"
        # on the *next* session that reuses them.  A proper fix is to pin one
        # dedicated thread+loop for the lifetime of the AHS process and submit
        # coroutines via asyncio.run_coroutine_threadsafe().
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            asyncio.run,
            build_external_mcp_entries(
                user_id=user_id,
                agent_id=agent_uuid,
                requested_slugs=external_mcps,
                agent_session_id=sess_uuid,
            ),
        )
        try:
            ext_servers, unavailable = future.result(timeout=20)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "external MCP resolution timed out (20 s); agent will run without external MCP servers",
                external_mcps=external_mcps,
                session_id=session_id,
            )
            unavailable = [{"slug": s, "reason": "resolver timed out"} for s in external_mcps]
        except Exception as e:
            logger.warning(
                "external MCP resolution failed; agent will run without external MCP servers this session",
                error=str(e),
                error_type=type(e).__name__,
                external_mcps=external_mcps,
                session_id=session_id,
            )
            unavailable = [{"slug": s, "reason": f"resolver crashed: {type(e).__name__}"} for s in external_mcps]
        finally:
            # Let the worker thread finish in the background; don't block the runner.
            pool.shutdown(wait=False, cancel_futures=True)
        servers.update(ext_servers)
        if unavailable:
            logger.info(
                "External MCPs unavailable",
                session_id=session_id,
                unavailable=unavailable,
            )

    logger.info(
        "Resolved MCP servers",
        session_id=session_id,
        mcp_servers=list(servers.keys()),
        allowed_servers=perms.allowed_servers,
        has_full_tool_access=perms.has_full_tool_access,
        external_requested=external_mcps or [],
    )
    return servers


def ensure_workspace_mcp_config(
    workspace: str,
    session_id: str = "",
    session_context: dict[str, Any] | None = None,
    is_slack: bool = False,
    agent_name: str = "",
    external_mcps: list[str] | None = None,
) -> None:
    """Write .mcp.json to the workspace root for Claude Code MCP server discovery.

    Claude Code reads .mcp.json at the project root to discover MCP servers.
    This format supports arbitrary headers for auth.
    """
    if not workspace:
        return

    servers = resolve_mcp_servers(
        session_id, session_context, is_slack, agent_name=agent_name, external_mcps=external_mcps
    )

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

    return args


def build_codex_mcp_env(session_id: str) -> dict[str, str]:
    """Return extra env vars needed for Codex MCP auth.

    Sets the bearer token env var that Codex reads for the harness MCP server.
    Format: "<secret>:<session_id>" — parsed by the MCP auth middleware.
    """
    return {CODEX_HARNESS_BEARER_ENV: f"{AHS_MCP_SECRET}:{session_id}"}
