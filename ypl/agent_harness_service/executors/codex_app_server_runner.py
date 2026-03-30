"""OpenAI Codex app-server runner for Agent Harness Service.

Starts `codex app-server --listen ws://127.0.0.1:{port}` per AHS session,
then communicates via WebSocket JSON-RPC 2.0.  Keeps the server alive across
turns within the same session, eliminating the per-turn cold-start cost of
CodexRunner (~2-5 s → ~0.1 s for turn 2+).

Architecture
────────────
• One `codex app-server` subprocess per AHS session_id.
• A module-level registry (_servers) maps session_id → _CodexServerState.
• Each `run()` call opens a fresh WS connection, performs the JSON-RPC
  initialise handshake, and either creates a new codex thread (first turn) or
  continues the existing one (subsequent turns).
• A background eviction loop terminates idle servers after IDLE_TTL_S.

Protocol
────────
The Codex app-server speaks JSON-RPC 2.0 without the "jsonrpc" field:

  request:      {"method": "...", "id": N, "params": {...}}
  response:     {"id": N, "result": {...}}    or   {"id": N, "error": {...}}
  notification: {"method": "...", "params": {...}}          # no "id"

Refs:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
  https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol/schema
"""

from __future__ import annotations
import asyncio
import json
import shlex
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.executors.mcp_config import build_codex_mcp_args, build_codex_mcp_env
from ypl.agent_harness_service.executors.runner import (
    AGENT_RESPONSE_DEBUG,
    AgentRunner,
    RunContext,
    StreamEvent,
    _SessionFileLogger,
    build_subprocess_env,
    extract_excerpt,
)
from ypl.agent_harness_service.executors.system_prompt import build_system_prompt
from ypl.structured_logger import get_logger

logger = get_logger()

# How long an idle server is kept alive before eviction.
IDLE_TTL_S: float = 600.0  # 10 minutes

# Seconds to wait for the app-server /readyz endpoint to respond.
SERVER_READY_TIMEOUT_S: float = 15.0

# Poll interval while waiting for the server to become ready.
SERVER_READY_POLL_S: float = 0.2

# Timeout for a single WS request/response round-trip.
RPC_TIMEOUT_S: float = 30.0

# Timeout for each individual HTTP request while polling /readyz.
SERVER_READY_REQUEST_TIMEOUT_S: float = 2.0

# ── module-level server registry ────────────────────────────────────────────


@dataclass
class _CodexServerState:
    proc: asyncio.subprocess.Process
    port: int
    thread_id: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    active_turns: int = 0  # number of turns currently executing against this server


# session_id → running server state
_servers: dict[str, _CodexServerState] = {}
_eviction_task: asyncio.Task[None] | None = None
# Per-session startup locks to prevent concurrent server spawning for the same session.
_server_locks: dict[str, asyncio.Lock] = {}


def _ensure_eviction_loop() -> None:
    """Start the background eviction task if it isn't already running."""
    global _eviction_task
    if _eviction_task is None or _eviction_task.done():
        _eviction_task = asyncio.create_task(_eviction_loop(), name="codex-app-server-eviction")


async def _eviction_loop() -> None:
    """Periodically terminate idle codex app-server processes."""
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        # Only evict servers that are idle AND not currently serving a turn.
        to_evict = [sid for sid, s in _servers.items() if now - s.last_used > IDLE_TTL_S and s.active_turns == 0]
        for sid in to_evict:
            # Re-check predicates before evicting — state may have changed while we awaited earlier evictions.
            state = _servers.get(sid)
            if state is None or state.active_turns > 0 or time.monotonic() - state.last_used <= IDLE_TTL_S:
                continue
            _servers.pop(sid, None)
            _server_locks.pop(sid, None)
            if state.proc.returncode is None:
                logger.info(
                    "Evicting idle codex app-server",
                    session_id=sid,
                    idle_s=int(now - state.last_used),
                )
                try:
                    state.proc.kill()
                    await state.proc.wait()
                except (ProcessLookupError, OSError):
                    pass


# ── helpers ──────────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Bind to port 0 to let the OS assign a free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_ready(port: int, timeout: float = SERVER_READY_TIMEOUT_S) -> None:
    """Poll GET http://127.0.0.1:{port}/readyz until 200 or timeout."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/readyz"
    async with aiohttp.ClientSession() as http:
        while time.monotonic() < deadline:
            try:
                async with http.get(url, timeout=aiohttp.ClientTimeout(total=SERVER_READY_REQUEST_TIMEOUT_S)) as resp:
                    if resp.status == 200:
                        return
            except (aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(SERVER_READY_POLL_S)
    raise TimeoutError(f"codex app-server did not become ready on port {port} within {timeout}s")


class _RpcError(Exception):
    """JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message


async def _rpc(
    ws: aiohttp.ClientWebSocketResponse,
    method: str,
    params: dict[str, Any] | None,
    req_id: int,
) -> dict[str, Any]:
    """Send a JSON-RPC request and await the matching response.

    Notifications arriving before the response are discarded (they are
    handled separately by the caller's notification loop).
    """
    msg: dict[str, Any] = {"method": method, "id": req_id}
    if params is not None:
        msg["params"] = params
    await ws.send_str(json.dumps(msg))

    deadline = time.monotonic() + RPC_TIMEOUT_S
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.receive(), timeout=max(0.1, deadline - time.monotonic()))
        if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise ConnectionError(f"WS closed while waiting for RPC response to '{method}'")
        if raw.type != aiohttp.WSMsgType.TEXT:
            continue
        data: dict[str, Any] = json.loads(raw.data)
        if data.get("id") != req_id:
            # This is a notification or a response to a different request — skip.
            # TODO: If the server ever sends notifications before acknowledging a request
            # (not guaranteed by JSON-RPC 2.0), those frames are permanently lost here.
            # A robust fix would buffer non-matching frames and replay them to
            # _iter_notifications via an asyncio.Queue. Over localhost WS the response
            # reliably arrives before any notifications in practice.
            continue
        if "error" in data:
            err = data["error"]
            raise _RpcError(err.get("code", -1), err.get("message", "unknown RPC error"))
        result = data.get("result")
        return dict(result if result is not None else {})
    raise TimeoutError(f"RPC '{method}' timed out after {RPC_TIMEOUT_S}s")


# ── event translation ─────────────────────────────────────────────────────────


# Helper re-exports from codex_runner kept inline to avoid tight coupling.
def _extract_mcp_tool_name(item: dict[str, Any]) -> str:
    tool_name = item.get("tool") or item.get("tool_name") or item.get("name") or ""
    return str(tool_name)


def _extract_mcp_tool_input(item: dict[str, Any]) -> dict[str, Any] | str:
    arguments = item.get("arguments", item.get("input", {}))
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    return str(arguments)


def _format_mcp_payload(payload: object) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = [b.get("text", "") for b in payload if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return " ".join(parts) if parts else json.dumps(payload, default=str)
    if isinstance(payload, dict):
        content = payload.get("content")
        if content is not None:
            return _format_mcp_payload(content)
        text = payload.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(payload, default=str)
    return str(payload)


def _mcp_tool_result(item: dict[str, Any]) -> tuple[str, bool]:
    status = str(item.get("status", ""))
    error = item.get("error")
    result = item.get("result", item.get("output"))
    is_error = status == "failed"
    if isinstance(result, dict):
        is_error = is_error or bool(result.get("is_error") or result.get("isError"))
    if error is not None:
        return _format_mcp_payload(error), True
    if result is not None:
        return _format_mcp_payload(result), is_error
    return ("MCP tool call failed" if is_error else ""), is_error


def _notification_to_event(
    method: str,
    params: dict[str, Any],
    thread_id: str | None,
    message_count: int,
    start_time: float,
) -> StreamEvent | None:
    """Translate a single WS notification into a StreamEvent, or None to skip."""
    item: dict[str, Any] = params.get("item", {}) or {}
    item_type: str = item.get("type", "") if isinstance(item, dict) else ""

    if method == "turn/started":
        return None  # no-op; matches codex_runner behaviour

    if method == "item/started":
        if item_type == "commandExecution":
            return StreamEvent(
                type="tool_use",
                raw={
                    "type": "tool_use",
                    "id": item.get("id", ""),
                    "tool_name": "Bash",
                    "input": item.get("command", ""),
                },
            )
        if item_type == "mcpToolCall":
            return StreamEvent(
                type="tool_use",
                raw={
                    "type": "tool_use",
                    "id": item.get("id", ""),
                    "tool_name": _extract_mcp_tool_name(item),
                    "input": _extract_mcp_tool_input(item),
                },
            )
        if item_type == "fileChange":
            return StreamEvent(
                type="tool_use",
                raw={
                    "type": "tool_use",
                    "id": item.get("id", ""),
                    "tool_name": "FileChange",
                    "input": f"{item.get('kind', 'change')} {item.get('path', '')}",
                },
            )
        return None

    if method == "item/completed":
        if item_type == "commandExecution":
            return StreamEvent(
                type="tool_result",
                raw={
                    "type": "tool_result",
                    "tool_use_id": item.get("id", ""),
                    "content": item.get("aggregatedOutput", item.get("aggregated_output", "")),
                },
            )
        if item_type == "mcpToolCall":
            content, is_error = _mcp_tool_result(item)
            return StreamEvent(
                type="tool_result",
                raw={
                    "type": "tool_result",
                    "tool_use_id": item.get("id", ""),
                    "content": content,
                    "is_error": is_error,
                },
            )
        if item_type == "agentMessage":
            return StreamEvent(
                type="assistant",
                raw={
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": item.get("text", "")}]},
                },
            )
        if item_type == "fileChange":
            changes = item.get("changes", [])
            summary = "; ".join(f"{c.get('kind', '?')} {c.get('path', '?')}" for c in changes)
            return StreamEvent(
                type="tool_result",
                raw={
                    "type": "tool_result",
                    "tool_use_id": item.get("id", ""),
                    "content": f"file_change: {summary}",
                },
            )
        if item_type in ("reasoning", "plan"):
            return None  # skip internal items
        if item_type == "error":
            error_text = item.get("message", item.get("text", "Unknown item error"))
            return StreamEvent(type="error", raw={"error": f"Codex item error: {error_text}"})
        return None

    if method == "turn/completed":
        turn_obj: dict[str, Any] = params.get("turn", {}) or {}
        status = turn_obj.get("status", "")
        if status == "failed":
            err = turn_obj.get("error", {}) or {}
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return StreamEvent(type="error", raw={"error": f"Codex turn failed: {msg}"})
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return StreamEvent(
            type="result",
            raw={
                "type": "result",
                "session_id": thread_id,
                "estimated_cost_usd": None,
                "duration_ms": elapsed_ms,
                "num_turns": message_count,
            },
        )

    # item/agentMessage/delta and other streaming deltas — skip (we use item/completed)
    return None


# ── runner ────────────────────────────────────────────────────────────────────


class CodexAppServerRunner(AgentRunner):
    """Runs OpenAI Codex via `codex app-server` + WebSocket JSON-RPC 2.0.

    Compared to CodexRunner (which spawns `codex exec --json` each turn):
    • The server process is reused across turns — no cold-start on turn 2+.
    • Conversation history is retained in the server's in-memory thread.
    • No Node.js dependency; the only runtime requirement is the `codex` CLI.
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    # ── arg building ──────────────────────────────────────────────────────

    def _build_server_args(self, port: int, context: RunContext) -> list[str]:
        """Build CLI args for `codex app-server --listen ws://127.0.0.1:{port}`."""
        args = ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"]
        args += build_codex_mcp_args(
            session_id=context.session_id,
            session_context=context.session_context,
            is_slack=context.is_slack,
        )
        return args

    def _build_thread_params(self, context: RunContext) -> dict[str, Any]:
        """Build params for the `thread/start` JSON-RPC call."""
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
        }
        if context.workspace:
            params["cwd"] = context.workspace
        if self.config.model:
            params["model"] = self.config.model
        system_prompt = build_system_prompt(
            self.config.name,
            session_id=context.session_id,
            slack_session_id=context.slack_session_id,
            is_slack=context.is_slack,
            is_task=context.is_task,
            session_context=context.session_context,
            additional_system_prompt=self.config.additional_system_prompt,
            has_native_skills=True,
        )
        if system_prompt:
            params["developerInstructions"] = system_prompt
        return params

    # ── server lifecycle ──────────────────────────────────────────────────

    async def _get_or_start_server(self, context: RunContext) -> _CodexServerState:
        """Return the running server for this session, starting one if needed."""
        _ensure_eviction_loop()
        sid = context.session_id
        state = _servers.get(sid)
        if state and state.proc.returncode is None:
            state.last_used = time.monotonic()
            return state

        # Per-session lock prevents two concurrent run() calls from each spawning
        # a server for the same session_id (unlikely in practice but possible on retries).
        if sid not in _server_locks:
            _server_locks[sid] = asyncio.Lock()
        async with _server_locks[sid]:
            # Re-check after acquiring the lock — another coroutine may have started it.
            state = _servers.get(sid)
            if state and state.proc.returncode is None:
                state.last_used = time.monotonic()
                return state

            # Start a fresh server on a free port.
            port = _find_free_port()
            args = self._build_server_args(port, context)
            env = build_subprocess_env()
            env.update(build_codex_mcp_env(context.session_id))

            logger.info(
                "Starting codex app-server",
                session_id=sid,
                port=port,
                command=shlex.join(args),
            )
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            # Drain stderr in a fire-and-forget task to avoid pipe deadlock.
            asyncio.create_task(_drain_stderr(proc, sid), name=f"codex-stderr-{sid}")

            try:
                await _wait_for_ready(port)
            except Exception:
                # Startup failed — kill the orphaned process before re-raising.
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
                raise

            state = _CodexServerState(proc=proc, port=port)
            _servers[sid] = state
            logger.info("codex app-server ready", session_id=sid, port=port, pid=proc.pid)
            return state

    # ── WS session helpers ────────────────────────────────────────────────

    @staticmethod
    async def _initialize_ws(ws: aiohttp.ClientWebSocketResponse, req_id_start: int) -> int:
        """Perform the JSON-RPC initialize handshake; return next req_id."""
        req_id = req_id_start
        await _rpc(
            ws,
            "initialize",
            {
                "clientInfo": {
                    "name": "ahs-codex-runner",
                    "title": "AHS Codex Runner",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": False, "optOutNotificationMethods": []},
            },
            req_id,
        )
        req_id += 1
        # Send `initialized` notification (no id).
        await ws.send_str(json.dumps({"method": "initialized"}))
        return req_id

    # ── main run loop ─────────────────────────────────────────────────────

    async def run(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Start/reuse a codex app-server, run one turn, yield StreamEvents."""
        model_label = self.config.model or "(codex default)"
        start_time = time.monotonic()

        state = await self._get_or_start_server(context)
        ws_url = f"ws://127.0.0.1:{state.port}"

        logger.info(
            "Connecting to codex app-server",
            session_id=context.session_id,
            ws_url=ws_url,
            model=model_label,
        )

        file_logger = _SessionFileLogger(context.llm_session_id)
        message_count = 0
        state.active_turns += 1

        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(ws_url, heartbeat=30) as ws:
                req_id = 0

                # Handshake
                req_id = await self._initialize_ws(ws, req_id)

                # Obtain (or re-use) the codex thread.
                # TODO: On warm-server miss (new server after eviction/restart), consider
                # attempting to resume from context.llm_session_id before creating a fresh
                # thread, to restore prior conversation context. Requires investigating
                # whether codex app-server exposes a thread-resume/attach API.
                thread_id = state.thread_id
                if thread_id is None:
                    result = await _rpc(ws, "thread/start", self._build_thread_params(context), req_id)
                    req_id += 1
                    thread_id = result.get("thread", {}).get("id") or result.get("id") or ""
                    if not thread_id:
                        raise ValueError("thread/start returned no thread id")
                    state.thread_id = thread_id
                    logger.info(
                        "codex thread created",
                        session_id=context.session_id,
                        thread_id=thread_id,
                    )
                    # Consume the thread/started notification.
                    await _drain_until_notification(ws, "thread/started")

                # Start the turn.
                await _rpc(
                    ws,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    },
                    req_id,
                )
                req_id += 1

                # Stream notifications until turn/completed.
                async for method, params in _iter_notifications(ws):
                    if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                        message_count += 1

                    event = _notification_to_event(method, params, thread_id, message_count, start_time)
                    if event is None:
                        continue

                    excerpt = extract_excerpt(event)
                    file_logger.log(model_label, event.type, excerpt, llm_session_id=thread_id)

                    if AGENT_RESPONSE_DEBUG:
                        logger.info(
                            f"Received message from {model_label}: [{event.type}] {excerpt}",
                            session_id=context.session_id,
                        )

                    yield event

                    if method == "turn/completed":
                        break

            state.last_used = time.monotonic()

        except Exception as e:
            logger.error(
                "Error during codex app-server turn",
                session_id=context.session_id,
                error=str(e),
                exc_info=True,
            )
            yield StreamEvent(type="error", raw={"error": str(e)})
        finally:
            state.active_turns -= 1
            file_logger.close()
            # If the server process has died, remove it from the registry.
            # Guard by identity to avoid removing a newer server started by another coroutine.
            if state.proc.returncode is not None and _servers.get(context.session_id) is state:
                _servers.pop(context.session_id, None)
                _server_locks.pop(context.session_id, None)
                logger.warning(
                    "codex app-server exited unexpectedly",
                    session_id=context.session_id,
                    returncode=state.proc.returncode,
                )


# ── module-level async helpers ────────────────────────────────────────────────


_MAX_STDERR_BYTES: int = 64 * 1024  # 64 KB cap — prevents unbounded growth for long-lived servers


async def _drain_stderr(proc: asyncio.subprocess.Process, session_id: str) -> None:
    """Drain stderr to avoid pipe deadlock; log on non-empty output.

    Uses a bounded buffer to prevent unbounded memory growth — the codex app-server
    is a long-lived process and may produce substantial stderr over its lifetime.
    """
    assert proc.stderr is not None
    buf = bytearray()
    async for chunk in proc.stderr:
        if len(buf) < _MAX_STDERR_BYTES:
            buf.extend(chunk[: _MAX_STDERR_BYTES - len(buf)])
    stderr = buf.decode("utf-8", errors="replace")
    if stderr.strip():
        logger.info("codex app-server stderr", session_id=session_id, stderr=stderr[:500])


async def _drain_until_notification(
    ws: aiohttp.ClientWebSocketResponse,
    target_method: str,
    timeout: float = RPC_TIMEOUT_S,
) -> dict[str, Any]:
    """Consume WS frames until we receive a notification with the given method."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.receive(), timeout=max(0.1, deadline - time.monotonic()))
        if raw.type != aiohttp.WSMsgType.TEXT:
            continue
        data: dict[str, Any] = json.loads(raw.data)
        if data.get("method") == target_method and "id" not in data:
            params = data.get("params")
            return dict(params if params is not None else {})
    raise TimeoutError(f"Timed out waiting for '{target_method}' notification")


async def shutdown_codex_servers() -> None:
    """Terminate all running codex app-server processes on service shutdown.

    Should be called from the application lifespan shutdown sequence, similar
    to shutdown_streaming(), to avoid orphaned processes on deploy/restart.
    """
    global _eviction_task
    if _eviction_task and not _eviction_task.done():
        _eviction_task.cancel()
        try:
            await _eviction_task
        except asyncio.CancelledError:
            pass
    for sid, state in list(_servers.items()):
        if state.proc.returncode is None:
            logger.info("Shutting down codex app-server", session_id=sid, pid=state.proc.pid)
            try:
                state.proc.kill()
                await state.proc.wait()
            except (ProcessLookupError, OSError):
                pass
    _servers.clear()
    _server_locks.clear()


async def _iter_notifications(
    ws: aiohttp.ClientWebSocketResponse,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (method, params) for every server notification until WS closes."""
    async for msg in ws:
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise ConnectionError(f"WebSocket closed unexpectedly: {msg.type!r}")
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data: dict[str, Any] = json.loads(msg.data)
        method = data.get("method", "")
        # Server-requests (approval prompts) have both "method" and "id".
        # We're running with approvalPolicy="never" so we should never see them,
        # but guard defensively — decline and continue.
        if "id" in data and method:
            await ws.send_str(
                json.dumps(
                    {
                        "method": "serverRequest/response",
                        "id": data["id"],
                        "params": {"response": "decline"},
                    }
                )
            )
            continue
        # Skip pure responses (they have "id" but no "method").
        if not method:
            continue
        params_raw = data.get("params")
        params: dict[str, Any] = params_raw if params_raw is not None else {}
        yield method, params
