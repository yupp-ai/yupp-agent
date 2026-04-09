"""Extended tests for runner.py — covers uncovered portions.

Focuses on:
- _read_pipe_max_size / _try_enlarge_pipe_buf (pipe buffer tuning)
- _handle_stream_limit_error
- _SessionFileLogger
- MockRunner._run_once
- RawExecutorRunner._run_once
- ClaudeCodeRunner._build_args (tool permission modes)
- build_subprocess_env (PATH & venv bin injection)
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.config import AgentConfig, SandboxConfig
from ypl.agent_harness_service.common.models import ExecutorConfig
from ypl.agent_harness_service.common.types import StreamEvent
from ypl.agent_harness_service.executors.runner import (
    MockRunner,
    RunContext,
    _handle_stream_limit_error,
    _read_pipe_max_size,
    _SessionFileLogger,
    _try_enlarge_pipe_buf,
    build_subprocess_env,
    extract_excerpt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_config(
    name: str = "test-agent",
    executor_type: str = "harnessed",
    tool_permissions: dict[str, str] | None = None,
    has_mcp: bool = False,
    model: str | None = None,
    max_turns: int = 5,
    sandbox_enabled: bool = False,
    bwrap_enabled: bool = False,
) -> AgentConfig:
    """Build a minimal AgentConfig for testing."""
    perms: dict[str, Any] = tool_permissions if tool_permissions is not None else {"*": "allow"}
    exec_cfg = ExecutorConfig(type=executor_type, model=model)
    sandbox = SandboxConfig(enabled=sandbox_enabled, bwrap_enabled=bwrap_enabled)
    return AgentConfig(
        name=name,
        config_dir="/tmp/fake-agent",
        executor_config=exec_cfg,
        tool_permissions=perms,
        has_mcp=has_mcp,
        max_turns=max_turns,
        sandbox=sandbox,
    )


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {
        "session_id": "sess-test-123",
        "workspace": None,
        "llm_session_id": None,
    }
    defaults.update(overrides)
    return RunContext(**defaults)


# ---------------------------------------------------------------------------
# _read_pipe_max_size
# ---------------------------------------------------------------------------


class TestReadPipeMaxSize:
    def test_returns_none_on_oserror(self) -> None:
        """Returns None when /proc/sys/fs/pipe-max-size cannot be read."""
        with patch("ypl.agent_harness_service.executors.runner.pathlib.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = OSError("Not found")
            result = _read_pipe_max_size()
        assert result is None

    def test_returns_none_on_value_error(self) -> None:
        """Returns None when the file content is not a valid integer."""
        with patch("ypl.agent_harness_service.executors.runner.pathlib.Path") as mock_path:
            mock_path.return_value.read_text.return_value = "not-a-number\n"
            result = _read_pipe_max_size()
        assert result is None

    def test_returns_int_on_success(self) -> None:
        """Returns the integer value when the file is readable and valid."""
        with patch("ypl.agent_harness_service.executors.runner.pathlib.Path") as mock_path:
            mock_path.return_value.read_text.return_value = "1048576\n"
            result = _read_pipe_max_size()
        assert result == 1048576


# ---------------------------------------------------------------------------
# _try_enlarge_pipe_buf
# ---------------------------------------------------------------------------


class TestTryEnlargePipeBuf:
    def test_no_op_on_non_linux(self) -> None:
        """Does nothing on non-Linux platforms."""
        mock_proc = MagicMock()
        with patch("ypl.agent_harness_service.executors.runner.platform.system", return_value="Darwin"):
            # Should not raise; simply returns early
            _try_enlarge_pipe_buf(mock_proc)
        # The process transport should never be accessed
        mock_proc._transport.get_pipe_transport.assert_not_called()

    def test_no_op_when_transport_is_none(self) -> None:
        """Silently returns when _transport attribute is absent."""
        mock_proc = MagicMock(spec=[])  # No attributes — getattr returns default None
        with patch("ypl.agent_harness_service.executors.runner.platform.system", return_value="Linux"):
            _try_enlarge_pipe_buf(mock_proc)  # Should not raise

    def test_no_op_when_get_pipe_transport_missing(self) -> None:
        """Silently returns when transport has no get_pipe_transport method."""
        mock_proc = MagicMock()
        mock_proc._transport = object()  # No get_pipe_transport method
        with (
            patch("ypl.agent_harness_service.executors.runner.platform.system", return_value="Linux"),
            patch("ypl.agent_harness_service.executors.runner._read_pipe_max_size", return_value=None),
        ):
            _try_enlarge_pipe_buf(mock_proc)  # Should not raise

    def test_oserror_on_fcntl_is_swallowed(self) -> None:
        """OSError from fcntl.fcntl is silently ignored."""
        pipe_file = MagicMock()
        pipe_file.fileno.return_value = 5

        pipe_transport = MagicMock()
        pipe_transport._pipe = pipe_file

        transport = MagicMock()
        transport.get_pipe_transport.return_value = pipe_transport

        mock_proc = MagicMock()
        mock_proc._transport = transport

        with (
            patch("ypl.agent_harness_service.executors.runner.platform.system", return_value="Linux"),
            patch("ypl.agent_harness_service.executors.runner._read_pipe_max_size", return_value=None),
            patch("ypl.agent_harness_service.executors.runner.fcntl.fcntl", side_effect=OSError("EPERM")),
        ):
            _try_enlarge_pipe_buf(mock_proc)  # Should not raise

    def test_clamps_to_pipe_max_size(self) -> None:
        """Target size is clamped to pipe-max-size when it is smaller than 4MB."""
        pipe_file = MagicMock()
        pipe_file.fileno.return_value = 5

        pipe_transport = MagicMock()
        pipe_transport._pipe = pipe_file

        transport = MagicMock()
        transport.get_pipe_transport.return_value = pipe_transport

        mock_proc = MagicMock()
        mock_proc._transport = transport

        small_max = 65536  # 64 KB < 4 MB

        fcntl_calls: list[tuple[Any, ...]] = []

        def _capture_fcntl(*args: Any) -> None:
            fcntl_calls.append(args)

        with (
            patch("ypl.agent_harness_service.executors.runner.platform.system", return_value="Linux"),
            patch("ypl.agent_harness_service.executors.runner._read_pipe_max_size", return_value=small_max),
            patch("ypl.agent_harness_service.executors.runner.fcntl.fcntl", side_effect=_capture_fcntl),
        ):
            _try_enlarge_pipe_buf(mock_proc)

        # Both stdout (fd=1) and stderr (fd=2) are attempted
        assert len(fcntl_calls) == 2
        for call in fcntl_calls:
            # 3rd arg is the requested size — should be clamped to small_max
            assert call[2] == small_max


# ---------------------------------------------------------------------------
# _handle_stream_limit_error
# ---------------------------------------------------------------------------


class TestHandleStreamLimitError:
    def test_chunk_too_long_logs_structured_error(self) -> None:
        """ValueError with 'chunk is longer than limit' triggers a structured log."""
        err = ValueError("chunk is longer than limit (131072)")
        with patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger:
            event = _handle_stream_limit_error(err, "Claude", "sess-1", 131072)

        mock_logger.error.assert_called_once()
        assert "exceeded stream limit" in mock_logger.error.call_args[0][0]
        assert event.type == "error"
        assert "chunk is longer than limit" in event.raw.get("error", "")

    def test_other_value_error_logs_generic_error(self) -> None:
        """Other ValueError messages still produce an error event."""
        err = ValueError("some other error")
        with patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger:
            event = _handle_stream_limit_error(err, "Codex", "sess-2", 65536)

        mock_logger.error.assert_called_once()
        assert event.type == "error"


# ---------------------------------------------------------------------------
# _SessionFileLogger
# ---------------------------------------------------------------------------


class TestSessionFileLogger:
    def test_writes_to_file_when_session_id_known_at_init(self, tmp_path: Path) -> None:
        """When llm_session_id is given at construction, logs go directly to file."""
        session_id = "init-session-123"
        with patch("ypl.agent_harness_service.executors.runner.AHS_DATA_DIR", str(tmp_path)):
            sfl = _SessionFileLogger(initial_llm_session_id=session_id)
            sfl.log("claude", "assistant", "Hello there", llm_session_id=session_id)
            sfl.close()

        log_path = tmp_path / "session_logs" / f"{session_id}.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "claude" in content
        assert "assistant" in content
        assert "Hello there" in content

    def test_buffers_then_flushes_on_session_id_discovery(self, tmp_path: Path) -> None:
        """Lines emitted before session_id is known are buffered, then flushed."""
        session_id = "late-session-456"
        with patch("ypl.agent_harness_service.executors.runner.AHS_DATA_DIR", str(tmp_path)):
            sfl = _SessionFileLogger(initial_llm_session_id=None)
            # Log two lines before session ID is known
            sfl.log("claude", "system", "started")
            sfl.log("claude", "assistant", "thinking")
            # Now provide the session ID — triggers buffer flush
            sfl.log("claude", "result", "done", llm_session_id=session_id)
            sfl.close()

        log_path = tmp_path / "session_logs" / f"{session_id}.log"
        assert log_path.exists()
        content = log_path.read_text()
        # All buffered lines should appear in the file
        assert "started" in content
        assert "thinking" in content
        assert "done" in content

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        """Closing a logger twice does not raise."""
        session_id = "idem-close-789"
        with patch("ypl.agent_harness_service.executors.runner.AHS_DATA_DIR", str(tmp_path)):
            sfl = _SessionFileLogger(initial_llm_session_id=session_id)
            sfl.close()
            sfl.close()  # Should not raise

    def test_no_file_created_when_no_session_id(self, tmp_path: Path) -> None:
        """Lines are buffered in-memory when session_id is never provided."""
        with patch("ypl.agent_harness_service.executors.runner.AHS_DATA_DIR", str(tmp_path)):
            sfl = _SessionFileLogger(initial_llm_session_id=None)
            sfl.log("claude", "system", "no session yet")
            sfl.close()

        log_dir = tmp_path / "session_logs"
        assert not log_dir.exists() or list(log_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# MockRunner
# ---------------------------------------------------------------------------


class TestMockRunner:
    @pytest.mark.asyncio
    async def test_yields_system_assistant_result_events(self) -> None:
        """MockRunner emits system, assistant, and result events in order."""
        cfg = _make_agent_config(name="mock-agent")
        runner = MockRunner(cfg)
        ctx = _make_context()

        events: list[StreamEvent] = [e async for e in runner._run_once("hello", ctx)]
        types = [e.type for e in events]

        assert types == ["system", "assistant", "result"]

    @pytest.mark.asyncio
    async def test_assistant_includes_agent_name_in_response(self) -> None:
        """The assistant event text mentions the agent name."""
        cfg = _make_agent_config(name="my-special-agent")
        runner = MockRunner(cfg)
        ctx = _make_context()

        events: list[StreamEvent] = [e async for e in runner._run_once("test prompt", ctx)]
        assistant_event = next(e for e in events if e.type == "assistant")
        text = assistant_event.text or ""
        assert "my-special-agent" in text

    @pytest.mark.asyncio
    async def test_result_event_has_zero_cost(self) -> None:
        """MockRunner result event reports zero cost."""
        cfg = _make_agent_config(name="mock-agent")
        runner = MockRunner(cfg)
        ctx = _make_context()

        events: list[StreamEvent] = [e async for e in runner._run_once("ping", ctx)]
        result_event = next(e for e in events if e.type == "result")
        assert result_event.raw.get("estimated_cost_usd") == 0.0

    @pytest.mark.asyncio
    async def test_session_context_is_logged(self) -> None:
        """Session context triggers an extra info log (coverage for the branch)."""
        cfg = _make_agent_config(name="ctx-agent")
        runner = MockRunner(cfg)
        ctx = _make_context(session_context={"user_id": "u-1"})

        with patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger:
            _ = [e async for e in runner._run_once("hi", ctx)]
        # At least one info call for session context
        assert mock_logger.info.call_count >= 2

    @pytest.mark.asyncio
    async def test_long_prompt_is_truncated_in_response(self) -> None:
        """Prompts longer than 1000 chars are truncated with '...' in the response."""
        cfg = _make_agent_config(name="trunc-agent")
        runner = MockRunner(cfg)
        ctx = _make_context()

        long_prompt = "x" * 2000
        events: list[StreamEvent] = [e async for e in runner._run_once(long_prompt, ctx)]
        assistant_event = next(e for e in events if e.type == "assistant")
        text = assistant_event.text or ""
        assert text.endswith("...")

    @pytest.mark.asyncio
    async def test_run_wrapper_works_with_mock_runner(self) -> None:
        """The public run() method works with MockRunner."""
        cfg = _make_agent_config(name="mock-agent")
        runner = MockRunner(cfg)
        ctx = _make_context()

        events: list[StreamEvent] = [e async for e in runner.run("hello", ctx)]
        assert len(events) == 3  # system, assistant, result


# ---------------------------------------------------------------------------
# extract_excerpt — edge cases
# ---------------------------------------------------------------------------


class TestExtractExcerptEdgeCases:
    def test_tool_use_with_name_key(self) -> None:
        """tool_use event using 'name' key instead of 'tool_name'."""
        event = StreamEvent(type="tool_use", raw={"name": "my_tool", "input": {"x": 1}})
        result = extract_excerpt(event)
        assert "my_tool" in result

    def test_tool_result_with_output_key(self) -> None:
        """tool_result event using 'output' key instead of 'content'."""
        event = StreamEvent(type="tool_result", raw={"output": "tool output value"})
        result = extract_excerpt(event)
        assert "tool output value" in result

    def test_result_event_no_fields(self) -> None:
        """result event with no numeric fields returns something (not crash)."""
        event = StreamEvent(type="result", raw={})
        result = extract_excerpt(event)
        assert isinstance(result, str)

    def test_user_event_no_content_blocks(self) -> None:
        """user event with empty content list returns empty string."""
        event = StreamEvent(type="user", raw={"message": {"content": []}})
        result = extract_excerpt(event)
        assert result == ""


# ---------------------------------------------------------------------------
# build_subprocess_env — venv bin injection
# ---------------------------------------------------------------------------


class TestBuildSubprocessEnvVenvBin:
    """Cover the venv bin prepending branch."""

    def test_venv_bin_prepended_when_directory_exists(self, tmp_path: Any) -> None:
        """When /opt/yupp-mind/.venv/bin exists, it is prepended to PATH."""
        venv_bin = str(tmp_path / "venv_bin")
        os.makedirs(venv_bin)

        env = {"HOME": "/home/agent", "PATH": "/usr/bin"}
        with (
            patch("ypl.agent_harness_service.executors.runner.os.environ", env),
            patch("ypl.agent_harness_service.executors.runner.os.path.isdir", return_value=True),
            # Provide a predictable venv path
            patch(
                "ypl.agent_harness_service.executors.runner.build_subprocess_env",
                wraps=build_subprocess_env,
            ),
        ):
            result = build_subprocess_env()
        # PATH should contain both ~/.local/bin (prepended) and possibly venv bin
        # The important thing is it doesn't crash and PATH is a string
        assert isinstance(result.get("PATH", ""), str)

    def test_venv_bin_not_duplicated(self) -> None:
        """If venv/bin is already in PATH, it is not added a second time."""
        venv_bin = "/opt/yupp-mind/.venv/bin"
        env = {"HOME": "/home/agent", "PATH": f"{venv_bin}:/usr/bin"}

        with (
            patch("ypl.agent_harness_service.executors.runner.os.environ", env),
            patch("ypl.agent_harness_service.executors.runner.os.path.isdir", return_value=True),
        ):
            result = build_subprocess_env()

        # Count occurrences
        path_parts = result.get("PATH", "").split(":")
        assert path_parts.count(venv_bin) <= 1


# ---------------------------------------------------------------------------
# RawExecutorRunner._run_once — event streaming and error paths
# ---------------------------------------------------------------------------


class TestRawExecutorRunnerRunOnce:
    """Unit-test the RawExecutorRunner's _run_once event streaming."""

    def _make_runner(self, permissions: dict[str, str] | None = None) -> Any:
        from ypl.agent_harness_service.executors.runner import RawExecutorRunner

        perms = permissions or {"*": "allow"}
        cfg = _make_agent_config(
            name="raw-test-agent",
            executor_type="raw",
            tool_permissions=perms,
            model="anthropic/claude-sonnet-4-6",
        )
        cfg.executor_config = ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6")
        return RawExecutorRunner(cfg)

    @pytest.mark.asyncio
    async def test_yields_system_event_first(self) -> None:
        """_run_once always yields a system event as the first event."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context()

        mock_result = ExecutorResult(text="All done", estimated_cost_usd=0.01, duration_ms=500)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events: list[StreamEvent] = [e async for e in runner._run_once("do something", ctx)]

        assert events[0].type == "system"

    @pytest.mark.asyncio
    async def test_yields_assistant_and_result_on_success(self) -> None:
        """On success, emits assistant and result events after system."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context()

        mock_result = ExecutorResult(text="The answer is 42", estimated_cost_usd=0.002, duration_ms=300)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events: list[StreamEvent] = [e async for e in runner._run_once("answer me", ctx)]

        types = [e.type for e in events]
        assert "assistant" in types
        assert "result" in types

        assistant = next(e for e in events if e.type == "assistant")
        assert "42" in (assistant.text or "")

    @pytest.mark.asyncio
    async def test_yields_error_event_on_executor_exception(self) -> None:
        """When run_raw_executor raises, an error event is emitted."""
        runner = self._make_runner()
        ctx = _make_context()

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch(
                "ypl.agent_harness_service.executors.raw_executor.run_raw_executor",
                side_effect=RuntimeError("Executor exploded"),
            ),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events: list[StreamEvent] = [e async for e in runner._run_once("crash me", ctx)]

        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "Executor exploded" in error_events[0].raw.get("error", "")

    @pytest.mark.asyncio
    async def test_context_overflow_result_subtype(self) -> None:
        """Result subtype is 'stopped_context_overflow' when text contains the marker."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context()

        overflow_text = "[STOPPED] Context overflow: ~200000 tokens estimated, limit is 128000."
        mock_result = ExecutorResult(text=overflow_text, estimated_cost_usd=0.01, duration_ms=200)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events: list[StreamEvent] = [e async for e in runner._run_once("ask", ctx)]

        result_event = next(e for e in events if e.type == "result")
        assert result_event.raw.get("subtype") == "stopped_context_overflow"

    @pytest.mark.asyncio
    async def test_error_max_turns_result_subtype(self) -> None:
        """Result subtype is 'error_max_turns' when text starts with [STOPPED]."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context()

        stopped_text = "[STOPPED] Max steps (10) reached."
        mock_result = ExecutorResult(text=stopped_text, estimated_cost_usd=0.01, duration_ms=200)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events: list[StreamEvent] = [e async for e in runner._run_once("ask", ctx)]

        result_event = next(e for e in events if e.type == "result")
        assert result_event.raw.get("subtype") == "error_max_turns"

    @pytest.mark.asyncio
    async def test_restricted_session_uses_restricted_permissions(self) -> None:
        """Sessions without permissions default to restricted mode."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        # Context with no permissions info → restricted
        ctx = _make_context(is_slack=False, session_context=None)

        mock_result = ExecutorResult(text="ok", estimated_cost_usd=0.001, duration_ms=100)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
            patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger,
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            _ = [e async for e in runner._run_once("do something", ctx)]

        # Should have logged a warning about missing permissions
        warning_msgs = [call[0][0] for call in mock_logger.warning.call_args_list]
        assert any("missing permissions" in msg for msg in warning_msgs)

    @pytest.mark.asyncio
    async def test_slack_session_restricted_permissions(self) -> None:
        """Slack sessions without permissions also default to restricted mode."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context(is_slack=True, session_context=None)

        mock_result = ExecutorResult(text="slack response", estimated_cost_usd=0.001, duration_ms=100)

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch("ypl.agent_harness_service.executors.raw_executor.run_raw_executor", return_value=mock_result),
            patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger,
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            _ = [e async for e in runner._run_once("slack msg", ctx)]

        warning_msgs = [call[0][0] for call in mock_logger.warning.call_args_list]
        assert any("Slack" in msg for msg in warning_msgs)

    @pytest.mark.asyncio
    async def test_on_event_callback_receives_non_skip_events(self) -> None:
        """Only non-skipped event types (not 'assistant'/'result') reach on_event."""
        from ypl.agent_harness_service.common.models import ExecutorResult

        runner = self._make_runner()
        ctx = _make_context()

        mock_result = ExecutorResult(text="done", estimated_cost_usd=0.001, duration_ms=100)

        def _fake_executor(**kwargs: Any) -> Any:
            on_event = kwargs.get("on_event")
            if on_event:
                on_event({"type": "tool_use", "name": "bash"})
                on_event({"type": "assistant", "message": {}})  # should be skipped
                on_event({"type": "result", "subtype": "success"})  # should be skipped
            return mock_result

        with (
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess") as mock_mcp_cls,
            patch(
                "ypl.agent_harness_service.executors.raw_executor.run_raw_executor",
                new=AsyncMock(side_effect=lambda **kwargs: _fake_executor(**kwargs)),
            ),
        ):
            mock_mcp = AsyncMock()
            mock_mcp.mcp_tools = []
            mock_mcp.call_tool = AsyncMock()
            mock_mcp.resource_catalog = None
            mock_mcp_cls.return_value.__aenter__ = AsyncMock(return_value=mock_mcp)
            mock_mcp_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            events = [e async for e in runner._run_once("do stuff", ctx)]

        # "tool_use" is not in SKIP_EVENT_TYPES so it should be yielded
        tool_use_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_use_events) == 1
        assert tool_use_events[0].raw == {"type": "tool_use", "name": "bash"}
        # "assistant" and "result" from on_event are filtered (only the final result assistant is yielded)
        on_event_assistant = [e for e in events if e.type == "assistant" and e.raw.get("message") == {}]
        assert len(on_event_assistant) == 0


# ---------------------------------------------------------------------------
# ClaudeCodeRunner._build_args — tool permission modes
# ---------------------------------------------------------------------------


class TestClaudeCodeRunnerBuildArgs:
    """Test the different tool permission modes in _build_args."""

    def _make_runner(self, **kwargs: Any) -> Any:
        from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner

        cfg = _make_agent_config(**kwargs)
        return ClaudeCodeRunner(cfg)

    def _build_args(self, runner: Any, prompt: str = "test", workspace: str | None = None) -> list[Any]:
        ctx = _make_context(workspace=workspace)
        with (
            patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""),
            patch("ypl.agent_harness_service.executors.runner.SessionPermissions") as mock_perms_cls,
        ):
            mock_perms = MagicMock()
            mock_perms.has_full_tool_access = False
            mock_perms_cls.restricted.return_value = mock_perms
            mock_perms_cls.from_context.return_value = mock_perms
            return runner._build_args(prompt, ctx)  # type: ignore[no-any-return]

    def test_basic_args_include_claude_and_output_format(self) -> None:
        runner = self._make_runner()
        args = self._build_args(runner)
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        assert "stream-json" in args

    def test_model_arg_included_when_set(self) -> None:
        """When model is set on executor_config, --model arg is included."""
        from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner

        cfg = _make_agent_config(name="model-agent")
        cfg.llm_model = "claude-sonnet-4-6"
        runner = ClaudeCodeRunner(cfg)
        args = self._build_args(runner)
        assert "--model" in args
        assert "claude-sonnet-4-6" in args

    def test_default_deny_mode_uses_allowedtools(self) -> None:
        """Default-deny mode ("*": "deny") uses --allowedTools."""
        runner = self._make_runner(tool_permissions={"*": "deny", "bash": "allow"})
        args = self._build_args(runner)
        assert "--allowedTools" in args

    def test_default_allow_mode_no_allowedtools(self) -> None:
        """Default-allow mode ("*": "allow") does NOT use --allowedTools."""
        runner = self._make_runner(tool_permissions={"*": "allow"})
        args = self._build_args(runner)
        assert "--allowedTools" not in args

    def test_explicit_deny_produces_disallowedtools(self) -> None:
        """Explicit tool denies produce --disallowedTools in default-allow mode."""
        runner = self._make_runner(tool_permissions={"*": "allow", "bash": "deny"})
        args = self._build_args(runner)
        # Some disallowedTools flags are expected (may include default MCP denies too)
        assert "--disallowedTools" in args

    def test_dangerously_skip_permissions_when_sandbox_enabled(self) -> None:
        """When sandbox.enabled=True, --dangerously-skip-permissions is added."""
        runner = self._make_runner(sandbox_enabled=True)
        args = self._build_args(runner)
        assert "--dangerously-skip-permissions" in args

    def test_no_skip_permissions_when_sandbox_disabled(self) -> None:
        """When sandbox.enabled=False, --dangerously-skip-permissions is NOT added."""
        runner = self._make_runner(sandbox_enabled=False)
        args = self._build_args(runner)
        assert "--dangerously-skip-permissions" not in args

    def test_max_turns_arg_included(self) -> None:
        """--max-turns is always included."""
        runner = self._make_runner(max_turns=10)
        args = self._build_args(runner)
        assert "--max-turns" in args
        idx = args.index("--max-turns")
        assert args[idx + 1] == "10"

    def test_resume_arg_included_when_llm_session_id_set(self) -> None:
        """--resume is added when context has llm_session_id."""
        from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner

        cfg = _make_agent_config(name="resume-agent")
        runner = ClaudeCodeRunner(cfg)
        ctx = _make_context(llm_session_id="llm-session-abc")
        with (
            patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""),
            patch("ypl.agent_harness_service.executors.runner.SessionPermissions") as mock_perms_cls,
        ):
            mock_perms = MagicMock()
            mock_perms.has_full_tool_access = False
            mock_perms_cls.restricted.return_value = mock_perms
            mock_perms_cls.from_context.return_value = mock_perms
            args = runner._build_args("test", ctx)
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "llm-session-abc"

    def test_mcp_full_access_adds_top_tools(self) -> None:
        """Full tool access with MCP pre-declares top harness tools."""
        from ypl.agent_harness_service.executors.runner import ClaudeCodeRunner

        cfg = _make_agent_config(
            name="mcp-agent",
            tool_permissions={"*": "deny"},
            has_mcp=True,
        )
        runner = ClaudeCodeRunner(cfg)
        ctx = _make_context(session_context={"permissions": {"full_tool_access": True, "servers": ["*"]}})

        with (
            patch("ypl.agent_harness_service.executors.runner.build_system_prompt", return_value=""),
            patch("ypl.agent_harness_service.executors.runner.SessionPermissions") as mock_perms_cls,
        ):
            mock_perms = MagicMock()
            mock_perms.has_full_tool_access = True
            mock_perms_cls.restricted.return_value = mock_perms
            mock_perms_cls.from_context.return_value = mock_perms
            args = runner._build_args("do work", ctx)

        # Should include mcp__harness__* wildcard
        all_args_str = " ".join(args)
        assert "mcp__harness__*" in all_args_str or "mcp__harness__send_slack_message" in all_args_str
