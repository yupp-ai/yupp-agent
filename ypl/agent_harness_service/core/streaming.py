"""WebSocket streaming for Agent Harness Service.

Provides Codex-compatible event streaming over WebSocket connections.

Components:
- translate_stream_event(): Converts AHS StreamEvent → Codex-compatible event dicts
- InMemoryPubSub: asyncio.Queue-based fan-out for single-instance deployment
- WebSocketManager: Tracks active WebSocket connections per session
"""

from __future__ import annotations
import asyncio
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ypl.agent_harness_service.common.constants import CONTEXT_OVERFLOW_NOTICE, TURN_LIMIT_NOTICE
from ypl.agent_harness_service.common.types import StreamEvent
from ypl.structured_logger import get_logger

logger = get_logger()

# Typed callbacks for WebSocket client→server messages
# (session_id, content, user_id, source)
OnUserMessage = Callable[[str, str, str | None, str | None], Coroutine[Any, Any, None]]
OnStop = Callable[[str], Coroutine[Any, Any, None]]

# ---------------------------------------------------------------------------
# Tool name → Codex item type mapping
# ---------------------------------------------------------------------------

COMMAND_TOOLS = frozenset({"Bash", "bash", "run_command", "Execute"})
FILE_TOOLS = frozenset({"Write", "Edit", "write_file", "edit_file", "MultiEdit"})
READ_TOOLS = frozenset({"Read", "read_file", "Glob", "glob", "Grep", "grep"})


def _classify_tool(tool_name: str) -> str:
    """Map an AHS tool name to a Codex item type."""
    if tool_name in COMMAND_TOOLS:
        return "command_execution"
    if tool_name in FILE_TOOLS:
        return "file_change"
    # Read tools and everything else → mcp_tool_call
    return "mcp_tool_call"


# ---------------------------------------------------------------------------
# StreamEvent → Codex event translation
# ---------------------------------------------------------------------------


# State tracker per session for correlating tool_use → tool_result
class TranslationState:
    """Tracks per-session state needed for translating StreamEvents to Codex events."""

    def __init__(self, session_id: str, turn_number: int) -> None:
        self.session_id = session_id
        self.turn_number = turn_number
        self.turn_id = f"{session_id}:{turn_number}"
        self.event_counter = 0
        self._current_message_item_id: str | None = None
        # Maps tool_use_id → full item dict for matching tool_result with context
        self._tool_items: dict[str, dict[str, Any]] = {}
        # Track accumulated text per message item
        self._message_texts: dict[str, str] = {}

    def next_item_id(self) -> str:
        return f"item_{uuid.uuid4().hex[:8]}"


def translate_stream_event(
    event: StreamEvent,
    state: TranslationState,
) -> list[dict[str, Any]]:
    """Convert an AHS StreamEvent into Codex-compatible event dicts.

    Returns a list because one StreamEvent can produce multiple Codex events
    (e.g., an assistant text event produces item/started + item/agentMessage/delta).
    """
    results: list[dict[str, Any]] = []

    if event.type == "system":
        # System event = session metadata. We emit thread.started from the WS handler
        # directly, so we can skip or pass through.
        pass

    elif event.type == "assistant":
        results.extend(_translate_assistant(event, state))

    elif event.type == "user":
        results.extend(_translate_user(event, state))

    elif event.type == "tool_use":
        results.extend(_translate_tool_use(event, state))

    elif event.type == "intermediate_text":
        results.extend(_translate_intermediate_text(event, state))

    elif event.type == "tool_result":
        results.extend(_translate_tool_result(event, state))

    elif event.type == "result":
        # Close any open message item
        results.extend(_close_message_item(state))

        # Inject a system notice before turn.completed when the agent was stopped.
        result_subtype = event.raw.get("subtype")
        if result_subtype == "error_max_turns":
            results.extend(build_notice_events(TURN_LIMIT_NOTICE))
        elif result_subtype == "stopped_context_overflow":
            results.extend(build_notice_events(CONTEXT_OVERFLOW_NOTICE))

        usage: dict[str, Any] = {
            "input_tokens": event.raw.get("input_tokens", 0),
            "output_tokens": event.raw.get("output_tokens", 0),
            "cached_input_tokens": event.raw.get("cached_input_tokens", 0),
        }
        if event.cost_usd is not None:
            usage["cost_usd"] = event.cost_usd
        if event.duration_ms is not None:
            usage["duration_ms"] = event.duration_ms
        if event.num_turns is not None:
            usage["num_agent_turns"] = event.num_turns

        results.append(
            {
                "type": "turn/completed",
                "turn_id": state.turn_id,
                "status": "completed",
                "usage": usage,
            }
        )

    elif event.type == "error":
        results.extend(_close_message_item(state))
        results.append(
            {
                "type": "turn/completed",
                "turn_id": state.turn_id,
                "status": "failed",
                "error": {"message": event.raw.get("error", "Unknown error")},
            }
        )

    return results


def _translate_assistant(event: StreamEvent, state: TranslationState) -> list[dict[str, Any]]:
    """Translate assistant events (text and/or tool_use content blocks)."""
    results: list[dict[str, Any]] = []
    content_blocks = event.raw.get("message", {}).get("content", [])

    for block in content_blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            if not text:
                continue

            # Start a new message item if we don't have one
            if state._current_message_item_id is None:
                item_id = state.next_item_id()
                state._current_message_item_id = item_id
                state._message_texts[item_id] = ""
                results.append(
                    {
                        "type": "item/started",
                        "item": {
                            "id": item_id,
                            "type": "agent_message",
                            "text": "",
                            "status": "in_progress",
                        },
                    }
                )

            item_id = state._current_message_item_id
            state._message_texts[item_id] = state._message_texts.get(item_id, "") + text
            results.append(
                {
                    "type": "item/agentMessage/delta",
                    "item_id": item_id,
                    "delta": text,
                }
            )

        elif block.get("type") == "tool_use":
            # Close any open message item before tool use
            results.extend(_close_message_item(state))
            # Tool use in assistant content block — start the tool item
            results.extend(_start_tool_item(block, state))

    return results


def _translate_user(event: StreamEvent, state: TranslationState) -> list[dict[str, Any]]:
    """Translate Claude user events that contain tool_result content blocks."""
    results: list[dict[str, Any]] = []
    content_blocks = event.raw.get("message", {}).get("content", [])

    for block in content_blocks:
        if block.get("type") != "tool_result":
            continue
        results.extend(translate_stream_event(StreamEvent(type="tool_result", raw=block), state))

    return results


def _translate_intermediate_text(event: StreamEvent, state: TranslationState) -> list[dict[str, Any]]:
    """Translate an intermediate_text event into agent_message events.

    Emitted by the raw executor when the model produces text alongside tool calls.
    Creates a complete message item (started + delta + completed) so downstream
    consumers see the text before the subsequent tool executions.
    """
    text = event.raw.get("text", "")
    if not text:
        return []

    # Close any prior open message item first
    results = _close_message_item(state)

    item_id = state.next_item_id()
    results.append(
        {
            "type": "item/started",
            "item": {"id": item_id, "type": "agent_message", "text": "", "status": "in_progress"},
        }
    )
    results.append({"type": "item/agentMessage/delta", "item_id": item_id, "delta": text})
    results.append(
        {
            "type": "item/completed",
            "item": {"id": item_id, "type": "agent_message", "text": text, "status": "completed"},
        }
    )
    return results


def _translate_tool_use(event: StreamEvent, state: TranslationState) -> list[dict[str, Any]]:
    """Translate a standalone tool_use event."""
    results: list[dict[str, Any]] = []
    # Close any open message item
    results.extend(_close_message_item(state))
    results.extend(_start_tool_item(event.raw, state))
    return results


def _start_tool_item(raw: dict[str, Any], state: TranslationState) -> list[dict[str, Any]]:
    """Create an item.started event for a tool invocation."""
    tool_name = raw.get("name", "") or raw.get("tool_name", "")
    raw_input = raw.get("input", raw.get("tool_input", {}))
    # Normalize: CodexRunner may emit input as a plain string
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    tool_input_str = str(raw_input) if not isinstance(raw_input, dict) else ""
    tool_use_id = raw.get("id", "") or raw.get("tool_use_id", "")
    item_type = _classify_tool(tool_name)
    item_id = state.next_item_id()

    if item_type == "command_execution":
        item: dict[str, Any] = {
            "id": item_id,
            "type": "command_execution",
            "command": tool_input.get("command", tool_input_str or str(raw_input)),
            "aggregated_output": "",
            "status": "in_progress",
        }
    elif item_type == "file_change":
        # Extract file path from tool input
        file_path = tool_input.get("file_path", "") or tool_input.get("path", "") or tool_input_str
        kind = "add" if tool_name in ("Write", "write_file") else "update"
        item = {
            "id": item_id,
            "type": "file_change",
            "changes": [{"path": file_path, "kind": kind}],
            "status": "in_progress",
        }
    else:
        # mcp_tool_call
        item = {
            "id": item_id,
            "type": "mcp_tool_call",
            "server": "ahs",
            "tool": tool_name,
            "arguments": tool_input,
            "status": "in_progress",
        }

    # Track full item dict for tool_result matching (richer completed events)
    if tool_use_id:
        state._tool_items[tool_use_id] = item

    return [{"type": "item/started", "item": item}]


def _translate_tool_result(event: StreamEvent, state: TranslationState) -> list[dict[str, Any]]:
    """Translate a tool_result event into an item.completed event."""
    raw = event.raw
    tool_use_id = raw.get("tool_use_id", "") or raw.get("id", "")

    # Find the matching item from the tool_use event
    original_item = state._tool_items.pop(tool_use_id, None)
    if not original_item:
        return []

    item_type = original_item["type"]
    content = raw.get("content", raw.get("output", ""))
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    content = str(content)
    is_error = raw.get("is_error", False)
    status = "failed" if is_error else "completed"

    if item_type == "command_execution":
        completed_item: dict[str, Any] = {
            **original_item,
            "aggregated_output": content,
            "exit_code": 1 if is_error else 0,
            "status": status,
        }
    elif item_type == "file_change":
        completed_item = {
            **original_item,
            "status": status,
        }
    else:
        completed_item = {
            **original_item,
            "result": content[:5000] if not is_error else None,
            "error": {"message": content[:2000]} if is_error else None,
            "status": status,
        }

    return [{"type": "item/completed", "item": completed_item}]


def build_notice_events(text: str) -> list[dict[str, Any]]:
    """Build codex events for a system notice displayed as an agent message."""
    item_id = f"item_{uuid.uuid4().hex[:8]}"
    return [
        {
            "type": "item/started",
            "item": {"id": item_id, "type": "agent_message", "text": "", "status": "in_progress"},
        },
        {"type": "item/agentMessage/delta", "item_id": item_id, "delta": text},
        {
            "type": "item/completed",
            "item": {"id": item_id, "type": "agent_message", "text": text, "status": "completed"},
        },
    ]


def _close_message_item(state: TranslationState) -> list[dict[str, Any]]:
    """Close the current open message item, if any."""
    if state._current_message_item_id is None:
        return []

    item_id = state._current_message_item_id
    full_text = state._message_texts.pop(item_id, "")
    state._current_message_item_id = None

    return [
        {
            "type": "item/completed",
            "item": {
                "id": item_id,
                "type": "agent_message",
                "text": full_text,
                "status": "completed",
            },
        }
    ]


# ---------------------------------------------------------------------------
# PubSub abstraction
# ---------------------------------------------------------------------------


@runtime_checkable
class PubSub(Protocol):
    """Publish/subscribe interface for event fan-out."""

    async def publish(self, channel: str, message: str) -> None: ...

    def subscribe(self, channel: str) -> AsyncIterator[str]: ...


class InMemoryPubSub:
    """Single-process pub/sub using asyncio.Queue per subscriber.

    Thread-safe within a single asyncio event loop. Suitable for single-instance
    AHS deployments. Can be swapped for RedisPubSub for multi-instance.
    """

    def __init__(self) -> None:
        # channel → set of subscriber queues
        self._subscribers: dict[str, set[asyncio.Queue[str | None]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, message: str) -> None:
        """Publish a message to all subscribers of a channel."""
        async with self._lock:
            subscribers = list(self._subscribers.get(channel, set()))
        for queue in subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "Subscriber queue full, dropping message",
                    channel=channel,
                )

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[str]]:
        """Subscribe to a channel. Yields an async iterator of messages.

        Usage:
            async with pubsub.subscribe("ch") as messages:
                async for msg in messages:
                    ...
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)

        async with self._lock:
            self._subscribers[channel].add(queue)

        try:

            async def _iter() -> AsyncIterator[str]:
                while True:
                    msg = await queue.get()
                    if msg is None:
                        break
                    yield msg

            yield _iter()
        finally:
            async with self._lock:
                self._subscribers[channel].discard(queue)
            # Signal the iterator to stop (best-effort under backpressure)
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def close_channel(self, channel: str) -> None:
        """Close all subscribers on a channel (e.g., session completed)."""
        async with self._lock:
            subscribers = self._subscribers.pop(channel, set())
        for queue in subscribers:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def has_subscribers(self, channel: str) -> bool:
        """Check if a channel has any active subscribers."""
        return bool(self._subscribers.get(channel))


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------


class WebSocketManager:
    """Manages WebSocket connections for streaming sessions.

    Responsibilities:
    - Track active connections per session
    - Forward PubSub events to WebSocket clients
    - Handle client→server messages (user_message, stop, resume)
    - Heartbeat keepalive
    """

    HEARTBEAT_INTERVAL_S: float = 15
    CHANNEL_PREFIX = "ahs:stream:"

    def __init__(self, pubsub: InMemoryPubSub) -> None:
        self.pubsub = pubsub
        # session_id → set of active WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    def channel_for(self, session_id: str) -> str:
        return f"{self.CHANNEL_PREFIX}{session_id}"

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        """Register a new WebSocket connection for a session."""
        async with self._lock:
            self._connections[session_id].add(websocket)
        logger.info("WebSocket connected", session_id=session_id)

    async def disconnect(self, websocket: WebSocket, session_id: str) -> None:
        """Unregister a WebSocket connection."""
        async with self._lock:
            conns = self._connections.get(session_id, set())
            conns.discard(websocket)
            if not conns:
                self._connections.pop(session_id, None)
        logger.info("WebSocket disconnected", session_id=session_id)

    async def run_connection(
        self,
        websocket: WebSocket,
        session_id: str,
        on_user_message: OnUserMessage | None = None,
        on_stop: OnStop | None = None,
    ) -> None:
        """Run the WebSocket connection lifecycle.

        Concurrently:
        1. Forward PubSub events → WebSocket (server→client)
        2. Listen for client messages → dispatch (client→server)
        3. Send heartbeat pings

        Args:
            websocket: The WebSocket connection
            session_id: The AHS session ID
            on_user_message: async callback(session_id, message_text) for user messages
            on_stop: async callback(session_id) for stop requests
        """
        await self.connect(websocket, session_id)
        channel = self.channel_for(session_id)
        event_counter = 0

        try:
            # Send thread.started immediately
            event_counter += 1
            await _ws_send_event(
                websocket,
                event_counter,
                {
                    "type": "thread/started",
                    "thread_id": session_id,
                },
            )

            # Run three concurrent tasks
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    self._forward_events(websocket, channel, event_counter),
                    name="forward_events",
                )
                tg.create_task(
                    self._receive_messages(websocket, session_id, on_user_message, on_stop),
                    name="receive_messages",
                )
                tg.create_task(
                    self._heartbeat(websocket),
                    name="heartbeat",
                )

        except* WebSocketDisconnect:
            logger.info("WebSocket client disconnected", session_id=session_id)
        except* Exception as eg:
            for exc in eg.exceptions:
                if not isinstance(exc, WebSocketDisconnect):
                    logger.error(
                        "WebSocket connection error",
                        session_id=session_id,
                        error=str(exc),
                    )
        finally:
            await self.disconnect(websocket, session_id)

    async def _forward_events(
        self,
        websocket: WebSocket,
        channel: str,
        start_counter: int,
    ) -> None:
        """Subscribe to PubSub and forward events to the WebSocket client."""
        counter = start_counter
        async with self.pubsub.subscribe(channel) as messages:
            async for message in messages:
                counter += 1
                event = json.loads(message)
                await _ws_send_event(websocket, counter, event)

    async def _receive_messages(
        self,
        websocket: WebSocket,
        session_id: str,
        on_user_message: OnUserMessage | None,
        on_stop: OnStop | None,
    ) -> None:
        """Listen for client→server messages."""
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.error(
                    "Error receiving WebSocket message, closing listener",
                    session_id=session_id,
                    exc_info=True,
                )
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Invalid JSON",
                    }
                )
                continue

            msg_type = msg.get("type")
            if msg_type == "user_message" and on_user_message:
                content = msg.get("content", "")
                user_id = msg.get("user_id")
                source = msg.get("source")  # optional; falls back to "websocket" in route handler
                if content:
                    await on_user_message(session_id, content, user_id, source)
            elif msg_type == "stop" and on_stop:
                await on_stop(session_id)
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                logger.warning(
                    "Unknown WebSocket message type",
                    session_id=session_id,
                    msg_type=msg_type,
                )

    async def _heartbeat(self, websocket: WebSocket) -> None:
        """Send periodic pings to keep the connection alive."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_S)
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "heartbeat"})
            except Exception:
                break

    async def shutdown(self) -> None:
        """Close all WebSocket connections gracefully."""
        async with self._lock:
            all_sessions = list(self._connections.keys())
            all_websockets = [ws for conns in self._connections.values() for ws in conns]
        for session_id in all_sessions:
            await self.pubsub.close_channel(self.channel_for(session_id))
        for ws in all_websockets:
            try:
                await ws.close(code=1001)
            except Exception:
                pass  # Already closed or broken


async def _ws_send_event(websocket: WebSocket, event_id: int, event: dict[str, Any]) -> None:
    """Send a Codex event over WebSocket with an event ID."""
    payload = {**event, "event_id": event_id}
    await websocket.send_json(payload)


# ---------------------------------------------------------------------------
# Module-level singleton (initialized in server.py lifespan)
# ---------------------------------------------------------------------------

_pubsub: InMemoryPubSub | None = None
_ws_manager: WebSocketManager | None = None


def init_streaming() -> tuple[InMemoryPubSub, WebSocketManager]:
    """Initialize the streaming infrastructure. Called once at startup."""
    global _pubsub, _ws_manager
    _pubsub = InMemoryPubSub()
    _ws_manager = WebSocketManager(_pubsub)
    logger.info("Streaming infrastructure initialized")
    return _pubsub, _ws_manager


def get_pubsub() -> InMemoryPubSub:
    """Get the global PubSub instance."""
    if _pubsub is None:
        raise RuntimeError("Streaming not initialized — call init_streaming() first")
    return _pubsub


def get_ws_manager() -> WebSocketManager:
    """Get the global WebSocketManager instance."""
    if _ws_manager is None:
        raise RuntimeError("Streaming not initialized — call init_streaming() first")
    return _ws_manager


async def shutdown_streaming() -> None:
    """Shut down streaming infrastructure. Called at app shutdown."""
    if _ws_manager:
        await _ws_manager.shutdown()
    logger.info("Streaming infrastructure shut down")
