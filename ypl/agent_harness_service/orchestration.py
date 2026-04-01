"""Subagent orchestration — spawning, execution, and result collection.

Implements the new_task and route_model tool logic. Subagents run as
isolated sessions using harnessed executors (Phase 1) or raw executors (Phase 2).
"""

import asyncio
import os
import random
import time
import uuid
from decimal import Decimal
from typing import Any

from sqlmodel import select

from ypl.agent_harness_service.common.agent_registry import get_agent_spec
from ypl.agent_harness_service.common.config import (
    AgentConfig,
    SandboxConfig,
    load_agent_config,
    load_agent_config_from_db,
)
from ypl.agent_harness_service.common.constants import (
    AHS_AGENTS_DIR,
    ALL_MCP_SERVERS,
    BLOCKED_HARNESS_TOOLS,
    DEFAULT_MODEL_ANTHROPIC,
    DEFAULT_TIMEOUT_S,
    EXECUTOR_TYPE_HARNESSED,
    EXECUTOR_TYPE_RAW,
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
    PERM_DENY,
)
from ypl.agent_harness_service.common.models import AgentSpec, ExecutorConfig, ExecutorResult, SubagentSession
from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.agent_harness_service.core.subagent_queue import MAX_SUBAGENT_DEPTH, SubagentResult, put_result
from ypl.agent_harness_service.executors.codex_app_server_runner import CodexAppServerRunner
from ypl.agent_harness_service.executors.providers import KNOWN_MODELS, parse_model_string, resolve_model
from ypl.agent_harness_service.executors.raw_executor import run_raw_executor
from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner, RunContext
from ypl.agent_harness_service.tools.mcp_client import MCPToolAccess
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()

DEFAULT_SUBAGENT_BUDGET_USD = 5.0

# Maps subagent db_session_id (UUID) -> asyncio.Task for running subagent tasks.
# Used by cancel_subagent_tasks() to cascade stop signals to subagents.
_active_subagent_tasks: dict[uuid.UUID, asyncio.Task] = {}


def _try_parse_uuid(id_str: str | None) -> uuid.UUID | None:
    """Parse a string as UUID, returning None if invalid or empty."""
    if not id_str:
        return None
    try:
        return uuid.UUID(id_str)
    except ValueError:
        return None


async def _create_db_session(
    agent_name: str,
    parent_session_id: str | None,
    model: str,
    workspace: str | None = None,
    context: dict | None = None,
    pre_session_id: str | None = None,
) -> uuid.UUID | None:
    """Create a DB AgentSession row for a subagent, linked to the parent session.

    Args:
        agent_name: Name of the subagent.
        parent_session_id: UUID string of the parent session.
        model: LLM model string.
        workspace: Workspace path inherited from parent.
        context: Session context dict (permissions, subagent_depth, etc.).
        pre_session_id: Pre-generated UUID to use as agent_session_id. When provided,
            the caller can return this ID immediately (fire-and-forget) before the DB
            write completes.

    Returns the new session's UUID, or None if the agent isn't in the DB.
    """
    try:
        async with get_async_session() as session:
            result = await session.exec(select(Agent).where(Agent.name == agent_name))
            agent = result.one_or_none()
            if not agent:
                logger.warning("Subagent not in DB, skipping session creation", agent_name=agent_name)
                return None

            parent_uuid = uuid.UUID(parent_session_id) if parent_session_id else None

            # Propagate creator_user_id from parent session so subagent sessions
            # are attributed to the human who initiated the top-level session.
            creator_user_id: str | None = None
            if parent_uuid:
                parent = await session.get(AgentSession, parent_uuid)
                if parent:
                    creator_user_id = parent.creator_user_id

            agent_session = AgentSession(
                agent_id=agent.agent_id,
                parent_session_id=parent_uuid,
                trigger=AgentSessionTrigger.API,
                model=model,
                workspace=workspace,
                context=context,
                status=AgentSessionStatus.ACTIVE,
                creator_user_id=creator_user_id,
            )
            # Use pre-generated session ID when provided (fire-and-forget callers
            # need the ID before the DB write completes).
            if pre_session_id:
                agent_session.agent_session_id = uuid.UUID(pre_session_id)

            session.add(agent_session)
            await session.commit()
            await session.refresh(agent_session)
            return agent_session.agent_session_id
    except Exception:
        logger.error("Failed to create DB session for subagent", agent_name=agent_name, exc_info=True)
        return None


async def _update_db_session_status(
    session_id: uuid.UUID,
    status: AgentSessionStatus,
) -> None:
    """Update a subagent's DB session status."""
    try:
        async with get_async_session() as session:
            result = await session.exec(select(AgentSession).where(AgentSession.agent_session_id == session_id))
            agent_session = result.one_or_none()
            if agent_session:
                agent_session.status = status
                await session.commit()
    except Exception:
        logger.error("Failed to update DB session status", session_id=str(session_id), exc_info=True)


async def _persist_subagent_messages(
    db_session_id: uuid.UUID,
    prompt: str,
    result: ExecutorResult,
    model: str,
    raw_events: list[dict[str, Any]] | None = None,
) -> None:
    """Persist USER and AGENT messages for a subagent session.

    Without this, subagent sessions exist in the DB (with parent_session_id
    linkage) but have zero AgentSessionMessage rows — making them invisible
    in the Streamlit console's thread view.
    """
    try:
        async with get_async_session() as session:
            # Look up the subagent session's creator_user_id (propagated from parent)
            # so USER messages are attributed to the human who initiated the chain.
            agent_session = await session.get(AgentSession, db_session_id)
            creator_user_id = agent_session.creator_user_id if agent_session else None

            # Persist the user prompt (turn 1)
            user_msg = AgentSessionMessage(
                agent_session_id=db_session_id,
                turn_number=1,
                role=AgentSessionMessageRole.USER,
                content=prompt[:10_000],  # cap prompt for storage
                llm_name=model,
                creator_user_id=creator_user_id,
            )
            session.add(user_msg)

            # Persist the agent response (turn 1)
            agent_msg = AgentSessionMessage(
                agent_session_id=db_session_id,
                turn_number=1,
                role=AgentSessionMessageRole.AGENT,
                content=result.text,
                raw_events=raw_events,
                llm_name=model,
                cost_usd=Decimal(str(result.estimated_cost_usd)) if result.estimated_cost_usd is not None else None,
                duration_ms=result.duration_ms,
            )
            session.add(agent_msg)
            await session.commit()
    except Exception:
        logger.error(
            "Failed to persist subagent messages",
            db_session_id=str(db_session_id),
            exc_info=True,
        )


async def cancel_subagent_tasks(parent_session_id: uuid.UUID) -> int:
    """Cancel all running subagent tasks that are children of the given session.

    Queries the DB for ACTIVE child sessions (recursively) and cancels
    their tracked tasks, which kills their CLI subprocesses.

    Returns:
        Number of subagent tasks cancelled.
    """
    # Collect all descendant session IDs via recursive DB query
    descendant_ids: list[uuid.UUID] = []
    to_visit = [parent_session_id]

    try:
        async with get_async_session() as session:
            while to_visit:
                current_id = to_visit.pop()
                result = await session.exec(
                    select(AgentSession.agent_session_id).where(
                        AgentSession.parent_session_id == current_id,
                        AgentSession.status == AgentSessionStatus.ACTIVE,
                    )
                )
                children = result.all()
                for child_id in children:
                    descendant_ids.append(child_id)
                    to_visit.append(child_id)
    except Exception:
        logger.error(
            "Failed to query descendant sessions for cancellation",
            parent_session_id=str(parent_session_id),
            exc_info=True,
        )
        raise

    # Cancel tracked tasks for each descendant
    cancelled = 0
    for child_id in descendant_ids:
        task = _active_subagent_tasks.pop(child_id, None)
        if task and not task.done():
            task.cancel()
            cancelled += 1
            logger.info(
                "Cancelled subagent task",
                parent_session_id=str(parent_session_id),
                subagent_session_id=str(child_id),
            )

    if cancelled:
        logger.info(
            "Cancelled subagent tasks",
            parent_session_id=str(parent_session_id),
            total_descendants=len(descendant_ids),
            cancelled=cancelled,
        )

    return cancelled


async def run_subagent(
    agent_type: str,
    prompt: str,
    description: str = "",
    model: str | None = None,
    parent_session_id: str | None = None,
    parent_model: str | None = None,
    parent_workspace: str | None = None,
    parent_permissions: dict[str, Any] | None = None,
    parent_depth: int = 0,
    pre_session_id: str | None = None,
) -> ExecutorResult:
    """Spawn and run a subagent to completion.

    This is the core implementation of the new_task tool. It:
    1. Enforces subagent depth limit (max 3 levels: root=0, child=1, grandchild=2)
    2. Resolves the agent config (spec registry, filesystem, or DB fallback)
    3. Resolves the model (param > agent config > parent)
    4. Creates a SubagentSession for tracking
    5. Runs the agent via the appropriate executor
    6. Pushes the result onto the parent's delivery queue

    Args:
        agent_type: Agent config name (e.g., 'reviewer', 'fixer').
        prompt: The task for the subagent.
        description: Short description for tracking.
        model: Optional model override (e.g., 'anthropic/claude-sonnet-4-6').
        parent_session_id: Parent session ID for linking.
        parent_model: Parent's model for fallback resolution.
        parent_workspace: Parent's workspace path for file access.
        parent_permissions: Serialized SessionPermissions dict from parent context.
        parent_depth: Nesting depth of the parent session (0 = root).
        pre_session_id: Pre-generated UUID for the new session (for fire-and-forget).

    Returns:
        ExecutorResult with the subagent's output.
    """
    start_time = time.monotonic()

    # Enforce subagent depth limit
    if parent_depth >= MAX_SUBAGENT_DEPTH:
        msg = (
            f"[ERROR] Subagent depth limit ({MAX_SUBAGENT_DEPTH}) reached. "
            f"Cannot spawn '{agent_type}' at depth {parent_depth + 1}."
        )
        logger.warning(
            "Subagent depth limit exceeded",
            agent_type=agent_type,
            parent_depth=parent_depth,
            max_depth=MAX_SUBAGENT_DEPTH,
            parent_session_id=parent_session_id,
        )
        result = ExecutorResult(text=msg)
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id="",
                    agent_type=agent_type,
                    model=model or "",
                    status="error",
                    text=msg,
                    description=description,
                ),
            )
        return result

    # Resolve agent config: try spec registry first, fall back to filesystem, then DB
    agent_spec = get_agent_spec(agent_type)
    fs_config = load_agent_config(agent_type) if not agent_spec else None

    if not agent_spec and not fs_config:
        # DB fallback: handle agents created after startup (not in _RUNTIME_CONFIGS yet)
        try:
            async with get_async_session() as _db_s:
                _db_result = await _db_s.exec(select(Agent).where(Agent.name == agent_type))
                _db_agent = _db_result.one_or_none()
                if _db_agent and _db_agent.config:
                    fs_config = load_agent_config_from_db(_db_agent)
                    logger.info("Agent resolved from DB fallback", agent_type=agent_type)
        except Exception:
            logger.warning("DB fallback for agent config failed", agent_type=agent_type, exc_info=True)

    if not agent_spec and not fs_config:
        msg = f"[ERROR] Unknown agent type: {agent_type!r}. Not found in predefined configs, filesystem, or DB."
        result = ExecutorResult(text=msg)
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id="",
                    agent_type=agent_type,
                    model=model or "",
                    status="error",
                    text=msg,
                    description=description,
                ),
            )
        return result

    # Resolve model (with executor-appropriate default fallback).
    # For raw executors, executor.model is the LLM model (provider/model_id).
    # For harnessed executors, executor.model is the CLI name — not an LLM model.
    agent_model = agent_spec.executor.llm_model if agent_spec else None
    try:
        resolved_model = resolve_model(model, agent_model, parent_model, default_model=DEFAULT_MODEL_ANTHROPIC)
    except ValueError as e:
        msg = f"[ERROR] Model resolution failed: {e}"
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id="",
                    agent_type=agent_type,
                    model=model or "",
                    status="error",
                    text=msg,
                    description=description,
                ),
            )
        return ExecutorResult(text=msg)

    # Determine timeout
    timeout_s = DEFAULT_TIMEOUT_S
    if agent_spec:
        timeout_s = agent_spec.timeout_s
    elif fs_config:
        timeout_s = fs_config.timeout_s

    # Create tracking session (in-memory)
    session = SubagentSession(
        parent_session_id=parent_session_id or "",
        agent_name=agent_type,
        model=resolved_model,
        executor_type=_resolve_executor_type(agent_spec, fs_config),
    )

    # Build context for subagent session (propagate permissions from parent)
    subagent_context: dict = {}
    if parent_permissions is not None:
        subagent_context["permissions"] = parent_permissions
    # Track depth so nested subagents can enforce the limit and system_prompt can load SUBAGENT.md
    subagent_context["subagent_depth"] = parent_depth + 1

    # Persist to DB with parent link (include workspace so nested subagents inherit it)
    db_session_id = await _create_db_session(
        agent_type,
        parent_session_id,
        resolved_model,
        workspace=parent_workspace,
        context=subagent_context,
        pre_session_id=pre_session_id,
    )

    # Derive effective permissions for logging
    _perms = SessionPermissions.from_context(subagent_context or {})

    # Log tool_permissions and subagent constraints from whichever config was resolved
    _tool_perms = dict(agent_spec.tools) if agent_spec else (fs_config.tool_permissions if fs_config else {})
    _allowed_subagents = (
        agent_spec.allowed_subagents if agent_spec else (fs_config.allowed_subagents if fs_config else [])
    )
    _max_steps = agent_spec.max_steps if agent_spec else (fs_config.max_turns if fs_config else None)

    logger.info(
        "Spawning subagent",
        agent_type=agent_type,
        model=resolved_model,
        executor_type=session.executor_type,
        description=description,
        session_id=session.id,
        db_session_id=str(db_session_id) if db_session_id else None,
        parent_session_id=parent_session_id,
        tool_permissions=_tool_perms,
        allowed_subagents=_allowed_subagents,
        max_steps=_max_steps,
        timeout_s=timeout_s,
        allowed_servers=_perms.allowed_servers,
        allowed_harness_tools=_perms.allowed_harness_tools,
    )

    # Use db_session_id as the effective session identity so nested subagents
    # can resolve parent context (model, workspace, allowed_subagents) via DB lookup.
    # Falls back to in-memory session.id if DB insert failed.
    effective_session_id = str(db_session_id) if db_session_id else session.id

    # Wrap the execution in a trackable task so cancel_subagent_tasks() can cancel it.
    execute_task = asyncio.ensure_future(
        _execute_subagent(
            agent_spec=agent_spec,
            fs_config=fs_config,
            prompt=prompt,
            model=resolved_model,
            session=session,
            workspace=parent_workspace,
            effective_session_id=effective_session_id,
            session_context=subagent_context,
            param_model=model,
        )
    )
    if db_session_id:
        _active_subagent_tasks[db_session_id] = execute_task

    _child_session_id_str = str(db_session_id) if db_session_id else (pre_session_id or "unknown")

    try:
        result = await asyncio.wait_for(asyncio.shield(execute_task), timeout=timeout_s)
    except TimeoutError:
        execute_task.cancel()
        try:
            await execute_task
        except (asyncio.CancelledError, Exception):
            pass
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        session.status = "error"
        session.time_completed = time.time()
        if db_session_id:
            await _update_db_session_status(db_session_id, AgentSessionStatus.STALE)
        logger.error(
            "Subagent timed out",
            agent_type=agent_type,
            session_id=session.id,
            timeout_s=timeout_s,
            elapsed_ms=elapsed_ms,
        )
        err_result = ExecutorResult(
            text=f"[ERROR] Subagent '{agent_type}' timed out after {timeout_s}s.",
            duration_ms=elapsed_ms,
            session_id=session.id,
        )
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id=_child_session_id_str,
                    agent_type=agent_type,
                    model=resolved_model,
                    status="timeout",
                    text=err_result.text,
                    duration_ms=elapsed_ms,
                    description=description,
                ),
            )
        return err_result
    except asyncio.CancelledError:
        execute_task.cancel()
        try:
            await execute_task
        except (asyncio.CancelledError, Exception):
            pass
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        session.status = "error"
        session.time_completed = time.time()
        if db_session_id:
            await _update_db_session_status(db_session_id, AgentSessionStatus.STALE)
        logger.info(
            "Subagent cancelled (parent stop)",
            agent_type=agent_type,
            session_id=session.id,
            elapsed_ms=elapsed_ms,
        )
        cancel_result = ExecutorResult(
            text=f"[INTERRUPTED] Subagent '{agent_type}' cancelled.",
            duration_ms=elapsed_ms,
            session_id=session.id,
        )
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id=_child_session_id_str,
                    agent_type=agent_type,
                    model=resolved_model,
                    status="cancelled",
                    text=cancel_result.text,
                    duration_ms=elapsed_ms,
                    description=description,
                ),
            )
        return cancel_result
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        session.status = "error"
        session.time_completed = time.time()
        if db_session_id:
            await _update_db_session_status(db_session_id, AgentSessionStatus.STALE)
        logger.error(
            "Subagent failed",
            agent_type=agent_type,
            session_id=session.id,
            error=str(e),
            exc_info=True,
        )
        fail_result = ExecutorResult(
            text=f"[ERROR] Subagent '{agent_type}' failed: {e}",
            duration_ms=elapsed_ms,
            session_id=session.id,
        )
        if parent_session_id:
            put_result(
                parent_session_id,
                SubagentResult(
                    db_session_id=_child_session_id_str,
                    agent_type=agent_type,
                    model=resolved_model,
                    status="error",
                    text=fail_result.text,
                    duration_ms=elapsed_ms,
                    description=description,
                ),
            )
        return fail_result
    finally:
        if db_session_id:
            _active_subagent_tasks.pop(db_session_id, None)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    session.status = "completed"
    session.time_completed = time.time()
    session.result = result
    if db_session_id:
        await _update_db_session_status(db_session_id, AgentSessionStatus.COMPLETED)

    # Ensure duration is set
    if result.duration_ms is None:
        result.duration_ms = elapsed_ms
    result.session_id = session.id

    logger.info(
        "Subagent completed",
        agent_type=agent_type,
        session_id=session.id,
        db_session_id=str(db_session_id) if db_session_id else None,
        model=resolved_model,
        duration_ms=result.duration_ms,
        cost_usd=result.estimated_cost_usd,
        text_length=len(result.text),
    )

    # Deliver result to parent session queue
    if parent_session_id:
        put_result(
            parent_session_id,
            SubagentResult(
                db_session_id=_child_session_id_str,
                agent_type=agent_type,
                model=resolved_model,
                status="completed",
                text=result.text,
                duration_ms=result.duration_ms,
                cost_usd=result.estimated_cost_usd,
                description=description,
            ),
        )

    return result


def _resolve_executor_type(
    agent_spec: AgentSpec | None,
    fs_config: AgentConfig | None,
) -> str:
    """Determine executor type from config."""
    if agent_spec:
        return agent_spec.executor.type
    return EXECUTOR_TYPE_HARNESSED


async def _execute_subagent(
    agent_spec: AgentSpec | None,
    fs_config: AgentConfig | None,
    prompt: str,
    model: str,
    session: SubagentSession,
    workspace: str | None,
    effective_session_id: str | None = None,
    session_context: dict | None = None,
    param_model: str | None = None,
) -> ExecutorResult:
    """Execute a subagent using the appropriate runner.

    Routing logic:
    - Raw executor (agent_spec.executor.type == "raw") → direct API calls
    - Harnessed executor → Claude Code CLI or Codex CLI subprocess

    Args:
        param_model: Explicit model override from new_task() call (not a fallback).
            Used by harnessed executors to avoid inheriting parent LLM models.
    """
    # Check if this should use the raw executor
    if agent_spec and agent_spec.executor.type == EXECUTOR_TYPE_RAW:
        perms = SessionPermissions.from_context(session_context or {})
        _db_sid = _try_parse_uuid(effective_session_id)
        return await _execute_raw(
            agent_spec,
            prompt,
            model,
            session_id=effective_session_id,
            permissions=perms,
            db_session_id=_db_sid,
            session_context=session_context,
        )

    # Harnessed executor path
    return await _execute_harnessed(
        agent_spec,
        fs_config,
        prompt,
        model,
        session,
        workspace,
        effective_session_id,
        session_context,
        param_model=param_model,
    )


async def _execute_raw(
    config: AgentSpec,
    prompt: str,
    model: str,
    session_id: str | None = None,
    permissions: SessionPermissions | None = None,
    db_session_id: uuid.UUID | None = None,
    session_context: dict[str, Any] | None = None,
) -> ExecutorResult:
    """Execute a subagent via the raw executor (direct API calls)."""
    from ypl.agent_harness_service.tools.local_mcp_server import (
        clear_session_sandbox,
        clear_session_websearch_count,
        reset_turn_websearch_count,
        set_session_sandbox,
    )

    # Register bwrap=False for raw executor subagents (AgentSpec doesn't have sandbox config yet).
    # This ensures mcp_bash can look up the setting; default to off.
    if session_id:
        set_session_sandbox(session_id, False)
        reset_turn_websearch_count(session_id)

    # Collect events from the raw executor for DB persistence
    collected_events: list[dict[str, Any]] = []

    def _on_event(event: dict[str, Any]) -> None:
        collected_events.append(event)

    try:
        # Phase 3: Construct session history path for cross-turn persistence
        session_history_path: str | None = None
        if session_id and config.executor.history.enabled:
            from ypl.agent_harness_service.executors.context import get_session_history_path

            session_history_path = get_session_history_path(session_id)

        # Merge session-level restrictions into agent-level tool config.
        # Tools not in the allowed list are explicitly denied.
        effective_tools: dict[str, str] = dict(config.tools)
        if permissions and not permissions.has_full_tool_access:
            for tool in BLOCKED_HARNESS_TOOLS:
                if tool not in permissions.allowed_harness_tools:
                    effective_tools[tool] = PERM_DENY
        allowed_servers = frozenset(permissions.allowed_servers) if permissions else frozenset(ALL_MCP_SERVERS)
        is_slack = bool(session_context and session_context.get("slack_channel_id"))
        slack_session_id = session_context.get("slack_session_id") if session_context else None
        requesting_user_id = (
            (session_context.get("current_turn_user_id") or session_context.get("user_id")) if session_context else None
        )
        async with MCPToolAccess(
            session_id, effective_tools, allowed_servers=allowed_servers, user_id=requesting_user_id
        ) as mcp:
            result = await run_raw_executor(
                agent=config,
                prompt=prompt,
                model=model,
                mcp_tools=mcp.mcp_tools,
                tool_executor=mcp.call_tool,
                session_history_path=session_history_path,
                resource_catalog=mcp.resource_catalog or None,
                on_event=_on_event,
                session_id=session_id,
                is_slack=is_slack,
                session_context=session_context,
                slack_session_id=slack_session_id,
            )

        # Persist messages so subagent sessions are visible in the console
        if db_session_id:
            await _persist_subagent_messages(db_session_id, prompt, result, model, raw_events=collected_events or None)

        return result
    finally:
        if session_id:
            clear_session_sandbox(session_id)
            clear_session_websearch_count(session_id)


async def _execute_harnessed(
    agent_spec: AgentSpec | None,
    fs_config: AgentConfig | None,
    prompt: str,
    model: str,
    session: SubagentSession,
    workspace: str | None,
    effective_session_id: str | None = None,
    session_context: dict | None = None,
    param_model: str | None = None,
) -> ExecutorResult:
    """Execute a subagent via a harnessed executor (CLI subprocess)."""
    from ypl.agent_harness_service.tools.local_mcp_server import (
        clear_session_sandbox,
        clear_session_websearch_count,
        reset_turn_websearch_count,
        set_session_sandbox,
    )

    # Build a filesystem-compatible AgentConfig for the runner
    runner_config = _build_runner_config(agent_spec, fs_config, model, param_model=param_model)

    # Register bwrap sandbox setting for harnessed subagents too.
    # When has_mcp=True, the agent can call mcp__harness__bash which looks up
    # _session_sandbox to decide whether to use bwrap.
    bwrap_enabled = runner_config.sandbox.bwrap_enabled if runner_config.sandbox else False
    sid = effective_session_id or session.id
    set_session_sandbox(sid, bwrap_enabled)
    reset_turn_websearch_count(sid)

    try:
        # Determine which runner to use
        runner: CodexAppServerRunner | ClaudeCodeRunner
        if runner_config.executor_config.model in (HARNESS_CODEX_CLI, HARNESS_CODEX_APP_SERVER):
            runner = CodexAppServerRunner(runner_config)
        else:
            runner = ClaudeCodeRunner(runner_config)

        # Use effective_session_id (DB session UUID) so nested subagents can resolve
        # parent context via DB lookup. Falls back to in-memory session.id.
        run_context = RunContext(
            session_id=sid,
            workspace=workspace,
            session_context=session_context,
        )

        # Collect output and raw events for DB persistence
        final_text = ""
        cost_usd: float | None = None
        duration_ms: int | None = None
        num_turns: int | None = None
        llm_session_id: str | None = None
        # TODO: apply _trim_value() to event.raw before appending — harnessed executor
        # events can include large tool results (file reads, command outputs). The raw
        # executor path already caps tool_result output at 500 chars. (see PR #10638)
        collected_events: list[dict[str, Any]] = []

        async for event in runner.run(prompt, run_context):
            collected_events.append(event.raw)
            if event.type == "assistant" and event.text:
                if final_text:
                    final_text += "\n\n"
                final_text += event.text
            elif event.type == "result":
                llm_session_id = event.session_id
                cost_usd = event.cost_usd
                duration_ms = event.duration_ms
                num_turns = event.num_turns
            elif event.type == "error":
                error_msg = event.raw.get("error", "Unknown error")
                if final_text:
                    final_text += f"\n\n[ERROR] {error_msg}"
                else:
                    final_text = f"[ERROR] {error_msg}"

        result = ExecutorResult(
            text=final_text or "(no output)",
            estimated_cost_usd=cost_usd,
            duration_ms=duration_ms,
            tokens={"num_turns": num_turns} if num_turns else None,
            session_id=llm_session_id,
        )

        # Persist messages so subagent sessions are visible in the console
        _db_sid = _try_parse_uuid(effective_session_id)
        if _db_sid:
            await _persist_subagent_messages(_db_sid, prompt, result, model, raw_events=collected_events or None)

        return result
    finally:
        clear_session_sandbox(sid)
        clear_session_websearch_count(sid)


def _build_runner_config(
    agent_spec: AgentSpec | None,
    fs_config: AgentConfig | None,
    model: str,
    param_model: str | None = None,
) -> AgentConfig:
    """Build a runner-compatible AgentConfig from agent spec or filesystem config.

    Args:
        agent_spec: Resolved agent specification (from spec registry).
        fs_config: Filesystem agent configuration (from config.json).
        model: Resolved model string (always set, may be a fallback).
        param_model: Explicit model override from new_task() call, if any.
            For harnessed executors, only param_model is used (not fallback model).
    """
    if fs_config:
        # Use filesystem config directly, override the LLM model if specified.
        # For harnessed executors, only use explicit param_model (not fallback).
        config = fs_config.model_copy()
        _effective = param_model if config.executor_config.type == EXECUTOR_TYPE_HARNESSED else model
        if _effective:
            _, model_id = parse_model_string(_effective)
            config.llm_model = model_id
        return config

    if agent_spec:
        # Convert agent spec to runner-compatible AgentConfig.
        # executor_config.model is the CLI name for harnessed executors;
        # the LLM model goes in llm_model.
        #
        # For harnessed executors, only set llm_model if the model was explicitly
        # requested (param_model from new_task). Fallback models (parent_model,
        # default_model) are LLM model strings (e.g., "anthropic/claude-sonnet-4-6")
        # that are meaningless to CLI harnesses like codex-cli — passing them causes
        # the Codex API to reject unsupported models.
        llm_model_id: str | None = None
        _effective_model = param_model if agent_spec.executor.type == EXECUTOR_TYPE_HARNESSED else model
        if _effective_model:
            _, llm_model_id = parse_model_string(_effective_model)

        return AgentConfig(
            name=agent_spec.name,
            config_dir=os.path.join(AHS_AGENTS_DIR, agent_spec.name),
            executor_config=ExecutorConfig(
                type=agent_spec.executor.type,
                model=agent_spec.executor.model,
            ),
            llm_model=llm_model_id,
            tool_permissions=dict(agent_spec.tools),
            max_turns=agent_spec.max_steps or 50,
            max_budget_usd=DEFAULT_SUBAGENT_BUDGET_USD,
            has_mcp=True,
            sandbox=SandboxConfig(enabled=True, auto_allow_bash_if_sandboxed=True),
            timeout_s=agent_spec.timeout_s,
        )

    raise ValueError("No agent config available")


def route_model_stub(
    task_description: str,
    count: int = 1,
    candidates: list[str] | None = None,
) -> list[str]:
    """Stub route_model implementation — returns diverse models.

    Phase 1: Simple stub that returns models from different providers.
    Future: Rule-based or LLM-based routing.

    Args:
        task_description: What the task is about.
        count: How many models to return.
        candidates: Restrict to these models (optional).

    Returns:
        List of model IDs (e.g., ['anthropic/claude-sonnet-4-6', 'openai/gpt-4o']).
    """
    pool = candidates or list(KNOWN_MODELS)

    if count <= 0:
        return []

    if count == 1:
        return [pool[0] if pool else "anthropic/claude-sonnet-4-6"]

    # For count > 1: ensure provider diversity
    selected: list[str] = []
    providers_used: set[str] = set()

    # Shuffle to add some variety
    shuffled = list(pool)
    random.shuffle(shuffled)

    # First pass: pick one from each provider
    for m in shuffled:
        if len(selected) >= count:
            break
        try:
            provider, _ = parse_model_string(m)
        except ValueError:
            continue
        if provider not in providers_used:
            selected.append(m)
            providers_used.add(provider)

    # Second pass: fill remaining slots if needed
    for m in shuffled:
        if len(selected) >= count:
            break
        if m not in selected:
            selected.append(m)

    logger.info(
        "route_model stub",
        task=task_description,
        count=count,
        selected=selected,
    )

    return selected[:count]
