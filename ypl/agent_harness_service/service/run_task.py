"""Agent turn runner — _run_agent_task and persistence helpers."""

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.common.constants import (
    CONTEXT_OVERFLOW_NOTICE,
    EXECUTOR_TYPE_RAW,
    HARNESS_CODEX_CLI,
    TURN_LIMIT_NOTICE,
)
from ypl.agent_harness_service.core.memory_persistence import sync_agent_memory_to_gcs
from ypl.agent_harness_service.core.session_persistence import sync_session_to_gcs
from ypl.agent_harness_service.core.session_title import maybe_generate_session_title
from ypl.agent_harness_service.core.streaming import (
    TranslationState,
    _close_message_item,
    get_pubsub,
    translate_stream_event,
)
from ypl.agent_harness_service.executors.codex_app_server_runner import CodexAppServerRunner
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
from ypl.agent_harness_service.service.message_helpers import (
    _extract_visible_content,
    _scrub_null_bytes,
    _trim_value,
    strip_thinking_tags,
)
from ypl.agent_harness_service.service.resolvers import (
    _load_agent_config_with_db_fallback,
    _mark_session_completed,
)
from ypl.agent_harness_service.service.state import (
    _EAGER_PERSIST_TOOL_INTERVAL,
    _GATEWAY_APPEND_THRESHOLD_SECONDS,
    _TASK_FAILURE_SUBTYPES,
    _active_tasks,
    _command_handlers,
    _pre_spawn_tasks,
)
from ypl.agent_harness_service.tools.local_mcp_server import (
    clear_session_sandbox,
    clear_session_websearch_count,
    reset_turn_websearch_count,
    set_session_sandbox,
)
from ypl.agent_harness_service.tools.repo_manager import scan_session_worktrees
from ypl.agent_harness_service.tools.workspace_tools import set_command_handler_manager
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.db.agent_harness import (
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageErrorType,
    AgentSessionMessageRole,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Eager persistence state
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Turn context — shared state passed between phase functions
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """Mutable state bag passed between turn phase functions.

    Created once at the start of _run_agent_task and threaded through
    each phase.  Fields are grouped by lifecycle stage.
    """

    # --- Immutable inputs (set once, never mutated) ---
    agent_session_id: uuid.UUID
    turn_number: int
    message: str
    agent_config_name: str
    workspace: str | None
    llm_session_id: str | None
    extra_dirs: list[str]
    slack_session_id: str | None
    is_slack: bool
    is_task: bool
    session_context: dict[str, Any] | None
    trigger: str | None
    session_created_at: datetime | None

    # --- Set by _setup_turn ---
    agent_config: AgentConfig | None = None
    runner: AgentRunner | None = None
    run_context: RunContext | None = None
    gateway: Gateway | None = None
    gateway_session_id: str | None = None
    gateway_username: str | None = None
    model_name: str = "__unknown__"

    # --- Streaming / accumulation state (mutated by _stream_events) ---
    events: list[dict] = field(default_factory=list)
    final_text: str = ""
    seen_outlet_tool_ids: set[str] = field(default_factory=set)
    last_gateway_reply_time: float = 0.0
    had_error: bool = False
    error_text: str = ""
    result_llm_session_id: str | None = None
    result_cost_usd: float | None = None
    result_duration_ms: int | None = None
    result_num_turns: int | None = None
    result_subtype: str | None = None
    first_text_time_ns: int | None = None
    last_text_time_ns: int | None = None
    eager: EagerPersistState = field(default_factory=EagerPersistState)
    translation_state: TranslationState | None = None
    stream_channel: str = ""
    turn_loop_start_ns: int = 0

    # --- Control flow ---
    was_cancelled: bool = False


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WebSocket publishing helper (closure-free, takes channel as arg)
# ---------------------------------------------------------------------------


async def _publish_codex_events(codex_events: list[dict], stream_channel: str) -> None:
    """Publish translated Codex events to the streaming PubSub."""
    from ypl.agent_harness_service.core.streaming import _pubsub

    if _pubsub is None:
        return  # Streaming not initialized (e.g., tests without server)
    pubsub = get_pubsub()
    for ce in codex_events:
        await pubsub.publish(stream_channel, json.dumps(ce))


# ---------------------------------------------------------------------------
# Phase 1: Setup — guard check, config, runner, gateway
# ---------------------------------------------------------------------------


async def _setup_turn(ctx: TurnContext) -> bool:
    """Prepare the turn: load config, select runner, resolve gateway.

    Returns True if setup succeeded and the turn should proceed,
    False if the turn was already interrupted or config is missing.
    """
    # Guard: bail out if stop_session() already wrote [INTERRUPTED] for this
    # turn before the task got a chance to run (sentinel race).
    async with get_async_session() as session:
        interrupted_check = await session.exec(
            select(func.count()).where(
                AgentSessionMessage.agent_session_id == ctx.agent_session_id,
                AgentSessionMessage.turn_number == ctx.turn_number,
                col(AgentSessionMessage.role) == AgentSessionMessageRole.SYSTEM,
            )
        )
        if interrupted_check.one() > 0:
            logger.info(
                "Turn already interrupted before task started, aborting",
                session_id=str(ctx.agent_session_id),
                turn_number=ctx.turn_number,
            )
            return False

    agent_config = await _load_agent_config_with_db_fallback(ctx.agent_config_name)
    if not agent_config:
        logger.error("Agent config not found in background task", name=ctx.agent_config_name)
        # Persist a SYSTEM error so the turn isn't permanently stuck as "inflight"
        try:
            async with get_async_session() as session:
                error_msg = AgentSessionMessage(
                    agent_session_id=ctx.agent_session_id,
                    turn_number=ctx.turn_number,
                    role=AgentSessionMessageRole.SYSTEM,
                    content=f"[ERROR] Agent config not found: {ctx.agent_config_name}",
                    completion_status=AgentSessionMessageCompletionStatus.FAILED,
                    error_type=AgentSessionMessageErrorType.ERROR_INTERNAL,
                )
                session.add(error_msg)
                await _mark_session_completed(session, ctx.agent_session_id)
                await session.commit()
        except Exception:
            logger.error("Failed to persist config-not-found error", session_id=str(ctx.agent_session_id))
        return False

    ctx.agent_config = agent_config

    # Register bwrap sandbox setting so MCP bash tool picks it up
    set_session_sandbox(str(ctx.agent_session_id), agent_config.sandbox.bwrap_enabled)
    # Reset per-turn websearch counter for the new turn
    reset_turn_websearch_count(str(ctx.agent_session_id))

    exec_cfg = agent_config.executor_config
    logger.info(
        "Agent config loaded for task",
        session_id=str(ctx.agent_session_id),
        agent_name=ctx.agent_config_name,
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
    pre_proc_task = _pre_spawn_tasks.pop(ctx.agent_session_id, None)

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

    ctx.runner = runner
    ctx.run_context = RunContext(
        session_id=str(ctx.agent_session_id),
        workspace=ctx.workspace,
        llm_session_id=ctx.llm_session_id,
        extra_dirs=ctx.extra_dirs,
        slack_session_id=ctx.slack_session_id,
        is_slack=ctx.is_slack,
        is_task=ctx.is_task,
        session_context=ctx.session_context,
        session_created_at=ctx.session_created_at,
    )

    # Resolve the outgoing gateway from the trigger type.
    gateway_name = TRIGGER_TO_GATEWAY.get(ctx.trigger or "")
    ctx.gateway_session_id = ctx.slack_session_id  # will generalize when more gateways exist
    # If the session was re-attached to Slack after creation (e.g. a CRON session that
    # received a human reply in its thread), slack_session_id is populated but the
    # original trigger doesn't map to a gateway.  Infer "slack" so the reply routes back.
    # Guard: only infer Slack when session_context confirms actual Slack attachment
    # (slack_channel_id is set by attach_slack_to_session / Slack-triggered sessions).
    # Without this check, API sessions whose generic session_id is stored in
    # slack_session_id would be misrouted to the Slack gateway.
    _has_slack_context = bool(ctx.session_context and ctx.session_context.get("slack_channel_id"))
    if not gateway_name and ctx.gateway_session_id and _has_slack_context:
        gateway_name = "slack"
        logger.info(
            "Gateway inferred from slack_session_id (trigger has no default gateway)",
            session_id=str(ctx.agent_session_id),
            trigger=ctx.trigger,
        )
    if gateway_name and ctx.gateway_session_id:
        registry = GatewayRegistry.get_instance()
        ctx.gateway = registry.get_for_session(gateway_name, agent_config)

    # Extract display name override from session context (set by personal agent resolution).
    ctx.gateway_username = (ctx.session_context or {}).get("display_name")
    ctx.model_name = exec_cfg.model or "__unknown__"

    # WebSocket streaming: set up translation state and publish channel
    ctx.translation_state = TranslationState(str(ctx.agent_session_id), ctx.turn_number)
    ctx.stream_channel = f"ahs:stream:{ctx.agent_session_id}"

    return True


# ---------------------------------------------------------------------------
# Phase 2: Stream events — the hot event loop
# ---------------------------------------------------------------------------


async def _stream_events(ctx: TurnContext) -> None:
    """Run the agent and process the event stream.

    Updates ctx with accumulated events, final_text, error state,
    result metadata, and timing info.
    """
    assert ctx.runner is not None
    assert ctx.run_context is not None

    # Tracks the in-flight status-hint task so we can cancel stale ones
    # before starting newer ones, preserving per-session ordering.
    _status_task: asyncio.Task[bool] | None = None

    # Emit turn/started
    assert ctx.translation_state is not None
    await _publish_codex_events(
        [{"type": "turn/started", "turn_id": ctx.translation_state.turn_id}],
        ctx.stream_channel,
    )

    # Anchor for TTFCT/TTLCT: recorded immediately before the first event
    # arrives from the runner.  Captures queue-drain + first-token latency
    # as seen by this process, excluding Python startup and MCP handshake.
    ctx.turn_loop_start_ns = time.monotonic_ns()

    async for event in ctx.runner.run(ctx.message, ctx.run_context):
        ctx.events.append(_trim_value(event.raw))

        # Log every event regardless of type
        excerpt = extract_excerpt(event)
        sid = str(ctx.agent_session_id)[-6:]
        logger.info(
            f"session {sid} [AGENT] [{event.type}]: {excerpt}",
            agent_name=ctx.agent_config_name,
            session_id=str(ctx.agent_session_id),
            role="AGENT",
            event_type=event.type,
            turn_number=ctx.turn_number,
        )

        # --- Eager persist: track tool calls ---
        # RawExecutor emits separate "tool_use" events; CLI runner embeds
        # tool_use blocks inside "assistant" events.
        tool_names_in_event: list[str] = []
        if event.type == "tool_use":
            tool_names_in_event = [event.raw.get("name", "unknown")]
        elif event.type == "assistant":
            content_blocks = event.raw.get("message", {}).get("content", [])
            tool_names_in_event = [b.get("name", "unknown") for b in content_blocks if b.get("type") == "tool_use"]

        if tool_names_in_event:
            ctx.eager.tool_call_count += len(tool_names_in_event)
            ctx.eager.pending_tool_names.extend(tool_names_in_event)
            should_persist = (
                ctx.eager.msg_id is None  # first tool call → INSERT "[started]"
                or (ctx.eager.tool_call_count - ctx.eager.last_persisted_tool_count) >= _EAGER_PERSIST_TOOL_INTERVAL
            )
            if should_persist:
                await _eager_persist_agent_msg(
                    ctx.eager, ctx.events, ctx.agent_session_id, ctx.turn_number, ctx.model_name
                )

            # Push a live status hint to the gateway (Slack shows it as a
            # small muted context block that updates in-place).
            if ctx.gateway and ctx.gateway_session_id:
                try:
                    total = ctx.eager.tool_call_count
                    # Show only the last few tool names to keep the hint concise.
                    recent = ctx.eager.pending_tool_names[-5:]
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
                        ctx.gateway.send_status_update(ctx.gateway_session_id, status_text)
                    )
                    # Retrieve the exception via callback to suppress the
                    # "Task exception was never retrieved" warning.
                    _status_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                except Exception:
                    logger.debug(
                        "Failed to schedule tool status update to gateway",
                        session_id=str(ctx.agent_session_id),
                        exc_info=True,
                    )

        # --- Single decision point: extract all user-visible content ---
        visible_contents = _extract_visible_content(event, ctx.seen_outlet_tool_ids)

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
        codex_events = translate_stream_event(ws_event, ctx.translation_state)
        if codex_events:
            await _publish_codex_events(codex_events, ctx.stream_channel)

        if event.type == "intermediate_text":
            # Send intermediate text to gateway so users see progress in Slack.
            # Tagged as "thinking" so the gateway renders it as muted/context text.
            intermediate_text = event.raw.get("text", "")
            cleaned_intermediate = strip_thinking_tags(intermediate_text)
            if cleaned_intermediate and ctx.gateway and ctx.gateway_session_id:
                now = time.monotonic()
                use_append = (
                    ctx.last_gateway_reply_time > 0
                    and (now - ctx.last_gateway_reply_time) < _GATEWAY_APPEND_THRESHOLD_SECONDS
                )
                try:
                    if use_append:
                        ok = await ctx.gateway.append_reply(
                            ctx.gateway_session_id,
                            "\n\n" + cleaned_intermediate,
                            reply_type="thinking",
                            username=ctx.gateway_username,
                        )
                    else:
                        ok = await ctx.gateway.send_reply(
                            ctx.gateway_session_id,
                            cleaned_intermediate,
                            reply_type="thinking",
                            username=ctx.gateway_username,
                        )
                    if ok:
                        ctx.last_gateway_reply_time = now
                except Exception:
                    logger.error(
                        "Failed to send intermediate text to gateway",
                        session_id=str(ctx.agent_session_id),
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
                if ctx.first_text_time_ns is None:
                    ctx.first_text_time_ns = _text_ts_ns
                ctx.last_text_time_ns = _text_ts_ns

            # --- Persist all visible content to DB ---
            for vc in visible_contents:
                if ctx.final_text:
                    ctx.final_text += "\n\n"
                ctx.final_text += vc.text
                ctx.eager.flush_pending_tools()
                ctx.eager.content_parts.append(vc.text)
            await _eager_persist_agent_msg(ctx.eager, ctx.events, ctx.agent_session_id, ctx.turn_number, ctx.model_name)

            # --- Relay to gateway for content not already delivered ---
            # Content marked already_delivered was posted to the outlet
            # directly by the MCP tool (e.g., send_slack_message); relaying
            # it again via the gateway would cause duplicates.
            undelivered = [vc for vc in visible_contents if not vc.already_delivered]
            if undelivered and ctx.gateway and ctx.gateway_session_id:
                relay_text = "\n\n".join(vc.text for vc in undelivered)
                now = time.monotonic()
                use_append = (
                    ctx.last_gateway_reply_time > 0
                    and (now - ctx.last_gateway_reply_time) < _GATEWAY_APPEND_THRESHOLD_SECONDS
                )
                try:
                    if use_append:
                        ok = await ctx.gateway.append_reply(
                            ctx.gateway_session_id, "\n\n" + relay_text, username=ctx.gateway_username
                        )
                    else:
                        ok = await ctx.gateway.send_reply(
                            ctx.gateway_session_id, relay_text, username=ctx.gateway_username
                        )
                    if ok:
                        ctx.last_gateway_reply_time = now
                except Exception:
                    logger.error("Failed to send reply to gateway", session_id=str(ctx.agent_session_id))
            elif undelivered:
                logger.info(
                    "Agent content block received (no gateway)",
                    session_id=str(ctx.agent_session_id),
                    text_preview=undelivered[0].text[:200],
                )

        elif event.type == "result":
            ctx.result_llm_session_id = event.session_id
            ctx.result_cost_usd = event.cost_usd
            ctx.result_duration_ms = event.duration_ms
            ctx.result_num_turns = event.num_turns
            ctx.result_subtype = event.subtype

        elif event.type == "error":
            ctx.had_error = True
            error_msg = event.raw.get("error", "Unknown agent error")
            ctx.error_text = f"[ERROR] {error_msg}"
            logger.error(
                "Agent returned error",
                session_id=str(ctx.agent_session_id),
                error=error_msg,
            )


# ---------------------------------------------------------------------------
# Phase 3a: Handle cancellation (CancelledError)
# ---------------------------------------------------------------------------


async def _handle_cancellation(ctx: TurnContext) -> None:
    """Handle user-initiated stop (CancelledError)."""
    ctx.was_cancelled = True
    logger.info(
        "Agent task cancelled (user stop)",
        session_id=str(ctx.agent_session_id),
        turn_number=ctx.turn_number,
    )
    assert ctx.translation_state is not None
    # Close any open message item before emitting turn/completed (failed)
    close_events = _close_message_item(ctx.translation_state)
    if close_events:
        await _publish_codex_events(close_events, ctx.stream_channel)
    # Notify WebSocket clients that the turn was cancelled
    await _publish_codex_events(
        [
            {
                "type": "turn/completed",
                "turn_id": ctx.translation_state.turn_id,
                "status": "failed",
                "error": {"message": "Turn cancelled by user"},
            }
        ],
        ctx.stream_channel,
    )
    # No SYSTEM message here — stop_session() handles that.
    # No Slack notification here — SAG handles the interruption message.
    # Delete the eagerly-persisted draft row if it exists.
    if ctx.eager.msg_id is not None:
        try:
            async with get_async_session() as session:
                await session.exec(
                    sa_delete(AgentSessionMessage).where(
                        col(AgentSessionMessage.agent_session_message_id) == ctx.eager.msg_id
                    )
                )
                await session.commit()
        except Exception:
            logger.error(
                "Failed to delete eager row on cancel",
                session_id=str(ctx.agent_session_id),
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Phase 3b: Handle crash (unexpected Exception)
# ---------------------------------------------------------------------------


async def _handle_crash(ctx: TurnContext, exc: Exception) -> None:
    """Handle unexpected runner crash."""
    logger.error(
        "Error running agent",
        session_id=str(ctx.agent_session_id),
        exc_info=True,
    )
    assert ctx.translation_state is not None
    # Close any open message item before emitting turn/completed (failed)
    close_events = _close_message_item(ctx.translation_state)
    if close_events:
        await _publish_codex_events(close_events, ctx.stream_channel)
    # Notify WebSocket clients of the crash
    await _publish_codex_events(
        [
            {
                "type": "turn/completed",
                "turn_id": ctx.translation_state.turn_id,
                "status": "failed",
                "error": {"message": f"Runner crashed: {exc}"},
            }
        ],
        ctx.stream_channel,
    )
    # Persist a SYSTEM failure message so the turn isn't stuck in "processing"
    crash_content = f"[ERROR] Runner crashed: {exc}"
    logger.info(
        f"Agent [{ctx.agent_config_name}] session {ctx.agent_session_id} [SYSTEM] message: [error] {str(exc)[:200]}",
        agent_name=ctx.agent_config_name,
        session_id=str(ctx.agent_session_id),
        role="SYSTEM",
        event_type="error",
        turn_number=ctx.turn_number,
        excerpt=str(exc)[:200],
    )
    try:
        async with get_async_session() as session:
            await _persist_system_msg(
                ctx.eager,
                crash_content,
                ctx.events,
                ctx.agent_session_id,
                ctx.turn_number,
                ctx.model_name,
                session,
            )
            await _mark_session_completed(session, ctx.agent_session_id)
            await session.commit()
    except Exception:
        logger.error(
            "Failed to persist crash record",
            session_id=str(ctx.agent_session_id),
            exc_info=True,
        )
    # Notify gateway so the user sees a terminal reply
    if ctx.gateway and ctx.gateway_session_id:
        try:
            await ctx.gateway.send_reply(ctx.gateway_session_id, crash_content, username=ctx.gateway_username)
        except Exception:
            logger.error("Failed to send crash reply to gateway", session_id=str(ctx.agent_session_id))

    from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

    await _maybe_update_task_completion(
        ctx.trigger, ctx.session_context, ctx.agent_session_id, success=False, result=None, error=crash_content
    )


# ---------------------------------------------------------------------------
# Phase 4: Handle post-loop results (error or success)
# ---------------------------------------------------------------------------


async def _handle_error_result(ctx: TurnContext) -> None:
    """Persist and notify when the runner returned an error event."""
    try:
        async with get_async_session() as session:
            await _persist_system_msg(
                ctx.eager,
                ctx.error_text,
                ctx.events,
                ctx.agent_session_id,
                ctx.turn_number,
                ctx.model_name,
                session,
            )
            await _mark_session_completed(session, ctx.agent_session_id)
            await session.commit()
    except Exception:
        logger.error(
            "Failed to persist error record",
            session_id=str(ctx.agent_session_id),
            exc_info=True,
        )

    logger.warning(
        "Agent turn had errors, persisted error record",
        session_id=str(ctx.agent_session_id),
        turn_number=ctx.turn_number,
    )
    # Notify gateway so the user sees a terminal reply
    if ctx.gateway and ctx.gateway_session_id:
        try:
            await ctx.gateway.send_reply(ctx.gateway_session_id, ctx.error_text, username=ctx.gateway_username)
        except Exception:
            logger.error("Failed to send error reply to gateway", session_id=str(ctx.agent_session_id))

    from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

    await _maybe_update_task_completion(
        ctx.trigger, ctx.session_context, ctx.agent_session_id, success=False, result=None, error=ctx.error_text
    )


async def _handle_success_result(ctx: TurnContext) -> None:
    """Persist the agent response, send stop notices, and update task status."""
    assert ctx.agent_config is not None

    if not ctx.final_text:
        logger.warning(
            "Agent produced empty response",
            session_id=str(ctx.agent_session_id),
        )

    # Determine final completion_status / error_type for the AGENT message
    # based on the runner's explicit result_subtype.
    if ctx.result_subtype == "error_max_turns":
        _msg_completion = AgentSessionMessageCompletionStatus.FAILED
        _msg_error_type = AgentSessionMessageErrorType.ERROR_MAX_TURNS
    elif ctx.result_subtype == "stopped_context_overflow":
        _msg_completion = AgentSessionMessageCompletionStatus.FAILED
        _msg_error_type = AgentSessionMessageErrorType.ERROR_CONTEXT_OVERFLOW
    elif ctx.result_subtype == "error":
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
                AgentSessionMessage.agent_session_id == ctx.agent_session_id,
                AgentSessionMessage.turn_number == ctx.turn_number,
                col(AgentSessionMessage.role) == AgentSessionMessageRole.SYSTEM,
            )
        )
        if interrupted_check.one() > 0:
            logger.info(
                "Turn was interrupted during agent run, skipping persist",
                session_id=str(ctx.agent_session_id),
                turn_number=ctx.turn_number,
            )
            # Delete the eagerly-persisted draft row — stop_session() already
            # wrote the authoritative SYSTEM message for this turn.
            if ctx.eager.msg_id is not None:
                await session.exec(
                    sa_delete(AgentSessionMessage).where(
                        col(AgentSessionMessage.agent_session_message_id) == ctx.eager.msg_id
                    )
                )
            # The agent ran to completion (a success result event was already
            # emitted before stop_session() won the write race). Transition the
            # session to COMPLETED so polling callers receive a terminal signal.
            await _mark_session_completed(session, ctx.agent_session_id)
            await session.commit()
            return

        # Sanitize LLM output before writing to DB — PostgreSQL VARCHAR columns
        # reject null bytes (\x00) and asyncpg raises UntranslatableCharacterError.
        safe_final_text: str = _scrub_null_bytes(ctx.final_text)
        safe_events: list[dict] = _scrub_null_bytes(ctx.events)

        # Compute TTFCT/TTLCT in milliseconds from the turn loop start anchor.
        # NULL when no assistant text was seen (tool-only turns, errors, etc.).
        _ttfct_ms = (
            (ctx.first_text_time_ns - ctx.turn_loop_start_ns) // 1_000_000
            if ctx.first_text_time_ns is not None
            else None
        )
        _ttlct_ms = (
            (ctx.last_text_time_ns - ctx.turn_loop_start_ns) // 1_000_000 if ctx.last_text_time_ns is not None else None
        )

        if ctx.eager.msg_id is not None:
            # Update the eagerly-persisted row with final data
            await session.exec(
                update(AgentSessionMessage)
                .where(col(AgentSessionMessage.agent_session_message_id) == ctx.eager.msg_id)
                .values(
                    content=safe_final_text,
                    raw_events=safe_events,
                    llm_message_id=ctx.result_llm_session_id,
                    cost_usd=Decimal(str(ctx.result_cost_usd)) if ctx.result_cost_usd is not None else None,
                    duration_ms=ctx.result_duration_ms,
                    num_agent_turns=ctx.result_num_turns,
                    completion_status=_msg_completion,
                    error_type=_msg_error_type,
                    ttfct_ms=_ttfct_ms,
                    ttlct_ms=_ttlct_ms,
                )
            )
        else:
            # No eager row (e.g. empty response — no tool calls and no text)
            agent_msg = AgentSessionMessage(
                agent_session_id=ctx.agent_session_id,
                turn_number=ctx.turn_number,
                role=AgentSessionMessageRole.AGENT,
                content=safe_final_text,
                raw_events=safe_events,
                llm_name=ctx.model_name,
                llm_message_id=ctx.result_llm_session_id,
                cost_usd=Decimal(str(ctx.result_cost_usd)) if ctx.result_cost_usd is not None else None,
                duration_ms=ctx.result_duration_ms,
                num_agent_turns=ctx.result_num_turns,
                completion_status=_msg_completion,
                error_type=_msg_error_type,
                ttfct_ms=_ttfct_ms,
                ttlct_ms=_ttlct_ms,
            )
            session.add(agent_msg)

        # Update session's llm_session_id for --resume, and add any new worktrees
        session_to_update = await session.get(AgentSession, ctx.agent_session_id)
        if session_to_update:
            if ctx.result_llm_session_id:
                session_to_update.llm_session_id = ctx.result_llm_session_id

            # Scan for worktrees created by MCP tools during this turn.
            # Add them as extra_dirs so the next turn gets --add-dir access.
            worktrees = scan_session_worktrees(str(ctx.agent_session_id))
            if worktrees:
                existing = set(session_to_update.extra_dirs or [])
                new_dirs = [w for w in worktrees if w not in existing]
                if new_dirs:
                    session_to_update.extra_dirs = list(existing | set(new_dirs))
                    logger.info("Added worktree dirs", session_id=str(ctx.agent_session_id), new_dirs=new_dirs)

            # Mark session COMPLETED so polling callers get a terminal signal.
            # All trigger types (SLACK, CRON, API, TASK) transition to COMPLETED
            # after each turn. SLACK multi-turn sessions are re-activated to ACTIVE
            # in send_message() when the next user message arrives.
            await _mark_session_completed(session, ctx.agent_session_id)

        await session.commit()

    # Notify the user if the agent was stopped (max turns or context overflow).
    # Runners set subtype="error_max_turns" or "stopped_context_overflow".
    stop_notice: str | None = None
    if ctx.result_subtype == "stopped_context_overflow":
        stop_notice = CONTEXT_OVERFLOW_NOTICE
    elif ctx.result_subtype == "error_max_turns":
        stop_notice = TURN_LIMIT_NOTICE

    if stop_notice and ctx.gateway and ctx.gateway_session_id:
        try:
            # For Slack: send as a muted status context block (Slack-only grey
            # hint).  For other gateways send_status_update returns False and we
            # fall back to a regular reply so they still see the message.
            delivered = await ctx.gateway.send_status_update(ctx.gateway_session_id, stop_notice)
            if not delivered:
                await ctx.gateway.send_reply(ctx.gateway_session_id, stop_notice, username=ctx.gateway_username)
        except Exception:
            logger.error(
                "Failed to send stop notice to gateway",
                session_id=str(ctx.agent_session_id),
            )

    # Auto-request feedback if enough turns have passed
    if ctx.gateway and ctx.gateway_session_id and ctx.agent_config.feedback_probability > 0:
        if ctx.turn_number >= ctx.agent_config.feedback_min_turns:
            if random.random() < ctx.agent_config.feedback_probability:
                try:
                    await ctx.gateway.request_feedback(ctx.gateway_session_id)
                    logger.info(
                        "Auto-requested feedback survey",
                        session_id=str(ctx.agent_session_id),
                        turn_number=ctx.turn_number,
                    )
                except Exception:
                    logger.warning(
                        "Failed to auto-request feedback",
                        session_id=str(ctx.agent_session_id),
                        turn_number=ctx.turn_number,
                        exc_info=True,
                    )

    # Determine if the session completed successfully or hit an error condition
    # based on the runner's explicit result_subtype.
    task_success = ctx.result_subtype not in _TASK_FAILURE_SUBTYPES

    # Build a descriptive failure reason for logging and task result storage.
    task_failure_reason: str | None = None
    if ctx.result_subtype in _TASK_FAILURE_SUBTYPES:
        task_failure_reason = f"Session ended with {ctx.result_subtype}"

    logger.info(
        "Agent task completed",
        session_id=str(ctx.agent_session_id),
        turn_number=ctx.turn_number,
        cost_usd=ctx.result_cost_usd,
        duration_ms=ctx.result_duration_ms,
        response_length=len(ctx.final_text),
        result_subtype=ctx.result_subtype,
        task_success=task_success,
    )

    # Fire-and-forget: generate/refresh session title after turn 1 and 3.
    trigger_enum = AgentSessionTrigger(ctx.trigger) if ctx.trigger else None
    create_background_task(maybe_generate_session_title(ctx.agent_session_id, ctx.turn_number, trigger=trigger_enum))

    # On failure, include the failure reason in the result so it's not lost.
    # (update_task_completion stores result if non-empty, dropping error otherwise)
    task_result: dict[str, Any] = {"output": ctx.final_text}
    if task_failure_reason:
        task_result["error"] = task_failure_reason

    # Determine error_subtype for resumable failures.
    task_error_subtype: str | None = None
    if not task_success:
        task_error_subtype = ctx.result_subtype

    from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

    await _maybe_update_task_completion(
        ctx.trigger,
        ctx.session_context,
        ctx.agent_session_id,
        success=task_success,
        result=task_result,
        error=task_failure_reason,
        error_subtype=task_error_subtype,
    )


# ---------------------------------------------------------------------------
# Phase 5: Cleanup — always runs (finally block)
# ---------------------------------------------------------------------------


async def _cleanup_turn(ctx: TurnContext) -> None:
    """Clean up session state after turn completion (any exit path)."""
    clear_session_sandbox(str(ctx.agent_session_id))
    clear_session_websearch_count(str(ctx.agent_session_id))

    # Stop the BCH manager for terminal trigger types (CRON / TASK / WEBHOOK /
    # API) where the session will not receive another message.  For interactive
    # SLACK sessions the manager stays alive; it is reaped by the idle timer
    # after AHS_BCH_IDLE_TIMEOUT_SECONDS of inactivity, or by stop_session().
    _is_interactive_session = (ctx.trigger or "").upper() == AgentSessionTrigger.SLACK.value
    if not _is_interactive_session:
        _bch_mgr = _command_handlers.pop(ctx.agent_session_id, None)
        if _bch_mgr is not None:
            set_command_handler_manager(str(ctx.agent_session_id), None)
            create_background_task(_bch_mgr.stop())

    # Cancel any pre-spawn task that wasn't consumed by the runner (e.g. the
    # session was stopped before _run_agent_task reached the runner creation
    # code, or the guard-check returned early).
    orphan_spawn = _pre_spawn_tasks.pop(ctx.agent_session_id, None)
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
    if not ctx.was_cancelled:
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages

        await _drain_pending_messages(ctx.agent_session_id)
    # Only remove if the mapping still points to *this* task. A newer message
    # (or a drained turn) may have already replaced it with a different task.
    current = asyncio.current_task()
    if _active_tasks.get(ctx.agent_session_id) is current:
        del _active_tasks[ctx.agent_session_id]

    # Best-effort sync session workspace (attachments + history) to GCS.
    # Runs on every exit path (success, error, cancellation) so partial
    # history from failed/cancelled turns is preserved.
    # Placed after session-state cleanup (sandbox, websearch counters) so a
    # slow sync does not delay resets that the next turn depends on.
    try:
        await sync_session_to_gcs(str(ctx.agent_session_id))
    except BaseException:
        logger.warning(
            "GCS session sync failed after agent turn",
            session_id=str(ctx.agent_session_id),
            exc_info=True,
        )

    # Best-effort sync agent memory to GCS for all agents.
    if ctx.agent_config_name:
        try:
            await sync_agent_memory_to_gcs(ctx.agent_config_name)
        except Exception:
            logger.warning(
                "GCS agent memory sync failed after agent turn",
                agent_name=ctx.agent_config_name,
                session_id=str(ctx.agent_session_id),
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


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
    ctx = TurnContext(
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
        session_created_at=session_created_at,
    )

    try:
        if not await _setup_turn(ctx):
            return

        try:
            await _stream_events(ctx)
        except asyncio.CancelledError:
            await _handle_cancellation(ctx)
            return
        except Exception as exc:
            await _handle_crash(ctx, exc)
            return

        if ctx.had_error:
            await _handle_error_result(ctx)
            return

        await _handle_success_result(ctx)

    finally:
        await _cleanup_turn(ctx)
