"""Tests for ClaudeAgentSdkRunner.

Covers:
  - tool list construction and permission filtering
  - tool dispatch routing (local disk tools → BCH, MCP tools → MCPToolAccess)
  - full agent loop with mocked SDK events (text + tool_use + end_turn)
  - conversation resumption (history load / save)
  - abort / task cancellation
  - factory registration in service.py (HARNESS_CLAUDE_SDK)
"""

from __future__ import annotations
import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.config import AgentConfig, SandboxConfig
from ypl.agent_harness_service.common.constants import HARNESS_CLAUDE_SDK
from ypl.agent_harness_service.common.models import ExecutorConfig
from ypl.agent_harness_service.executors.claude_agent_sdk_runner import (
    _LOCAL_DISK_TOOLS,
    _LOCAL_TOOL_SCHEMAS,
    ClaudeAgentSdkRunner,
    _build_local_tool_list,
)
from ypl.agent_harness_service.executors.runner import RunContext

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "name": "sdk-test-agent",
        "config_dir": "/data/ahs/agents/sdk-test-agent",
        "executor_config": ExecutorConfig(type="harnessed", model=HARNESS_CLAUDE_SDK),
        "llm_model": "claude-opus-4-5",
        "default_repo": "yupp-agent",
        "tool_permissions": {"*": "allow"},
        "sandbox": SandboxConfig(enabled=True, auto_allow_bash_if_sandboxed=True),
        "max_turns": 5,
        "max_budget_usd": 1.0,
        "has_mcp": True,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {
        "session_id": "test-session-abc123",
        "workspace": "/data/ahs/sessions/test-session-abc123",
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


def _make_anthropic_response(
    text: str | None = None,
    tool_uses: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Build a mock anthropic.types.Message response."""
    response = MagicMock()
    response.stop_reason = stop_reason

    content = []
    if text:
        block = MagicMock()
        block.type = "text"
        block.text = text
        content.append(block)

    for tu in tool_uses or []:
        block = MagicMock()
        block.type = "tool_use"
        block.id = tu["id"]
        block.name = tu["name"]
        block.input = tu["input"]
        content.append(block)

    response.content = content
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 0
    return response


async def _collect(gen: AsyncIterator[Any]) -> list[Any]:
    """Collect all items from an async generator."""
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# Unit tests: local tool list building
# ---------------------------------------------------------------------------


class TestBuildLocalToolList:
    def test_all_allowed_returns_all_local_tools(self) -> None:
        cfg = _make_config(tool_permissions={"*": "allow"})
        tools = _build_local_tool_list(cfg)
        names = {t["name"] for t in tools}
        assert names == _LOCAL_DISK_TOOLS

    def test_deny_bash_excludes_bash(self) -> None:
        cfg = _make_config(tool_permissions={"*": "allow", "bash": "deny"})
        tools = _build_local_tool_list(cfg)
        names = {t["name"] for t in tools}
        assert "Bash" not in names
        assert "Read" in names

    def test_deny_all_returns_empty(self) -> None:
        cfg = _make_config(tool_permissions={"*": "deny"})
        tools = _build_local_tool_list(cfg)
        assert tools == []

    def test_deny_all_allow_read_only(self) -> None:
        cfg = _make_config(tool_permissions={"*": "deny", "read": "allow"})
        tools = _build_local_tool_list(cfg)
        names = {t["name"] for t in tools}
        assert names == {"Read"}

    def test_tool_schemas_have_required_keys(self) -> None:
        cfg = _make_config()
        tools = _build_local_tool_list(cfg)
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"].get("type") == "object"

    def test_all_local_disk_tool_schemas_defined(self) -> None:
        assert set(_LOCAL_TOOL_SCHEMAS.keys()) == _LOCAL_DISK_TOOLS


class TestLocalToolSchemas:
    """Verify inline JSON Schema definitions for each local disk tool."""

    def test_bash_has_command_required(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Bash"]["input_schema"]
        assert "command" in schema["properties"]
        assert "command" in schema["required"]

    def test_read_has_file_path_required(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Read"]["input_schema"]
        assert "file_path" in schema["properties"]
        assert "file_path" in schema["required"]

    def test_write_has_file_path_and_content_required(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Write"]["input_schema"]
        assert "file_path" in schema["required"]
        assert "content" in schema["required"]

    def test_edit_has_three_required_fields(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Edit"]["input_schema"]
        assert set(schema["required"]) >= {"file_path", "old_string", "new_string"}

    def test_glob_has_pattern_required(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Glob"]["input_schema"]
        assert "pattern" in schema["required"]

    def test_grep_has_pattern_required(self) -> None:
        schema = _LOCAL_TOOL_SCHEMAS["Grep"]["input_schema"]
        assert "pattern" in schema["required"]


# ---------------------------------------------------------------------------
# Unit tests: agent loop with mocked Anthropic client
# ---------------------------------------------------------------------------


class TestClaudeAgentSdkRunnerLoop:
    """Test the full run() generator with mocked anthropic + MCPToolAccess."""

    def _patch_run(self, responses: list[MagicMock]) -> tuple[Any, Any, Any]:
        """Return (mock_client_cls, mock_mcp_cls, mock_history) patches."""
        # Anthropic client mock
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=responses)
        mock_client_cls = MagicMock(return_value=mock_client)

        # MCPToolAccess mock (async context manager)
        mock_mcp = MagicMock()
        mock_mcp.mcp_tools = []
        mock_mcp.call_tool = AsyncMock(return_value="mcp-result")
        mock_mcp_cm = AsyncMock()
        mock_mcp_cm.__aenter__ = AsyncMock(return_value=mock_mcp)
        mock_mcp_cm.__aexit__ = AsyncMock(return_value=None)
        mock_mcp_cls = MagicMock(return_value=mock_mcp_cm)

        return mock_client_cls, mock_mcp_cls, mock_client

    @pytest.mark.asyncio
    async def test_simple_text_response_yields_events(self) -> None:
        """A single-turn text response yields system, assistant, result events."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        response = _make_anthropic_response(text="Hello, world!")
        mock_client_cls, mock_mcp_cls, _ = self._patch_run([response])

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            events = await _collect(runner.run("hello", ctx))

        event_types = [e.type for e in events]
        assert "system" in event_types
        assert "assistant" in event_types
        assert "result" in event_types
        assert "error" not in event_types

        assistant_events = [e for e in events if e.type == "assistant"]
        assert len(assistant_events) == 1
        text = assistant_events[0].raw["message"]["content"][0]["text"]
        assert text == "Hello, world!"

    @pytest.mark.asyncio
    async def test_result_event_contains_session_id(self) -> None:
        """Result event must carry session_id so service.py stores llm_session_id."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        response = _make_anthropic_response(text="done")
        mock_client_cls, mock_mcp_cls, _ = self._patch_run([response])

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            events = await _collect(runner.run("hello", ctx))

        result_events = [e for e in events if e.type == "result"]
        assert len(result_events) == 1
        raw = result_events[0].raw
        assert "session_id" in raw
        assert isinstance(raw["session_id"], str)
        assert len(raw["session_id"]) > 0
        assert "cost_usd" in raw
        assert "duration_ms" in raw
        assert "num_turns" in raw

    @pytest.mark.asyncio
    async def test_local_tool_dispatch_goes_to_bch(self) -> None:
        """Local disk tool calls are dispatched to the BCH manager, not MCP."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        # Turn 1: model requests a Bash tool call
        # Turn 2: model returns text and stops
        turn1 = _make_anthropic_response(
            tool_uses=[{"id": "tu-1", "name": "Bash", "input": {"command": "ls"}}],
            stop_reason="tool_use",
        )
        turn2 = _make_anthropic_response(text="Done")

        mock_bch = MagicMock()
        mock_bch.call_tool = AsyncMock(return_value="file1.py\nfile2.py")

        mock_client_cls, mock_mcp_cls, _ = self._patch_run([turn1, turn2])

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=mock_bch,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            events = await _collect(runner.run("list files", ctx))

        # BCH was called for Bash
        mock_bch.call_tool.assert_called_once_with("Bash", {"command": "ls"})

        # MCP call_tool was NOT called for Bash
        mock_mcp = mock_mcp_cls.return_value.__aenter__.return_value
        mock_mcp.call_tool.assert_not_called()

        event_types = [e.type for e in events]
        assert "tool_use" in event_types
        assert "tool_result" in event_types

    @pytest.mark.asyncio
    async def test_mcp_tool_dispatch_goes_to_mcp(self) -> None:
        """MCP tool calls (non-local) are dispatched to MCPToolAccess, not BCH."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        turn1 = _make_anthropic_response(
            tool_uses=[
                {
                    "id": "tu-2",
                    "name": "mcp__harness__send_slack_message",
                    "input": {"channel": "C123", "text": "hello"},
                }
            ],
            stop_reason="tool_use",
        )
        turn2 = _make_anthropic_response(text="Message sent")

        mock_bch = MagicMock()
        mock_bch.call_tool = AsyncMock(return_value="bch-result")

        mock_client_cls, mock_mcp_cls, _ = self._patch_run([turn1, turn2])

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=mock_bch,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            await _collect(runner.run("send a message", ctx))

        # MCP was called for the harness tool
        mock_mcp = mock_mcp_cls.return_value.__aenter__.return_value
        mock_mcp.call_tool.assert_called_once_with(
            "mcp__harness__send_slack_message",
            {"channel": "C123", "text": "hello"},
        )

        # BCH was NOT called
        mock_bch.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_resumption_loads_history(self) -> None:
        """When llm_session_id is set, history is loaded from disk."""
        cfg = _make_config()
        ctx = _make_context(llm_session_id="existing-sdk-session-uuid")
        runner = ClaudeAgentSdkRunner(cfg)

        prior_messages = [
            {"role": "user", "content": "prior message"},
            {"role": "assistant", "content": [{"type": "text", "text": "prior response"}]},
        ]
        # Capture count before run() mutates the list via in-place appends.
        prior_count = len(prior_messages)

        response = _make_anthropic_response(text="Resuming")
        mock_client_cls, mock_mcp_cls, mock_client = self._patch_run([response])

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history",
                return_value=(prior_messages, "anthropic"),
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            await _collect(runner.run("continue", ctx))

        # The first API call must have included prior history + new user message.
        # Use call_args_list[0] to get the arguments from the first (and only) call.
        first_call = mock_client.messages.create.call_args_list[0]
        sent_messages: list[dict[str, Any]] = first_call.kwargs.get(
            "messages", first_call.args[0] if first_call.args else []
        )
        # Prior messages + new user message → at least prior_count + 1
        assert len(sent_messages) >= prior_count + 1

    @pytest.mark.asyncio
    async def test_history_saved_after_turn(self) -> None:
        """Conversation history is saved to disk after each completed turn."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        response = _make_anthropic_response(text="Done")
        mock_client_cls, mock_mcp_cls, _ = self._patch_run([response])

        mock_save = MagicMock()
        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history", mock_save),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            await _collect(runner.run("hello", ctx))

        mock_save.assert_called_once()
        # Saved with Anthropic provider format
        _, args, _ = mock_save.mock_calls[0]
        provider = args[2] if len(args) > 2 else mock_save.call_args[0][2]
        assert provider == "anthropic"


# ---------------------------------------------------------------------------
# Abort / cancellation tests
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError raised during API call propagates out of run()."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        async def _raise_cancelled(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_raise_cancelled)
        mock_client_cls = MagicMock(return_value=mock_client)

        mock_mcp = MagicMock()
        mock_mcp.mcp_tools = []
        mock_mcp_cm = AsyncMock()
        mock_mcp_cm.__aenter__ = AsyncMock(return_value=mock_mcp)
        mock_mcp_cm.__aexit__ = AsyncMock(return_value=None)
        mock_mcp_cls = MagicMock(return_value=mock_mcp_cm)

        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            async for _ in runner.run("hello", ctx):
                pass  # Consume events until cancellation propagates

    @pytest.mark.asyncio
    async def test_partial_history_saved_on_cancellation(self) -> None:
        """When cancelled, any accumulated messages are still saved to disk."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        async def _raise_cancelled(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_raise_cancelled)
        mock_client_cls = MagicMock(return_value=mock_client)

        mock_mcp = MagicMock()
        mock_mcp.mcp_tools = []
        mock_mcp_cm = AsyncMock()
        mock_mcp_cm.__aenter__ = AsyncMock(return_value=mock_mcp)
        mock_mcp_cm.__aexit__ = AsyncMock(return_value=None)
        mock_mcp_cls = MagicMock(return_value=mock_mcp_cm)

        mock_save = MagicMock()
        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history", mock_save),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            try:
                async for _ in runner.run("hello", ctx):
                    pass
            except asyncio.CancelledError:
                pass

        # Partial history should have been saved despite cancellation
        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_result_event_on_cancellation(self) -> None:
        """A cancelled run must NOT yield a result event (would confuse service.py)."""
        cfg = _make_config()
        ctx = _make_context()
        runner = ClaudeAgentSdkRunner(cfg)

        async def _raise_cancelled(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_raise_cancelled)
        mock_client_cls = MagicMock(return_value=mock_client)

        mock_mcp = MagicMock()
        mock_mcp.mcp_tools = []
        mock_mcp_cm = AsyncMock()
        mock_mcp_cm.__aenter__ = AsyncMock(return_value=mock_mcp)
        mock_mcp_cm.__aexit__ = AsyncMock(return_value=None)
        mock_mcp_cls = MagicMock(return_value=mock_mcp_cm)

        collected: list[Any] = []
        with (
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.anthropic.AsyncAnthropic", mock_client_cls
            ),
            patch("ypl.agent_harness_service.tools.mcp_client.MCPToolAccess", mock_mcp_cls),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.get_command_handler_manager",
                return_value=None,
            ),
            patch("ypl.agent_harness_service.executors.claude_agent_sdk_runner.save_session_history"),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.load_session_history", return_value=None
            ),
            patch(
                "ypl.agent_harness_service.executors.claude_agent_sdk_runner.build_system_prompt", return_value="sys"
            ),
        ):
            try:
                collected.extend([event async for event in runner.run("hello", ctx)])
            except asyncio.CancelledError:
                pass

        result_events = [e for e in collected if e.type == "result"]
        assert result_events == [], "Must not yield result event when cancelled"


# ---------------------------------------------------------------------------
# Factory / constant registration tests
# ---------------------------------------------------------------------------


class TestConstantAndFactory:
    def test_harness_claude_sdk_constant_value(self) -> None:
        assert HARNESS_CLAUDE_SDK == "claude-agent-sdk"

    def test_runner_is_agent_runner_subclass(self) -> None:
        from ypl.agent_harness_service.executors.runner import AgentRunner

        cfg = _make_config()
        runner = ClaudeAgentSdkRunner(cfg)
        assert isinstance(runner, AgentRunner)

    def test_service_py_imports_harness_claude_sdk(self) -> None:
        """run_task.py must import HARNESS_CLAUDE_SDK — checked via grep on the source."""
        import inspect

        import ypl.agent_harness_service.service.run_task as run_task_module

        src = inspect.getsource(run_task_module)
        assert "HARNESS_CLAUDE_SDK" in src, (
            "run_task.py must import and use HARNESS_CLAUDE_SDK for the runner factory dispatch"
        )

    def test_service_py_skips_warm_pool_for_sdk(self) -> None:
        """session_lifecycle.py pre-spawn condition must exclude HARNESS_CLAUDE_SDK."""
        import inspect

        import ypl.agent_harness_service.service.session_lifecycle as lifecycle_module

        src = inspect.getsource(lifecycle_module)
        # The condition must appear somewhere in create_session pre-spawn logic
        assert "HARNESS_CLAUDE_SDK" in src
        # Rudimentary check that the guard appears near pre_spawn logic
        idx = src.find("HARNESS_CLAUDE_SDK")
        surrounding = src[max(0, idx - 300) : idx + 300]
        assert "pre_spawn" in surrounding or "HARNESS_CODEX_CLI" in surrounding, (
            "HARNESS_CLAUDE_SDK guard should appear near other pre-spawn skip conditions"
        )
