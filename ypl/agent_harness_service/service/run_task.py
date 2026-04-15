"""Agent turn runner — _run_agent_task and persistence helpers."""

import asyncio
import json
import random
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.agent_harness_service.common.constants import (
    CONTEXT_OVERFLOW_NOTICE,
    EXECUTOR_TYPE_HARNESSED,
    EXECUTOR_TYPE_RAW,
    HARNESS_CLAUDE_SDK,
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
    HARNESSED_MODELS,
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


# ---------------------------------------------------------------------------
# Gateway tool-event helpers
# ---------------------------------------------------------------------------

# Keys tried in order to extract the most meaningful "command" string from a
# tool input dict for display in the Slack tool-cluster block.
_TOOL_COMMAND_KEYS = ["command", "pattern", "file_path", "path", "query", "text", "prompt", "message"]


def _format_tool_command(name: str, input_dict: Any) -> str:
    """Return a short display string for a tool call's primary argument.

    Tries well-known field names in priority order, then falls back to the
    first non-empty string value.  Always capped at 200 characters.

    Args:
        name: Tool name (not currently used for dispatch, reserved for future).
        input_dict: Raw ``input`` dict from the tool_use event.

    Returns:
        Formatted command string, or empty string if nothing useful found.
    """
    if not isinstance(input_dict, dict) or not input_dict:
        return ""
    for key in _TOOL_COMMAND_KEYS:
        raw_val = input_dict.get(key)
        if raw_val and isinstance(raw_val, str):
            return str(raw_val)[:200]
    # Fallback: first non-empty string value in the dict
    for raw_val in input_dict.values():
        if isinstance(raw_val, str) and raw_val:
            return str(raw_val)[:200]
    return ""


def _determine_result_status(output: Any, is_error: bool) -> tuple[str, str | None]:
    """Classify a tool result as 'done', 'empty', or 'failed'.

    Args:
        output: Raw output from the tool_result event (str, list, or None).
        is_error: True if the tool call returned an error.

    Returns:
        Tuple of (result_status, error_msg).  error_msg is only set for 'failed'.
    """
    if is_error:
        # Extract text from the error output for a short error message.
        if isinstance(output, str):
            err = output.strip()
        elif isinstance(output, list):
            err = " ".join(str(b.get("text", "")) for b in output if isinstance(b, dict) and b.get("text")).strip()
        else:
            err = str(output).strip() if output is not None else ""
        return "failed", (err[:50] if err else None)

    # Not an error — check if output is empty.
    if output is None:
        return "empty", None
    if isinstance(output, str) and not output.strip():
        return "empty", None
    if isinstance(output, list) and not output:
        return "empty", None
    return "done", None


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

        # Apply force_model override from session context (set at session creation time).
        # Switches the executor type + model without mutating the shared agent config.
        _force_model: str | None = (session_context or {}).get("force_model")
        if _force_model:
            _is_harness = _force_model in HARNESSED_MODELS
            _forced_type = EXECUTOR_TYPE_HARNESSED if _is_harness else EXECUTOR_TYPE_RAW
            exec_cfg = exec_cfg.model_copy(update={"type": _forced_type, "model": _force_model})
            agent_config = agent_config.model_copy(update={"executor_config": exec_cfg})
            logger.info(
                "force_model override applied",
                session_id=str(agent_session_id),
                force_model=_force_model,
                executor_type=_forced_type,
            )

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
        elif exec_cfg.model in (HARNESS_CODEX_CLI, HARNESS_CODEX_APP_SERVER):
            runner = CodexAppServerRunner(agent_config)
            # Codex app-server runner uses WebSocket — cancel the Claude CLI pre-spawn.
            if pre_proc_task is not None:
                pre_proc_task.cancel()
                pre_proc_task = None
        elif exec_cfg.model == HARNESS_CLAUDE_SDK:
            from ypl.agent_harness_service.executors.claude_agent_sdk_runner import ClaudeAgentSdkRunner

            runner = ClaudeAgentSdkRunner(agent_config)
            # SDK runner makes a direct HTTPS call — no subprocess to pre-warm.
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

                # --- Eager persist + gateway tool-start events ---
                # RawExecutor emits separate "tool_use" events; CLI runner embeds
                # tool_use blocks inside "assistant" events.
                tool_start_blocks_in_event: list[dict[str, Any]] = []
                if event.type == "tool_use":
                    tool_start_blocks_in_event = [event.raw]
                elif event.type == "assistant":
                    content_blocks = event.raw.get("message", {}).get("content", [])
                    tool_start_blocks_in_event = [
                        b for b in content_blocks if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]

                tool_names_in_event = [b.get("name", "unknown") for b in tool_start_blocks_in_event]

                if tool_names_in_event:
                    eager.tool_call_count += len(tool_names_in_event)
                    eager.pending_tool_names.extend(tool_names_in_event)
                    should_persist = (
                        eager.msg_id is None  # first tool call → INSERT "[started]"
                        or (eager.tool_call_count - eager.last_persisted_tool_count) >= _EAGER_PERSIST_TOOL_INTERVAL
                    )
                    if should_persist:
                        await _eager_persist_agent_msg(eager, events, agent_session_id, turn_number, model_name)

                    # Fire a structured tool-start event to the gateway for each
                    # tool invocation so SAG can render the live cluster display.
                    if gateway and gateway_session_id:
                        # tool_call_count is already incremented by len(tool_start_blocks_in_event)
                        # above, so compute per-block anon IDs by working backwards from the end.
                        _anon_base = eager.tool_call_count - len(tool_start_blocks_in_event)
                        for _i, block in enumerate(tool_start_blocks_in_event):
                            _name = block.get("name", "unknown")
                            _tool_use_id = block.get("id") or f"anon_{_anon_base + _i + 1}"
                            _command = _format_tool_command(_name, block.get("input") or {})
                            try:
                                _t = asyncio.create_task(
                                    gateway.send_tool_event(
                                        gateway_session_id,
                                        kind="start",
                                        tool_use_id=_tool_use_id,
                                        name=_name,
                                        command=_command,
                                    )
                                )
                                _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                            except Exception:
                                logger.debug(
                                    "Failed to fire tool start event to gateway",
                                    session_id=str(agent_session_id),
                                    exc_info=True,
                                )

                # --- Gateway tool-result events ---
                # RawExecutor: standalone "tool_result" event.
                # CLI runner: tool_result blocks embedded inside "user" events.
                if gateway and gateway_session_id:
                    tool_result_blocks_in_event: list[dict[str, Any]] = []
                    if event.type == "tool_result":
                        tool_result_blocks_in_event = [event.raw]
                    elif event.type == "user":
                        _user_content = event.raw.get("message", {}).get("content", [])
                        tool_result_blocks_in_event = [
                            b for b in _user_content if isinstance(b, dict) and b.get("type") == "tool_result"
                        ]
                    for block in tool_result_blocks_in_event:
                        _tool_use_id = block.get("tool_use_id") or ""
                        if not _tool_use_id:
                            continue
                        _is_error = block.get("is_error", False)
                        _output = block.get("output") or block.get("content", "")
                        _result_status, _error_msg = _determine_result_status(_output, _is_error)
                        # Extract the first non-empty line of output for the live display.
                        _result_content: str | None = None
                        if _result_status == "done" and _output:
                            _raw_text: str = ""
                            if isinstance(_output, str):
                                _raw_text = _output
                            elif isinstance(_output, list):
                                for _b in _output:
                                    if isinstance(_b, dict) and _b.get("text"):
                                        _raw_text = str(_b["text"])
                                        break
                            _first_line = _raw_text.strip().splitlines()[0].strip() if _raw_text.strip() else ""
                            if _first_line:
                                _result_content = _first_line[:150]
                        try:
                            _t = asyncio.create_task(
                                gateway.send_tool_event(
                                    gateway_session_id,
                                    kind="result",
                                    tool_use_id=_tool_use_id,
                                    result_status=_result_status,
                                    error_msg=_error_msg,
                                    result_content=_result_content,
                                )
                            )
                            _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        except Exception:
                            logger.debug(
                                "Failed to fire tool result event to gateway",
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

            from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

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

            from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

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
                # send_status_update is a no-op for all gateways; fall back to a
                # visible reply so the user sees the stop notice.
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

        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

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
            from ypl.agent_harness_service.service.session_lifecycle import (
                _drain_pending_messages,
                _drain_session_inbox,
            )

            await _drain_pending_messages(agent_session_id)

            # Scenario B (A2A inject): drain any messages that arrived in the
            # session's Redis inbox while this turn was running.  Runs after
            # _drain_pending_messages so all user-queued messages are already
            # dispatched before we pick up peer-agent messages.  Each message
            # gets its own AsyncSession internally; no db parameter needed here.
            await _drain_session_inbox(agent_session_id)
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
