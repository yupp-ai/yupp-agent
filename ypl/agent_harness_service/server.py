"""FastAPI server for Agent Harness Service.

Standalone service that hosts AI agents with persistent identity,
memory, and sandboxed repo access. Communicates with the Slack
gateway (and other callers) via REST + SSE.
"""

import asyncio
import glob
import json
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from sqlmodel import col, func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.common.config import AgentConfig, discover_agents
from ypl.agent_harness_service.common.constants import (
    AHS_AUTO_STALE_INTERVAL_S,
    AHS_MCP_SECRET,
    AHS_SESSION_STALE_TIMEOUT_HOURS,
    mcp_session_id_var,
)
from ypl.agent_harness_service.core.streaming import init_streaming, shutdown_streaming
from ypl.agent_harness_service.core.subagent_queue import register_delivery_callback
from ypl.agent_harness_service.executors.codex_app_server_runner import shutdown_codex_servers
from ypl.agent_harness_service.gateway import init_gateways
from ypl.agent_harness_service.github_webhook import webhook_router
from ypl.agent_harness_service.orchestration import route_model_stub, run_subagent
from ypl.agent_harness_service.projects.project_routes import project_router
from ypl.agent_harness_service.routes import AHSRequestLoggingMiddleware, router
from ypl.agent_harness_service.scheduler import SCHEDULER_ENABLED, run_scheduler, wait_for_in_flight_tasks
from ypl.agent_harness_service.service import send_slack_restart_courtesy, send_slack_shutdown_courtesy
from ypl.agent_harness_service.task_executor import (
    TASK_EXECUTOR_ENABLED,
    wait_for_in_flight_task_executions,
)
from ypl.agent_harness_service.tools.github_token_storage import close_store as close_github_token_store
from ypl.agent_harness_service.tools.local_mcp_server import mcp as harness_mcp
from ypl.agent_harness_service.tools.local_mcp_server import register_orchestration_callbacks
from ypl.backend.db import get_async_session
from ypl.backend.routes.v1.yuppaste import router as yuppaste_router
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

# Module-level task references to prevent GC
_scheduler_task: asyncio.Task | None = None
_auto_stale_task: asyncio.Task | None = None



# Create the MCP sub-app once so we can wire its lifespan into the main app.
# json_response=True: return application/json instead of SSE-wrapped responses.
# stateless_http=True: no server-side session tracking (session_id is a tool param).
# Matches the yuppster-mcp-server config in ypl/mcp_server/server.py.
mcp_app = harness_mcp.http_app(
    path="/",
    transport="streamable-http",
    json_response=True,
    stateless_http=True,
)


class _McpTokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject MCP requests that don't carry the process-local secret token.

    The token is generated at startup (AHS_MCP_SECRET) and injected into the
    per-session MCP config that the runner writes for Claude CLI. External
    callers won't know the token, so they can't hit the MCP endpoints.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        token = request.headers.get("x-ahs-token")
        session_id = request.headers.get("x-ahs-session-id", "")

        if token != AHS_MCP_SECRET:
            # Fallback: Accept Bearer token in format "<secret>:<session_id>".
            # Codex CLI only supports bearer_token_env_var for MCP auth, so we
            # encode both the secret and session ID into a single Bearer token.
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                bearer = auth_header[7:]
                if ":" in bearer:
                    bearer_secret, bearer_session_id = bearer.split(":", 1)
                    if bearer_secret == AHS_MCP_SECRET:
                        token = bearer_secret
                        session_id = bearer_session_id

        if token != AHS_MCP_SECRET:
            return Response(content="Unauthorized", status_code=401)
        # Propagate session identity so MCP tools can enforce access control.
        mcp_session_id_var.set(session_id)
        return await call_next(request)


mcp_app.add_middleware(_McpTokenAuthMiddleware)


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
                for field, value in db_fields.items():
                    if getattr(agent, field) != value:
                        setattr(agent, field, value)
                        changed_fields.append(field)
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


# Sessions older than this threshold are considered stale on startup (i.e. left
# ACTIVE from the previous process and not currently being processed).
_STALE_SESSION_THRESHOLD_MINUTES = 15


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle."""
    global _scheduler_task, _auto_stale_task
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
    if SCHEDULER_ENABLED:
        _scheduler_task = asyncio.create_task(run_scheduler(), name="scheduler")
        logger.info("Scheduler started")

    # Start background auto-stale sweep for hung ACTIVE sessions
    _auto_stale_task = asyncio.create_task(_auto_stale_inactive_sessions(), name="auto-stale-sweep")

    # Initialize the MCP app's lifespan (required by FastMCP).
    async with mcp_app.lifespan(mcp_app):
        yield

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
        if _auto_stale_task and not _auto_stale_task.done():
            _auto_stale_task.cancel()
            try:
                await _auto_stale_task
            except asyncio.CancelledError:
                pass
            logger.info("Auto-stale sweep stopped")

        # Shutdown scheduler INSIDE MCP lifespan so in-flight tasks can still use MCP tools
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
            try:
                await _scheduler_task
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
                    logger.warning(
                        "Some task execution tasks were cancelled during shutdown", count=task_cancelled_count
                    )

            logger.info("Scheduler shutdown complete")

    # Shut down WebSocket streaming
    await shutdown_streaming()

    # Terminate codex app-server subprocesses to avoid orphaned processes on restart.
    await shutdown_codex_servers()

    # Close GitHub token storage Redis connection
    await close_github_token_store()

    logger.info("Shutting down Agent Harness Service")


app = FastAPI(
    title="Agent Harness Service",
    description="Hosts AI agents with persistent identity, memory, and sandboxed repo access",
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Any *.yupp.ai subdomain + Vercel frontends + localhost for dev
    allow_origins=[
        "https://yupp-soul.vercel.app",
        "https://chaos-soul.vercel.app",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:8080",
    ],
    allow_origin_regex=r"https://.*\.yupp\.ai",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(AHSRequestLoggingMiddleware)
router.include_router(project_router)
router.include_router(yuppaste_router, tags=["yuppaste"], dependencies=[Depends(verify_api_key)])
router.include_router(webhook_router)
app.include_router(router)

# Mount the MCP server as an HTTP sub-app (streamable-http transport).
# Agents connect to this endpoint instead of spawning a stdio subprocess.
app.mount("/mcp/harness", mcp_app)


# Health check
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
