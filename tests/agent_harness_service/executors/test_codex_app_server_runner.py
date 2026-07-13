"""Tests for CodexAppServerRunner — WebSocket JSON-RPC 2.0 client.

Covers:
  - _notification_to_event: pure-function translation of WS notifications → StreamEvent
  - _build_server_args / _build_thread_params: parameter construction
  - Turn lifecycle (start → stream → complete) with mocked WebSocket
  - Abort / CancelledError handling
  - MCP server URL injection via build_codex_mcp_args
  - Factory registration constants (HARNESS_CODEX_CLI, HARNESS_CODEX_APP_SERVER)
"""

from __future__ import annotations
import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from ypl.agent_harness_service.common.config import AgentConfig, SandboxConfig
from ypl.agent_harness_service.common.constants import (
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
)
from ypl.agent_harness_service.common.models import ExecutorConfig
from ypl.agent_harness_service.executors.codex_app_server_runner import (
    CodexAppServerRunner,
    _CodexServerState,
    _notification_to_event,
    _server_locks,
    _servers,
    shutdown_codex_servers,
)
from ypl.agent_harness_service.executors.runner import AgentRunner, RunContext

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "name": "codex-app-test",
        "config_dir": "/data/ahs/agents/codex-app-test",
        "executor_config": ExecutorConfig(type="harnessed", model=HARNESS_CODEX_CLI),
        "llm_model": "o3-mini",
        "default_repo": "yupp-agent",
        "tool_permissions": {"*": "allow"},
        "sandbox": SandboxConfig(enabled=True, auto_allow_bash_if_sandboxed=True),
        "max_turns": 10,
        "max_budget_usd": 1.0,
        "has_mcp": False,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {
        "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "workspace": "/data/ahs/sessions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "llm_session_id": None,
        "session_context": {
            "permissions": {
                "allowed_servers": ["harness", "platform-mcp-server"],
                "allowed_harness_tools": ["*"],
            }
        },
    }
    defaults.update(overrides)
    return RunContext(**defaults)


def _make_server_state(port: int = 59999, thread_id: str | None = None) -> _CodexServerState:
    """Build a fake _CodexServerState with a mock process."""
    proc = AsyncMock()
    proc.returncode = None
    proc.pid = 99999
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    state = _CodexServerState(proc=proc, port=port)
    state.thread_id = thread_id
    return state


class _FakeWS:
    """Fake WebSocket that replays a pre-loaded list of JSON messages.

    Supports both receive() (used by _rpc / _drain_until_notification) and
    async iteration (used by _iter_notifications).  Both paths share the same
    message queue so the delivery order is deterministic.
    """

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._msgs: list[aiohttp.WSMessage] = [
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(m), None) for m in messages
        ]
        self._idx = 0
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive(self) -> aiohttp.WSMessage:
        if self._idx < len(self._msgs):
            msg = self._msgs[self._idx]
            self._idx += 1
            return msg
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)

    @property
    def closed(self) -> bool:
        return False

    async def close(self) -> None:
        pass

    def __aiter__(self) -> _FakeWSIter:
        return _FakeWSIter(self)

    # Context-manager support (kept for backwards compat with any remaining uses)
    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakeWSIter:
    """Async iterator over _FakeWS messages; stops on CLOSED."""

    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    def __aiter__(self) -> _FakeWSIter:
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return msg


def _make_full_turn_messages(
    thread_id: str = "t-abc",
    assistant_text: str = "Hello from Codex!",
) -> list[dict[str, Any]]:
    """Build the full server-side message sequence for one successful turn.

    Message order:
      id=0 → initialize response
      id=1 → thread/start response
             thread/started notification
      id=2 → turn/start response
             item/started (commandExecution)
             item/completed (commandExecution)
             item/completed (agentMessage)
             turn/completed
    """
    return [
        # initialize response
        {"id": 0, "result": {"serverInfo": {"name": "codex-app-server", "version": "0.1"}}},
        # thread/start response
        {"id": 1, "result": {"thread": {"id": thread_id}}},
        # thread/started notification (consumed by _drain_until_notification)
        {"method": "thread/started", "params": {"thread": {"id": thread_id}}},
        # turn/start response
        {"id": 2, "result": {}},
        # turn/started notification (no-op in _notification_to_event)
        {"method": "turn/started", "params": {}},
        # Bash tool started
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "item_1",
                    "type": "commandExecution",
                    "command": "ls /workspace",
                }
            },
        },
        # Bash tool completed
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "item_1",
                    "type": "commandExecution",
                    "aggregatedOutput": "README.md\nsrc/\n",
                }
            },
        },
        # Agent message
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "item_2",
                    "type": "agentMessage",
                    "text": assistant_text,
                }
            },
        },
        # turn/completed
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "completed"}},
        },
    ]


def _patch_file_logger() -> Any:
    return patch(
        "ypl.agent_harness_service.executors.codex_app_server_runner._SessionFileLogger",
        return_value=MagicMock(),
    )


def _patch_system_prompt(text: str = "You are a test agent.") -> Any:
    return patch(
        "ypl.agent_harness_service.executors.codex_app_server_runner.build_system_prompt",
        return_value=text,
    )


def _patch_mcp_args(extra_args: list[str] | None = None) -> Any:
    return patch(
        "ypl.agent_harness_service.executors.codex_app_server_runner.build_codex_mcp_args",
        return_value=extra_args or [],
    )


def _patch_mcp_env() -> Any:
    return patch(
        "ypl.agent_harness_service.executors.codex_app_server_runner.build_codex_mcp_env",
        return_value={},
    )


# ---------------------------------------------------------------------------
# _notification_to_event tests (pure function — no mocking needed)
# ---------------------------------------------------------------------------


class TestNotificationToEvent:
    """Direct unit tests for _notification_to_event."""

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        return _notification_to_event(method, params, thread_id="t-123", message_count=0, start_time=time.monotonic())

    def test_turn_started_returns_none(self) -> None:
        assert self._call("turn/started", {}) is None

    def test_item_started_command_execution_yields_tool_use(self) -> None:
        params = {"item": {"id": "i1", "type": "commandExecution", "command": "echo hi"}}
        event = self._call("item/started", params)
        assert event is not None
        assert event.type == "tool_use"
        assert event.raw["tool_name"] == "Bash"
        assert event.raw["input"] == "echo hi"

    def test_item_started_mcp_tool_call_yields_tool_use(self) -> None:
        params = {
            "item": {
                "id": "i2",
                "type": "mcpToolCall",
                "tool": "search_gcp_logs",
                "arguments": {"query": "ERROR"},
            }
        }
        event = self._call("item/started", params)
        assert event is not None
        assert event.type == "tool_use"
        assert event.raw["tool_name"] == "search_gcp_logs"
        assert event.raw["input"] == {"query": "ERROR"}

    def test_item_started_file_change_yields_tool_use(self) -> None:
        params = {"item": {"id": "i3", "type": "fileChange", "kind": "edit", "path": "main.py"}}
        event = self._call("item/started", params)
        assert event is not None
        assert event.type == "tool_use"
        assert event.raw["tool_name"] == "FileChange"
        assert "edit main.py" in event.raw["input"]

    def test_item_completed_command_execution_yields_tool_result(self) -> None:
        params = {
            "item": {
                "id": "i1",
                "type": "commandExecution",
                "aggregatedOutput": "hello\nworld",
            }
        }
        event = self._call("item/completed", params)
        assert event is not None
        assert event.type == "tool_result"
        assert event.raw["tool_use_id"] == "i1"
        assert event.raw["content"] == "hello\nworld"

    def test_item_completed_agent_message_yields_assistant(self) -> None:
        params = {"item": {"id": "i2", "type": "agentMessage", "text": "I'm done."}}
        event = self._call("item/completed", params)
        assert event is not None
        assert event.type == "assistant"
        assert event.text == "I'm done."

    def test_item_completed_mcp_tool_call_with_error(self) -> None:
        params = {
            "item": {
                "id": "i3",
                "type": "mcpToolCall",
                "error": {"message": "Rate limit hit"},
                "status": "failed",
            }
        }
        event = self._call("item/completed", params)
        assert event is not None
        assert event.type == "tool_result"
        assert event.raw["is_error"] is True
        assert "Rate limit hit" in event.raw["content"]

    def test_item_completed_reasoning_returns_none(self) -> None:
        params = {"item": {"id": "i4", "type": "reasoning", "text": "Let me think..."}}
        assert self._call("item/completed", params) is None

    def test_turn_completed_success_yields_result(self) -> None:
        params = {"turn": {"status": "completed"}}
        start = time.monotonic() - 0.5  # 500ms ago
        event = _notification_to_event("turn/completed", params, "t-123", 2, start)
        assert event is not None
        assert event.type == "result"
        assert event.session_id == "t-123"
        assert event.num_turns == 2
        assert event.duration_ms is not None and event.duration_ms >= 400

    def test_turn_completed_failed_yields_error(self) -> None:
        params = {"turn": {"status": "failed", "error": {"message": "Context window exceeded"}}}
        event = self._call("turn/completed", params)
        assert event is not None
        assert event.type == "error"
        assert "Context window exceeded" in event.raw["error"]

    def test_unknown_method_returns_none(self) -> None:
        assert self._call("some/unknown/method", {}) is None


# ---------------------------------------------------------------------------
# _build_server_args / _build_thread_params tests
# ---------------------------------------------------------------------------


class TestBuildArgs:
    def test_build_server_args_contains_codex_app_server(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        with _patch_mcp_args():
            args = runner._build_server_args(12345, ctx)
        assert args[0] == "codex"
        assert args[1] == "app-server"
        assert "--listen" in args
        listen_idx = args.index("--listen")
        assert "ws://127.0.0.1:12345" == args[listen_idx + 1]

    def test_build_server_args_includes_mcp_args(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        fake_mcp_args = ["-c", 'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"']
        with _patch_mcp_args(fake_mcp_args):
            args = runner._build_server_args(9000, ctx)
        assert "-c" in args
        assert 'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"' in args

    def test_build_thread_params_includes_cwd(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context(workspace="/data/ahs/sessions/my-session")
        with _patch_system_prompt("test system prompt"):
            params = runner._build_thread_params(ctx)
        assert params["cwd"] == "/data/ahs/sessions/my-session"

    def test_build_thread_params_includes_model(self) -> None:
        runner = CodexAppServerRunner(_make_config(llm_model="o3"))
        ctx = _make_context()
        with _patch_system_prompt(""):
            params = runner._build_thread_params(ctx)
        assert params.get("model") == "o3"

    def test_build_thread_params_sets_sandbox(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        with _patch_system_prompt(""):
            params = runner._build_thread_params(ctx)
        assert params.get("sandbox") == "workspace-write"

    def test_build_thread_params_sets_developer_instructions(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        with _patch_system_prompt("You are a helpful agent."):
            params = runner._build_thread_params(ctx)
        assert "You are a helpful agent." in params.get("developerInstructions", "")
        assert "Codex Runtime Notes" in params.get("developerInstructions", "")

    def test_build_thread_params_includes_codex_instructions_when_base_prompt_empty(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        with _patch_system_prompt(""):
            params = runner._build_thread_params(ctx)
        assert "Codex Runtime Notes" in params.get("developerInstructions", "")


# ---------------------------------------------------------------------------
# Turn lifecycle tests (mocked WebSocket)
# ---------------------------------------------------------------------------


def _make_aiohttp_mock(fake_ws: _FakeWS) -> tuple[Any, Any]:
    """Return (mock_session, mock_cls) for patching aiohttp.ClientSession.

    _get_or_connect_ws calls ``aiohttp.ClientSession()`` (no CM) then
    ``await http_session.ws_connect(url, ...)``.
    """
    mock_session = MagicMock()
    mock_session.ws_connect = AsyncMock(return_value=fake_ws)
    mock_session.closed = False
    mock_session.close = AsyncMock()

    mock_cls = MagicMock(return_value=mock_session)
    return mock_session, mock_cls


class TestTurnLifecycle:
    """Full turn flow tests using mocked WebSocket."""

    @pytest.mark.asyncio
    async def test_full_turn_yields_expected_event_types(self) -> None:
        """Complete turn: tool_use, tool_result, assistant, result events in order."""
        messages = _make_full_turn_messages(thread_id="t-xyz", assistant_text="Files listed.")
        fake_ws = _FakeWS(messages)
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        state = _make_server_state()
        runner = CodexAppServerRunner(_make_config())

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("ls /workspace", _make_context())]

        types = [e.type for e in collected]
        assert "tool_use" in types
        assert "tool_result" in types
        assert "assistant" in types
        assert "result" in types
        # No error events in a successful turn
        assert "error" not in types

    @pytest.mark.asyncio
    async def test_assistant_text_extracted_correctly(self) -> None:
        messages = _make_full_turn_messages(assistant_text="The answer is 42.")
        fake_ws = _FakeWS(messages)
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        state = _make_server_state()
        runner = CodexAppServerRunner(_make_config())

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("answer?", _make_context())]

        assistant_events = [e for e in collected if e.type == "assistant"]
        assert len(assistant_events) == 1
        assert assistant_events[0].text == "The answer is 42."

    @pytest.mark.asyncio
    async def test_result_event_has_thread_id_and_duration(self) -> None:
        messages = _make_full_turn_messages(thread_id="t-result-test")
        fake_ws = _FakeWS(messages)
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        state = _make_server_state()
        runner = CodexAppServerRunner(_make_config())

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("run", _make_context())]

        result_events = [e for e in collected if e.type == "result"]
        assert len(result_events) == 1
        r = result_events[0]
        assert r.session_id == "t-result-test"
        assert r.duration_ms is not None
        assert r.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_failed_turn_yields_error_event(self) -> None:
        messages: list[dict[str, Any]] = [
            {"id": 0, "result": {}},  # initialize
            {"id": 1, "result": {"thread": {"id": "t-fail"}}},  # thread/start
            {"method": "thread/started", "params": {}},
            {"id": 2, "result": {}},  # turn/start
            {
                "method": "turn/completed",
                "params": {"turn": {"status": "failed", "error": {"message": "Model overloaded"}}},
            },
        ]
        fake_ws = _FakeWS(messages)
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        state = _make_server_state()
        runner = CodexAppServerRunner(_make_config())

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("run", _make_context())]

        error_events = [e for e in collected if e.type == "error"]
        assert len(error_events) == 1
        assert "Model overloaded" in error_events[0].raw["error"]

    @pytest.mark.asyncio
    async def test_thread_id_stored_in_server_state(self) -> None:
        """Second turn reuses the thread_id from state (no thread/start on warm server)."""
        messages_first_turn = _make_full_turn_messages(thread_id="t-warm")
        fake_ws = _FakeWS(messages_first_turn)
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        state = _make_server_state()
        assert state.thread_id is None  # fresh server

        runner = CodexAppServerRunner(_make_config())
        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            _ = [e async for e in runner._run_once("turn 1", _make_context())]

        # After the first turn, thread_id should be persisted on the state object
        assert state.thread_id == "t-warm"

    @pytest.mark.asyncio
    async def test_warm_ws_skips_thread_start(self) -> None:
        """If state already has a persistent WS + thread_id, no initialize/thread/start is sent."""
        # Warm turn messages: only turn/start response + turn/completed (no handshake)
        messages_warm: list[dict[str, Any]] = [
            {"id": 3, "result": {}},  # turn/start (req_id=3 from prior turns)
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ]
        fake_ws = _FakeWS(messages_warm)

        # Pre-warm: state already has ws + thread_id (persistent connection from prior turn)
        state = _make_server_state(thread_id="t-existing")
        state.ws = fake_ws  # type: ignore[assignment]
        state.req_id = 3

        runner = CodexAppServerRunner(_make_config())
        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("turn 2", _make_context())]

        # No initialize or thread/start — only turn/start
        sent_methods = [json.loads(m).get("method") for m in fake_ws.sent]
        assert "thread/start" not in sent_methods
        assert "initialize" not in sent_methods
        assert "turn/start" in sent_methods

        result_events = [e for e in collected if e.type == "result"]
        assert len(result_events) == 1


# ---------------------------------------------------------------------------
# Abort / cancellation tests
# ---------------------------------------------------------------------------


class TestAbortFlow:
    @pytest.mark.asyncio
    async def test_active_turns_decremented_on_cancellation(self) -> None:
        """active_turns must reach 0 after cancellation (via finally block)."""
        # Use an event to pause the WS iteration mid-stream
        pause = asyncio.Event()

        async def slow_notifications(ws: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
            await pause.wait()  # blocks until cancelled
            yield "turn/completed", {"turn": {"status": "completed"}}  # unreachable

        state = _make_server_state()
        assert state.active_turns == 0

        runner = CodexAppServerRunner(_make_config())

        async def run_and_collect() -> list[Any]:
            return [e async for e in runner._run_once("prompt", _make_context())]

        fake_ws = _FakeWS([{"id": 0, "result": {}}, {"id": 1, "result": {"thread": {"id": "t-x"}}}])
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._iter_notifications",
                side_effect=lambda ws: slow_notifications(ws),
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._drain_until_notification",
                new_callable=AsyncMock,
                return_value={},
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            task = asyncio.create_task(run_and_collect())
            # Give the task time to enter the WS loop and increment active_turns
            await asyncio.sleep(0.05)
            # Cancel the task (simulates /stop)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # The finally block must have decremented active_turns back to 0
        assert state.active_turns == 0

    @pytest.mark.asyncio
    async def test_ws_connection_error_yields_error_event(self) -> None:
        """A WS connection error during the turn yields a StreamEvent(type=error)."""
        state = _make_server_state()
        runner = CodexAppServerRunner(_make_config())

        async def failing_notifications(ws: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
            # Yield a no-op notification first (to satisfy the async-generator type),
            # then raise — _notification_to_event returns None for "turn/started" so
            # no event is emitted before the error propagates.
            yield "turn/started", {}
            raise ConnectionError("WS closed unexpectedly")

        # Provide initialize + thread/start + turn/start RPC responses so that
        # _run_once reaches the _iter_notifications call before the error fires.
        fake_ws = _FakeWS(
            [
                {"id": 0, "result": {}},  # initialize
                {"id": 1, "result": {"thread": {"id": "t-err"}}},  # thread/start
                {"id": 2, "result": {}},  # turn/start
            ]
        )
        mock_session, mock_cls = _make_aiohttp_mock(fake_ws)

        with (
            patch.object(runner, "_get_or_start_server", return_value=state),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.aiohttp.ClientSession",
                mock_cls,
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._iter_notifications",
                side_effect=lambda ws: failing_notifications(ws),
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._drain_until_notification",
                new_callable=AsyncMock,
                return_value={},
            ),
            _patch_file_logger(),
            _patch_system_prompt(),
        ):
            collected = [e async for e in runner._run_once("prompt", _make_context())]

        error_events = [e for e in collected if e.type == "error"]
        assert len(error_events) == 1
        assert "WS closed unexpectedly" in error_events[0].raw["error"]
        # active_turns must be reset even after an error
        assert state.active_turns == 0


# ---------------------------------------------------------------------------
# MCP server URL injection tests
# ---------------------------------------------------------------------------


class TestMcpServerUrlInjection:
    def test_build_server_args_calls_build_codex_mcp_args_with_session_id(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context(session_id="ffffffff-0000-1111-2222-333333333333")

        with patch(
            "ypl.agent_harness_service.executors.codex_app_server_runner.build_codex_mcp_args",
            return_value=[],
        ) as mock_mcp:
            runner._build_server_args(8888, ctx)

        mock_mcp.assert_called_once()
        call_kwargs = mock_mcp.call_args
        assert call_kwargs.kwargs.get("session_id") == "ffffffff-0000-1111-2222-333333333333"

    def test_build_server_args_passes_agent_and_external_mcp_context(self) -> None:
        runner = CodexAppServerRunner(_make_config(name="veronia", external_mcps=["gmail"]))
        ctx = _make_context(session_context={"user_id": "user-1"})

        with patch(
            "ypl.agent_harness_service.executors.codex_app_server_runner.build_codex_mcp_args",
            return_value=[],
        ) as mock_mcp:
            runner._build_server_args(8888, ctx)

        mock_mcp.assert_called_once()
        assert mock_mcp.call_args.kwargs["agent_name"] == "veronia"
        assert mock_mcp.call_args.kwargs["external_mcps"] == ["gmail"]

    def test_build_system_prompt_uses_load_skill_catalog_not_native_skills(self) -> None:
        runner = CodexAppServerRunner(_make_config(required_tools=["mcp__harness__read_slack_thread"]))
        ctx = _make_context()

        with patch(
            "ypl.agent_harness_service.executors.codex_app_server_runner.build_system_prompt",
            return_value="base prompt",
        ) as mock_prompt:
            prompt = runner._build_system_prompt(ctx)

        assert "Codex Runtime Notes" in prompt
        assert "load_skill" in prompt
        mock_prompt.assert_called_once()
        assert mock_prompt.call_args.kwargs["has_native_skills"] is False
        assert mock_prompt.call_args.kwargs["required_tools"] is None

    def test_build_server_args_harness_and_platform_urls_in_args(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()
        mcp_args = [
            "-c",
            'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"',
            "-c",
            'mcp_servers.platform.url="https://mcp.example.com/"',
        ]
        with _patch_mcp_args(mcp_args):
            args = runner._build_server_args(9090, ctx)

        assert 'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"' in args
        assert 'mcp_servers.platform.url="https://mcp.example.com/"' in args

    @pytest.mark.asyncio
    async def test_spawn_passes_mcp_env_to_subprocess(self) -> None:
        """build_codex_mcp_env result is merged into subprocess env."""
        runner = CodexAppServerRunner(_make_config())
        ctx = _make_context()

        # The _get_or_start_server path spawns a subprocess; we mock
        # asyncio.create_subprocess_exec and _wait_for_ready to avoid real I/O.
        fake_proc = AsyncMock()
        fake_proc.returncode = None
        fake_proc.pid = 11111
        fake_proc.stderr = AsyncMock()
        fake_proc.stderr.__aiter__ = MagicMock(return_value=iter([]))

        with (
            patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_spawn,
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._wait_for_ready",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._ensure_eviction_loop",
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._find_free_port",
                return_value=61234,
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.build_codex_mcp_env",
                return_value={"AHS_MCP_BEARER": "some-token"},
            ),
            _patch_mcp_args(),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner.build_subprocess_env",
                return_value={"PATH": "/usr/bin"},
            ),
            patch(
                "ypl.agent_harness_service.executors.codex_app_server_runner._drain_stderr",
                new_callable=AsyncMock,
            ),
        ):
            # Clean slate for this session
            _servers.pop(ctx.session_id, None)
            _server_locks.pop(ctx.session_id, None)

            await runner._get_or_start_server(ctx)

        mock_spawn.assert_called_once()
        _, call_kwargs = mock_spawn.call_args[0], mock_spawn.call_args[1]
        env = call_kwargs.get("env", {})
        assert env.get("AHS_MCP_BEARER") == "some-token"

        # Clean up registry after test
        _servers.pop(ctx.session_id, None)
        _server_locks.pop(ctx.session_id, None)


# ---------------------------------------------------------------------------
# Factory registration tests
# ---------------------------------------------------------------------------


class TestFactoryRegistration:
    def test_harness_codex_cli_constant_exists(self) -> None:
        assert HARNESS_CODEX_CLI == "codex-cli"

    def test_harness_codex_app_server_constant_exists(self) -> None:
        assert HARNESS_CODEX_APP_SERVER == "codex-app-server"

    def test_codex_app_server_runner_is_agent_runner(self) -> None:
        runner = CodexAppServerRunner(_make_config())
        assert isinstance(runner, AgentRunner)

    def test_codex_app_server_runner_with_app_server_model(self) -> None:
        config = _make_config(executor_config=ExecutorConfig(type="harnessed", model=HARNESS_CODEX_APP_SERVER))
        runner = CodexAppServerRunner(config)
        assert isinstance(runner, AgentRunner)

    def test_run_task_codex_cli_maps_to_codex_app_server_runner(self) -> None:
        """run_task.py factory: codex-cli model → CodexAppServerRunner."""
        from ypl.agent_harness_service.common.constants import (
            HARNESS_CODEX_APP_SERVER,
            HARNESS_CODEX_CLI,
        )
        from ypl.agent_harness_service.executors.codex_app_server_runner import CodexAppServerRunner

        # Both codex-cli and codex-app-server must resolve to CodexAppServerRunner.
        # We verify the constants + class names are consistent, not the full dispatch
        # (which requires a DB session and is covered by integration tests).
        assert HARNESS_CODEX_CLI == "codex-cli"
        assert HARNESS_CODEX_APP_SERVER == "codex-app-server"
        assert CodexAppServerRunner.__name__ == "CodexAppServerRunner"

    def test_codex_app_server_port_default(self) -> None:
        from ypl.agent_harness_service.common.constants import CODEX_APP_SERVER_PORT

        assert CODEX_APP_SERVER_PORT == 8765


# ---------------------------------------------------------------------------
# shutdown_codex_servers tests
# ---------------------------------------------------------------------------


class TestShutdownCodexServers:
    @pytest.mark.asyncio
    async def test_shutdown_kills_all_running_servers(self) -> None:
        """shutdown_codex_servers() kills all registered server processes."""
        sid1, sid2 = "ssss1111-0000-0000-0000-000000000000", "ssss2222-0000-0000-0000-000000000000"

        proc1 = AsyncMock()
        proc1.returncode = None
        proc1.kill = MagicMock()
        proc1.wait = AsyncMock()

        proc2 = AsyncMock()
        proc2.returncode = None
        proc2.kill = MagicMock()
        proc2.wait = AsyncMock()

        _servers[sid1] = _CodexServerState(proc=proc1, port=19001)
        _servers[sid2] = _CodexServerState(proc=proc2, port=19002)

        await shutdown_codex_servers()

        proc1.kill.assert_called_once()
        proc2.kill.assert_called_once()
        assert sid1 not in _servers
        assert sid2 not in _servers

    @pytest.mark.asyncio
    async def test_shutdown_skips_already_dead_servers(self) -> None:
        sid = "dddd0000-0000-0000-0000-000000000000"

        proc = AsyncMock()
        proc.returncode = 1  # already dead
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        _servers[sid] = _CodexServerState(proc=proc, port=19003)

        await shutdown_codex_servers()

        proc.kill.assert_not_called()
        assert sid not in _servers
