"""Shared mutable state and constants for the service package."""

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field

from ypl.agent_harness_service.common.types import AHSSource, AttachmentInfo
from ypl.agent_harness_service.executors.command_handler import CommandHandlerManager
from ypl.structured_logger import get_logger

logger = get_logger()

# Maps agent_session_id (UUID) -> asyncio.Task for running agent tasks.
# Used by stop_session() to cancel inflight turns.
_active_tasks: dict[uuid.UUID, asyncio.Task] = {}

# Pre-spawn tasks: subprocess started concurrently with DB writes in create_session().
# Maps agent_session_id → asyncio.Task[asyncio.subprocess.Process].
# Consumed (popped) by _run_agent_task on the first turn; absent for all later turns.
_pre_spawn_tasks: dict[uuid.UUID, asyncio.Task] = {}

# BCH managers: one CommandHandlerManager per active session.
# Created at session start (create_session()), registered with workspace_tools.py
# so all tool dispatch routes through the warm bwrapped proxy.
# Keyed by agent_session_id, same schema as _active_tasks.
_command_handlers: dict[uuid.UUID, CommandHandlerManager] = {}

# Session subtypes that indicate the agent was stopped before completing its task.
# When a session ends with one of these subtypes, the associated task should be
# marked FAILED, not COMPLETED.
_TASK_FAILURE_SUBTYPES: frozenset[str] = frozenset(
    {
        "error",  # Explicit error from runner
        "error_max_turns",  # Hit turn limit (Claude CLI or raw executor)
        "stopped_context_overflow",  # Context overflow
    }
)

# Global parallelism cap for automated executions (scheduler, task executor).
# Counts all active turns (including user-initiated ones) so the total system
# load is bounded. User requests are not blocked by this cap but their active
# turns do consume capacity, ensuring we don't overload the machine.


def _parse_env_int(env_var: str, default: int) -> int:
    """Parse an integer from environment variable with fallback to default."""
    try:
        value = int(os.environ.get(env_var, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


# Raised from 20 → 40: the original default was set conservatively before
# multi-master-reviewer workloads existed.  Under a 15-PR sprint each master
# spawns 3-5 sub-reviewers, so we need headroom for ~20 concurrent automated
# turns.  40 gives 2× headroom while staying well within Cloud Run memory.
MAX_CONCURRENT_EXECUTIONS = _parse_env_int("AHS_MAX_CONCURRENT_EXECUTIONS", 40)

# ---------------------------------------------------------------------------
# API subagent concurrency — caps concurrent new_task subagent processes.
#
# Without this cap, multiple simultaneous master-reviewer sessions each spawn
# 3-5 sub-reviewers (reviewer-claude, reviewer-codex, reviewer-glm), quickly
# creating 15-20+ concurrent CLI subprocesses on the same instance.  Resource
# contention under that load balloons reviewer-claude from a ~31s solo baseline
# to 90-300s.  Capping to MAX_CONCURRENT_API_SUBAGENTS (default 12) ensures
# excess sessions queue until a slot opens, keeping each individual execution
# at near-solo speed.  With 12 slots and 15 pending subagents:
#   - First 12 start immediately, each finishing in ~30-35s
#   - Last 3 start once the first wave completes — total wait ≈ 65s worst case
# This is better than letting all 15 contend and each taking 150-300s.
#
# Separate from MAX_CONCURRENT_EXECUTIONS (which gates the scheduler/task
# executor) to avoid starving CRON/SLACK/TASK sessions when master-reviewer
# sprints are active.
# ---------------------------------------------------------------------------
MAX_CONCURRENT_API_SUBAGENTS = _parse_env_int("AHS_MAX_CONCURRENT_API_SUBAGENTS", 12)

# Lazily initialized to avoid creating the Semaphore before the event loop starts.
_api_subagent_semaphore: asyncio.Semaphore | None = None


def get_api_subagent_semaphore() -> asyncio.Semaphore:
    """Return the global semaphore that caps concurrent API subagent executions.

    Lazily initialized on first call so it is always created inside a running
    event loop (required by asyncio.Semaphore in Python ≤ 3.9; safe in 3.10+).
    """
    global _api_subagent_semaphore
    if _api_subagent_semaphore is None:
        _api_subagent_semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_SUBAGENTS)
    return _api_subagent_semaphore


def get_active_turn_count() -> int:
    """Return the number of currently active turns (all types)."""
    return len(_active_tasks)


def has_execution_capacity() -> bool:
    """Check if there is capacity to start a new automated execution.

    Counts all active turns (user-initiated and automated) against
    MAX_CONCURRENT_EXECUTIONS. User requests still run freely; only
    automated spawns (scheduler/task executor) call this gate.
    """
    active_count = len(_active_tasks)
    if active_count < MAX_CONCURRENT_EXECUTIONS:
        return True

    logger.info(
        "At capacity, deferring automated execution",
        active_turns=active_count,
        max_concurrent=MAX_CONCURRENT_EXECUTIONS,
        active_session_ids=[str(sid) for sid in _active_tasks],
    )
    return False


# ---------------------------------------------------------------------------
# Slack courtesy messages — SIGTERM shutdown and post-restart notification
# ---------------------------------------------------------------------------

_SLACK_SHUTDOWN_COURTESY_MSG = (
    "🔄 *Server is restarting.* Your session is safe — send me a message to continue when I'm back up."
)
_SLACK_RESTART_COURTESY_MSG = "✅ *I'm back online.* Send me a message to continue where we left off."


@dataclass
class PendingMessage:
    """A message queued while the agent is busy processing a prior turn."""

    message: str
    slack_ts: str | None = None
    slack_user_id: str | None = None
    user_id: str | None = None
    attachments: list[AttachmentInfo] = field(default_factory=list)
    source: AHSSource = "api"  # preserve original request source for accurate downstream logging
    queued_at: float = field(default_factory=time.time)  # for future staleness/timeout checks


# Maps agent_session_id (UUID) -> list of messages that arrived while the agent
# was busy. Drained and combined into a single turn after the current turn completes.
_pending_messages: dict[uuid.UUID, list[PendingMessage]] = {}
_MAX_PENDING_MESSAGES = 50

# If the gap between consecutive content blocks is less than this, append to
# the same Slack message instead of posting a new one. SAG handles overflow
# if the message exceeds Slack's limit. Increased from 20s to 120s to reduce
# notification spam while still allowing new messages for long gaps.
_GATEWAY_APPEND_THRESHOLD_SECONDS = 120.0

# Keys whose string values are truncated in raw_events before DB storage.
_TRIM_KEYS = frozenset({"text", "content", "prompt"})
_TRIM_MAX_LEN = 100
_TRIM_SUFFIX = "... (truncated)"

# ---------------------------------------------------------------------------
# Eager persistence: persist agent messages incrementally during execution
# so the console shows progress before the turn completes.
# ---------------------------------------------------------------------------

_EAGER_PERSIST_TOOL_INTERVAL = 5  # UPDATE every N tool calls


# Tool names whose payloads are delivered directly to user-visible outlets
# (e.g., Slack) by their MCP server.  Content from these tools is persisted in
# DB but skipped for gateway relay to avoid duplicates.
_OUTLET_TOOL_NAMES: tuple[str, ...] = ("send_slack_message",)

# Agent names that support per-user personalization.
# When a session targets one of these agents and a user_id is available,
# AHS attempts to resolve a user-specific variant (e.g., "yuppclaw-alice").
PERSONAL_AGENT_PREFIXES: frozenset[str] = frozenset({"yuppclaw"})

_TOOL_OUTPUT_MAX_CHARS = 500
