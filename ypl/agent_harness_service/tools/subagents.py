"""Subagent orchestration tools for the harness MCP server.

Provides tools for agents to spawn subagents, pick executor models, list
available agent configs, and create new personal agents.
"""

from __future__ import annotations
import asyncio
import re
import uuid as _uuid
from typing import Any

from sqlalchemy import text

import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance
from ypl.agent_harness_service.common.agent_registry import get_agent_spec, list_predefined_agents
from ypl.agent_harness_service.common.config import load_agent_config
from ypl.agent_harness_service.common.constants import (
    PERSONAL_AGENT_DEFAULT_CONFIG,
    PERSONAL_AGENT_PREFIXES,
    is_personal_agent,
    mcp_session_id_var,
)
from ypl.agent_harness_service.common.types import AgentCreateRequest, ExecutorConfigRequest, SandboxConfigRequest
from ypl.agent_harness_service.tools.mcp_instance import (
    _resolve_parent_session,
    _validate_session_id,
    mcp,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _check_tool_explicitly_allowed(agent_name: str, tool_name: str) -> bool:
    """Check if *tool_name* is explicitly listed as "allow" in the agent's config.

    Tries on-disk config first (fast, lru_cache), then falls back to the DB
    config JSONB for DB-only agents (e.g. bizbot, personal agents without an
    on-disk directory).

    A wildcard ``"*": "allow"`` entry is intentionally NOT treated as explicit
    permission — the tool name must appear literally in ``tool_permissions``.
    """
    cfg = load_agent_config(agent_name)
    if cfg is not None:
        return cfg.tool_permissions.get(tool_name) == "allow"

    # DB-only agent — query raw config JSONB directly.
    async with get_async_session_read_replica() as db:
        result = await db.execute(
            text("SELECT config FROM agents WHERE name = :name"),
            {"name": agent_name},
        )
        row = result.first()
        if not row or not row[0]:
            return False
        from ypl.agent_harness_service.common.models import expand_tool_permissions

        raw_perms: dict[str, Any] = row[0].get("tool_permissions", {})
        try:
            expanded = expand_tool_permissions(raw_perms)
        except ValueError:
            # Unknown toolset in DB config — fall back to raw check.
            expanded = raw_perms
        return expanded.get(tool_name) == "allow"


async def _resolve_current_personal_agent(
    session_id: str | None = None,
    tool_name: str = "",
) -> tuple[str | None, str | None]:
    """Resolve the current agent name and verify it has access to self-prompt tools.

    Access is granted if:
    - The agent is a personal agent (yuppclaw-*), OR
    - The agent explicitly has *tool_name* set to ``"allow"`` in its config
      (on-disk or DB).

    Returns:
        (agent_name, error_message) — one will be None.
    """
    effective_session_id = mcp_session_id_var.get() or session_id or ""
    if not effective_session_id:
        return None, "No session context available"

    parent_info = await _resolve_parent_session(effective_session_id)
    agent_name = parent_info.get("agent_name")
    if not agent_name:
        return None, f"Could not resolve agent for session {effective_session_id}"

    agent_name = str(agent_name)

    # Auto-allow personal agents (yuppclaw-*).
    if is_personal_agent(agent_name):
        return agent_name, None

    # Also allow if the tool is explicitly listed as "allow" in the agent's config.
    # TODO: For shared-session agents (e.g. Slack multi-user bots), add caller-level
    # authorization to verify the requester is the session creator or has a privileged
    # role before allowing system prompt reads/writes. See PR #11253.
    if tool_name and await _check_tool_explicitly_allowed(agent_name, tool_name):
        return agent_name, None

    return (
        None,
        f"This tool is only available to personal agents or agents with explicit config access, not '{agent_name}'",
    )


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="new_task",
    description=(
        "Spawn a subagent in a new session. The subagent runs asynchronously — "
        "this call returns immediately with a session_id and status='spawned'. "
        "The subagent's result will arrive as a follow-up message in this session "
        "once it completes. Use this to delegate tasks to specialized agents "
        "(e.g., reviewer, fixer, coordinator, sre). "
        "Multiple new_task calls in a single response execute in parallel."
    ),
)
async def new_task(
    agent_type: str,
    prompt: str,
    description: str = "",
    model: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Spawn a subagent asynchronously and return immediately.

    Args:
        agent_type: Which agent config to use (e.g., 'reviewer', 'fixer', 'coordinator').
            Available agents can be listed with list_agents.
        prompt: The task for the subagent to perform.
        description: Short (3-5 words) description for tracking.
        model: Optional model override (e.g., 'anthropic/claude-sonnet-4-6').
            If not specified, uses the agent config's default model.
        session_id: The calling agent's harness session ID, used to link parent-child sessions.

    Returns:
        Dict with session_id, agent_name, model, status='spawned', and a note explaining
        that the result will arrive as a follow-up message.
    """
    # Prefer session_id from the HTTP header (injected by runner into .mcp.json).
    # Fall back to the tool parameter (passed by LLM) for backward compatibility.
    effective_session_id = mcp_session_id_var.get() or session_id

    logger.info(
        "MCP tool: new_task",
        agent_type=agent_type,
        description=description,
        model=model,
        parent_session_id=effective_session_id,
    )

    # Resolve parent session context (model, workspace, agent name, depth) from DB
    parent_info: dict[str, Any] = {
        "agent_name": None,
        "model": None,
        "workspace": None,
        "permissions": None,
        "subagent_depth": 0,
    }
    if effective_session_id:
        _validate_session_id(effective_session_id)
        parent_info = await _resolve_parent_session(effective_session_id)

    parent_model = parent_info["model"]
    parent_workspace = parent_info["workspace"]
    parent_depth: int = parent_info.get("subagent_depth", 0)

    # Enforce allowed_subagents
    parent_agent_name = parent_info["agent_name"]
    if parent_agent_name and effective_session_id:
        allowed: list[str] | None = None
        parent_spec = get_agent_spec(parent_agent_name)
        if parent_spec:
            allowed = parent_spec.allowed_subagents
        else:
            parent_config = load_agent_config(parent_agent_name)
            if parent_config:
                allowed = parent_config.allowed_subagents

        if allowed is not None and "*" not in allowed and agent_type not in allowed:
            return {
                "error": (
                    f"Agent '{parent_agent_name}' is not allowed to spawn "
                    f"subagent '{agent_type}'. Allowed: {allowed or '(none)'}."
                ),
                "status": "error",
            }

    parent_permissions = parent_info.get("permissions") if effective_session_id else None

    if _mcp_instance._run_subagent_fn is None:
        return {
            "error": (
                "orchestration callbacks not registered"
                " — server.py must call register_orchestration_callbacks() at startup."
            ),
            "status": "error",
        }

    # Pre-generate session UUID so the caller gets it immediately
    pre_session_id = str(_uuid.uuid4())
    preview_model = model or parent_model or "anthropic/claude-sonnet-4-6"

    # Fire-and-forget: run subagent in background; result delivered via subagent queue
    _subagent_task = asyncio.ensure_future(
        _mcp_instance._run_subagent_fn(
            agent_type=agent_type,
            prompt=prompt,
            description=description,
            model=model,
            parent_session_id=effective_session_id,
            parent_model=parent_model,
            parent_workspace=parent_workspace,
            parent_permissions=parent_permissions,
            parent_depth=parent_depth,
            pre_session_id=pre_session_id,
        )
    )
    # Prevent "Task exception was never retrieved" warnings if the coroutine raises
    # before run_subagent's own try/except (e.g. during argument validation).
    _captured_pre_session_id = pre_session_id
    _captured_agent_type = agent_type

    def _log_subagent_exception(t: asyncio.Task[None]) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error(
                "Unhandled exception in fire-and-forget subagent task",
                pre_session_id=_captured_pre_session_id,
                agent_type=_captured_agent_type,
                exc_info=t.exception(),
            )

    _subagent_task.add_done_callback(_log_subagent_exception)

    return {
        "session_id": pre_session_id,
        "agent_name": agent_type,
        "model": preview_model,
        "status": "spawned",
        "description": description or prompt[:120],
        "note": "Running asynchronously. Result will arrive as a follow-up message in this session.",
    }


@mcp.tool(
    name="route_model",
    description=(
        "Pick executor model(s) for upcoming tasks. Returns a list of model IDs "
        "in '{provider}/{model_id}' format. When count > 1, guarantees diversity "
        "(models from different providers). Use this before new_task to select "
        "which model(s) to use."
    ),
)
def route_model(
    task_description: str,
    count: int = 1,
    candidates: list[str] | None = None,
) -> list[str]:
    """Pick model(s) for a task.

    Args:
        task_description: What the task is about (e.g., 'code review', 'bug fix').
        count: How many models to return (default 1). Use count=2 for dual-review.
        candidates: Optional list to restrict selection to these models.

    Returns:
        List of model IDs (e.g., ['anthropic/claude-sonnet-4-6', 'openai/gpt-4o']).
    """
    logger.info("MCP tool: route_model", task=task_description, count=count)
    if _mcp_instance._route_model_stub_fn is None:
        return ["ERROR: orchestration callbacks not registered"]
    return _mcp_instance._route_model_stub_fn(task_description, count, candidates)


@mcp.tool(
    name="list_agents",
    description="List all available agent configurations. Use agent names with new_task.",
)
def list_agents() -> list[dict[str, str]]:
    """List all available agent configs.

    Returns:
        List of dicts with name, description, executor type, and model for each agent.
    """
    logger.info("MCP tool: list_agents")

    return [
        {
            "name": config.name,
            "description": config.description or "",
            "executor_type": config.executor.type,
            "model": config.executor.model or "(assigned at spawn time)",
        }
        for config in list_predefined_agents()
    ]


@mcp.tool(
    name="create_agent",
    description=(
        "Create a new personal agent. Used by creation-helper agents to set up "
        "user-specific agents (e.g., yuppclaw-alice). The agent is registered in "
        "the database and immediately available for use."
    ),
)
async def create_agent_tool(
    display_name: str,
    persona: str,
    user_id: str,
    owner_name: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Create a new personal agent.

    The agent name is auto-derived from the user's email prefix (e.g. alice@example.com → yuppclaw-alice).
    Pass user_id and owner_name from the session context.

    Args:
        display_name: Human-readable display name for the agent (e.g., "Alice's Claw").
        persona: The agent's personality, vibe, and any user preferences.
        user_id: The user ID of the agent's owner (from session context).
        owner_name: The owner's display name (from session context). Falls back to 'your owner'.
        description: Optional description of the agent.

    Returns:
        Dict with success status, agent name, and instructions to share with the user.
    """
    if _mcp_instance._create_agent_fn is None:
        return {
            "error": "create_agent callback not registered"
            " — server.py must call register_orchestration_callbacks() at startup."
        }

    if not user_id:
        return {"error": "user_id is required to create a personal agent"}

    # Resolve email prefix from user_id to derive the agent name
    try:
        async with get_async_session() as db_session:
            result = await db_session.execute(
                text("SELECT email FROM users WHERE user_id = :uid"),
                {"uid": user_id},
            )
            row = result.first()
    except Exception:
        logger.error("Failed to look up user email for agent name", user_id=user_id, exc_info=True)
        return {"error": "Failed to look up user email"}

    if not row or not row[0]:
        return {"error": f"No email found for user_id '{user_id}'"}

    email = str(row[0])
    email_prefix = re.sub(r"[^a-z0-9-]", "-", email.split("@")[0].lower()).strip("-")
    # Use the first personal agent prefix (yuppclaw)
    base_prefix = sorted(PERSONAL_AGENT_PREFIXES)[0]
    name = f"{base_prefix}-{email_prefix}"

    logger.info("MCP tool: create_agent", name=name, display_name=display_name, user_id=user_id)

    owner_name = owner_name or "your owner"

    # Build the additional_system_prompt with identity, owner info, and persona.
    # Default to first name for addressing; the creator agent may include a preferred
    # name in the persona if the user requests something different.
    first_name = owner_name.split()[0] if owner_name and " " in owner_name else owner_name
    additional_system_prompt = (
        f"# Personal Agent Identity\n\n"
        f"Your name is **{display_name}**. You are the personal agent of **{owner_name}**.\n"
        f"Address them as **{first_name}** unless they ask you to call them something else.\n\n"
        f"## Persona\n\n"
        f"{persona}\n"
    )

    # Use the default personal agent config template (mirrors SRE agent config).
    cfg = PERSONAL_AGENT_DEFAULT_CONFIG
    sandbox_raw = cfg.get("sandbox", {})
    assert isinstance(sandbox_raw, dict)
    exec_raw = cfg.get("executor_config", {})
    assert isinstance(exec_raw, dict)
    tool_perms = cfg.get("tool_permissions", {"*": "allow"})
    assert isinstance(tool_perms, dict)
    allowed_sub = cfg.get("allowed_subagents", [])
    assert isinstance(allowed_sub, list)
    allowed_gw = cfg.get("allowed_gateways", ["*"])
    assert isinstance(allowed_gw, list)

    request = AgentCreateRequest(
        name=name,
        user_id=user_id,
        display_name=display_name,
        description=description or f"{owner_name}'s personal agent",
        executor_config=ExecutorConfigRequest(
            type=str(exec_raw.get("type", "harnessed")),
            model=str(exec_raw.get("model", "")) or None,
        ),
        tool_permissions={str(k): str(v) for k, v in tool_perms.items()},
        allowed_subagents=[str(s) for s in allowed_sub],
        default_repo=str(cfg.get("default_repo", "yupp-agent")),
        sandbox=SandboxConfigRequest(
            enabled=bool(sandbox_raw.get("enabled", True)),
            auto_allow_bash_if_sandboxed=bool(sandbox_raw.get("autoAllowBashIfSandboxed", True)),
            bwrap_enabled=bool(sandbox_raw.get("bwrapEnabled", True)),
        ),
        max_turns=int(cfg.get("max_turns", 50)),
        max_budget_usd=float(cfg.get("max_budget_usd", 3.0)),
        feedback_probability=float(cfg.get("feedback_probability", 0.2)),
        feedback_min_turns=int(cfg.get("feedback_min_turns", 5)),
        timeout_s=int(cfg.get("timeout_s", 300)),
        allowed_gateways=[str(g) for g in allowed_gw],
        additional_system_prompt=additional_system_prompt,
    )

    try:
        create_result = await _mcp_instance._create_agent_fn(request)
        return {
            "success": True,
            "name": create_result.name,
            "status": create_result.status,
            "message": (
                f"Agent '{create_result.name}' created successfully. "
                f"Tell the user their personal agent name is '{create_result.name}' "
                f"and they should start a new thread mentioning @yuppclaw to talk to it."
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("Failed to create agent", name=name, error=str(e), exc_info=True)
        return {"error": f"Failed to create agent: {e}"}
