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
        # Caller owns the MCP lifespan context so exception info is forwarded.
        async with state._mcp_lifespan_ctx:
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
from ypl.agent_harness_service.service import (
    create_agent as service_create_agent,
)
from ypl.agent_harness_service.service import (
    dispatch_resume_turn,
    list_resume_pending_session_ids,
    send_slack_restart_courtesy,
    send_slack_shutdown_courtesy,
)
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

# Wrapper timeout for the shutdown-side Slack courtesy broadcast.  systemd's
# ``TimeoutStopSec=30`` gives us 30s total before SIGKILL; 25s leaves 5s for
# the rest of the drain (subprocess teardown, scheduler stop, etc.).  The
# prior 5s was too tight — a single slow Slack API call (the per-call SAG
# timeout is 10s) ate the whole budget and the broadcast silently aborted.
_SHUTDOWN_COURTESY_WRAPPER_TIMEOUT_S = 25.0


def _record_shutdown_courtesy_wrapper_failure(*, reason: str) -> None:
    """Increment ``ahs/courtesy_broadcast`` with ``outcome=wrapper_failed``.

    Distinct from the per-message failure counter emitted by
    ``_send_slack_courtesy``: this counter fires when the broadcast itself
    timed out or raised before any per-message bookkeeping happened, so we
    can alert on the wrapper-level failure mode in isolation.
    """
    try:
        from ypl.backend.utils.monitoring import metric_inc_with_labels

        metric_inc_with_labels(
            "ahs/courtesy_broadcast",
            {"event": "shutdown", "outcome": "wrapper_failed", "reason": reason},
        )
    except Exception:  # pragma: no cover — metric write must not propagate
        logger.debug("metric_inc failed for ahs/courtesy_broadcast wrapper failure", exc_info=True)


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
        a2a_listener_task: Background asyncio.Task running the A2A dispatch
                           BLPOP listener (``listen_new_session_messages``).
        mcp_app:           The FastMCP HTTP sub-application (needed so
                           ahs_shutdown() can exit its lifespan context).
        _mcp_lifespan_ctx: The *unentered* async context manager for mcp_app's
                           lifespan.  Created by ahs_startup() but entered and
                           exited by the caller (server.py lifespan()) via
                           ``async with state._mcp_lifespan_ctx`` so that real
                           exception info is forwarded to ``__aexit__``.
    """

    scheduler_task: asyncio.Task[None] | None
    auto_stale_task: asyncio.Task[None]
    a2a_listener_task: asyncio.Task[None] | None
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
        "external_mcps": config.external_mcps,
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

    Every top-level ``ACTIVE`` session is examined regardless of how recently it
    was modified.  The previous 15-minute idle threshold was tuned for the
    auto-stale sweep but did the wrong thing here: a session killed mid-turn
    by SIGTERM has ``modified_at`` within the last few seconds, so the new
    instance (booting ~5–10s later under systemd) skipped exactly the
    sessions that needed recovery.  The classification logic below already
    distinguishes "completed but unwritten" from "interrupted mid-turn" by
    walking the message history, so dropping the time filter is safe.

    Subagent sessions (``parent_session_id IS NOT NULL``) are still excluded;
    the orchestrator already writes their final status.
    """
    async with get_async_session() as session:
        result = await session.exec(
            select(AgentSession)
            .where(AgentSession.status == AgentSessionStatus.ACTIVE)
            .where(col(AgentSession.parent_session_id).is_(None))
        )
        # Despite the name, this is *every* top-level ACTIVE session, not just
        # the ones we'll classify as STALE.  We need the full list so we can
        # broadcast the "back online" ping to all Slack threads that survived
        # the previous deploy, even ones whose last turn completed cleanly.
        active_sessions = result.all()

        # Slack session IDs from the recovery scan — populated below.  Used to
        # send the restart-side courtesy *regardless* of how each session is
        # classified, so a user whose last turn finished cleanly right before a
        # deploy still sees a "back online" notice on the next user message.
        all_slack_session_ids: list[uuid.UUID] = []

        if not active_sessions:
            # Nothing to recover and no Slack threads to notify; let the rest
            # of startup proceed.
            return

        completed_count = 0
        stale_count = 0

        for s in active_sessions:
            if s.slack_session_id:
                all_slack_session_ids.append(s.agent_session_id)

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
                #
                # The "did this session need a resume preamble?" classification
                # lives separately in Redis: ``promote_executor_running_to_resume_pending``
                # (called earlier in startup) converts every leftover
                # ``ahs:executor_running:*`` key into an ``ahs:resume_pending:*``
                # key, and the next ``send_message`` consumes that flag.  This
                # decouples the STALE/COMPLETED status decision (which still
                # needs the message-history walk) from the preamble decision
                # (which Redis already knows the answer to perfectly).
                s.status = AgentSessionStatus.STALE
                stale_count += 1
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
        total=len(active_sessions),
        completed=completed_count,
        stale=stale_count,
        slack_in_scope=len(all_slack_session_ids),
    )

    if not all_slack_session_ids:
        return

    # Partition the Slack-in-scope set into two buckets via the Redis
    # ``ahs:resume_pending:*`` key namespace (set by
    # ``promote_executor_running_to_resume_pending``, which ran earlier in
    # startup):
    #
    # * ``interrupted_ids`` — previous turn killed mid-flight.  Send the
    #   "picking up where we left off" courtesy and auto-fire a synthetic
    #   continuation turn so the user does not have to send a message.
    # * ``idle_ids`` — previous turn finished cleanly.  Send the bare
    #   "back online" courtesy; do nothing else.
    #
    # ``list_resume_pending_session_ids`` returns ``set()`` on any Redis
    # error, so partitioning falls through to "everything is idle" — the
    # exact behaviour we had before this PR.  No regression on Redis
    # outages, just no auto-resume that day.
    pending = await list_resume_pending_session_ids()
    interrupted_ids = [sid for sid in all_slack_session_ids if sid in pending]
    idle_ids = [sid for sid in all_slack_session_ids if sid not in pending]

    logger.info(
        "Partitioned Slack restart-courtesy audience by resume-pending state",
        interrupted=len(interrupted_ids),
        idle=len(idle_ids),
    )

    # Send the interrupted-bucket courtesy *before* dispatching the
    # synthetic turn so the user sees "picking up where we left off"
    # arrive in Slack ahead of the auto-continued AGENT reply.
    if interrupted_ids:
        try:
            await send_slack_restart_courtesy(interrupted_ids, interrupted=True)
        except Exception:
            logger.warning(
                "Failed to send interrupted-bucket restart courtesy",
                exc_info=True,
            )

        # Fire each ``dispatch_resume_turn`` as an independent background
        # task so a single slow / failing session cannot stall the rest of
        # startup.  ``dispatch_resume_turn`` swallows its own exceptions
        # (logs + emits ``ahs/courtesy_broadcast{event=auto_resume,outcome=failed}``).
        for sid in interrupted_ids:
            asyncio.create_task(dispatch_resume_turn(sid), name=f"auto-resume-{sid}")

    if idle_ids:
        try:
            await send_slack_restart_courtesy(idle_ids, interrupted=False)
        except Exception:
            logger.warning(
                "Failed to send idle-bucket restart courtesy",
                exc_info=True,
            )


async def _run_auto_stale_check() -> None:
    """Sweep ACTIVE sessions that have been idle longer than the stale timeout.

    Queries all top-level ACTIVE sessions whose modified_at is older than
    AHS_SESSION_STALE_TIMEOUT_HOURS and transitions them to STALE.

    Note: this path intentionally does NOT touch the Redis resume-pending
    flag.  A 6-hour idle window is not a crash — telling the user "the
    previous turn was interrupted by a server restart" the next time they
    ping a long-abandoned thread would be misleading.  The Redis flag is
    only ever set by ``promote_executor_running_to_resume_pending`` at
    startup, off keys whose ``finally`` block never ran (i.e. genuine
    SIGTERM/crash leftovers).
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
    register_orchestration_callbacks(run_subagent, route_model_stub, service_create_agent)

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

    # Promote leftover ``ahs:executor_running:*`` keys to
    # ``ahs:resume_pending:*`` so the next inbound user message on each
    # interrupted session picks up the auto-resume preamble.  Done BEFORE
    # ``_recover_stale_sessions`` so the resume-pending flags are in place
    # if a fast user reply lands while the DB classifier is still running
    # (the two are otherwise independent: the DB walk decides STALE vs
    # COMPLETED, Redis decides "needs preamble").
    try:
        from ypl.agent_harness_service.service.session_lifecycle import (
            promote_executor_running_to_resume_pending,
        )

        await promote_executor_running_to_resume_pending()
    except Exception:
        logger.error(
            "Failed to promote executor_running keys to resume_pending — "
            "continuing startup; some interrupted sessions may miss the preamble",
            exc_info=True,
        )

    # Recover sessions left ACTIVE from the previous process (SIGTERM, crash, or
    # the previous code path that skipped writing COMPLETED for SLACK sessions).
    try:
        await _recover_stale_sessions()
    except Exception:
        logger.error("Failed to recover stale sessions — continuing startup", exc_info=True)

    # ── A2A startup tasks ────────────────────────────────────────────────────
    # Imports are deferred to avoid circular-import issues at module load time.
    from ypl.agent_harness_service.service.agent_message_delivery import listen_new_session_messages
    from ypl.agent_harness_service.service.agent_message_recovery import recover_agent_messages
    from ypl.agent_harness_service.service.agent_user_sweep import sweep_agent_user_identities

    # Initialise to None so AHSState construction succeeds even if an exception
    # interrupts startup before the create_task call — matches the scheduler_task pattern.
    a2a_listener_task: asyncio.Task[None] | None = None

    # 1. Await crash recovery synchronously so all stale DELIVERING rows are reset
    #    and QUEUED rows are pushed onto Redis *before* the BLPOP listener starts
    #    consuming the same queue.  recover_agent_messages() swallows all exceptions
    #    internally; no outer try/except needed.
    await recover_agent_messages()

    # 2. Backfill missing agent user-identity rows.  Fire-and-forget — completes
    #    quickly.  Attach a done_callback so any unexpected exception (despite the
    #    internal guard) is logged rather than silently swallowed at GC time.
    _a2a_sweep_task = asyncio.create_task(sweep_agent_user_identities(), name="a2a-identity-sweep")
    _a2a_sweep_task.add_done_callback(
        lambda t: logger.warning("a2a-identity-sweep exited unexpectedly", exc_info=t.exception())
        if not t.cancelled() and t.exception() is not None
        else None
    )

    # 3. Start the BLPOP listener for Scenario A (new-session) messages.  Runs
    #    for the lifetime of the process; stored in AHSState so ahs_shutdown()
    #    can cancel it cleanly.
    a2a_listener_task = asyncio.create_task(listen_new_session_messages(), name="a2a-dispatch-listener")
    logger.info("A2A dispatch listener started")
    # ────────────────────────────────────────────────────────────────────────

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

    # Build the MCP app's lifespan context manager but do NOT enter it here.
    # server.py uses `async with state._mcp_lifespan_ctx:` so that real
    # exception information is forwarded to __aexit__ if the server crashes.
    mcp_lifespan_ctx = mcp_app.lifespan(mcp_app)

    return AHSState(
        scheduler_task=scheduler_task,
        auto_stale_task=auto_stale_task,
        a2a_listener_task=a2a_listener_task,
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
    # Notify every live Slack thread that the server is restarting.  Done
    # first, before any processes are killed, so the message reaches users
    # while their session is still registered in the gateway.  The wrapper
    # timeout was bumped from 5s to 25s — see
    # ``_SHUTDOWN_COURTESY_WRAPPER_TIMEOUT_S`` for the rationale.  We log at
    # error level (not warning) with ``exc_info=True`` so a failure here is
    # actually visible in production rather than blending into routine noise.
    try:
        await asyncio.wait_for(
            send_slack_shutdown_courtesy(),
            timeout=_SHUTDOWN_COURTESY_WRAPPER_TIMEOUT_S,
        )
    except TimeoutError:
        logger.error(
            "Shutdown courtesy broadcast timed out",
            timeout_s=_SHUTDOWN_COURTESY_WRAPPER_TIMEOUT_S,
            exc_info=True,
        )
        _record_shutdown_courtesy_wrapper_failure(reason="timeout")
    except Exception:
        logger.error("Failed to send shutdown courtesy messages", exc_info=True)
        _record_shutdown_courtesy_wrapper_failure(reason="exception")

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
        except Exception:
            logger.warning("Auto-stale sweep exited with unexpected exception during shutdown", exc_info=True)
        logger.info("Auto-stale sweep stopped")

    # Cancel A2A dispatch listener
    if state.a2a_listener_task is not None and not state.a2a_listener_task.done():
        state.a2a_listener_task.cancel()
        try:
            await state.a2a_listener_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("A2A dispatch listener exited with unexpected exception during shutdown", exc_info=True)
        logger.info("A2A dispatch listener stopped")

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

        logger.info("Scheduler shutdown complete")

    # Wait for in-flight task executions regardless of whether the scheduler was enabled.
    # TASK_EXECUTOR_ENABLED is independent of SCHEDULER_ENABLED — skipping this block when
    # the scheduler is disabled would silently abandon in-flight task executor work.
    if TASK_EXECUTOR_ENABLED:
        task_cancelled_count = await wait_for_in_flight_task_executions(timeout_seconds=30.0)
        if task_cancelled_count > 0:
            logger.warning("Some task execution tasks were cancelled during shutdown", count=task_cancelled_count)

    # NOTE: The MCP app's lifespan context (__aexit__) is handled by the
    # caller (server.py) via `async with state._mcp_lifespan_ctx:` so that
    # real exception info is forwarded.  Do NOT call __aexit__ here.

    # Shut down WebSocket streaming
    await shutdown_streaming()

    # Terminate codex app-server subprocesses to avoid orphaned processes on restart.
    await shutdown_codex_servers()

    # Close GitHub token storage Redis connection
    await close_github_token_store()

    logger.info("Shutting down Agent Harness Service")
