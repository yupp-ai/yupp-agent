"""Core Agent Harness Service — session management, message handling.

This is the main orchestrator. It:
1. Creates/resumes sessions
2. Stores user messages
3. Kicks off the agent runner as a background task
4. Persists agent responses with metadata
"""

import asyncio
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from ypl.agent_harness_service.core.subagent_queue import SubagentResult

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, text, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

import ypl.db.all_models  # noqa: F401 — register all tables so FK references resolve
from ypl.agent_harness_service.common.config import (
    AgentConfig,
    discover_agents,
    load_agent_config,
    load_agent_config_from_db,
    validate_agent_name,
)
from ypl.agent_harness_service.common.constants import (
    AHS_AGENTS_DIR,
    AHS_MEMORIES_DIR,
    AHS_REPOS_DIR,
    AHS_SESSIONS_DIR,
    CONTEXT_OVERFLOW_NOTICE,
    EXECUTOR_TYPE_RAW,
    HARNESS_CODEX_CLI,
    TURN_LIMIT_NOTICE,
)
from ypl.agent_harness_service.common.types import (
    AgentCreateRequest,
    AgentCreateResponse,
    AgentDetailResponse,
    AgentEditRequest,
    AgentEditResponse,
    AgentInfo,
    AgentListResponse,
    AHSSource,
    AHSValidationError,
    AttachmentInfo,
    FeedbackResponse,
    MessageHistoryItem,
    SessionAttachSlackRequest,
    SessionAttachSlackResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionFeedbackRequest,
    SessionHistoryResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionPermissions,
    SessionStopResponse,
    ToolUseItem,
)
from ypl.agent_harness_service.core.memory_persistence import sync_agent_memory_to_gcs
from ypl.agent_harness_service.core.session_persistence import sync_session_to_gcs
from ypl.agent_harness_service.core.session_title import maybe_generate_session_title
from ypl.agent_harness_service.core.streaming import (
    TranslationState,
    _close_message_item,
    build_notice_events,
    get_pubsub,
    translate_stream_event,
)
from ypl.agent_harness_service.executors.codex_app_server_runner import CodexAppServerRunner
from ypl.agent_harness_service.executors.command_handler import CommandHandlerManager
from ypl.agent_harness_service.executors.runner import (
    AgentRunner,
    ClaudeCodeRunner,
    MockRunner,
    RawExecutorRunner,
    RunContext,
    StreamEvent,
    extract_excerpt,
)
from ypl.agent_harness_service.gateway import TRIGGER_TO_GATEWAY, GatewayRegistry
from ypl.agent_harness_service.gateway.base import Gateway
from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content
from ypl.agent_harness_service.orchestration import cancel_subagent_tasks
from ypl.agent_harness_service.tools.local_mcp_server import (
    clear_session_sandbox,
    clear_session_state,
    clear_session_websearch_count,
    reset_turn_websearch_count,
    set_session_current_user,
    set_session_sandbox,
)
from ypl.agent_harness_service.tools.repo_manager import scan_session_worktrees
from ypl.agent_harness_service.tools.workspace_tools import set_command_handler_manager
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.slack_utils import resolve_slack_user_to_yupp_user_id
from ypl.backend.utils.soul_utils import has_permission_by_user_id_cached
from ypl.db.agent_harness import (
    Agent,
    AgentExecutorType,
    AgentFeedback,
    AgentFeedbackRating,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageErrorType,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)
from ypl.db.soul_rbac import SoulPermission
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


MAX_CONCURRENT_EXECUTIONS = _parse_env_int("AHS_MAX_CONCURRENT_EXECUTIONS", 20)


def get_active_turn_count() -> int:
    """Return the number of currently active turns (all types)."""
    return len(_active_tasks)


# ---------------------------------------------------------------------------
# Slack courtesy messages — SIGTERM shutdown and post-restart notification
# ---------------------------------------------------------------------------

_SLACK_SHUTDOWN_COURTESY_MSG = (
    "🔄 *Server is restarting.* Your session is safe — send me a message to continue when I'm back up."
)
_SLACK_RESTART_COURTESY_MSG = "✅ *I'm back online.* Send me a message to continue where we left off."


async def _send_slack_courtesy(session_ids: list[uuid.UUID], text: str, event: str) -> None:
    """Send a courtesy message to the Slack threads for the given session IDs.

    Silently skips sessions that are not Slack-triggered or have no
    ``slack_session_id``.  Errors from individual sends are logged but do not
    propagate — courtesy messages are best-effort.
    """
    if not session_ids:
        return

    registry = GatewayRegistry.get_instance()
    gateway = registry.get("slack")
    if gateway is None:
        logger.warning("Slack gateway unavailable — skipping courtesy messages", event=event)
        return

    async with get_async_session() as db_session:
        result = await db_session.exec(
            select(AgentSession)
            .where(col(AgentSession.agent_session_id).in_(session_ids))
            .where(col(AgentSession.slack_session_id).is_not(None))
        )
        slack_sessions = result.all()

    if not slack_sessions:
        logger.info("No in-scope Slack sessions for courtesy message", event=event)
        return

    logger.info("Sending courtesy messages to Slack sessions", event=event, count=len(slack_sessions))

    results = await asyncio.gather(
        *[gateway.send_reply(s.slack_session_id, text) for s in slack_sessions if s.slack_session_id],
        return_exceptions=True,
    )

    sent = sum(1 for r in results if r is True)
    failed = len(results) - sent
    logger.info("Slack courtesy messages complete", event=event, sent=sent, failed=failed)


async def send_slack_shutdown_courtesy() -> None:
    """Send a courtesy message to all in-flight Slack sessions before shutdown.

    Called during graceful shutdown (SIGTERM) so users know the server is
    restarting and their session will be available again shortly.
    """
    await _send_slack_courtesy(
        list(_active_tasks.keys()),
        _SLACK_SHUTDOWN_COURTESY_MSG,
        "shutdown",
    )


async def send_slack_restart_courtesy(stale_session_ids: list[uuid.UUID]) -> None:
    """Send a courtesy message to stale Slack sessions after the server restarts.

    Called from ``_recover_stale_sessions`` at startup for sessions that were
    interrupted mid-turn by the previous SIGTERM so users know they can continue.
    """
    await _send_slack_courtesy(
        stale_session_ids,
        _SLACK_RESTART_COURTESY_MSG,
        "restart",
    )


async def stop_all_command_handler_managers() -> None:
    """Stop all active BCH managers.

    Called during graceful SIGTERM shutdown to cleanly terminate every
    bwrapped proxy process.  Errors from individual stops are logged but do
    not propagate so one broken manager cannot block the rest.
    """
    managers = dict(_command_handlers)
    _command_handlers.clear()
    for session_id, manager in managers.items():
        set_command_handler_manager(str(session_id), None)
        try:
            await manager.stop()
        except Exception:
            logger.warning(
                "Failed to stop BCH manager during shutdown",
                session_id=str(session_id),
                exc_info=True,
            )


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


def _trim_value(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _trim_field(k, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim_value(item) for item in obj]
    return obj


def _trim_field(key: str, value: Any) -> Any:
    if key in _TRIM_KEYS and isinstance(value, str) and len(value) > _TRIM_MAX_LEN:
        return value[:_TRIM_MAX_LEN] + _TRIM_SUFFIX
    return _trim_value(value)


def _scrub_null_bytes(obj: Any) -> Any:
    """Recursively strip PostgreSQL-illegal null bytes (\\x00) from strings.

    asyncpg raises ``UntranslatableCharacterError`` when a VARCHAR column
    receives a string that contains ``\\u0000`` / ``\\x00``.  LLM output can
    occasionally contain these bytes, so we sanitise before every DB write.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _scrub_null_bytes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_null_bytes(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Eager persistence: persist agent messages incrementally during execution
# so the console shows progress before the turn completes.
# ---------------------------------------------------------------------------

_EAGER_PERSIST_TOOL_INTERVAL = 5  # UPDATE every N tool calls


# Tool names whose payloads are delivered directly to user-visible outlets
# (e.g., Slack) by their MCP server.  Content from these tools is persisted in
# DB but skipped for gateway relay to avoid duplicates.
_OUTLET_TOOL_NAMES: tuple[str, ...] = ("send_slack_message",)


class VisibleContent(NamedTuple):
    """User-visible text extracted from a stream event."""

    text: str
    # True if already delivered to an outlet (e.g., send_slack_message posted
    # to Slack directly via MCP tool). When True, gateway relay is skipped to
    # avoid duplicate delivery, but the text is still persisted in DB content.
    # TODO: This flag is set at tool_use time (intent), not after tool_result
    # confirms success.  If the MCP tool fails, content won't reach Slack but
    # gateway relay is still skipped.  Consider deferring until tool_result or
    # adding a fallback relay on tool failure.  (PR #11130)
    already_delivered: bool


def _extract_visible_content(
    event: StreamEvent,
    seen_outlet_tool_ids: set[str],
) -> list[VisibleContent]:
    """Single decision point for all user-visible content in a stream event.

    All text returned here is persisted in DB content and shown in the
    Streamlit console.  Text NOT marked ``already_delivered`` is also relayed
    to connected outlets (Slack gateway).

    Sources of user-visible content:
    - Agent text blocks (assistant events with text output)
    - Tool calls that post to outlets (e.g., send_slack_message)

    To add a new outlet tool, extend ``_extract_outlet_tool_content``.

    Args:
        event: The stream event to inspect.
        seen_outlet_tool_ids: Mutable set tracking tool-call IDs already
            extracted, so the same call is not counted twice when the CLI
            runner emits both a ``tool_use`` and an ``assistant`` event.
    """
    results: list[VisibleContent] = []

    # 1. Text blocks from assistant events
    if event.type == "assistant" and event.text:
        cleaned = strip_thinking_tags(event.text)
        if cleaned:
            results.append(VisibleContent(text=cleaned, already_delivered=False))

    # 2. Tool calls that deliver content directly to outlets
    _extract_outlet_tool_content(event, results, seen_outlet_tool_ids)

    return results


def _extract_outlet_tool_content(
    event: StreamEvent,
    out: list[VisibleContent],
    seen_tool_ids: set[str],
) -> None:
    """Extract text from tool calls that post to user-visible outlets.

    Currently handles:
    - ``send_slack_message``: text posted directly to Slack by the MCP tool.

    To support a new outlet tool, add extraction logic here and add the tool
    name to ``_OUTLET_TOOL_NAMES``.

    Args:
        event: The stream event to inspect.
        out: Accumulator for visible content items.
        seen_tool_ids: Set of tool-call IDs already processed; used to
            deduplicate when the same tool call appears in both a ``tool_use``
            event and the ``assistant`` event's content blocks.
    """
    tool_blocks: list[dict] = []
    if event.type == "tool_use":
        tool_blocks = [event.raw]
    elif event.type == "assistant":
        tool_blocks = [b for b in event.raw.get("message", {}).get("content", []) if b.get("type") == "tool_use"]

    for block in tool_blocks:
        tool_id: str = block.get("id", "")
        if tool_id and tool_id in seen_tool_ids:
            continue

        name: str = block.get("name", "")
        if name in _OUTLET_TOOL_NAMES:
            text: str | None = block.get("input", {}).get("text")
            if text:
                out.append(VisibleContent(text=text, already_delivered=True))
                if tool_id:
                    seen_tool_ids.add(tool_id)


class EagerPersistState(BaseModel):
    """Mutable state for incremental agent message persistence."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    msg_id: uuid.UUID | None = None  # set after first INSERT
    tool_call_count: int = 0
    last_persisted_tool_count: int = 0
    # Running content parts: list of "[N tool calls: A, B]" and text blocks
    content_parts: list[str] = []
    # Tool names accumulated since last text block
    pending_tool_names: list[str] = []

    def flush_pending_tools(self) -> None:
        """Clear accumulated tool names without adding them to content.

        Tool summaries like '[3 tool calls: Bash, Grep, Read]' are synthetic
        text that should not reach users via streaming or the history API.
        """
        self.pending_tool_names = []

    def build_content(self) -> str:
        """Build the current content string from real text parts only."""
        return "\n\n".join(self.content_parts) if self.content_parts else "[started]"


async def _persist_system_msg(
    eager: EagerPersistState,
    content: str,
    events: list[dict],
    agent_session_id: uuid.UUID,
    turn_number: int,
    model: str,
    session: AsyncSession,
    completion_status: AgentSessionMessageCompletionStatus = AgentSessionMessageCompletionStatus.FAILED,
    error_type: AgentSessionMessageErrorType = AgentSessionMessageErrorType.ERROR_EXECUTOR,
) -> None:
    """Persist a SYSTEM message, reusing the eager row if one exists."""
    # Sanitise before writing — PostgreSQL VARCHAR rejects \x00.
    content = _scrub_null_bytes(content)
    events = _scrub_null_bytes(events)

    if eager.msg_id is not None:
        await session.exec(
            update(AgentSessionMessage)
            .where(col(AgentSessionMessage.agent_session_message_id) == eager.msg_id)
            .values(
                role=AgentSessionMessageRole.SYSTEM,
                content=content,
                raw_events=events,
                completion_status=completion_status,
                error_type=error_type,
            )
        )
    else:
        session.add(
            AgentSessionMessage(
                agent_session_id=agent_session_id,
                turn_number=turn_number,
                role=AgentSessionMessageRole.SYSTEM,
                content=content,
                raw_events=events,
                llm_name=model,
                completion_status=completion_status,
                error_type=error_type,
            )
        )


async def _eager_persist_agent_msg(
    state: EagerPersistState,
    events: list[dict],
    agent_session_id: uuid.UUID,
    turn_number: int,
    model: str,
) -> None:
    """INSERT or UPDATE the agent message row with current progress.

    - First call: INSERT with role=AGENT, content from state.
    - Subsequent calls: UPDATE content + raw_events only (cheap PK update).
    """
    content = _scrub_null_bytes(state.build_content())
    safe_events: list[dict] = _scrub_null_bytes(events)
    is_insert = state.msg_id is None
    try:
        async with get_async_session() as session:
            if is_insert:
                msg = AgentSessionMessage(
                    agent_session_id=agent_session_id,
                    turn_number=turn_number,
                    role=AgentSessionMessageRole.AGENT,
                    content=content,
                    raw_events=safe_events,
                    llm_name=model,
                    completion_status=AgentSessionMessageCompletionStatus.IN_PROGRESS,
                    error_type=AgentSessionMessageErrorType.NONE,
                )
                session.add(msg)
                await session.commit()
                # Set msg_id only after commit succeeds so a failed INSERT
                # doesn't leave state pointing at a nonexistent row.
                state.msg_id = msg.agent_session_message_id
            else:
                await session.exec(
                    update(AgentSessionMessage)
                    .where(col(AgentSessionMessage.agent_session_message_id) == state.msg_id)
                    .values(content=content)
                )
                await session.commit()
        state.last_persisted_tool_count = state.tool_call_count
        sid = str(agent_session_id)[-6:]
        op = "INSERT" if is_insert else "UPDATE"
        logger.info(
            f"Eager persist {op} session {sid} msg {state.msg_id}: {content[:120]}",
            session_id=str(agent_session_id),
            agent_session_message_id=str(state.msg_id),
            turn_number=turn_number,
            eager_op=op,
        )
    except Exception:
        logger.error(
            "Failed to eager-persist agent message",
            session_id=str(agent_session_id),
            turn_number=turn_number,
            exc_info=True,
        )


# Regex to strip <thinking>...</thinking> or <think>...</think> tags (case-insensitive, multiline).
# Some models (e.g., Minimax) emit reasoning tokens wrapped in these tags that shouldn't be shown to users.
# Uses alternation to ensure matching open/close tags: <think> with </think>, <thinking> with </thinking>.
# Note: Nested thinking tags are not expected from current models; the non-greedy .*? handles the common case.
_THINKING_TAG_PATTERN = re.compile(r"<thinking>(.*?)</thinking>|<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> and <think>...</think> tags from text.

    Some models emit internal reasoning wrapped in thinking tags. These should
    be filtered out before displaying to users (e.g., in Slack).

    Note: This operates on complete API responses (not streaming chunks), so
    tags won't be split across calls. The raw executor uses non-streaming API
    calls that return complete responses per step.
    """
    return _THINKING_TAG_PATTERN.sub("", text).strip()


async def _resolve_user_name_from_db(user_id: str) -> str | None:
    """Look up a user's name from the users table."""
    async with get_async_session() as session:
        result = await session.execute(text("SELECT name FROM users WHERE user_id = :uid"), {"uid": user_id})
        row = result.first()
        return str(row[0]) if row and row[0] else None


# Agent names that support per-user personalization.
# When a session targets one of these agents and a user_id is available,
# AHS attempts to resolve a user-specific variant (e.g., "yuppclaw-alice").
PERSONAL_AGENT_PREFIXES: frozenset[str] = frozenset({"yuppclaw"})


async def _resolve_personal_agent_for_user(base_agent_name: str, user_id: str) -> tuple[str | None, str | None]:
    """Resolve a personal agent name and display name for a user.

    Returns:
        Tuple of (personal_agent_name, display_name) if the user has a personal
        agent, or (None, None) to fall back to the base agent.
    """
    async with get_async_session() as session:
        # Look up user email
        result = await session.execute(text("SELECT email, name FROM users WHERE user_id = :uid"), {"uid": user_id})
        row = result.first()
        if not row or not row[0]:
            logger.warning(
                "Could not resolve email for personal agent",
                user_id=user_id,
                base_agent_name=base_agent_name,
            )
            return None, None

        email = str(row[0])
        user_name = str(row[1]) if row[1] else None

        # Only yupp.ai users can have personal agents
        if not email.endswith("@yupp.ai"):
            logger.info(
                "Non-yupp.ai user, skipping personal agent resolution",
                user_id=user_id,
                email_domain=email.split("@")[-1],
            )
            return None, None

        username = re.sub(r"[^a-z0-9-]", "-", email.split("@")[0].lower()).strip("-")
        personal_agent_name = f"{base_agent_name}-{username}"

        # Check if the personal agent exists in DB
        check = await session.execute(text("SELECT 1 FROM agents WHERE name = :name"), {"name": personal_agent_name})
        if not check.first():
            logger.info(
                "No personal agent found, using base agent",
                base_agent_name=base_agent_name,
                personal_agent_name=personal_agent_name,
                user_id=user_id,
            )
            return None, None

    # Derive display name: "Alice's yClaw"
    first_name = user_name.split()[0] if user_name else username.capitalize()
    display_name = f"{first_name}\u2019s yClaw"

    logger.info(
        "Resolved personal agent",
        base_agent_name=base_agent_name,
        personal_agent_name=personal_agent_name,
        display_name=display_name,
        user_id=user_id,
    )
    return personal_agent_name, display_name


async def _resolve_agent(session: AsyncSession, agent_name: str) -> Agent | None:
    """Look up an agent by name."""
    result = await session.exec(select(Agent).where(Agent.name == agent_name))
    return result.one_or_none()


async def _load_agent_config_with_db_fallback(agent_name: str) -> AgentConfig | None:
    """Load agent config from disk, falling back to DB for DB-only agents (e.g. personal agents).

    This is the primary way to obtain an AgentConfig at runtime. Disk-based agents
    (with config.json on disk) are loaded first; if not found, the Agent DB record's
    config JSONB is used instead.
    """
    cfg = load_agent_config(agent_name)
    if cfg:
        return cfg
    try:
        async with get_async_session() as session:
            db_agent = await _resolve_agent(session, agent_name)
            if db_agent and db_agent.config:
                return load_agent_config_from_db(db_agent)
    except Exception:
        logger.error("Failed to load agent config from DB", name=agent_name, exc_info=True)
    return None


async def _resolve_session(session: AsyncSession, session_id: str) -> AgentSession | None:
    """Look up a session by UUID or slack_session_id."""
    # Try UUID first
    try:
        session_uuid = uuid.UUID(session_id)
        result = await session.exec(select(AgentSession).where(AgentSession.agent_session_id == session_uuid))
        agent_session = result.one_or_none()
        if agent_session:
            return agent_session
    except ValueError:
        pass

    # Try slack_session_id
    result = await session.exec(select(AgentSession).where(AgentSession.slack_session_id == session_id))
    return result.one_or_none()


async def _next_turn_number(session: AsyncSession, agent_session_id: uuid.UUID) -> int:
    """Get the next turn number for a session."""
    result = await session.exec(
        select(func.coalesce(func.max(AgentSessionMessage.turn_number), 0)).where(
            AgentSessionMessage.agent_session_id == agent_session_id
        )
    )
    current_max = result.one()
    return current_max + 1


async def _has_inflight_turn(session: AsyncSession, agent_session_id: uuid.UUID) -> bool:
    """Check if there's a USER message whose turn has no AGENT/SYSTEM response yet.

    Must be called under FOR UPDATE lock on the session row.
    """
    # Find the latest turn that has a USER message
    latest_user_turn = await session.exec(
        select(func.max(AgentSessionMessage.turn_number)).where(
            AgentSessionMessage.agent_session_id == agent_session_id,
            col(AgentSessionMessage.role) == AgentSessionMessageRole.USER,
        )
    )
    max_user_turn: int | None = latest_user_turn.one()
    if max_user_turn is None:
        return False

    # Check if that turn has a completed response.  Any message whose
    # completion_status is not IN_PROGRESS counts — SYSTEM messages are always
    # terminal; AGENT eager-persist draft rows carry IN_PROGRESS and are excluded.
    response_result = await session.exec(
        select(func.count()).where(
            AgentSessionMessage.agent_session_id == agent_session_id,
            AgentSessionMessage.turn_number == max_user_turn,
            col(AgentSessionMessage.role) != AgentSessionMessageRole.USER,
            col(AgentSessionMessage.completion_status) != AgentSessionMessageCompletionStatus.IN_PROGRESS,
        )
    )
    response_count = response_result.one()
    return response_count == 0


async def _download_attachments_to_workspace(
    attachments: list[AttachmentInfo],
    workspace: str,
) -> list[str]:
    """Download attachments from GCS into the session workspace.

    Files are saved to {workspace}/attachments/{filename}. Already-downloaded
    files (same path exists) are skipped to avoid redundant downloads.

    Args:
        attachments: Attachment metadata with GCS URLs.
        workspace: Session workspace root directory.

    Returns:
        List of relative paths (e.g., "attachments/screenshot.png") for
        successfully downloaded files.
    """
    from ypl.backend.utils.gcs_utils import download_from_gcs

    attachments_dir = os.path.join(workspace, "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    downloaded_paths: list[str] = []
    for att in attachments:
        # Sanitize: use only the basename and reject path separators / ".." / "."
        safe_name = os.path.basename(att.filename)
        if not safe_name or safe_name in ("..", "."):
            logger.warning("Skipping attachment with unsafe filename", filename=att.filename)
            continue
        local_path = os.path.join(attachments_dir, safe_name)
        relative_path = f"attachments/{safe_name}"

        try:
            data = await download_from_gcs(att.gcs_url)
            with open(local_path, "wb") as f:
                f.write(data)
            downloaded_paths.append(relative_path)
            logger.info(
                "Downloaded attachment to workspace",
                filename=att.filename,
                size=len(data),
                workspace=workspace,
            )
        except Exception:
            logger.error(
                "Failed to download attachment from GCS",
                filename=att.filename,
                gcs_url=att.gcs_url,
                exc_info=True,
            )

    return downloaded_paths


def _prepend_attachment_paths(message: str, paths: list[str]) -> str:
    """Prepend attachment file paths to the message text.

    The paths are listed so the agent knows which files are available
    in its workspace and can read them with the `read` tool.
    """
    if not paths:
        return message
    paths_str = ", ".join(paths)
    return f"[Attached files: {paths_str}]\n\n{message}"


async def _mark_session_completed(
    session: AsyncSession,
    agent_session_id: uuid.UUID,
) -> None:
    """Mark a session COMPLETED regardless of trigger type.

    All triggers (SLACK, CRON, API, TASK, WEBHOOK) transition to COMPLETED after
    each successful turn.  SLACK sessions are multi-turn: when a follow-up message
    arrives, send_message() re-activates the session to ACTIVE before the new turn
    starts, so the status correctly reflects in-progress vs idle state.
    """
    s = await session.get(AgentSession, agent_session_id)
    if s:
        s.status = AgentSessionStatus.COMPLETED


async def _drain_pending_messages(agent_session_id: uuid.UUID) -> None:
    """Process queued messages after a turn completes.

    Pops all pending messages for the session, combines them into a single
    message, and dispatches a new turn via send_message(). If only one message
    is queued, it's sent as-is. Multiple messages get a header and numbered list.
    """
    pending = _pending_messages.pop(agent_session_id, [])
    if not pending:
        return

    if len(pending) == 1:
        combined = pending[0].message
    else:
        parts = [f"[{len(pending)} messages arrived while processing the previous turn]\n"]
        for i, p in enumerate(pending, 1):
            parts.append(f"({i}) {p.message}")
        combined = "\n\n".join(parts)

    # Use metadata from the most recent queued message.
    # TODO: if multi-user threads need per-sender permissions, enforce same-sender
    # batching or apply least-privilege when combining (see PR #10645).
    last = pending[-1]

    # Merge attachments from all pending messages (each may carry its own files).
    all_attachments: list[AttachmentInfo] = []
    for p in pending:
        all_attachments.extend(p.attachments)

    logger.info(
        "Draining pending messages into new turn",
        session_id=str(agent_session_id),
        count=len(pending),
        combined_length=len(combined),
    )

    try:
        await send_message(
            SessionMessageRequest(
                session_id=str(agent_session_id),
                message=combined,
                slack_ts=last.slack_ts,
                slack_user_id=last.slack_user_id,
                user_id=last.user_id,
                attachments=all_attachments or None,
                source=last.source,  # preserve original request source
            )
        )
    except Exception:
        logger.error(
            "Failed to drain pending messages, re-queuing",
            session_id=str(agent_session_id),
            count=len(pending),
            exc_info=True,
        )
        # Re-queue so the messages aren't silently dropped after the user was told "queued".
        existing = _pending_messages.get(agent_session_id, [])
        _pending_messages[agent_session_id] = pending + existing


async def _inject_internal_message(
    agent_session_id: uuid.UUID,
    message: str,
    agent_session_data: dict[str, Any],
) -> None:
    """Inject a harness-generated message into a session, bypassing user validation.

    Used to deliver subagent results back to the parent session without requiring
    the message to come from a real user. The session's creator_user_id is used
    for DB attribution.

    Args:
        agent_session_id: UUID of the target session.
        message: The message content to inject.
        agent_session_data: Pre-fetched session metadata (from deliver_subagent_result_to_parent).
    """
    agent_config_name: str | None = agent_session_data.get("agent_name")
    creator_user_id: str | None = agent_session_data.get("creator_user_id")

    if not agent_config_name:
        logger.error(
            "Cannot inject internal message: missing agent_name",
            session_id=str(agent_session_id),
        )
        return

    agent_config = await _load_agent_config_with_db_fallback(agent_config_name)
    if not agent_config:
        logger.error(
            "Cannot inject internal message: agent config not found",
            session_id=str(agent_session_id),
            agent_name=agent_config_name,
        )
        return

    # Write USER message and re-activate session in a single DB transaction
    turn_number: int
    async with get_async_session() as session:
        result = await session.exec(select(AgentSession).where(AgentSession.agent_session_id == agent_session_id))
        agent_session = result.one_or_none()
        if not agent_session:
            logger.error(
                "Cannot inject internal message: session not found",
                session_id=str(agent_session_id),
            )
            return

        # Re-activate if in a terminal-but-resumable state
        if agent_session.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
            agent_session.status = AgentSessionStatus.ACTIVE

        turn_number = await _next_turn_number(session, agent_session_id)
        user_msg = AgentSessionMessage(
            agent_session_id=agent_session_id,
            turn_number=turn_number,
            role=AgentSessionMessageRole.USER,
            content=message,
            creator_user_id=creator_user_id,
        )
        session.add(user_msg)
        await session.commit()

    # Capture session fields needed for _run_agent_task (session object is now detached)
    workspace = agent_session_data.get("workspace")
    extra_dirs: list[str] = agent_session_data.get("extra_dirs") or []
    slack_session_id = agent_session_data.get("slack_session_id")
    llm_session_id = agent_session_data.get("llm_session_id")
    trigger = agent_session_data.get("trigger")
    session_context: dict[str, Any] = dict(agent_session_data.get("context") or {})
    is_slack = trigger == AgentSessionTrigger.SLACK.value if trigger else False
    is_task = trigger == AgentSessionTrigger.TASK.value if trigger else False

    # Register sentinel and fire background task
    _active_tasks[agent_session_id] = None  # type: ignore[assignment]
    task = create_background_task(
        _run_agent_task(
            agent_session_id=agent_session_id,
            turn_number=turn_number,
            message=message,
            agent_config_name=agent_config_name,
            workspace=workspace,
            llm_session_id=llm_session_id,
            extra_dirs=extra_dirs,
            slack_session_id=slack_session_id,
            is_slack=is_slack,
            is_task=is_task,
            session_context=session_context,
            trigger=trigger,
        )
    )
    _active_tasks[agent_session_id] = task

    logger.info(
        "Injected internal message into session",
        session_id=str(agent_session_id),
        turn_number=turn_number,
        agent_name=agent_config_name,
        message_length=len(message),
    )


async def deliver_subagent_result_to_parent(parent_session_id: str, result: "SubagentResult") -> None:
    """Deliver a completed subagent's result to its parent session.

    Called by subagent_queue._drain() after a subagent finishes (any outcome).
    Formats the result as a user-turn message and either:
    - Queues it in _pending_messages if the parent is currently processing a turn
    - Injects it directly via _inject_internal_message if the parent is idle

    Args:
        parent_session_id: UUID string of the parent session.
        result: SubagentResult with status, text, and metadata.
    """
    # Import here to avoid circular import at module load
    from ypl.agent_harness_service.core.subagent_queue import SubagentResult  # noqa: F401

    try:
        parent_uuid = uuid.UUID(parent_session_id)
    except ValueError:
        logger.error(
            "Invalid parent_session_id for subagent delivery",
            parent_session_id=parent_session_id,
        )
        return

    # Format the message
    status_icon = "✅" if result.status == "completed" else "❌"
    short_sid = result.db_session_id[:8] if result.db_session_id else "unknown"
    duration_str = f"{result.duration_ms / 1000:.1f}s" if result.duration_ms else "?"
    header = f"[SUBAGENT RESULT] {status_icon} `{result.agent_type}` — session `{short_sid}` — {duration_str}s"
    if result.cost_usd:
        header += f" — ${result.cost_usd:.2f}"
    if result.description:
        header += f"\n_{result.description}_"

    message = f"{header}\n\n{result.text}" if result.text else header

    # Fetch parent session metadata
    agent_session_data: dict[str, Any] | None = None
    try:
        async with get_async_session() as db:
            agent_session_obj = await db.get(AgentSession, parent_uuid)
            if agent_session_obj:
                agent_obj = await db.get(Agent, agent_session_obj.agent_id)
                agent_session_data = {
                    "creator_user_id": agent_session_obj.creator_user_id,
                    "agent_name": agent_obj.name if agent_obj else None,
                    "workspace": agent_session_obj.workspace,
                    "extra_dirs": agent_session_obj.extra_dirs or [],
                    "slack_session_id": agent_session_obj.slack_session_id,
                    "llm_session_id": agent_session_obj.llm_session_id,
                    "trigger": agent_session_obj.trigger.value if agent_session_obj.trigger else None,
                    "context": dict(agent_session_obj.context) if agent_session_obj.context else {},
                    "status": agent_session_obj.status,
                }
    except Exception:
        logger.error(
            "Failed to fetch parent session for subagent result delivery",
            parent_session_id=parent_session_id,
            exc_info=True,
        )
        return

    if not agent_session_data:
        logger.warning(
            "Parent session not found for subagent result delivery",
            parent_session_id=parent_session_id,
            agent_type=result.agent_type,
        )
        return

    creator_user_id = agent_session_data.get("creator_user_id")

    logger.info(
        "Delivering subagent result to parent",
        parent_session_id=parent_session_id,
        agent_type=result.agent_type,
        status=result.status,
        db_session_id=result.db_session_id,
        parent_has_inflight=parent_uuid in _active_tasks,
    )

    # Queue or inject
    if parent_uuid in _active_tasks:
        # Parent is busy — queue for delivery after current turn completes
        queue = _pending_messages.setdefault(parent_uuid, [])
        if len(queue) < _MAX_PENDING_MESSAGES:
            queue.append(
                PendingMessage(
                    message=message,
                    user_id=creator_user_id,
                    source="api",
                )
            )
            logger.info(
                "Subagent result queued (parent busy)",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                queue_depth=len(queue),
            )
        else:
            logger.error(
                "Subagent result dropped: pending queue full",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                queue_size=_MAX_PENDING_MESSAGES,
            )
    else:
        # Parent is idle — inject directly
        await _inject_internal_message(parent_uuid, message, agent_session_data)


async def _maybe_update_task_completion(
    trigger: str | None,
    session_context: dict[str, Any] | None,
    agent_session_id: uuid.UUID,
    success: bool,
    result: dict | None,
    error: str | None,
    error_subtype: str | None = None,
) -> None:
    """Update task completion status if session was triggered by a task.

    This is a no-op if the session was not triggered by the task executor.
    """
    if trigger != AgentSessionTrigger.TASK.value:
        return
    if not session_context or not session_context.get("task_id"):
        return

    try:
        # Inline import to avoid circular dependency (task_executor imports service.create_session)
        from ypl.agent_harness_service.task_executor import update_task_completion

        await update_task_completion(
            task_id=uuid.UUID(session_context["task_id"]),
            session_id=str(agent_session_id),
            success=success,
            result=result,
            error=error,
            error_subtype=error_subtype,
        )
    except Exception:
        logger.error(
            "Failed to update task completion",
            task_id=session_context.get("task_id"),
            session_id=str(agent_session_id),
            exc_info=True,
        )


async def _run_agent_task(
    agent_session_id: uuid.UUID,
    turn_number: int,
    message: str,
    agent_config_name: str,
    workspace: str | None,
    llm_session_id: str | None,
    extra_dirs: list[str],
    slack_session_id: str | None = None,
    is_slack: bool = False,
    is_task: bool = False,
    session_context: dict[str, Any] | None = None,
    trigger: str | None = None,
    session_created_at: datetime | None = None,
) -> None:
    """Background task: run the agent and persist results.

    This runs outside the request lifecycle. On completion it stores the
    agent response in the DB, updates the session's llm_session_id
    so the next turn can --resume, and calls the gateway to push the reply.
    """
    was_cancelled = False
    try:
        # Guard: bail out if stop_session() already wrote [INTERRUPTED] for this
        # turn before the task got a chance to run (sentinel race).
        async with get_async_session() as session:
            interrupted_check = await session.exec(
                select(func.count()).where(
                    AgentSessionMessage.agent_session_id == agent_session_id,
                    AgentSessionMessage.turn_number == turn_number,
                    col(AgentSessionMessage.role) == AgentSessionMessageRole.SYSTEM,
                )
            )
            if interrupted_check.one() > 0:
                logger.info(
                    "Turn already interrupted before task started, aborting",
                    session_id=str(agent_session_id),
                    turn_number=turn_number,
                )
                return

        agent_config = await _load_agent_config_with_db_fallback(agent_config_name)
        if not agent_config:
            logger.error("Agent config not found in background task", name=agent_config_name)
            # Persist a SYSTEM error so the turn isn't permanently stuck as "inflight"
            try:
                async with get_async_session() as session:
                    error_msg = AgentSessionMessage(
                        agent_session_id=agent_session_id,
                        turn_number=turn_number,
                        role=AgentSessionMessageRole.SYSTEM,
                        content=f"[ERROR] Agent config not found: {agent_config_name}",
                        completion_status=AgentSessionMessageCompletionStatus.FAILED,
                        error_type=AgentSessionMessageErrorType.ERROR_INTERNAL,
                    )
                    session.add(error_msg)
                    await _mark_session_completed(session, agent_session_id)
                    await session.commit()
            except Exception:
                logger.error("Failed to persist config-not-found error", session_id=str(agent_session_id))
            return

        # Register bwrap sandbox setting so MCP bash tool picks it up
        set_session_sandbox(str(agent_session_id), agent_config.sandbox.bwrap_enabled)
        # Reset per-turn websearch counter for the new turn
        reset_turn_websearch_count(str(agent_session_id))

        exec_cfg = agent_config.executor_config
        logger.info(
            "Agent config loaded for task",
            session_id=str(agent_session_id),
            agent_name=agent_config_name,
            executor_type=exec_cfg.type,
            model=agent_config.model,
            has_mcp=agent_config.has_mcp,
            tool_permissions=agent_config.tool_permissions,
            allowed_subagents=agent_config.allowed_subagents,
            sandbox_enabled=agent_config.sandbox.enabled,
            sandbox_bwrap_enabled=agent_config.sandbox.bwrap_enabled,
            sandbox_auto_allow_bash=agent_config.sandbox.auto_allow_bash_if_sandboxed,
            max_turns=agent_config.max_turns,
            max_budget_usd=agent_config.max_budget_usd,
            timeout_s=agent_config.timeout_s,
        )

        # Pop any pre-spawned process task started by create_session().
        # Only present on the first turn of a new session; None for all later turns.
        pre_proc_task = _pre_spawn_tasks.pop(agent_session_id, None)

        runner: AgentRunner
        if exec_cfg.type == EXECUTOR_TYPE_RAW:
            if exec_cfg.model == "mock":
                runner = MockRunner(agent_config)
            else:
                runner = RawExecutorRunner(agent_config)
            # Raw/mock executor doesn't use the Claude CLI subprocess.
            if pre_proc_task is not None:
                pre_proc_task.cancel()
                pre_proc_task = None
        elif exec_cfg.model == HARNESS_CODEX_CLI:
            runner = CodexAppServerRunner(agent_config)
            # Codex runner uses a different CLI — cancel the Claude CLI pre-spawn.
            if pre_proc_task is not None:
                pre_proc_task.cancel()
                pre_proc_task = None
        else:
            runner = ClaudeCodeRunner(agent_config, pre_proc_task=pre_proc_task)

        run_context = RunContext(
            session_id=str(agent_session_id),
            workspace=workspace,
            llm_session_id=llm_session_id,
            extra_dirs=extra_dirs,
            slack_session_id=slack_session_id,
            is_slack=is_slack,
            is_task=is_task,
            session_context=session_context,
            session_created_at=session_created_at,
        )

        # Resolve the outgoing gateway from the trigger type.
        gateway_name = TRIGGER_TO_GATEWAY.get(trigger or "")
        gateway_session_id = slack_session_id  # will generalize when more gateways exist
        # If the session was re-attached to Slack after creation (e.g. a CRON session that
        # received a human reply in its thread), slack_session_id is populated but the
        # original trigger doesn't map to a gateway.  Infer "slack" so the reply routes back.
        # Guard: only infer Slack when session_context confirms actual Slack attachment
        # (slack_channel_id is set by attach_slack_to_session / Slack-triggered sessions).
        # Without this check, API sessions whose generic session_id is stored in
        # slack_session_id would be misrouted to the Slack gateway.
        _has_slack_context = bool(session_context and session_context.get("slack_channel_id"))
        if not gateway_name and gateway_session_id and _has_slack_context:
            gateway_name = "slack"
            logger.info(
                "Gateway inferred from slack_session_id (trigger has no default gateway)",
                session_id=str(agent_session_id),
                trigger=trigger,
            )
        gateway: Gateway | None = None
        if gateway_name and gateway_session_id:
            registry = GatewayRegistry.get_instance()
            gateway = registry.get_for_session(gateway_name, agent_config)

        # Extract display name override from session context (set by personal agent resolution).
        gateway_username: str | None = (session_context or {}).get("display_name")

        events: list[dict] = []
        final_text = ""
        seen_outlet_tool_ids: set[str] = set()  # dedup outlet tool extraction across event types
        last_gateway_reply_time = 0.0  # monotonic; 0 ensures first block always creates a new message
        had_error = False
        error_text = ""
        result_llm_session_id: str | None = None
        result_cost_usd: float | None = None
        result_duration_ms: int | None = None
        result_num_turns: int | None = None
        result_subtype: str | None = None
        # TTFCT/TTLCT: wall-clock timestamps (nanoseconds, monotonic) for the
        # first and last assistant text events seen in this turn.  NULL until
        # the first/last text-bearing assistant event is observed.
        first_text_time_ns: int | None = None
        last_text_time_ns: int | None = None
        eager = EagerPersistState()
        model_name = exec_cfg.model or "__unknown__"
        # Tracks the in-flight status-hint task so we can cancel stale ones
        # before starting newer ones, preserving per-session ordering.
        _status_task: asyncio.Task[bool] | None = None

        # WebSocket streaming: set up translation state and publish channel
        translation_state = TranslationState(str(agent_session_id), turn_number)
        stream_channel = f"ahs:stream:{agent_session_id}"

        async def _publish_codex_events(codex_events: list[dict]) -> None:
            """Publish translated Codex events to the streaming PubSub."""
            from ypl.agent_harness_service.core.streaming import _pubsub

            if _pubsub is None:
                return  # Streaming not initialized (e.g., tests without server)
            pubsub = get_pubsub()
            for ce in codex_events:
                await pubsub.publish(stream_channel, json.dumps(ce))

        # Emit turn/started
        await _publish_codex_events(
            [
                {
                    "type": "turn/started",
                    "turn_id": translation_state.turn_id,
                }
            ]
        )

        # Anchor for TTFCT/TTLCT: recorded immediately before the first event
        # arrives from the runner.  Captures queue-drain + first-token latency
        # as seen by this process, excluding Python startup and MCP handshake.
        turn_loop_start_ns = time.monotonic_ns()

        try:
            async for event in runner.run(message, run_context):
                events.append(_trim_value(event.raw))

                # Log every event regardless of type
                excerpt = extract_excerpt(event)
                sid = str(agent_session_id)[-6:]
                logger.info(
                    f"session {sid} [AGENT] [{event.type}]: {excerpt}",
                    agent_name=agent_config_name,
                    session_id=str(agent_session_id),
                    role="AGENT",
                    event_type=event.type,
                    turn_number=turn_number,
                )

                # --- Eager persist: track tool calls ---
                # RawExecutor emits separate "tool_use" events; CLI runner embeds
                # tool_use blocks inside "assistant" events.
                tool_names_in_event: list[str] = []
                if event.type == "tool_use":
                    tool_names_in_event = [event.raw.get("name", "unknown")]
                elif event.type == "assistant":
                    content_blocks = event.raw.get("message", {}).get("content", [])
                    tool_names_in_event = [
                        b.get("name", "unknown") for b in content_blocks if b.get("type") == "tool_use"
                    ]

                if tool_names_in_event:
                    eager.tool_call_count += len(tool_names_in_event)
                    eager.pending_tool_names.extend(tool_names_in_event)
                    should_persist = (
                        eager.msg_id is None  # first tool call → INSERT "[started]"
                        or (eager.tool_call_count - eager.last_persisted_tool_count) >= _EAGER_PERSIST_TOOL_INTERVAL
                    )
                    if should_persist:
                        await _eager_persist_agent_msg(eager, events, agent_session_id, turn_number, model_name)

                    # Push a live status hint to the gateway (Slack shows it as a
                    # small muted context block that updates in-place).
                    if gateway and gateway_session_id:
                        try:
                            total = eager.tool_call_count
                            # Show only the last few tool names to keep the hint concise.
                            recent = eager.pending_tool_names[-5:]
                            tool_list = ", ".join(recent)
                            if total > len(recent):
                                tool_list += f" (+{total - len(recent)} more)"
                            plural = "s" if total != 1 else ""
                            status_text = f"🔧 {total} tool{plural} used: {tool_list}"
                            # Fire-and-forget: don't block the hot event-loop path on the
                            # gateway RPC (a slow/degraded SAG would add N * timeout latency).
                            # Cancel any in-flight status task first to preserve per-session
                            # ordering — an older task that arrives after a newer one would
                            # overwrite status_pending with stale content.
                            if _status_task is not None and not _status_task.done():
                                _status_task.cancel()
                            _status_task = asyncio.create_task(
                                gateway.send_status_update(gateway_session_id, status_text)
                            )
                            # Retrieve the exception via callback to suppress the
                            # "Task exception was never retrieved" warning.
                            _status_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        except Exception:
                            logger.debug(
                                "Failed to schedule tool status update to gateway",
                                session_id=str(agent_session_id),
                                exc_info=True,
                            )

                # --- Single decision point: extract all user-visible content ---
                visible_contents = _extract_visible_content(event, seen_outlet_tool_ids)

                # If an assistant event had text but it was all thinking tags,
                # skip remaining processing (including WS streaming).
                if event.type == "assistant" and event.text and not visible_contents:
                    continue

                # Translate and publish to WebSocket streaming.
                # Sanitize content blocks so WS clients don't receive raw <thinking> tags.
                ws_event = event
                if event.type == "intermediate_text":
                    raw_text = event.raw.get("text", "")
                    cleaned = strip_thinking_tags(raw_text)
                    if not cleaned:
                        continue  # Skip if the entire block was thinking content
                    if cleaned != raw_text:
                        ws_event = StreamEvent(type=event.type, raw={**event.raw, "text": cleaned})
                elif event.type == "assistant":
                    sanitized_raw = dict(event.raw)
                    msg = sanitized_raw.get("message", {})
                    if "content" in msg:
                        sanitized_raw["message"] = {
                            **msg,
                            "content": [
                                {**b, "text": strip_thinking_tags(b.get("text", ""))} if b.get("type") == "text" else b
                                for b in msg["content"]
                            ],
                        }
                    ws_event = StreamEvent(type=event.type, raw=sanitized_raw)
                codex_events = translate_stream_event(ws_event, translation_state)
                if codex_events:
                    await _publish_codex_events(codex_events)

                if event.type == "intermediate_text":
                    # Send intermediate text to gateway so users see progress in Slack.
                    # Tagged as "thinking" so the gateway renders it as muted/context text.
                    intermediate_text = event.raw.get("text", "")
                    cleaned_intermediate = strip_thinking_tags(intermediate_text)
                    if cleaned_intermediate and gateway and gateway_session_id:
                        now = time.monotonic()
                        use_append = (
                            last_gateway_reply_time > 0
                            and (now - last_gateway_reply_time) < _GATEWAY_APPEND_THRESHOLD_SECONDS
                        )
                        try:
                            if use_append:
                                ok = await gateway.append_reply(
                                    gateway_session_id,
                                    "\n\n" + cleaned_intermediate,
                                    reply_type="thinking",
                                    username=gateway_username,
                                )
                            else:
                                ok = await gateway.send_reply(
                                    gateway_session_id,
                                    cleaned_intermediate,
                                    reply_type="thinking",
                                    username=gateway_username,
                                )
                            if ok:
                                last_gateway_reply_time = now
                        except Exception:
                            logger.error(
                                "Failed to send intermediate text to gateway",
                                session_id=str(agent_session_id),
                            )

                elif visible_contents:
                    # --- Record TTFCT/TTLCT for assistant text events ---
                    # Only assistant events with actual visible text carry direct
                    # LLM-generated content; outlet tool content (send_slack_message
                    # etc.) may appear in visible_contents even for assistant events
                    # that have tool_use blocks but no text.  Gate on cleaned text
                    # (strip_thinking_tags) to exclude events whose only content is
                    # thinking tags, ensuring these metrics reflect pure inference
                    # latency and the NULL-for-tool-only semantics are preserved.
                    if event.type == "assistant" and strip_thinking_tags(event.text or ""):
                        _text_ts_ns = time.monotonic_ns()
                        if first_text_time_ns is None:
                            first_text_time_ns = _text_ts_ns
                        last_text_time_ns = _text_ts_ns

                    # --- Persist all visible content to DB ---
                    for vc in visible_contents:
                        if final_text:
                            final_text += "\n\n"
                        final_text += vc.text
                        eager.flush_pending_tools()
                        eager.content_parts.append(vc.text)
                    await _eager_persist_agent_msg(eager, events, agent_session_id, turn_number, model_name)

                    # --- Relay to gateway for content not already delivered ---
                    # Content marked already_delivered was posted to the outlet
                    # directly by the MCP tool (e.g., send_slack_message); relaying
                    # it again via the gateway would cause duplicates.
                    undelivered = [vc for vc in visible_contents if not vc.already_delivered]
                    if undelivered and gateway and gateway_session_id:
                        relay_text = "\n\n".join(vc.text for vc in undelivered)
                        now = time.monotonic()
                        use_append = (
                            last_gateway_reply_time > 0
                            and (now - last_gateway_reply_time) < _GATEWAY_APPEND_THRESHOLD_SECONDS
                        )
                        try:
                            if use_append:
                                ok = await gateway.append_reply(
                                    gateway_session_id, "\n\n" + relay_text, username=gateway_username
                                )
                            else:
                                ok = await gateway.send_reply(gateway_session_id, relay_text, username=gateway_username)
                            if ok:
                                last_gateway_reply_time = now
                        except Exception:
                            logger.error("Failed to send reply to gateway", session_id=str(agent_session_id))
                    elif undelivered:
                        logger.info(
                            "Agent content block received (no gateway)",
                            session_id=str(agent_session_id),
                            text_preview=undelivered[0].text[:200],
                        )

                elif event.type == "result":
                    result_llm_session_id = event.session_id
                    result_cost_usd = event.cost_usd
                    result_duration_ms = event.duration_ms
                    result_num_turns = event.num_turns
                    result_subtype = event.subtype

                elif event.type == "error":
                    had_error = True
                    error_msg = event.raw.get("error", "Unknown agent error")
                    error_text = f"[ERROR] {error_msg}"
                    logger.error(
                        "Agent returned error",
                        session_id=str(agent_session_id),
                        error=error_msg,
                    )

        except asyncio.CancelledError:
            was_cancelled = True
            logger.info(
                "Agent task cancelled (user stop)",
                session_id=str(agent_session_id),
                turn_number=turn_number,
            )
            # Close any open message item before emitting turn/completed (failed)
            close_events = _close_message_item(translation_state)
            if close_events:
                await _publish_codex_events(close_events)
            # Notify WebSocket clients that the turn was cancelled
            await _publish_codex_events(
                [
                    {
                        "type": "turn/completed",
                        "turn_id": translation_state.turn_id,
                        "status": "failed",
                        "error": {"message": "Turn cancelled by user"},
                    }
                ]
            )
            # No SYSTEM message here — stop_session() handles that.
            # No Slack notification here — SAG handles the interruption message.
            # Delete the eagerly-persisted draft row if it exists.
            if eager.msg_id is not None:
                try:
                    async with get_async_session() as session:
                        await session.exec(
                            sa_delete(AgentSessionMessage).where(
                                col(AgentSessionMessage.agent_session_message_id) == eager.msg_id
                            )
                        )
                        await session.commit()
                except Exception:
                    logger.error(
                        "Failed to delete eager row on cancel",
                        session_id=str(agent_session_id),
                        exc_info=True,
                    )
            return

        except Exception as exc:
            logger.error(
                "Error running agent",
                session_id=str(agent_session_id),
                exc_info=True,
            )
            # Close any open message item before emitting turn/completed (failed)
            close_events = _close_message_item(translation_state)
            if close_events:
                await _publish_codex_events(close_events)
            # Notify WebSocket clients of the crash
            await _publish_codex_events(
                [
                    {
                        "type": "turn/completed",
                        "turn_id": translation_state.turn_id,
                        "status": "failed",
                        "error": {"message": f"Runner crashed: {exc}"},
                    }
                ]
            )
            # Persist a SYSTEM failure message so the turn isn't stuck in "processing"
            crash_content = f"[ERROR] Runner crashed: {exc}"
            logger.info(
                f"Agent [{agent_config_name}] session {agent_session_id} [SYSTEM] message: [error] {str(exc)[:200]}",
                agent_name=agent_config_name,
                session_id=str(agent_session_id),
                role="SYSTEM",
                event_type="error",
                turn_number=turn_number,
                excerpt=str(exc)[:200],
            )
            try:
                async with get_async_session() as session:
                    await _persist_system_msg(
                        eager, crash_content, events, agent_session_id, turn_number, model_name, session
                    )
                    await _mark_session_completed(session, agent_session_id)
                    await session.commit()
            except Exception:
                logger.error(
                    "Failed to persist crash record",
                    session_id=str(agent_session_id),
                    exc_info=True,
                )
            # Notify gateway so the user sees a terminal reply
            if gateway and gateway_session_id:
                try:
                    await gateway.send_reply(gateway_session_id, crash_content, username=gateway_username)
                except Exception:
                    logger.error("Failed to send crash reply to gateway", session_id=str(agent_session_id))

            await _maybe_update_task_completion(
                trigger, session_context, agent_session_id, success=False, result=None, error=crash_content
            )
            return

        # Persist error as a SYSTEM message so we have a record of the failure
        if had_error:
            try:
                async with get_async_session() as session:
                    await _persist_system_msg(
                        eager, error_text, events, agent_session_id, turn_number, model_name, session
                    )
                    await _mark_session_completed(session, agent_session_id)
                    await session.commit()
            except Exception:
                logger.error(
                    "Failed to persist error record",
                    session_id=str(agent_session_id),
                    exc_info=True,
                )

            logger.warning(
                "Agent turn had errors, persisted error record",
                session_id=str(agent_session_id),
                turn_number=turn_number,
            )
            # Notify gateway so the user sees a terminal reply
            if gateway and gateway_session_id:
                try:
                    await gateway.send_reply(gateway_session_id, error_text, username=gateway_username)
                except Exception:
                    logger.error("Failed to send error reply to gateway", session_id=str(agent_session_id))

            await _maybe_update_task_completion(
                trigger, session_context, agent_session_id, success=False, result=None, error=error_text
            )
            return

        if not final_text:
            logger.warning(
                "Agent produced empty response",
                session_id=str(agent_session_id),
            )

        # Determine final completion_status / error_type for the AGENT message
        # based on the runner's explicit result_subtype.
        if result_subtype == "error_max_turns":
            _msg_completion = AgentSessionMessageCompletionStatus.FAILED
            _msg_error_type = AgentSessionMessageErrorType.ERROR_MAX_TURNS
        elif result_subtype == "stopped_context_overflow":
            _msg_completion = AgentSessionMessageCompletionStatus.FAILED
            _msg_error_type = AgentSessionMessageErrorType.ERROR_CONTEXT_OVERFLOW
        elif result_subtype == "error":
            _msg_completion = AgentSessionMessageCompletionStatus.FAILED
            _msg_error_type = AgentSessionMessageErrorType.ERROR_EXECUTOR
        else:
            _msg_completion = AgentSessionMessageCompletionStatus.SUCCESS
            _msg_error_type = AgentSessionMessageErrorType.NONE

        # Persist agent response — but first re-check that the turn wasn't
        # interrupted while we were streaming. If stop_session() wrote
        # [INTERRUPTED] during our run, skip persisting to avoid contradictory records.
        async with get_async_session() as session:
            interrupted_check = await session.exec(
                select(func.count()).where(
                    AgentSessionMessage.agent_session_id == agent_session_id,
                    AgentSessionMessage.turn_number == turn_number,
                    col(AgentSessionMessage.role) == AgentSessionMessageRole.SYSTEM,
                )
            )
            if interrupted_check.one() > 0:
                logger.info(
                    "Turn was interrupted during agent run, skipping persist",
                    session_id=str(agent_session_id),
                    turn_number=turn_number,
                )
                # Delete the eagerly-persisted draft row — stop_session() already
                # wrote the authoritative SYSTEM message for this turn.
                if eager.msg_id is not None:
                    await session.exec(
                        sa_delete(AgentSessionMessage).where(
                            col(AgentSessionMessage.agent_session_message_id) == eager.msg_id
                        )
                    )
                # The agent ran to completion (a success result event was already
                # emitted before stop_session() won the write race). Transition the
                # session to COMPLETED so polling callers receive a terminal signal.
                await _mark_session_completed(session, agent_session_id)
                await session.commit()
                return

            # Sanitize LLM output before writing to DB — PostgreSQL VARCHAR columns
            # reject null bytes (\x00) and asyncpg raises UntranslatableCharacterError.
            safe_final_text: str = _scrub_null_bytes(final_text)
            safe_events: list[dict] = _scrub_null_bytes(events)

            # Compute TTFCT/TTLCT in milliseconds from the turn loop start anchor.
            # NULL when no assistant text was seen (tool-only turns, errors, etc.).
            _ttfct_ms = (
                (first_text_time_ns - turn_loop_start_ns) // 1_000_000 if first_text_time_ns is not None else None
            )
            _ttlct_ms = (last_text_time_ns - turn_loop_start_ns) // 1_000_000 if last_text_time_ns is not None else None

            if eager.msg_id is not None:
                # Update the eagerly-persisted row with final data
                await session.exec(
                    update(AgentSessionMessage)
                    .where(col(AgentSessionMessage.agent_session_message_id) == eager.msg_id)
                    .values(
                        content=safe_final_text,
                        raw_events=safe_events,
                        llm_message_id=result_llm_session_id,
                        cost_usd=Decimal(str(result_cost_usd)) if result_cost_usd is not None else None,
                        duration_ms=result_duration_ms,
                        num_agent_turns=result_num_turns,
                        completion_status=_msg_completion,
                        error_type=_msg_error_type,
                        ttfct_ms=_ttfct_ms,
                        ttlct_ms=_ttlct_ms,
                    )
                )
            else:
                # No eager row (e.g. empty response — no tool calls and no text)
                agent_msg = AgentSessionMessage(
                    agent_session_id=agent_session_id,
                    turn_number=turn_number,
                    role=AgentSessionMessageRole.AGENT,
                    content=safe_final_text,
                    raw_events=safe_events,
                    llm_name=model_name,
                    llm_message_id=result_llm_session_id,
                    cost_usd=Decimal(str(result_cost_usd)) if result_cost_usd is not None else None,
                    duration_ms=result_duration_ms,
                    num_agent_turns=result_num_turns,
                    completion_status=_msg_completion,
                    error_type=_msg_error_type,
                    ttfct_ms=_ttfct_ms,
                    ttlct_ms=_ttlct_ms,
                )
                session.add(agent_msg)

            # Update session's llm_session_id for --resume, and add any new worktrees
            session_to_update = await session.get(AgentSession, agent_session_id)
            if session_to_update:
                if result_llm_session_id:
                    session_to_update.llm_session_id = result_llm_session_id

                # Scan for worktrees created by MCP tools during this turn.
                # Add them as extra_dirs so the next turn gets --add-dir access.
                worktrees = scan_session_worktrees(str(agent_session_id))
                if worktrees:
                    existing = set(session_to_update.extra_dirs or [])
                    new_dirs = [w for w in worktrees if w not in existing]
                    if new_dirs:
                        session_to_update.extra_dirs = list(existing | set(new_dirs))
                        logger.info("Added worktree dirs", session_id=str(agent_session_id), new_dirs=new_dirs)

                # Mark session COMPLETED so polling callers get a terminal signal.
                # All trigger types (SLACK, CRON, API, TASK) transition to COMPLETED
                # after each turn. SLACK multi-turn sessions are re-activated to ACTIVE
                # in send_message() when the next user message arrives.
                await _mark_session_completed(session, agent_session_id)

            await session.commit()

        # Notify the user if the agent was stopped (max turns or context overflow).
        # Runners set subtype="error_max_turns" or "stopped_context_overflow".
        stop_notice: str | None = None
        if result_subtype == "stopped_context_overflow":
            stop_notice = CONTEXT_OVERFLOW_NOTICE
        elif result_subtype == "error_max_turns":
            stop_notice = TURN_LIMIT_NOTICE

        if stop_notice and gateway and gateway_session_id:
            try:
                # For Slack: send as a muted status context block (Slack-only grey
                # hint).  For other gateways send_status_update returns False and we
                # fall back to a regular reply so they still see the message.
                delivered = await gateway.send_status_update(gateway_session_id, stop_notice)
                if not delivered:
                    await gateway.send_reply(gateway_session_id, stop_notice, username=gateway_username)
            except Exception:
                logger.error(
                    "Failed to send stop notice to gateway",
                    session_id=str(agent_session_id),
                )

        # Auto-request feedback if enough turns have passed
        if gateway and gateway_session_id and agent_config.feedback_probability > 0:
            if turn_number >= agent_config.feedback_min_turns:
                if random.random() < agent_config.feedback_probability:
                    try:
                        await gateway.request_feedback(gateway_session_id)
                        logger.info(
                            "Auto-requested feedback survey",
                            session_id=str(agent_session_id),
                            turn_number=turn_number,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to auto-request feedback",
                            session_id=str(agent_session_id),
                            turn_number=turn_number,
                            exc_info=True,
                        )

        # Determine if the session completed successfully or hit an error condition
        # based on the runner's explicit result_subtype.
        task_success = result_subtype not in _TASK_FAILURE_SUBTYPES

        # Build a descriptive failure reason for logging and task result storage.
        task_failure_reason: str | None = None
        if result_subtype in _TASK_FAILURE_SUBTYPES:
            task_failure_reason = f"Session ended with {result_subtype}"

        logger.info(
            "Agent task completed",
            session_id=str(agent_session_id),
            turn_number=turn_number,
            cost_usd=result_cost_usd,
            duration_ms=result_duration_ms,
            response_length=len(final_text),
            result_subtype=result_subtype,
            task_success=task_success,
        )

        # Fire-and-forget: generate/refresh session title after turn 1 and 3.
        trigger_enum = AgentSessionTrigger(trigger) if trigger else None
        create_background_task(maybe_generate_session_title(agent_session_id, turn_number, trigger=trigger_enum))

        # On failure, include the failure reason in the result so it's not lost.
        # (update_task_completion stores result if non-empty, dropping error otherwise)
        task_result: dict[str, Any] = {"output": final_text}
        if task_failure_reason:
            task_result["error"] = task_failure_reason

        # Determine error_subtype for resumable failures.
        task_error_subtype: str | None = None
        if not task_success:
            task_error_subtype = result_subtype

        await _maybe_update_task_completion(
            trigger,
            session_context,
            agent_session_id,
            success=task_success,
            result=task_result,
            error=task_failure_reason,
            error_subtype=task_error_subtype,
        )

    finally:
        clear_session_sandbox(str(agent_session_id))
        clear_session_websearch_count(str(agent_session_id))

        # Stop the BCH manager for terminal trigger types (CRON / TASK / WEBHOOK /
        # API) where the session will not receive another message.  For interactive
        # SLACK sessions the manager stays alive; it is reaped by the idle timer
        # after AHS_BCH_IDLE_TIMEOUT_SECONDS of inactivity, or by stop_session().
        _is_interactive_session = (trigger or "").upper() == AgentSessionTrigger.SLACK.value
        if not _is_interactive_session:
            _bch_mgr = _command_handlers.pop(agent_session_id, None)
            if _bch_mgr is not None:
                set_command_handler_manager(str(agent_session_id), None)
                create_background_task(_bch_mgr.stop())

        # Cancel any pre-spawn task that wasn't consumed by the runner (e.g. the
        # session was stopped before _run_agent_task reached the runner creation
        # code, or the guard-check returned early).
        orphan_spawn = _pre_spawn_tasks.pop(agent_session_id, None)
        if orphan_spawn is not None:
            if orphan_spawn.done():
                # Task already completed — kill the spawned subprocess directly.
                try:
                    proc = orphan_spawn.result()
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
                except Exception:
                    pass
            else:
                orphan_spawn.cancel()
        # Note: clear_session_state is NOT called here because this runs after every turn,
        # and it would cancel any in-flight GitHub auth polling tasks. Session state cleanup
        # (including polling task cancellation) happens at explicit session end only.
        # GitHub tokens persist across sessions (keyed by user_id) and expire naturally.
        # Drain pending messages unless the user explicitly stopped the session.
        # On cancellation, stop_session() clears the pending queue itself.
        if not was_cancelled:
            await _drain_pending_messages(agent_session_id)
        # Only remove if the mapping still points to *this* task. A newer message
        # (or a drained turn) may have already replaced it with a different task.
        current = asyncio.current_task()
        if _active_tasks.get(agent_session_id) is current:
            del _active_tasks[agent_session_id]

        # Best-effort sync session workspace (attachments + history) to GCS.
        # Runs on every exit path (success, error, cancellation) so partial
        # history from failed/cancelled turns is preserved.
        # Placed after session-state cleanup (sandbox, websearch counters) so a
        # slow sync does not delay resets that the next turn depends on.
        try:
            await sync_session_to_gcs(str(agent_session_id))
        except BaseException:
            logger.warning(
                "GCS session sync failed after agent turn",
                session_id=str(agent_session_id),
                exc_info=True,
            )

        # Best-effort sync agent memory to GCS for all agents.
        if agent_config_name:
            try:
                await sync_agent_memory_to_gcs(agent_config_name)
            except Exception:
                logger.warning(
                    "GCS agent memory sync failed after agent turn",
                    agent_name=agent_config_name,
                    session_id=str(agent_session_id),
                    exc_info=True,
                )


async def create_session(request: SessionCreateRequest) -> SessionCreateResponse:
    """Create a new session or resume an existing one.

    If request.message is provided, also kicks off the first agent turn
    in the background.
    """
    # If session_id provided, try to resume.
    # Resume-without-message is read-only (returns session_id/status) so user_id
    # is not required.  Resume-with-message delegates to send_message() which
    # enforces user_id and sender==creator checks.
    if request.session_id:
        async with get_async_session() as session:
            existing = await _resolve_session(session, request.session_id)
        if existing:
            # DB session closed — send_message opens its own
            if request.message:
                _ctx = request.context or {}
                msg_request = SessionMessageRequest(
                    session_id=request.session_id,
                    message=request.message,
                    slack_user_id=_ctx.get("slack_user_id"),
                    user_id=_ctx.get("user_id") or _ctx.get("yupp_user_id") or request.user_id,
                    attachments=request.attachments,
                    source=request.source,
                )
                await send_message(msg_request)
                # If the session has an inflight turn, send_message queues the
                # message instead of rejecting — so no error handling needed.
            return SessionCreateResponse(
                session_id=str(existing.agent_session_id),
                status=existing.status.value,
            )

    # Map trigger string to enum (before DB session so we can branch on it).
    trigger_map = {
        "slack": AgentSessionTrigger.SLACK,
        "webhook": AgentSessionTrigger.WEBHOOK,  # not currently used; reserved for future integrations
        "cron": AgentSessionTrigger.CRON,
        "task": AgentSessionTrigger.TASK,  # Triggered by project task executor
        "api": AgentSessionTrigger.API,
    }
    trigger = trigger_map.get(request.trigger.lower(), AgentSessionTrigger.API)

    # TODO: If slack_channel_id is present but slack_channel_name is missing,
    # look up the channel name via Slack conversations.info API and cache the
    # mapping (channel_id → channel_name) so subsequent sessions reuse it.
    context = request.context or {}

    # Resolve user_id FIRST so it's available for personal agent resolution.
    # Priority: top-level request.user_id (preferred) → context fallbacks (backward compat).
    user_id = request.user_id or context.get("user_id") or context.get("yupp_user_id")
    if trigger == AgentSessionTrigger.SLACK:
        # Backward compat: resolve from slack_user_id if SAG didn't provide user_id.
        if not user_id:
            slack_user_id = context.get("slack_user_id")
            if slack_user_id:
                try:
                    user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id)
                except Exception:
                    logger.warning(
                        "Failed to resolve slack_user_id to user_id",
                        slack_user_id=slack_user_id,
                    )

    if not user_id:
        logger.error(
            "Rejected session create: missing user_id",
            source=request.source,
            agent_id=request.agent_id,
            trigger=request.trigger,
            request=request.model_dump(),
        )
        raise AHSValidationError("user_id is required to create a session")

    # Resolve personal agent variant (e.g., "yuppclaw" → "yuppclaw-alice")
    # before looking up the agent in the DB.
    resolved_agent_id = request.agent_id
    display_name: str | None = None
    if request.agent_id in PERSONAL_AGENT_PREFIXES and user_id:
        try:
            personal_name, personal_display = await _resolve_personal_agent_for_user(request.agent_id, user_id)
            if personal_name:
                resolved_agent_id = personal_name
                display_name = personal_display
        except Exception:
            logger.warning(
                "Failed to resolve personal agent, using base agent",
                base_agent_name=request.agent_id,
                user_id=user_id,
                exc_info=True,
            )

    async with get_async_session() as session:
        # Resolve agent (using personal variant if resolved, else base name)
        agent = await _resolve_agent(session, resolved_agent_id)
        if not agent:
            # Auto-register agent from config if found on disk
            agent_config = load_agent_config(resolved_agent_id)
            if not agent_config:
                raise ValueError(f"Agent not found: {resolved_agent_id}")
            agent = Agent(
                name=resolved_agent_id,
                display_name=agent_config.display_name or resolved_agent_id,
                description=agent_config.description,
            )
            session.add(agent)
            await session.flush()

        if "permissions" in context:
            # Permissions already set by an earlier create_session caller. Respect them.
            pass
        else:
            try:
                has_mcp_access = await has_permission_by_user_id_cached(user_id, SoulPermission.USE_MCP)
                if not has_mcp_access:
                    logger.info(
                        "User lacks USE_MCP permission, yuppster-mcp tools will be disabled",
                        user_id=user_id,
                    )
            except Exception as e:
                has_mcp_access = False
                logger.warning(
                    "Error checking USE_MCP permission, disabling yuppster-mcp",
                    user_id=user_id,
                    error=str(e),
                )
            _perms = SessionPermissions.full_access() if has_mcp_access else SessionPermissions.restricted()
            context["permissions"] = _perms.model_dump(mode="json")

        # Ensure user_id is always in context so it appears in the system prompt
        # and is available to MCP tools (e.g., create_agent) via session context.
        if user_id:
            context.setdefault("user_id", user_id)

        # Enrich context with user_name from users table if not already set.
        # This ensures the system prompt has the user's real name regardless of
        # how the session was created (Slack, TUI, API, etc.).
        if user_id and not context.get("user_name"):
            try:
                _user_name = await _resolve_user_name_from_db(user_id)
                if _user_name:
                    context["user_name"] = _user_name
            except Exception:
                logger.warning("Failed to resolve user_name for context", user_id=user_id)

        # Store display name for gateway calls (e.g., "Alice's yClaw").
        # The gateway uses this to override the bot's Slack display name.
        if display_name:
            context["display_name"] = display_name

        # Proactively fetch the Slack thread now that the request is validated
        # (agent resolved, user_id confirmed).  We are still inside the DB session,
        # so a strict timeout (5s) prevents Slack latency from starving the DB pool.
        # The content is injected into context so the system prompt can include
        # thread messages on turn 1 without an MCP round-trip (~1.2-3.6s saved).
        if trigger == AgentSessionTrigger.SLACK:
            _channel = context.get("slack_channel_id", "")
            _thread_ts = context.get("slack_thread_ts", "")
            if _channel and _thread_ts:
                try:
                    _prefetched_thread = await asyncio.wait_for(
                        fetch_slack_thread_content(_channel, _thread_ts),
                        timeout=5.0,
                    )
                except TimeoutError:
                    logger.warning(
                        "Slack thread prefetch timed out, skipping",
                        channel=_channel,
                        thread_ts=_thread_ts,
                    )
                    _prefetched_thread = None
                if _prefetched_thread is not None:
                    context["slack_thread_prefetched"] = _prefetched_thread

        # Log the final resolved MCP permissions for debugging.
        _resolved_perms = SessionPermissions.from_context(context)
        logger.info(
            "MCP access resolved",
            agent_id=resolved_agent_id,
            trigger=request.trigger,
            user_id=user_id,
            allowed_servers=_resolved_perms.allowed_servers,
            allowed_harness_tools=_resolved_perms.allowed_harness_tools,
            has_full_tool_access=_resolved_perms.has_full_tool_access,
        )

        # Build constructor kwargs for the new session.
        _agent_session_kwargs: dict = {}

        # Create session (need agent_session_id for workspace path)
        agent_session = AgentSession(
            **_agent_session_kwargs,
            agent_id=agent.agent_id,
            slack_session_id=request.session_id,
            trigger=trigger,
            context=context,
            workspace="",  # set below after we know the session_id
            status=AgentSessionStatus.ACTIVE,
            creator_user_id=user_id,
        )
        session.add(agent_session)
        await session.flush()  # assigns agent_session_id (or confirms pre-issued one)

        # Set up workspace from scratch.
        # Consolidated session workspace: everything the agent needs lives here.
        # Layout: .claude/, .mcp.json, repo symlinks, worktrees, history/, attachments/
        workspace = os.path.join(AHS_SESSIONS_DIR, str(agent_session.agent_session_id))
        os.makedirs(workspace, exist_ok=True)

        # Symlink .claude/ so Claude CLI detects this dir as the project root.
        # Uses yupp-mind's .claude/ which has settings.json and hooks.
        claude_link = os.path.join(workspace, ".claude")
        claude_target = os.path.join(AHS_REPOS_DIR, "yupp-mind", ".claude")
        try:
            os.symlink(claude_target, claude_link)
        except FileExistsError:
            pass

        # Symlink all repos from AHS_REPOS_DIR into the workspace so agents
        # can access them without a separate --add-dir flag.
        if os.path.isdir(AHS_REPOS_DIR):
            for repo_entry in os.listdir(AHS_REPOS_DIR):
                repo_src = os.path.join(AHS_REPOS_DIR, repo_entry)
                if not os.path.isdir(repo_src):
                    continue
                repo_link = os.path.join(workspace, repo_entry)
                try:
                    os.symlink(repo_src, repo_link)
                except FileExistsError:
                    pass

        # Create history/ subdir for session history persistence
        os.makedirs(os.path.join(workspace, "history"), exist_ok=True)

        # Symlink persistent memory directory into workspace for all agents.
        # This makes agent_memories/ available across sessions.
        memory_dir = os.path.join(AHS_MEMORIES_DIR, resolved_agent_id, "agent_memories")
        os.makedirs(memory_dir, exist_ok=True)
        memory_link = os.path.join(workspace, "agent_memories")
        try:
            os.symlink(memory_dir, memory_link)
        except FileExistsError:
            pass
        logger.info(
            "Symlinked agent memory directory",
            agent_name=resolved_agent_id,
            memory_dir=memory_dir,
            workspace=workspace,
        )

        # Create the BCH manager for this session.  The bwrap proxy process starts
        # lazily on the first tool call; creating the manager here is cheap (no I/O).
        # Stored in a local so we can stop it on commit failure (try/finally below).
        _bch_manager = CommandHandlerManager(workspace=workspace)

        # Start pre-spawning the subprocess so the bwrap+CLI cold start (~4.2s) overlaps
        # with the remaining DB writes (session.commit, send_message DB ops, background
        # task setup).  Prerequisites satisfied above: workspace dir, .claude symlink,
        # repo symlinks, and memory symlink all exist.
        #
        # Constraints:
        #   • ClaudeCodeRunner only — raw executor and Codex CLI don't use Claude CLI.
        #   • No attachments — send_message() prepends attachment paths to the prompt,
        #     so the pre-built args would diverge from the final prompt.
        _pre_spawn_task: asyncio.Task | None = None
        if request.message and not request.attachments:
            _spawn_cfg = load_agent_config(request.agent_id)
            if (
                _spawn_cfg
                and _spawn_cfg.executor_config.type != EXECUTOR_TYPE_RAW
                and _spawn_cfg.executor_config.model != HARNESS_CODEX_CLI
            ):
                _spawn_context = RunContext(
                    session_id=str(agent_session.agent_session_id),
                    workspace=workspace,
                    llm_session_id=None,  # new session — no --resume
                    extra_dirs=[],
                    slack_session_id=request.session_id,
                    is_slack=(trigger == AgentSessionTrigger.SLACK),
                    is_task=(trigger == AgentSessionTrigger.TASK),
                    session_context=context,
                )
                _pre_spawn_task = asyncio.create_task(
                    ClaudeCodeRunner(_spawn_cfg).pre_spawn(request.message, _spawn_context)
                )
                logger.info(
                    "Subprocess pre-spawn started",
                    session_id=str(agent_session.agent_session_id),
                    agent_name=request.agent_id,
                )

        agent_session.workspace = workspace
        try:
            await session.commit()
            await session.refresh(agent_session)
        except Exception:
            # Cancel the pre-spawn task to avoid an orphaned subprocess.
            if _pre_spawn_task is not None:
                _pre_spawn_task.cancel()
            # Stop the BCH manager — it hasn't started yet (lazy start), so
            # this is a no-op today but ensures no state leaks on future retries.
            await _bch_manager.stop()
            raise

        # Register the pre-spawn task now that the commit succeeded, so
        # _run_agent_task can find it by session ID on the first turn.
        if _pre_spawn_task is not None:
            _pre_spawn_tasks[agent_session.agent_session_id] = _pre_spawn_task

        # Register the BCH manager now that the session is committed.
        # workspace_tools.py tool handlers query this dict on every tool call;
        # registering here (after commit) ensures no phantom entries survive a
        # failed creation.
        _command_handlers[agent_session.agent_session_id] = _bch_manager
        set_command_handler_manager(str(agent_session.agent_session_id), _bch_manager)
        logger.info(
            "BCH manager registered for session",
            session_id=str(agent_session.agent_session_id),
        )

        _session_perms = SessionPermissions.from_context(context)
        logger.info(
            f"Created session {agent_session.agent_session_id} for agent '{resolved_agent_id}'",
            session_id=str(agent_session.agent_session_id),
            agent_name=resolved_agent_id,
            trigger=request.trigger,
            workspace=workspace,
            allowed_servers=_session_perms.allowed_servers,
            allowed_harness_tools=_session_perms.allowed_harness_tools,
        )

    # Best-effort: notify all channels that a new session was created.
    _sid = agent_session.agent_session_id
    _env = os.environ.get("ENVIRONMENT", "local")
    _lit_hosts = {
        "production": "https://agent-streamlit-server-production-451082535721.us-east4.run.app",
        "staging": "https://agent-streamlit-server-staging-451082535721.us-east4.run.app",
    }
    _lit_base = _lit_hosts.get(_env, "http://localhost:8501")
    _lit_url = f"{_lit_base}/agent_harness_console?session_id={_sid}"
    if _env == "production":
        _war_room_url = f"https://war-room.yuppster.ai/session/{_sid}"
        _links = f"(<{_war_room_url}|WR> | <{_lit_url}|Lit>)"
    else:
        _links = f"(<{_lit_url}|Lit>)"
    _env_suffix = f" — {_env}" if _env != "production" else ""
    session_notice = f"_Session {_sid} {_links}{_env_suffix}_"

    # WebSocket stream
    # TODO: this notice is effectively dropped for new sessions because WebSocket
    # clients can only subscribe after create_session() returns the UUID, and
    # InMemoryPubSub has no replay buffer. Low impact since clients already get
    # the session ID from the API response. Fix requires replay buffer or deferred
    # publish. (see PR #10756)
    try:
        pubsub = get_pubsub()
        stream_channel = f"ahs:stream:{agent_session.agent_session_id}"
        for evt in build_notice_events(session_notice):
            await pubsub.publish(stream_channel, json.dumps(evt))
    except RuntimeError:
        pass  # streaming may not be initialized (e.g., tests)

    # Gateway (Slack)
    gateway_name = TRIGGER_TO_GATEWAY.get(trigger.value)
    if gateway_name and request.session_id:
        agent_cfg = await _load_agent_config_with_db_fallback(resolved_agent_id)
        if agent_cfg:
            gw = GatewayRegistry.get_instance().get_for_session(gateway_name, agent_cfg)
            if gw:
                try:
                    await gw.send_reply(request.session_id, session_notice)
                except Exception:
                    logger.error(
                        "Failed to send session-created notice to gateway",
                        session_id=str(agent_session.agent_session_id),
                    )

    # If a message was provided, kick off the first turn.
    # Pass user IDs so send_message can re-check USE_MCP for the sender
    # (without these, it defaults to restricted and overrides permissions).
    if request.message:
        msg_request = SessionMessageRequest(
            session_id=str(agent_session.agent_session_id),
            message=request.message,
            slack_user_id=context.get("slack_user_id"),
            user_id=context.get("user_id") or context.get("yupp_user_id") or request.user_id,
            attachments=request.attachments,
            source=request.source,
        )
        await send_message(msg_request)

    return SessionCreateResponse(
        session_id=str(agent_session.agent_session_id),
        status=agent_session.status.value,
    )


async def send_message(request: SessionMessageRequest) -> SessionMessageResponse:
    """Send a message to an existing session.

    Stores the user message and kicks off the agent in the background.
    Returns immediately with the turn number.
    """
    # Resolve user_id for this message before entering the DB session block.
    # Different users can send messages to the same session (e.g., Slack threads),
    # so each message tracks its own creator.
    msg_creator_user_id = request.user_id
    if not msg_creator_user_id and request.slack_user_id:
        try:
            msg_creator_user_id = await resolve_slack_user_to_yupp_user_id(request.slack_user_id)
        except Exception:
            logger.warning(
                "Failed to resolve slack_user_id for message creator",
                slack_user_id=request.slack_user_id,
            )

    if not msg_creator_user_id:
        logger.error(
            "Rejected message: missing user_id",
            source=request.source,
            session_id=request.session_id,
            request=request.model_dump(),
        )
        raise AHSValidationError("user_id is required to send a message")

    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {request.session_id}")

        # For non-Slack sessions, enforce that the message sender matches the session creator.
        # Fail closed: also reject if the session has no creator_user_id (legacy sessions).
        is_slack = agent_session.trigger == AgentSessionTrigger.SLACK
        if not is_slack:
            if not agent_session.creator_user_id:
                logger.error(
                    "Rejected message: legacy session has no creator_user_id",
                    source=request.source,
                    session_id=request.session_id,
                    sender_user_id=msg_creator_user_id,
                )
                raise AHSValidationError(
                    "Cannot send messages to a legacy session without a creator. Please create a new session."
                )
            if msg_creator_user_id != agent_session.creator_user_id:
                logger.error(
                    "Rejected message: sender does not match session creator",
                    source=request.source,
                    session_id=request.session_id,
                    sender_user_id=msg_creator_user_id,
                    session_creator_user_id=agent_session.creator_user_id,
                    request=request.model_dump(),
                )
                raise AHSValidationError("Message sender must match session creator for non-Slack sessions")

        # Lock the session row to serialize concurrent message sends
        await session.exec(
            select(AgentSession)
            .where(AgentSession.agent_session_id == agent_session.agent_session_id)
            .with_for_update()
        )

        # Queue message if a prior turn is still processing instead of rejecting.
        # The message will be drained after the current turn completes.
        # Exception: STALE sessions have an orphaned inflight turn from a now-dead
        # process.  There is no active task to drain _pending_messages for STALE
        # sessions, so we must NOT queue — fall through and start a fresh turn.
        is_stale = agent_session.status == AgentSessionStatus.STALE
        if not is_stale and await _has_inflight_turn(session, agent_session.agent_session_id):
            queue = _pending_messages.setdefault(agent_session.agent_session_id, [])
            if len(queue) >= _MAX_PENDING_MESSAGES:
                raise RuntimeError(f"Pending message queue full ({_MAX_PENDING_MESSAGES}). Try again later.")
            queue.append(
                PendingMessage(
                    message=request.message,
                    slack_ts=request.slack_ts,
                    slack_user_id=request.slack_user_id,
                    user_id=request.user_id,
                    attachments=request.attachments or [],
                    source=request.source,
                )
            )
            logger.info(
                "Message queued (agent busy)",
                session_id=str(agent_session.agent_session_id),
                queue_depth=len(queue),
            )
            return SessionMessageResponse(
                session_id=str(agent_session.agent_session_id),
                turn_number=-1,  # not yet assigned; will be set when drained
                status="queued",
            )

        agent = await session.get(Agent, agent_session.agent_id)
        if not agent:
            raise ValueError("Agent not found for session")

        agent_config = await _load_agent_config_with_db_fallback(agent.name)
        if not agent_config:
            raise ValueError(f"Agent config not found: {agent.name}")

        # Re-activate session if it is in a terminal-but-resumable state.
        # All sessions are now marked COMPLETED after each turn completes, so multi-turn
        # sessions (SLACK) receive follow-up messages with status=COMPLETED between turns.
        # Sessions marked STALE by the startup scan (interrupted mid-turn by SIGTERM) can
        # also receive follow-up messages — re-activate them too so status-based monitoring
        # shows ACTIVE during processing instead of STALE.
        if agent_session.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
            agent_session.status = AgentSessionStatus.ACTIVE

        # Store user message (turn_number is safe under FOR UPDATE lock)
        turn_number = await _next_turn_number(session, agent_session.agent_session_id)
        user_msg = AgentSessionMessage(
            agent_session_id=agent_session.agent_session_id,
            turn_number=turn_number,
            role=AgentSessionMessageRole.USER,
            content=request.message,
            slack_ts=request.slack_ts,
            creator_user_id=msg_creator_user_id,
        )
        session.add(user_msg)
        await session.commit()

        excerpt = (request.message or "")[:200]
        sid = str(agent_session.agent_session_id)[-6:]
        logger.info(
            f"session {sid} [USER]: {excerpt}",
            agent_name=agent.name,
            session_id=str(agent_session.agent_session_id),
            role="USER",
            turn_number=turn_number,
        )

    # Re-check MCP permission for the message sender (may differ from session creator).
    # For Slack sessions, different users can send follow-up messages in the same thread.
    session_context = dict(agent_session.context) if agent_session.context else {}
    is_slack = agent_session.trigger == AgentSessionTrigger.SLACK
    is_task = agent_session.trigger == AgentSessionTrigger.TASK
    if is_slack:
        # Use user_id from request (resolved by SAG) or fall back to session context.
        sender_user_id = (
            request.user_id or session_context.get("user_id") or session_context.get("yupp_user_id")  # backward compat
        )
        # Backward compat: reuse the resolution from earlier in this function
        # rather than making another Slack API + DB call.
        if not sender_user_id and request.slack_user_id:
            sender_user_id = msg_creator_user_id

        has_mcp_access = False
        if sender_user_id:
            try:
                has_mcp_access = await has_permission_by_user_id_cached(sender_user_id, SoulPermission.USE_MCP)
            except Exception:
                logger.error(
                    "Error checking USE_MCP for follow-up sender, disabling MCP",
                    user_id=sender_user_id,
                    session_id=str(agent_session.agent_session_id),
                    exc_info=True,
                )
        else:
            logger.warning(
                "Slack message missing user_id, disabling yuppster-mcp",
                slack_user_id=request.slack_user_id,
                session_id=str(agent_session.agent_session_id),
            )
        _msg_perms = SessionPermissions.full_access() if has_mcp_access else SessionPermissions.restricted()
        session_context["permissions"] = _msg_perms.model_dump(mode="json")

    # Track the current message sender in memory so MCP tools (e.g., create_pr)
    # can attribute actions to the user who asked for them.
    if msg_creator_user_id:
        set_session_current_user(str(agent_session.agent_session_id), msg_creator_user_id)
        # Also propagate to session_context so the yuppster-mcp-server can attribute
        # resources to the current turn sender (not just the session creator).
        session_context["current_turn_user_id"] = msg_creator_user_id

    # Persist updated session_context to DB so subagents (via new_task) inherit
    # the sender's MCP access rather than the stale session creator's value.
    if is_slack:
        try:
            async with get_async_session() as session:
                result = await session.exec(
                    select(AgentSession).where(AgentSession.agent_session_id == agent_session.agent_session_id)
                )
                db_session = result.one_or_none()
                if db_session:
                    db_session.context = session_context
                    await session.commit()
        except Exception:
            logger.error(
                "Failed to persist updated session_context",
                session_id=str(agent_session.agent_session_id),
                exc_info=True,
            )

    # Download attachments from GCS into the workspace and prepend paths to the
    # message so the agent knows which files are available via the `read` tool.
    agent_message = request.message
    if request.attachments and agent_session.workspace:
        attachment_paths = await _download_attachments_to_workspace(request.attachments, agent_session.workspace)
        agent_message = _prepend_attachment_paths(agent_message, attachment_paths)

        # Fire-and-forget: persist downloaded attachments to GCS immediately
        # so they survive pod restarts before the agent turn completes.
        async def _sync_attachments() -> None:
            try:
                await sync_session_to_gcs(str(agent_session.agent_session_id))
            except Exception:
                logger.warning(
                    "GCS session sync failed after attachment download",
                    session_id=str(agent_session.agent_session_id),
                    exc_info=True,
                )

        create_background_task(_sync_attachments())

    # Register a placeholder *before* creating the task so that stop_session()
    # cannot land in a gap where the inflight turn exists but no task is tracked.
    # A sentinel value (None) tells stop_session the task is being set up.
    _active_tasks[agent_session.agent_session_id] = None  # type: ignore[assignment]

    # Fire-and-forget: run agent in background.
    task = create_background_task(
        _run_agent_task(
            agent_session_id=agent_session.agent_session_id,
            turn_number=turn_number,
            message=agent_message,
            agent_config_name=agent.name,
            workspace=agent_session.workspace,
            llm_session_id=agent_session.llm_session_id,
            extra_dirs=agent_session.extra_dirs or [],
            slack_session_id=agent_session.slack_session_id,
            is_slack=is_slack,
            is_task=is_task,
            session_context=session_context,
            trigger=agent_session.trigger.value if agent_session.trigger else None,
            # Only pass session_created_at on turn 1: queue_wait_ms measures the
            # gap between session creation and CLI launch, which is only meaningful
            # for the first turn.  On subsequent turns, created_at is the session
            # age (minutes/hours), not queue delay, so we omit it to avoid skewing
            # latency metrics and alerting baselines.
            session_created_at=agent_session.created_at if turn_number == 1 else None,
        )
    )
    _active_tasks[agent_session.agent_session_id] = task

    return SessionMessageResponse(
        session_id=str(agent_session.agent_session_id),
        turn_number=turn_number,
        status="processing",
    )


async def attach_slack_to_session(request: SessionAttachSlackRequest) -> SessionAttachSlackResponse:
    """Attach Slack thread context to an existing headless AHS session.

    Called by SAG when a human replies to an agent-initiated thread.
    Updates the session's slack_session_id and merges Slack context so that
    callbacks (add_reply, etc.) route back to the correct Slack thread.
    """
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if agent_session is None:
            raise ValueError(f"Session not found: {request.session_id}")

        agent_session.slack_session_id = request.slack_session_id

        # Merge Slack context into the session's existing context.
        # Copy the dict so SQLAlchemy detects the change on JSONB columns.
        if request.context:
            agent_session.context = {**(agent_session.context or {}), **request.context}

        session.add(agent_session)
        await session.commit()

        logger.info(
            "Attached Slack context to session",
            session_id=str(agent_session.agent_session_id),
            slack_session_id=request.slack_session_id,
        )

    return SessionAttachSlackResponse(
        session_id=str(agent_session.agent_session_id),
        status="attached",
    )


async def stop_session(session_id: str) -> SessionStopResponse:
    """Stop a running agent task for a session.

    Cancels the background task (which kills the CLI subprocess and any subagent
    processes), records a SYSTEM message noting the interruption, and marks
    the turn as complete so the session can accept new messages.

    Idempotent: stopping an already-idle session is a no-op.
    """
    # Phase 1: Check for inflight turn under lock, capture the turn number,
    # and snapshot the task reference atomically.
    agent_session_id: uuid.UUID | None = None
    inflight_turn: int | None = None
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        # Lock the session row to serialize with concurrent sends/stops
        await session.exec(
            select(AgentSession)
            .where(AgentSession.agent_session_id == agent_session.agent_session_id)
            .with_for_update()
        )

        # Always clear pending messages when the user explicitly stops — even if the
        # current turn already finished, the finally-block drain hasn't run yet and
        # would otherwise process them.
        dropped = _pending_messages.pop(agent_session.agent_session_id, [])
        if dropped:
            logger.info(
                "Discarded pending messages due to session stop",
                session_id=str(agent_session.agent_session_id),
                count=len(dropped),
            )

        # Check if there's an inflight turn to stop
        if not await _has_inflight_turn(session, agent_session.agent_session_id):
            return SessionStopResponse(
                session_id=str(agent_session.agent_session_id),
                status="no_inflight_turn",
            )

        # Capture the inflight turn number so Phase 3 writes [INTERRUPTED]
        # for this specific turn, not a newer one that might start later.
        latest_user_turn_result = await session.exec(
            select(func.max(AgentSessionMessage.turn_number)).where(
                AgentSessionMessage.agent_session_id == agent_session.agent_session_id,
                col(AgentSessionMessage.role) == AgentSessionMessageRole.USER,
            )
        )
        inflight_turn = latest_user_turn_result.one() or 0

        agent_session_id = agent_session.agent_session_id

        # Snapshot the task while still under lock so a concurrent send_message
        # can't swap in a newer task between our check and our cancel.
        task = _active_tasks.get(agent_session_id)
    # Lock released — safe to wait on the task without risking deadlock.

    # Phase 2: Cancel subagent tasks and the main task outside the DB lock.
    try:
        await cancel_subagent_tasks(agent_session_id)
    except Exception:
        logger.error(
            "Failed to cancel subagent tasks, proceeding with main task cancellation",
            session_id=str(agent_session_id),
            exc_info=True,
        )

    if task and not task.done():
        task.cancel()
        # Give the task a moment to clean up (proc.kill + await proc.wait)
        try:
            async with asyncio.timeout(5.0):
                await asyncio.shield(task)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass

    # Phase 3: Re-open a session to write the SYSTEM message for the
    # specific turn we observed in Phase 1. Re-check that this turn is
    # still inflight — if the task completed between Phase 1 and now,
    # it already wrote an AGENT/SYSTEM message and we should not duplicate.
    async with get_async_session() as session:
        # Check for a completed response: any non-USER message that is not
        # IN_PROGRESS.  Eager-persist drafts (IN_PROGRESS) are excluded so we
        # don't mistake an actively-running turn for one that finished naturally.
        response_count_result = await session.exec(
            select(func.count()).where(
                AgentSessionMessage.agent_session_id == agent_session_id,
                AgentSessionMessage.turn_number == inflight_turn,
                col(AgentSessionMessage.role) != AgentSessionMessageRole.USER,
                col(AgentSessionMessage.completion_status) != AgentSessionMessageCompletionStatus.IN_PROGRESS,
            )
        )
        if response_count_result.one() > 0:
            # Turn already completed naturally — no need to write [INTERRUPTED]
            logger.info(
                "Stop requested but turn already completed",
                session_id=str(agent_session_id),
                turn_number=inflight_turn,
            )
            return SessionStopResponse(
                session_id=str(agent_session_id),
                status="no_inflight_turn",
            )

        # Record a SYSTEM message to mark the turn as complete
        system_msg = AgentSessionMessage(
            agent_session_id=agent_session_id,
            turn_number=inflight_turn,
            role=AgentSessionMessageRole.SYSTEM,
            content="[INTERRUPTED] Session stopped by user.",
            completion_status=AgentSessionMessageCompletionStatus.ABORTED,
            error_type=AgentSessionMessageErrorType.NONE,
        )
        session.add(system_msg)
        await session.commit()

    # Clean up session-specific state (auth terminal states, current user tracking,
    # polling tasks). Safe to call here since this is explicit session end.
    clear_session_state(str(agent_session_id))

    # Stop and deregister the BCH manager for this session.
    _bch_mgr = _command_handlers.pop(agent_session_id, None)
    if _bch_mgr is not None:
        set_command_handler_manager(str(agent_session_id), None)
        await _bch_mgr.stop()

    logger.info(
        "Session stopped by user",
        session_id=str(agent_session_id),
        turn_number=inflight_turn,
        had_task=task is not None,
    )

    return SessionStopResponse(
        session_id=str(agent_session_id),
        status="stopped",
    )


async def send_feedback(request: SessionFeedbackRequest) -> FeedbackResponse:
    """Record feedback on a session or message."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {request.session_id}")

        message_uuid = None
        if request.message_id:
            try:
                message_uuid = uuid.UUID(request.message_id)
            except ValueError:
                raise ValueError(f"Invalid message_id: {request.message_id}") from None
            # Verify the message exists and belongs to this session
            msg_result = await session.exec(
                select(AgentSessionMessage).where(
                    AgentSessionMessage.agent_session_message_id == message_uuid,
                    AgentSessionMessage.agent_session_id == agent_session.agent_session_id,
                )
            )
            if not msg_result.one_or_none():
                raise ValueError(f"Message {request.message_id} not found in session {request.session_id}")

        # Convert rating string to enum
        rating_enum: AgentFeedbackRating | None = None
        if request.rating:
            try:
                rating_enum = AgentFeedbackRating(request.rating.upper())
            except ValueError:
                raise ValueError(f"Invalid rating: {request.rating}. Must be POSITIVE or NEGATIVE.") from None

        user_id = request.user_id or "SYSTEM"

        feedback = AgentFeedback(
            agent_session_id=agent_session.agent_session_id,
            agent_session_message_id=message_uuid,
            user_id=user_id,
            rating=rating_enum,
            structured=request.structured,
            comment=request.comment,
            slack_ts=request.slack_ts,
        )
        session.add(feedback)
        await session.commit()

        logger.info(
            "Recorded feedback",
            session_id=str(agent_session.agent_session_id),
            message_id=request.message_id,
            rating=request.rating,
        )

    return FeedbackResponse(status="recorded")


_TOOL_OUTPUT_MAX_CHARS = 500


def _truncate_tool_output(output: Any) -> str | None:
    """Truncate tool output to a safe length for API responses."""
    if output is None:
        return None
    s = str(output)
    if len(s) <= _TOOL_OUTPUT_MAX_CHARS:
        return s
    return s[:_TOOL_OUTPUT_MAX_CHARS] + f"... ({len(s)} chars)"


def _message_content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized message content blocks from a raw event."""
    message = event.get("message", {})
    if not isinstance(message, dict):
        return []
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _iter_tool_use_blocks(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract tool_use blocks in the same order the WS translator consumes them."""
    tool_uses: list[dict[str, Any]] = []

    for event in raw_events:
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_uses.append(event)
            continue
        if event_type != "assistant":
            continue

        tool_uses.extend(block for block in _message_content_blocks(event) if block.get("type") == "tool_use")

    return tool_uses


def _iter_tool_result_blocks(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract tool_result blocks from standalone and embedded user events."""
    tool_results: list[dict[str, Any]] = []

    for event in raw_events:
        event_type = event.get("type")
        if event_type == "tool_result":
            tool_results.append(event)
            continue
        if event_type != "user":
            continue

        tool_results.extend(block for block in _message_content_blocks(event) if block.get("type") == "tool_result")

    return tool_results


def _extract_tool_use_id(payload: dict[str, Any]) -> str:
    """Normalize the tool call identifier across runner-specific event shapes."""
    tool_use_id = payload.get("tool_use_id") or payload.get("id") or ""
    return str(tool_use_id)


def _extract_tool_name(payload: dict[str, Any]) -> str:
    """Normalize the tool name across runner-specific event shapes."""
    name = payload.get("name") or payload.get("tool_name") or ""
    return str(name)


def _extract_tool_input(payload: dict[str, Any]) -> Any | None:
    """Normalize the tool input across runner-specific event shapes."""
    if "input" in payload:
        return payload.get("input")
    if "tool_input" in payload:
        return payload.get("tool_input")
    return None


def _extract_tool_output(payload: dict[str, Any]) -> str | None:
    """Normalize tool outputs to the same textual shape WS clients receive."""
    output = payload.get("output", payload.get("content"))
    if isinstance(output, list):
        output = " ".join(
            str(block.get("text", "")) for block in output if isinstance(block, dict) and block.get("text") is not None
        )
    return _truncate_tool_output(output)


def _extract_tool_uses(raw_events: list[dict[str, Any]] | None) -> list[ToolUseItem] | None:
    """Extract tool use/result pairs from raw_events into structured ToolUseItems.

    Supports the same tool-call shapes that WebSocket streaming translates:
    standalone tool_use/tool_result events plus assistant/user content blocks.
    Returns None if no tool uses found (keeps response compact for non-tool messages).
    """
    if not raw_events:
        return None

    results_by_id: dict[str, dict[str, Any]] = {}
    tool_uses: list[ToolUseItem] = []

    for event in _iter_tool_result_blocks(raw_events):
        tool_use_id = _extract_tool_use_id(event)
        if tool_use_id:
            results_by_id[tool_use_id] = event

    for event in _iter_tool_use_blocks(raw_events):
        tool_use_id = _extract_tool_use_id(event)
        result_event = results_by_id.get(tool_use_id, {})
        tool_uses.append(
            ToolUseItem(
                tool_use_id=tool_use_id,
                name=_extract_tool_name(event),
                input=_extract_tool_input(event),
                output=_extract_tool_output(result_event),
                is_error=bool(result_event.get("is_error", False)),
                duration_ms=result_event.get("duration_ms"),
                step=event.get("step", result_event.get("step")),
            )
        )

    return tool_uses or None


async def get_session_history(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
) -> SessionHistoryResponse:
    """Get message history for a session with pagination."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        agent = await session.get(Agent, agent_session.agent_id)

        # Total count
        count_result = await session.exec(
            select(func.count())
            .select_from(AgentSessionMessage)
            .where(AgentSessionMessage.agent_session_id == agent_session.agent_session_id)
        )
        total = count_result.one()

        # Paginated messages
        result = await session.exec(
            select(AgentSessionMessage)
            .where(AgentSessionMessage.agent_session_id == agent_session.agent_session_id)
            .order_by(AgentSessionMessage.turn_number, AgentSessionMessage.created_at)  # type: ignore[arg-type]
            .offset(offset)
            .limit(limit)
        )
        messages = result.all()

        return SessionHistoryResponse(
            session_id=str(agent_session.agent_session_id),
            agent_id=agent.name if agent else str(agent_session.agent_id),
            status=agent_session.status.value,
            messages=[
                MessageHistoryItem(
                    message_id=str(msg.agent_session_message_id),
                    turn_number=msg.turn_number,
                    role=msg.role.value,
                    content=msg.content,
                    tool_uses=_extract_tool_uses(msg.raw_events),
                    cost_usd=msg.cost_usd,
                    duration_ms=msg.duration_ms,
                    num_agent_turns=msg.num_agent_turns,
                    slack_ts=msg.slack_ts,
                    created_at=msg.created_at,
                )
                for msg in messages
            ],
            total_messages=total,
            limit=limit,
            offset=offset,
        )


def _build_agent_info(cfg: AgentConfig) -> AgentInfo:
    """Build an AgentInfo from an AgentConfig."""
    return AgentInfo(
        name=cfg.name,
        display_name=cfg.display_name,
        description=cfg.description,
        executor_type=cfg.executor_config.type,
        executor_model=cfg.executor_config.model,
        llm_model=cfg.llm_model,
        tool_permissions={k: str(v) for k, v in cfg.tool_permissions.items()},
        allowed_subagents=cfg.allowed_subagents,
        default_repo=cfg.default_repo,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        timeout_s=cfg.timeout_s,
        sandbox_enabled=cfg.sandbox.enabled,
        allowed_gateways=cfg.allowed_gateways,
    )


def _build_agent_info_from_db(agent: Agent) -> AgentInfo:
    """Build an AgentInfo from a DB Agent row."""
    cfg = agent.config or {}
    executor_cfg = cfg.get("executor_config", {})
    sandbox_cfg = cfg.get("sandbox", {})
    return AgentInfo(
        name=agent.name,
        display_name=agent.display_name,
        description=agent.description,
        executor_type=executor_cfg.get("type", agent.executor_type or "harnessed"),
        executor_model=executor_cfg.get("model", agent.executor_model),
        llm_model=executor_cfg.get("model") if executor_cfg.get("type") == "raw" else None,
        tool_permissions=cfg.get("tool_permissions", {"*": "allow"}),
        allowed_subagents=cfg.get("allowed_subagents", []),
        default_repo=cfg.get("default_repo", "yupp-mind"),
        max_turns=cfg.get("max_turns", 20),
        max_budget_usd=cfg.get("max_budget_usd", 2.0),
        timeout_s=cfg.get("timeout_s", 300),
        sandbox_enabled=sandbox_cfg.get("enabled", True),
        allowed_gateways=cfg.get("allowed_gateways", ["*"]),
        creator_user_id=agent.creator_user_id,
    )


async def list_agents(
    *,
    user_id: str | None = None,
    include_all: bool = False,
) -> AgentListResponse:
    """List agents with optional user filtering.

    When include_all=False (default), only returns agents created by user_id (DB agents).
    When include_all=True, returns all filesystem agents + all DB agents.
    """
    result_agents: list[AgentInfo] = []

    if include_all:
        # Include all filesystem agents
        fs_agents = discover_agents()
        result_agents.extend(_build_agent_info(cfg) for cfg in fs_agents.values())

    # Query DB agents with user_id filter when not including all.
    # When include_all=False and user_id is missing, return no DB agents
    # to avoid leaking cross-user agent metadata.
    async with get_async_session() as session:
        query = select(Agent)
        if not include_all:
            if not user_id:
                return AgentListResponse(agents=result_agents)
            query = query.where(Agent.creator_user_id == user_id)
        db_result = await session.exec(query)
        db_agents = db_result.all()

        # Track names already added from filesystem to avoid duplicates
        seen_names = {a.name for a in result_agents}
        for agent in db_agents:
            if agent.name in seen_names:
                continue
            seen_names.add(agent.name)
            result_agents.append(_build_agent_info_from_db(agent))

    if user_id:
        for agent_info in result_agents:
            agent_info.is_owner = agent_info.creator_user_id == user_id

    return AgentListResponse(agents=result_agents)


async def get_agent_detail(
    agent_name: str,
    include_system_prompts: bool = False,
) -> AgentDetailResponse:
    """Get a single agent's config, optionally including system prompt files."""
    cfg = await _load_agent_config_with_db_fallback(agent_name)
    if not cfg:
        raise ValueError(f"Agent not found: {agent_name}")

    prompts: dict[str, str] | None = None
    if include_system_prompts:
        from ypl.agent_harness_service.common.config import read_file_if_exists
        from ypl.agent_harness_service.common.constants import AHS_SHARED_DIR

        prompts = {}
        # Shared files: SOUL.md first, then rest alphabetically
        import glob as glob_mod

        soul_path = os.path.join(AHS_SHARED_DIR, "SOUL.md")
        soul_content = read_file_if_exists(soul_path)
        if soul_content:
            prompts["shared/SOUL.md"] = soul_content

        for md_path in sorted(glob_mod.glob(os.path.join(AHS_SHARED_DIR, "*.md"))):
            if md_path == soul_path:
                continue
            content = read_file_if_exists(md_path)
            if content:
                prompts[f"shared/{os.path.basename(md_path)}"] = content

        # Agent-specific files (with legacy core/ fallback)
        role_content = read_file_if_exists(os.path.join(AHS_AGENTS_DIR, agent_name, "ROLE.md"))
        if role_content:
            prompts[f"{agent_name}/ROLE.md"] = role_content
        else:
            for fname in ["ROLE.md", "SOUL.md"]:
                content = read_file_if_exists(os.path.join(AHS_AGENTS_DIR, agent_name, "core", fname))
                if content:
                    prompts[f"{agent_name}/{fname}"] = content

    additional_system_prompt = cfg.additional_system_prompt if include_system_prompts else None
    return AgentDetailResponse(
        agent=_build_agent_info(cfg),
        system_prompts=prompts,
        additional_system_prompt=additional_system_prompt,
    )


def _build_session_info(
    row: AgentSession,
    agent_name: str,
    message_count: int = 0,
) -> SessionInfo:
    """Build a SessionInfo from a DB row."""
    ctx = row.context or {}
    perms = None
    has_full = None
    if "permissions" in ctx:
        sp = SessionPermissions.from_context(ctx)
        perms = {
            "allowed_servers": ",".join(sp.allowed_servers),
            "allowed_harness_tools": ",".join(sp.allowed_harness_tools),
        }
        has_full = sp.has_full_tool_access

    return SessionInfo(
        session_id=str(row.agent_session_id),
        agent_name=agent_name,
        status=row.status.value,
        trigger=row.trigger.value,
        model=row.model,
        created_at=row.created_at,
        slack_channel_name=ctx.get("slack_channel_name"),
        slack_user_id=ctx.get("slack_user_id"),
        tool_permissions=perms,
        has_full_tool_access=has_full,
        parent_session_id=str(row.parent_session_id) if row.parent_session_id else None,
        title=row.title,
        message_count=message_count,
    )


async def list_sessions(
    *,
    status: str | None = None,
    agent_name: str | None = None,
    trigger: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    root_sessions_only: bool = True,
    user_id: str | None = None,
    include_all: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> SessionListResponse:
    """List sessions with optional filters and pagination.

    When include_all=False (default), only returns sessions created by user_id.
    When include_all=True, returns all sessions regardless of creator.
    """
    async with get_async_session() as session:
        # Build base query
        query = select(AgentSession).join(Agent, AgentSession.agent_id == Agent.agent_id)  # type: ignore[arg-type]

        if not include_all:
            if not user_id:
                return SessionListResponse(sessions=[], total=0, limit=limit, offset=offset)
            query = query.where(AgentSession.creator_user_id == user_id)
        if root_sessions_only:
            query = query.where(AgentSession.parent_session_id.is_(None))  # type: ignore[union-attr]
        if status:
            try:
                query = query.where(AgentSession.status == AgentSessionStatus(status.upper()))
            except ValueError:
                raise ValueError(
                    f"Invalid status filter: {status!r}. Valid values: {[s.value for s in AgentSessionStatus]}"
                ) from None
        if agent_name:
            query = query.where(Agent.name == agent_name)
        if trigger:
            try:
                query = query.where(AgentSession.trigger == AgentSessionTrigger(trigger.upper()))
            except ValueError:
                raise ValueError(
                    f"Invalid trigger filter: {trigger!r}. Valid values: {[t.value for t in AgentSessionTrigger]}"
                ) from None
        if since:
            query = query.where(col(AgentSession.created_at) >= since)
        if until:
            query = query.where(col(AgentSession.created_at) <= until)

        # Total count (same filters, no pagination)
        count_q = select(func.count()).select_from(query.subquery())
        count_result = await session.exec(count_q)
        total = count_result.one()

        # Paginated results
        query = query.order_by(col(AgentSession.created_at).desc()).offset(offset).limit(limit)
        result = await session.exec(query)
        rows = result.all()

        # Message counts: batch query for all session IDs in this page
        session_ids = [r.agent_session_id for r in rows]
        msg_counts: dict[uuid.UUID, int] = {}
        if session_ids:
            mc_q = (
                select(AgentSessionMessage.agent_session_id, func.count())
                .where(col(AgentSessionMessage.agent_session_id).in_(session_ids))
                .group_by(col(AgentSessionMessage.agent_session_id))
            )
            mc_result = await session.exec(mc_q)
            msg_counts = dict(mc_result.all())

        # Resolve agent names via batch query
        agent_ids = list({r.agent_id for r in rows})
        agent_map: dict[uuid.UUID, str] = {}
        if agent_ids:
            agent_result = await session.exec(select(Agent).where(col(Agent.agent_id).in_(agent_ids)))
            agent_map = {a.agent_id: a.name for a in agent_result.all()}

        items = [
            _build_session_info(
                r,
                agent_name=agent_map.get(r.agent_id, str(r.agent_id)),
                message_count=msg_counts.get(r.agent_session_id, 0),
            )
            for r in rows
        ]

        return SessionListResponse(sessions=items, total=total, limit=limit, offset=offset)


async def get_session_detail(session_id: str) -> SessionDetailResponse:
    """Get a single session with a flat list of all descendant subsessions."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        # Collect all descendant subsessions via iterative BFS
        from collections import deque

        max_depth = 20
        all_rows: list[AgentSession] = [agent_session]
        queue: deque[tuple[uuid.UUID, int]] = deque([(agent_session.agent_session_id, 0)])
        seen: set[uuid.UUID] = {agent_session.agent_session_id}
        while queue:
            parent_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            result = await session.exec(select(AgentSession).where(AgentSession.parent_session_id == parent_id))
            children = result.all()
            for child in children:
                if child.agent_session_id not in seen:
                    seen.add(child.agent_session_id)
                    all_rows.append(child)
                    queue.append((child.agent_session_id, depth + 1))

        # Batch-resolve agent names
        agent_ids = list({r.agent_id for r in all_rows})
        agent_map: dict[uuid.UUID, str] = {}
        if agent_ids:
            agent_result = await session.exec(select(Agent).where(col(Agent.agent_id).in_(agent_ids)))
            agent_map = {a.agent_id: a.name for a in agent_result.all()}

        # Batch-resolve message counts
        all_session_ids = [r.agent_session_id for r in all_rows]
        msg_counts: dict[uuid.UUID, int] = {}
        if all_session_ids:
            mc_q = (
                select(AgentSessionMessage.agent_session_id, func.count())
                .where(col(AgentSessionMessage.agent_session_id).in_(all_session_ids))
                .group_by(col(AgentSessionMessage.agent_session_id))
            )
            mc_result = await session.exec(mc_q)
            msg_counts = dict(mc_result.all())

        # Build SessionInfo list
        infos = [
            _build_session_info(
                r,
                agent_name=agent_map.get(r.agent_id, str(r.agent_id)),
                message_count=msg_counts.get(r.agent_session_id, 0),
            )
            for r in all_rows
        ]

        return SessionDetailResponse(
            session=infos[0],
            subsessions=infos[1:],
        )


async def create_agent(request: AgentCreateRequest) -> AgentCreateResponse:
    """Create a new agent: register in DB with config stored as JSONB."""
    validate_agent_name(request.name)

    # Build config payload (stored in the Agent.config JSONB column)
    config_data: dict[str, Any] = {
        "default_repo": request.default_repo,
        "max_turns": request.max_turns,
        "max_budget_usd": request.max_budget_usd,
        "feedback_probability": request.feedback_probability,
        "feedback_min_turns": request.feedback_min_turns,
        "timeout_s": request.timeout_s,
        "executor_config": {
            "type": request.executor_config.type,
            "model": request.executor_config.model,
        },
        "tool_permissions": request.tool_permissions,
        "allowed_subagents": request.allowed_subagents,
        "allowed_gateways": request.allowed_gateways,
        "sandbox": {
            "enabled": request.sandbox.enabled,
            "autoAllowBashIfSandboxed": request.sandbox.auto_allow_bash_if_sandboxed,
            "bwrapEnabled": request.sandbox.bwrap_enabled,
        },
    }

    if request.role_md:
        config_data["role_md"] = request.role_md
    if request.soul_md:
        config_data["soul_md"] = request.soul_md

    async with get_async_session() as session:
        existing = await _resolve_agent(session, request.name)
        if existing:
            raise ValueError(f"Agent already exists: {request.name}")

        agent = Agent(
            name=request.name,
            display_name=request.display_name or request.name,
            description=request.description,
            executor_type=request.executor_config.type.upper(),
            executor_model=request.executor_config.model,
            config=config_data,
            creator_user_id=request.user_id,
            additional_system_prompt=request.additional_system_prompt,
        )
        session.add(agent)
        await session.commit()

    logger.info("Created agent", name=request.name, user_id=request.user_id)
    return AgentCreateResponse(name=request.name, status="created")


async def edit_agent(request: AgentEditRequest) -> AgentEditResponse:
    """Edit an existing agent's configuration in DB."""
    async with get_async_session() as session:
        agent = await _resolve_agent(session, request.name)
        if not agent:
            raise ValueError(f"Agent not found: {request.name}")

        # Ownership check: only the creator can edit their agent.
        # Agents without a creator_user_id are filesystem/global agents — not editable via API.
        if not agent.creator_user_id:
            raise PermissionError(f"Agent '{request.name}' is a global agent and cannot be edited via API")
        if agent.creator_user_id != request.user_id:
            raise PermissionError(f"Only the agent creator can edit agent '{request.name}'")

        # Update top-level fields
        # TODO: `is not None` pattern means fields can't be cleared back to None once set.
        # Add a `fields_to_clear` list or sentinel value if clearing becomes a requirement.
        if request.display_name is not None:
            agent.display_name = request.display_name
        if request.description is not None:
            agent.description = request.description
        if request.executor_config is not None:
            agent.executor_type = AgentExecutorType(request.executor_config.type.upper())
            agent.executor_model = request.executor_config.model
        if request.additional_system_prompt is not None:
            agent.additional_system_prompt = request.additional_system_prompt

        # Update config JSONB — merge provided fields into existing config
        config_data = dict(agent.config) if agent.config else {}
        if request.executor_config is not None:
            config_data["executor_config"] = {
                "type": request.executor_config.type,
                "model": request.executor_config.model,
            }
        if request.tool_permissions is not None:
            config_data["tool_permissions"] = request.tool_permissions
        if request.allowed_subagents is not None:
            config_data["allowed_subagents"] = request.allowed_subagents
        if request.default_repo is not None:
            config_data["default_repo"] = request.default_repo
        if request.sandbox is not None:
            config_data["sandbox"] = {
                "enabled": request.sandbox.enabled,
                "autoAllowBashIfSandboxed": request.sandbox.auto_allow_bash_if_sandboxed,
                "bwrapEnabled": request.sandbox.bwrap_enabled,
            }
        if request.max_turns is not None:
            config_data["max_turns"] = request.max_turns
        if request.max_budget_usd is not None:
            config_data["max_budget_usd"] = request.max_budget_usd
        if request.timeout_s is not None:
            config_data["timeout_s"] = request.timeout_s
        if request.allowed_gateways is not None:
            config_data["allowed_gateways"] = request.allowed_gateways
        if request.role_md is not None:
            config_data["role_md"] = request.role_md
        if request.soul_md is not None:
            config_data["soul_md"] = request.soul_md

        agent.config = config_data
        session.add(agent)
        await session.commit()

    logger.info("Edited agent", name=request.name, user_id=request.user_id)
    return AgentEditResponse(name=request.name, status="updated")
