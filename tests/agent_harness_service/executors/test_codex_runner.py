"""Tests for CodexRunner — arg building, event parsing, and config dispatch."""

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.config import AgentConfig, SandboxConfig, load_agent_config
from ypl.agent_harness_service.common.models import ExecutorConfig
from ypl.agent_harness_service.executors.codex_runner import CodexRunner
from ypl.agent_harness_service.executors.runner import AgentRunner, ClaudeCodeRunner, RunContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "name": "codex-test",
        "config_dir": "/data/agents/codex-test",
        "executor_config": ExecutorConfig(type="harnessed", model="codex-cli"),
        "default_repo": "yupp-mind",
        "tool_permissions": {"*": "allow"},
        "sandbox": SandboxConfig(enabled=True, auto_allow_bash_if_sandboxed=True),
        "max_turns": 20,
        "max_budget_usd": 0.50,
        "has_mcp": False,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {
        "session_id": "aaa-bbb-ccc",
        "workspace": "/data/repos",
        "llm_session_id": None,
    }
    defaults.update(overrides)
    return RunContext(**defaults)


# ---------------------------------------------------------------------------
# _build_args tests
# ---------------------------------------------------------------------------


class TestBuildArgs:
    def test_basic_args(self) -> None:
        runner = CodexRunner(_make_config())
        ctx = _make_context()
        args = runner._build_args("hello world", ctx)

        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "hello world" in args
        assert "--json" in args
        assert "--full-auto" in args

    def test_model_flag(self) -> None:
        runner = CodexRunner(
            _make_config(executor_config=ExecutorConfig(type="harnessed", model="codex-cli"), llm_model="o3")
        )
        args = runner._build_args("prompt", _make_context())

        idx = args.index("-m")
        assert args[idx + 1] == "o3"

    def test_workspace_flag(self) -> None:
        runner = CodexRunner(_make_config())
        args = runner._build_args("prompt", _make_context(workspace="/my/workspace"))

        idx = args.index("-C")
        assert args[idx + 1] == "/my/workspace"

    def test_no_workspace_flag_when_none(self) -> None:
        runner = CodexRunner(_make_config())
        args = runner._build_args("prompt", _make_context(workspace=None))

        assert "-C" not in args

    def test_resume_session(self) -> None:
        runner = CodexRunner(_make_config())
        ctx = _make_context(llm_session_id="thread-abc-123")
        args = runner._build_args("follow up", ctx)

        assert args[0:2] == ["codex", "exec"]
        assert "resume" in args
        resume_idx = args.index("resume")
        assert args[resume_idx + 1] == "thread-abc-123"
        assert "follow up" in args

    def test_resume_skips_developer_instructions(self) -> None:
        runner = CodexRunner(_make_config())
        ctx = _make_context(llm_session_id="thread-abc-123")
        args = runner._build_args("follow up", ctx)

        # Should not include -c developer_instructions when resuming
        for i, arg in enumerate(args):
            if arg == "-c" and i + 1 < len(args):
                assert "developer_instructions" not in args[i + 1]

    def test_resume_skips_workspace_flag(self) -> None:
        """Resume rejects -C; cwd is set via create_subprocess_exec instead."""
        runner = CodexRunner(_make_config())
        ctx = _make_context(llm_session_id="thread-abc-123", workspace="/my/workspace")
        args = runner._build_args("follow up", ctx)

        assert "-C" not in args

    def test_resume_keeps_mcp_config(self) -> None:
        """Resume should still inject MCP config overrides for tool availability."""
        runner = CodexRunner(_make_config())
        ctx = _make_context(llm_session_id="thread-abc-123")

        with patch(
            "ypl.agent_harness_service.executors.codex_runner.build_codex_mcp_args",
            return_value=[
                "-c",
                'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"',
                "-c",
                'mcp_servers.harness.bearer_token_env_var="AHS_MCP_BEARER"',
            ],
        ):
            args = runner._build_args("follow up", ctx)

        assert args.count("-c") == 2
        assert 'mcp_servers.harness.url="http://127.0.0.1:8090/mcp/harness/"' in args
        assert 'mcp_servers.harness.bearer_token_env_var="AHS_MCP_BEARER"' in args

    def test_developer_instructions_included(self) -> None:
        runner = CodexRunner(_make_config())
        ctx = _make_context()

        with patch(
            "ypl.agent_harness_service.executors.codex_runner.build_system_prompt",
            return_value="You are a test agent.",
        ):
            args = runner._build_args("prompt", ctx)

        assert "-c" in args
        c_idx = args.index("-c")
        config_val = args[c_idx + 1]
        assert "developer_instructions" in config_val
        assert "You are a test agent." in config_val
        # Should use TOML double-quoted format, not shell single-quote
        assert config_val.startswith('developer_instructions="')

    def test_developer_instructions_toml_escaping(self) -> None:
        """Apostrophes and special chars should be TOML-escaped, not shell-escaped."""
        runner = CodexRunner(_make_config())
        ctx = _make_context()

        with patch(
            "ypl.agent_harness_service.executors.codex_runner.build_system_prompt",
            return_value='You\'re a "great" agent.\nLine two.',
        ):
            args = runner._build_args("prompt", ctx)

        c_idx = args.index("-c")
        config_val = args[c_idx + 1]
        # Apostrophes should pass through unchanged (only special to shell, not TOML)
        assert "You're" in config_val
        # Double quotes inside the value should be escaped
        assert '\\"great\\"' in config_val
        # Newlines should be escaped
        assert "\\n" in config_val


# ---------------------------------------------------------------------------
# Event parsing tests (mock subprocess)
# ---------------------------------------------------------------------------


def _make_codex_events(dot_format: bool = False) -> list[dict[str, Any]]:
    """Return a realistic sequence of Codex V2 JSONL events.

    Args:
        dot_format: If True, use dot-separated event types (e.g. "item.completed")
            as emitted by the Codex CLI binary.  If False (default), use the
            slash-separated streaming/server format ("item/completed").
    """

    def t(name: str) -> str:
        return name.replace("/", ".") if dot_format else name

    return [
        {"type": t("thread/started"), "thread_id": "thread-uuid-1"},
        {"type": t("turn/started")},
        {
            "type": t("item/started"),
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "ls -la",
                "status": "in_progress",
            },
        },
        {
            "type": t("item/completed"),
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "total 42\ndrwxr-xr-x ...",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": t("item/completed"),
            "item": {"id": "item_2", "type": "agent_message", "text": "Here are the files."},
        },
        {
            "type": t("item/completed"),
            "item": {"id": "item_3", "type": "reasoning", "text": "I should list files..."},
        },
        {
            "type": t("turn/completed"),
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50},
        },
    ]


async def _mock_subprocess(events: list[dict[str, Any]], returncode: int = 0) -> AsyncMock:
    """Create a mock subprocess whose stdout yields JSONL lines."""
    lines = [json.dumps(e).encode() + b"\n" for e in events]

    async def stdout_iter() -> AsyncIterator[bytes]:
        for line in lines:
            yield line

    async def stderr_iter() -> AsyncIterator[bytes]:
        return
        yield  # make it a proper async generator

    proc = AsyncMock()
    proc.stdout = stdout_iter()
    proc.stderr = stderr_iter()
    proc.returncode = returncode
    proc.wait = AsyncMock()
    proc.kill = AsyncMock()
    return proc


def _patch_runner() -> contextlib.AbstractContextManager[Iterator[MagicMock]]:
    """Context manager that patches the file logger."""
    return patch(
        "ypl.agent_harness_service.executors.codex_runner._SessionFileLogger",
        return_value=MagicMock(),
    )


class TestEventParsing:
    @pytest.mark.asyncio
    async def test_thread_started_yields_system_event(self) -> None:
        events = [{"type": "thread/started", "thread_id": "t-123"}]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        system_events = [e for e in collected if e.type == "system"]
        assert len(system_events) == 1
        assert system_events[0].session_id == "t-123"

    @pytest.mark.asyncio
    async def test_agent_message_yields_assistant_event(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/completed",
                "item": {"id": "item_1", "type": "agent_message", "text": "Hello!"},
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        assistant_events = [e for e in collected if e.type == "assistant"]
        assert len(assistant_events) == 1
        assert assistant_events[0].text == "Hello!"

    @pytest.mark.asyncio
    async def test_command_execution_yields_tool_events(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/started",
                "item": {"id": "item_1", "type": "command_execution", "command": "git status"},
            },
            {
                "type": "item/completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "git status",
                    "aggregated_output": "On branch main",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        tool_use = [e for e in collected if e.type == "tool_use"]
        tool_result = [e for e in collected if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].raw["tool_name"] == "Bash"
        assert tool_use[0].raw["input"] == "git status"
        assert len(tool_result) == 1
        assert tool_result[0].raw["content"] == "On branch main"

    @pytest.mark.asyncio
    async def test_turn_completed_yields_result_event(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/completed",
                "item": {"id": "item_1", "type": "agent_message", "text": "Done"},
            },
            {"type": "turn/completed", "usage": {"input_tokens": 100, "output_tokens": 50}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        result_events = [e for e in collected if e.type == "result"]
        assert len(result_events) == 1
        assert result_events[0].session_id == "t-1"
        assert result_events[0].cost_usd is None
        assert result_events[0].duration_ms is not None
        assert result_events[0].num_turns == 1  # one agent_message

    @pytest.mark.asyncio
    async def test_reasoning_events_are_skipped(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/completed",
                "item": {"id": "item_1", "type": "reasoning", "text": "thinking..."},
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        # No reasoning events should appear
        types = [e.type for e in collected]
        assert "reasoning" not in types

    @pytest.mark.asyncio
    async def test_file_change_yields_tool_events(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/started",
                "item": {"id": "item_1", "type": "file_change", "path": "src/main.py", "kind": "edit"},
            },
            {
                "type": "item/completed",
                "item": {
                    "id": "item_1",
                    "type": "file_change",
                    "changes": [{"kind": "edit", "path": "src/main.py"}],
                },
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        tool_use = [e for e in collected if e.type == "tool_use"]
        tool_result = [e for e in collected if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].raw["tool_name"] == "FileChange"
        assert "edit src/main.py" in tool_use[0].raw["input"]
        assert len(tool_result) == 1
        assert "file_change" in tool_result[0].raw["content"]

    @pytest.mark.asyncio
    async def test_mcp_tool_call_yields_tool_events(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/started",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "server": "yuppster-mcp-server",
                    "tool": "search_gcp_logs",
                    "arguments": {"query": "severity>=ERROR"},
                },
            },
            {
                "type": "item/completed",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "server": "yuppster-mcp-server",
                    "tool": "search_gcp_logs",
                    "arguments": {"query": "severity>=ERROR"},
                    "result": {
                        "content": [
                            {"type": "text", "text": "Found 2 matching log entries"},
                        ]
                    },
                    "status": "completed",
                },
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        tool_use = [e for e in collected if e.type == "tool_use"]
        tool_result = [e for e in collected if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].raw["tool_name"] == "search_gcp_logs"
        assert tool_use[0].raw["input"] == {"query": "severity>=ERROR"}
        assert len(tool_result) == 1
        assert tool_result[0].raw["tool_use_id"] == "item_1"
        assert tool_result[0].raw["content"] == "Found 2 matching log entries"
        assert tool_result[0].raw["is_error"] is False

    @pytest.mark.asyncio
    async def test_failed_mcp_tool_call_yields_error_tool_result(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {
                "type": "item/started",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "tool_name": "search_gcp_logs",
                    "arguments": {"query": "severity>=ERROR"},
                },
            },
            {
                "type": "item/completed",
                "item": {
                    "id": "item_1",
                    "type": "mcp_tool_call",
                    "tool_name": "search_gcp_logs",
                    "arguments": {"query": "severity>=ERROR"},
                    "error": {"message": "Permission denied"},
                    "status": "failed",
                },
            },
            {"type": "turn/completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        tool_result = [e for e in collected if e.type == "tool_result"]
        assert len(tool_result) == 1
        assert tool_result[0].raw["tool_use_id"] == "item_1"
        assert tool_result[0].raw["content"] == '{"message": "Permission denied"}'
        assert tool_result[0].raw["is_error"] is True

    @pytest.mark.asyncio
    async def test_turn_failed_yields_error_event(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {"type": "turn/completed", "status": "failed", "error": {"message": "Rate limit exceeded"}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        error_events = [e for e in collected if e.type == "error"]
        assert len(error_events) == 1
        assert "Rate limit exceeded" in error_events[0].raw["error"]

    @pytest.mark.asyncio
    async def test_codex_error_event_yields_error(self) -> None:
        events: list[dict[str, Any]] = [
            {"type": "thread/started", "thread_id": "t-1"},
            {"type": "error", "message": "Authentication failed"},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        error_events = [e for e in collected if e.type == "error"]
        assert len(error_events) == 1
        assert "Authentication failed" in error_events[0].raw["error"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_yields_error_event(self) -> None:
        events = [{"type": "thread/started", "thread_id": "t-1"}]
        proc = await _mock_subprocess(events, returncode=1)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        error_events = [e for e in collected if e.type == "error"]
        assert len(error_events) == 1
        assert "code 1" in error_events[0].raw["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dot_format", [False, True], ids=["slash_format", "dot_format"])
    async def test_full_event_sequence(self, dot_format: bool) -> None:
        """End-to-end: realistic Codex session produces expected StreamEvent sequence.

        Runs twice — once with slash-separated types (streaming/server wire format)
        and once with dot-separated types (Codex CLI binary output format) — to
        verify that the normalisation step handles both transparently.
        """
        events = _make_codex_events(dot_format=dot_format)
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("test", _make_context())]

        types = [e.type for e in collected]
        # turn/started and reasoning should be skipped in both formats
        assert types == ["system", "tool_use", "tool_result", "assistant", "result"]

    @pytest.mark.asyncio
    async def test_dot_format_agent_message(self) -> None:
        """Dot-format event types from the Codex CLI binary are parsed correctly."""
        events: list[dict[str, Any]] = [
            {"type": "thread.started", "thread_id": "t-dot"},
            {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "2"}},
            {"type": "turn.completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            collected = [e async for e in runner.run("what is 1+1", _make_context())]

        system_events = [e for e in collected if e.type == "system"]
        assistant_events = [e for e in collected if e.type == "assistant"]
        result_events = [e for e in collected if e.type == "result"]
        assert len(system_events) == 1
        assert system_events[0].session_id == "t-dot"
        assert len(assistant_events) == 1
        assert assistant_events[0].text == "2"
        assert len(result_events) == 1

    @pytest.mark.asyncio
    async def test_null_type_event_is_skipped(self) -> None:
        """Malformed events with a null or non-string 'type' are skipped without error.

        Regression test: raw.get("type", "") returns None when the key is present
        but null.  Calling .replace() on None raises AttributeError — ensure we
        guard with isinstance() so the session continues instead of aborting.
        """
        events: list[dict[str, Any]] = [
            {"type": "thread.started", "thread_id": "t-null"},
            {"type": None, "item": {"id": "bad", "type": "agent_message", "text": "oops"}},
            {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {}},
        ]
        proc = await _mock_subprocess(events)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("ypl.agent_harness_service.executors.codex_runner.build_system_prompt", return_value=""),
            _patch_runner(),
        ):
            runner = CodexRunner(_make_config())
            # Must not raise — null-type event should be skipped silently
            collected = [e async for e in runner.run("test", _make_context())]

        assistant_events = [e for e in collected if e.type == "assistant"]
        assert len(assistant_events) == 1
        assert assistant_events[0].text == "ok"


# ---------------------------------------------------------------------------
# Config & dispatch tests
# ---------------------------------------------------------------------------


class TestConfigAndDispatch:
    def test_executor_defaults_to_harnessed_claude_code(self) -> None:
        config = AgentConfig(name="test", config_dir="/tmp")
        assert config.executor_config.type == "harnessed"
        assert config.executor_config.model == "claude-code-cli"

    def test_executor_config_codex_parsed(self) -> None:
        config = AgentConfig(
            name="test",
            config_dir="/tmp",
            executor_config=ExecutorConfig(type="harnessed", model="codex-cli"),
        )
        assert config.executor_config.model == "codex-cli"

    def test_codex_runner_is_agent_runner(self) -> None:
        runner = CodexRunner(_make_config())
        assert isinstance(runner, AgentRunner)

    def test_claude_runner_is_agent_runner(self) -> None:
        config = _make_config(executor_config=ExecutorConfig(type="harnessed"))
        runner = ClaudeCodeRunner(config)
        assert isinstance(runner, AgentRunner)

    @patch("ypl.agent_harness_service.common.config.os.path.isfile", return_value=True)
    @patch(
        "builtins.open",
        MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(
                    return_value=MagicMock(
                        read=MagicMock(
                            return_value=(
                                '{"executor_config": {"type": "harnessed", "model": "codex-cli"}, "max_turns": 10}'
                            )
                        )
                    )
                ),
                __exit__=MagicMock(return_value=False),
            )
        ),
    )
    def test_load_agent_config_codex_executor(self, _mock_isfile: Any) -> None:
        from ypl.agent_harness_service.common.config import clear_config_cache

        clear_config_cache()
        clear_config_cache()
        config = load_agent_config("codex-test")
        assert config is not None
        assert config.executor_config.model == "codex-cli"
