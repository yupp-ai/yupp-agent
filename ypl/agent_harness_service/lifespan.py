"""AHS application lifespan — startup and shutdown logic.

Exposes two importable functions that manage all initialisation and teardown
for the Agent Harness Service.  Both the standalone server
(ypl/agent_harness_service/server.py) and the future monolith entry-point
(ypl/mono_server/) call these functions so startup/shutdown behaviour stays
identical regardless of which entry-point is used.

Usage in server.py::

    from ypl.agent_harness_service.lifespan import AHSState, ahs_startup, ahs_shutdown

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        state = await ahs_startup(app, mcp_app)
        try:
            yield
        finally:
            await ahs_shutdown(state)
"""

import asyncio
import glob
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from sqlmodel import col, func, select

from ypl.agent_harness_service.common.config import AgentConfig, discover_agents
from ypl.agent_harness_service.common.constants import (
    AHS_AUTO_STALE_INTERVAL_S,
    AHS_SESSION_STALE_TIMEOUT_HOURS,
)
from ypl.agent_harness_service.core.streaming import init_streaming, shutdown_streaming
from ypl.agent_harness_service.core.subagent_queue import register_delivery_callback
from ypl.agent_harness_service.executors.codex_app_server_runner import shutdown_codex_servers
from ypl.agent_harness_service.gateway import init_gateways
from ypl.agent_harness_service.orchestration import route_model_stub, run_subagent
from ypl.agent_harness_service.scheduler import SCHEDULER_ENABLED, run_scheduler, wait_for_in_flight_tasks
from ypl.agent_harness_service.service import send_slack_restart_courtesy, send_slack_shutdown_courtesy
from ypl.agent_harness_service.task_executor import (
    TASK_EXECUTOR_ENABLED,
    wait_for_in_flight_task_executions,
)
from ypl.agent_harness_service.tools.github_token_storage import close_store as close_github_token_store
from ypl.agent_harness_service.tools.local_mcp_server import register_orchestration_callbacks
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentExecutorType,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
)
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()

# Sessions older than this threshold are considered stale on startup (i.e. left
# ACTIVE from the previous process and not currently being processed).
_STALE_SESSION_THRESHOLD_MINUTES = 15


# ---------------------------------------------------------------------------
# State object
# ---------------------------------------------------------------------------


@dataclass
class AHSState:
    """Runtime state produced by ahs_startup() and consumed by ahs_shutdown().

    Fields:
        scheduler_task:    Background asyncio.Task running the scheduler loop,
                           or None if the scheduler is disabled.
        auto_stale_task:   Background asyncio.Task running the periodic stale
                           session sweep.
        mcp_app:           The FastMCP HTTP sub-application (needed so
                           ahs_shutdown() can exit its lifespan context).
        _mcp_lifespan_ctx: The entered async context manager for mcp_app's
                           lifespan.  Set by ahs_startup(); exited by
                           ahs_shutdown().
    """

    scheduler_task: asyncio.Task[None] | None
    auto_stale_task: asyncio.Task[None]
    mcp_app: Any
    _mcp_lifespan_ctx: Any = field(repr=False)


# ---------------------------------------------------------------------------
# Private helpers (previously in server.py)
# ---------------------------------------------------------------------------


def _agent_config_to_db_fields(config: AgentConfig) -> dict[str, Any]:
    """Extract DB-storable fields from an AgentConfig."""
    executor_type_str = config.executor_config.type.upper()
    try:
        executor_type = AgentExecutorType(executor_type_str)
    except ValueError:
        executor_type = AgentExecutorType.HARNESSED

    # Store the full config.json content as JSONB for DB-only consumers
    config_jsonb: dict[str, Any] = {
        "executor_config": {
            "type": config.executor_config.type,
        },
        "tool_permissions": {k: str(v) for k, v in config.tool_permissions.items()},
        "allowed_subagents": config.allowed_subagents,
        "default_repo": config.default_repo,
        "max_turns": config.max_turns,
        "max_budget_usd": config.max_budget_usd,
        "timeout_s": config.timeout_s,
        "sandbox": {
            "enabled": config.sandbox.enabled,
        },
        "allowed_gateways": config.allowed_gateways,
    }
    if config.executor_config.model:
        config_jsonb["executor_config"]["model"] = config.executor_config.model

    return {
        "display_name": config.display_name or config.name,
        "description": config.description,
        "executor_type": executor_type,
        "executor_model": config.executor_config.model if config.executor_config.type == "raw" else None,
        "config": config_jsonb,
    }


async def _sync_agents_to_db(agents: dict[str, AgentConfig]) -> None:
    """Ensure all discovered agent configs have matching DB rows.

    Inserts new agents and updates existing ones so the DB always reflects
    the on-disk config (display_name, description, executor_type, config JSONB).
    DB-only fields (creator_user_id, additional_system_prompt) are preserved.
    """
    async with get_async_session() as session:
        result = await session.exec(select(Agent).where(Agent.name.in_(list(agents.keys()))))  # type: ignore[attr-defined]
        existing_agents = {agent.name: agent for agent in result.all()}

        new_agents: list[Agent] = []
        updated_count = 0

        for name, config in agents.items():
            db_fields = _agent_config_to_db_fields(config)

            if name in existing_agents:
                # Update existing agent with current disk config
                agent = existing_agents[name]
                changed_fields: list[str] = []
                for field_name, value in db_fields.items():
                    if getattr(agent, field_name) != value:
                        setattr(agent, field_name, value)
                        changed_fields.append(field_name)
                if changed_fields:
                    session.add(agent)
                    updated_count += 1
                    logger.debug("Agent config changed", agent_name=name, changed_fields=changed_fields)
            else:
                new_agents.append(Agent(name=name, **db_fields))

        for agent in new_agents:
            session.add(agent)

        if new_agents or updated_count:
            await session.commit()

        if new_agents:
            logger.info(
                "Registered new agents to DB",
                count=len(new_agents),
                names=[a.name for a in new_agents],
            )
        if updated_count:
            logger.info("Updated existing agents in DB", count=updated_count)


async def _recover_stale_sessions() -> None:
    """On startup, resolve sessions left ACTIVE from the previous AHS process.

    Two scenarios are handled:

    1. **Normal completion, status not written** — Sessions whose last non-USER
       message (AGENT or SYSTEM) has a terminal completion_status (SUCCESS, FAILED,
       or ABORTED) were completed but the DB update was skipped (the previous code
       skipped SLACK sessions).  These are transitioned to COMPLETED.

    2. **Mid-turn interruption** — Sessions killed by SIGTERM/crash while a turn was
       still in-flight have no terminal non-USER message (or only an IN_PROGRESS
       AGENT draft).  These are transitioned to STALE so monitoring dashboards can
       detect them.

    Only top-level sessions (parent_session_id IS NULL) older than
    _STALE_SESSION_THRESHOLD_MINUTES are examined — subagent sessions are managed
    by the orchestrator which already writes their final status.
    """
    stale_threshold = datetime.now(UTC) - timedelta(minutes=_STALE_SESSION_THRESHOLD_MINUTES)

    async with get_async_session() as session:
        result = await session.exec(
            select(AgentSession)
            .where(AgentSession.status == AgentSessionStatus.ACTIVE)
            .where(col(AgentSession.modified_at) < stale_threshold)
            .where(col(AgentSession.parent_session_id).is_(None))
        )
        stale_sessions = result.all()

        if not stale_sessions:
            return

        completed_count = 0
        stale_count = 0
        # Collect IDs of sessions we classify as STALE so we can send courtesy
        # messages after the DB commit (outside the session context).
        stale_session_ids: list[uuid.UUID] = []

        for s in stale_sessions:
            # Find the most recent non-USER (terminal) message for this session.
            # Terminal turns are persisted as AGENT (normal completion) or SYSTEM
            # (config-not-found, runner crash, explicit interruption).  Querying only
            # AGENT misses SYSTEM terminal turns, causing those sessions to be
            # incorrectly classified as STALE.
            msg_result = await session.exec(
                select(AgentSessionMessage)
                .where(AgentSessionMessage.agent_session_id == s.agent_session_id)
                .where(col(AgentSessionMessage.role) != AgentSessionMessageRole.USER)
                .order_by(
                    col(AgentSessionMessage.turn_number).desc(),
                    col(AgentSessionMessage.created_at).desc(),
                )
                .limit(1)
            )
            latest_terminal_msg = msg_result.one_or_none()

            # Also check the latest USER turn number.  If a crash happened AFTER
            # committing the USER message for turn N but BEFORE persisting the
            # eager-AGENT draft row, latest_terminal_msg comes from a prior
            # completed turn (N-1) and would incorrectly classify the session as
            # COMPLETED.  Guard against this by checking whether the highest USER
            # turn number exceeds the turn of the latest terminal message — if so,
            # the session has an unanswered turn and must be STALE.
            latest_user_turn_result = await session.exec(
                select(func.max(AgentSessionMessage.turn_number)).where(
                    AgentSessionMessage.agent_session_id == s.agent_session_id,
                    col(AgentSessionMessage.role) == AgentSessionMessageRole.USER,
                )
            )
            latest_user_turn: int | None = latest_user_turn_result.one()

            has_unanswered_user_turn = latest_user_turn is not None and (
                latest_terminal_msg is None or latest_user_turn > latest_terminal_msg.turn_number
            )

            if (
                has_unanswered_user_turn
                or latest_terminal_msg is None
                or (latest_terminal_msg.completion_status == AgentSessionMessageCompletionStatus.IN_PROGRESS)
            ):
                # Session was interrupted mid-turn (SIGTERM/crash): the CLI was killed
                # while the eager-persist draft was still IN_PROGRESS, or no terminal
                # message was ever persisted, or a USER message was committed but the
                # agent never responded.  Mark STALE so monitoring can detect
                # genuine crash leftovers.
                s.status = AgentSessionStatus.STALE
                stale_count += 1
                if s.slack_session_id:
                    stale_session_ids.append(s.agent_session_id)
            else:
                # Session's last turn reached a terminal state (SUCCESS, FAILED, or ABORTED)
                # and all USER turns have corresponding responses — the session completed
                # but the status was never written (old Slack guard or a race on restart).
                # These are NOT crash leftovers — mark COMPLETED.
                s.status = AgentSessionStatus.COMPLETED
                completed_count += 1

        await session.commit()

    logger.info(
        "Recovered stale sessions on startup",
        total=len(stale_sessions),
        completed=completed_count,
        stale=stale_count,
    )

    # Send "server is back" courtesy to Slack threads that were interrupted.
    # Best-effort: errors are logged inside send_slack_restart_courtesy.
    if stale_session_ids:
        try:
            await send_slack_restart_courtesy(stale_session_ids)
        except Exception:
            logger.warning("Failed to send restart courtesy messages", exc_info=True)


async def _run_auto_stale_check() -> None:
    """Sweep ACTIVE sessions that have been idle longer than the stale timeout.

    Queries all top-level ACTIVE sessions whose modified_at is older than
    AHS_SESSION_STALE_TIMEOUT_HOURS and transitions them to STALE.
    """
    stale_threshold = datetime.now(UTC) - timedelta(hours=AHS_SESSION_STALE_TIMEOUT_HOURS)

    async with get_async_session() as session:
        result = await session.exec(
            select(AgentSession)
            .where(AgentSession.status == AgentSessionStatus.ACTIVE)
            .where(col(AgentSession.modified_at) < stale_threshold)
            .where(col(AgentSession.parent_session_id).is_(None))
        )
        candidates = result.all()

        if not candidates:
            return

        now = datetime.now(UTC)
        for s in candidates:
            inactive_hours = (now - s.modified_at).total_seconds() / 3600 if s.modified_at else 0
            s.status = AgentSessionStatus.STALE
            logger.info(
                "session_auto_staled",
                session_id=str(s.agent_session_id),
                inactive_hours=round(inactive_hours, 1),
            )

        await session.commit()

        logger.info(
            "Auto-stale sweep completed",
            staled=len(candidates),
        )


async def _auto_stale_inactive_sessions() -> None:
    """Background loop that periodically sweeps idle ACTIVE sessions to STALE.

    Runs every AHS_AUTO_STALE_INTERVAL_S seconds. The first check is deferred
    by one full interval so startup recovery (_recover_stale_sessions) completes
    first and doesn't race with this sweep.
    """
    logger.info(
        "Auto-stale session sweep started",
        interval_s=AHS_AUTO_STALE_INTERVAL_S,
        default_timeout_hours=AHS_SESSION_STALE_TIMEOUT_HOURS,
    )

    # Defer the first check so startup recovery runs uncontested.
    await asyncio.sleep(AHS_AUTO_STALE_INTERVAL_S)

    while True:
        try:
            await _run_auto_stale_check()
        except Exception:
            logger.error("Auto-stale sweep failed", exc_info=True)

        await asyncio.sleep(AHS_AUTO_STALE_INTERVAL_S)


def _ensure_claude_json() -> None:
    """Ensure ~/.claude.json exists so the CLI doesn't warn on every launch.

    Claude CLI prints a startup warning (~3× per session) when ~/.claude.json is
    missing. This function creates the file if absent, preferring the most-recent
    backup under ~/.claude/backups/ and falling back to a minimal empty config.
    """
    home = os.path.expanduser("~")
    target = os.path.join(home, ".claude.json")
    if os.path.exists(target):
        return

    # Try to restore the most-recent backup written by the CLI.
    backup_pattern = os.path.join(home, ".claude", "backups", ".claude.json.backup.*")
    backups = sorted(glob.glob(backup_pattern))
    if backups:
        shutil.copy2(backups[-1], target)
        logger.info("Restored ~/.claude.json from backup", backup=backups[-1])
        return

    # No backup available — write a minimal valid config so the CLI stays quiet.
    with open(target, "w") as fh:
        json.dump({}, fh)
    logger.info("Created minimal ~/.claude.json (no backup found)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ahs_startup(app: FastAPI, mcp_app: Any) -> AHSState:
    """Perform all AHS startup initialisation and return a state object.

    Called once, at server startup, before the ASGI app begins accepting
    requests.  Returns an :class:`AHSState` that must be passed to
    :func:`ahs_shutdown` during teardown.

    Args:
        app:     The FastAPI application instance (reserved for future use,
                 e.g. attaching state to ``app.state``).
        mcp_app: The FastMCP HTTP sub-application whose lifespan is entered
                 during startup and exited during shutdown.
    """
    setup_asyncio_logging()
    logger.info("Starting Agent Harness Service")

    # Ensure ~/.claude.json exists before any CLI subprocess is spawned.
    # Missing file triggers a 3× warning per session and wastes ~0.1-0.5s.
    try:
        _ensure_claude_json()
    except Exception:
        logger.warning("Failed to ensure ~/.claude.json — CLI may show startup warnings", exc_info=True)

    # Wire orchestration callbacks into MCP tools (dependency injection)
    register_orchestration_callbacks(run_subagent, route_model_stub)

    # Register subagent result delivery callback
    from ypl.agent_harness_service.service import deliver_subagent_result_to_parent

    register_delivery_callback(deliver_subagent_result_to_parent)

    # Initialize WebSocket streaming infrastructure
    init_streaming()

    # Initialize outgoing gateways (Slack, etc.)
    init_gateways()

    # Discover agent configs on startup
    agents = discover_agents()
    logger.info("Discovered agent configs", agent_count=len(agents))

    # Ensure all agents have matching DB rows
    try:
        await _sync_agents_to_db(agents)
    except Exception:
        logger.error("Failed to sync agents to DB — continuing startup", exc_info=True)

    # Load all DB agents (including DB-only ones) into the in-memory spec registry.
    # Must run after _sync_agents_to_db so on-disk agents are already in the DB.
    try:
        from ypl.agent_harness_service.common.agent_registry import load_registry_from_db_async

        await load_registry_from_db_async()
    except Exception:
        logger.error(
            "Failed to load agent registry from DB — DB-only agents may not be available as subagents", exc_info=True
        )

    # Recover sessions left ACTIVE from the previous process (SIGTERM, crash, or
    # the previous code path that skipped writing COMPLETED for SLACK sessions).
    try:
        await _recover_stale_sessions()
    except Exception:
        logger.error("Failed to recover stale sessions — continuing startup", exc_info=True)

    # Start warm process pool (pre-start bwrap+Claude processes to cut first-turn latency).
    # process_pool ships as a separate feature; import is guarded so this PR can merge first.
    try:
        from ypl.agent_harness_service.process_pool import get_pool

        _pool = get_pool()
        _pool.configure(agents)
        await _pool.start()
    except ImportError:
        logger.info("process_pool module not available — warm pool disabled")
    except Exception:
        logger.error("Failed to start warm process pool — continuing without it", exc_info=True)

    logger.info("Agent Harness Service ready")

    # Start scheduler polling loop
    scheduler_task: asyncio.Task[None] | None = None
    if SCHEDULER_ENABLED:
        scheduler_task = asyncio.create_task(run_scheduler(), name="scheduler")
        logger.info("Scheduler started")

    # Start background auto-stale sweep for hung ACTIVE sessions
    auto_stale_task: asyncio.Task[None] = asyncio.create_task(_auto_stale_inactive_sessions(), name="auto-stale-sweep")

    # Enter the MCP app's lifespan (required by FastMCP).
    mcp_lifespan_ctx = mcp_app.lifespan(mcp_app)
    await mcp_lifespan_ctx.__aenter__()

    return AHSState(
        scheduler_task=scheduler_task,
        auto_stale_task=auto_stale_task,
        mcp_app=mcp_app,
        _mcp_lifespan_ctx=mcp_lifespan_ctx,
    )


async def ahs_shutdown(state: AHSState) -> None:
    """Perform all AHS teardown in reverse startup order.

    Must be called with the :class:`AHSState` returned by :func:`ahs_startup`.
    All cleanup is best-effort: errors are logged and execution continues so
    that remaining teardown steps still run.

    Args:
        state: The state object returned by :func:`ahs_startup`.
    """
    # Notify in-flight Slack sessions that the server is restarting.
    # Done first, before any processes are killed, so the message reaches
    # users while their session is still registered in the gateway.
    try:
        await asyncio.wait_for(send_slack_shutdown_courtesy(), timeout=5.0)
    except Exception:
        logger.warning("Failed to send shutdown courtesy messages", exc_info=True)

    # Shutdown warm process pool (kills idle pre-warmed processes)
    try:
        from ypl.agent_harness_service.process_pool import get_pool

        await get_pool().stop()
    except ImportError:
        pass

    # Cancel auto-stale sweep
    if not state.auto_stale_task.done():
        state.auto_stale_task.cancel()
        try:
            await state.auto_stale_task
        except asyncio.CancelledError:
            pass
        logger.info("Auto-stale sweep stopped")

    # Shutdown scheduler INSIDE MCP lifespan so in-flight tasks can still use MCP tools
    if state.scheduler_task is not None and not state.scheduler_task.done():
        state.scheduler_task.cancel()
        try:
            await state.scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Scheduler polling stopped")

        # Wait for in-flight schedule executions to complete gracefully
        cancelled_count = await wait_for_in_flight_tasks(timeout_seconds=30.0)
        if cancelled_count > 0:
            logger.warning("Some schedule execution tasks were cancelled during shutdown", count=cancelled_count)

        # Wait for in-flight task executions to complete gracefully
        if TASK_EXECUTOR_ENABLED:
            task_cancelled_count = await wait_for_in_flight_task_executions(timeout_seconds=30.0)
            if task_cancelled_count > 0:
                logger.warning("Some task execution tasks were cancelled during shutdown", count=task_cancelled_count)

        logger.info("Scheduler shutdown complete")

    # Exit the MCP app's lifespan context
    await state._mcp_lifespan_ctx.__aexit__(None, None, None)

    # Shut down WebSocket streaming
    await shutdown_streaming()

    # Terminate codex app-server subprocesses to avoid orphaned processes on restart.
    await shutdown_codex_servers()

    # Close GitHub token storage Redis connection
    await close_github_token_store()

    logger.info("Shutting down Agent Harness Service")
