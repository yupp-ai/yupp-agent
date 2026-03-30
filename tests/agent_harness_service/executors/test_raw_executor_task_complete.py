"""Tests for standard tool loop execution in raw executor."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest
from ypl.agent_harness_service.common.models import AgentSpec, ExecutorConfig
from ypl.agent_harness_service.executors.raw_executor import (
    run_raw_executor,
)


def _make_agent(name: str = "test-agent") -> AgentSpec:
    return AgentSpec(
        name=name,
        executor=ExecutorConfig(type="raw", model="anthropic/claude-sonnet-4-6"),
        max_steps=10,
    )


def _anthropic_response(
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a mock Anthropic API response."""
    tcs = tool_calls or []
    raw_content: list[dict[str, Any]] = []
    if text:
        raw_content.append({"type": "text", "text": text})
    raw_content.extend({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]} for tc in tcs)
    return {
        "text": text,
        "tool_calls": tcs,
        "finish_reason": "tool_use" if tcs else "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "raw_content": raw_content,
    }


def _tool_call(name: str = "bash", call_id: str = "tc_1", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments or {"cmd": "echo hi"}}


@pytest.mark.asyncio
class TestStandardToolLoop:
    """Tests for the standard tool loop (continue on tool_calls, stop on no tool_calls)."""

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_text_only_response_ends_turn(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """Text-only response (no tool calls) ends the loop immediately."""
        mock_run.return_value = _anthropic_response(text="Here is the answer.")
        agent = _make_agent()
        result = await run_raw_executor(agent, "do something", "anthropic/claude-sonnet-4-6")
        assert result.text == "Here is the answer."
        assert mock_run.call_count == 1

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_tool_calls_continue_loop(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """Tool calls continue the loop; text-only response ends it."""
        mock_run.side_effect = [
            # Step 1: tool call
            _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]),
            # Step 2: text-only = done
            _anthropic_response(text="All done."),
        ]
        agent = _make_agent()
        tool_executor = AsyncMock(return_value="ok")
        result = await run_raw_executor(
            agent,
            "do something",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
        )
        assert result.text == "All done."
        assert mock_run.call_count == 2
        tool_executor.assert_called_once()

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_text_with_tool_calls_emits_intermediate_text(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """Text alongside tool calls is emitted as intermediate_text events."""
        mock_run.side_effect = [
            # Step 1: text + tool call
            _anthropic_response(text="Running the command...", tool_calls=[_tool_call("bash", "tc_1")]),
            # Step 2: final text
            _anthropic_response(text="Done!"),
        ]
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        tool_executor = AsyncMock(return_value="hello")
        result = await run_raw_executor(
            agent,
            "echo hello",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
            on_event=events.append,
        )
        assert result.text == "Done!"
        assert mock_run.call_count == 2

        # Check intermediate_text event was emitted for text alongside tool calls
        intermediate_events = [e for e in events if e.get("type") == "intermediate_text"]
        assert len(intermediate_events) == 1
        assert intermediate_events[0]["text"] == "Running the command..."

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_max_steps_safety_net(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """Model keeps calling tools forever -> hits max_steps limit."""
        mock_run.return_value = _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")])
        agent = _make_agent()
        agent.max_steps = 3
        tool_executor = AsyncMock(return_value="ok")
        result = await run_raw_executor(
            agent,
            "do something",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
        )
        assert "[STOPPED]" in result.text
        assert "3" in result.text
        assert mock_run.call_count == 3

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_no_task_complete_in_tool_schemas(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """task_complete tool is NOT injected into the tool schemas."""
        mock_run.return_value = _anthropic_response(text="Done")
        agent = _make_agent()
        await run_raw_executor(agent, "do something", "anthropic/claude-sonnet-4-6")

        call_kwargs = mock_run.call_args.kwargs
        tool_names = [t["name"] for t in call_kwargs["tools"]]
        assert "task_complete" not in tool_names

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_tool_result_events_emitted(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """Tool execution emits both tool_use and tool_result events."""
        mock_run.side_effect = [
            _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]),
            _anthropic_response(text="Done."),
        ]
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        tool_executor = AsyncMock(return_value="tool output")
        await run_raw_executor(
            agent,
            "do something",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
            on_event=events.append,
        )

        tool_use_events = [e for e in events if e.get("type") == "tool_use"]
        tool_result_events = [e for e in events if e.get("type") == "tool_result"]
        assert len(tool_use_events) == 1
        assert tool_use_events[0]["name"] == "bash"
        assert len(tool_result_events) == 1
        assert tool_result_events[0]["name"] == "bash"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_multi_step_interleaved_text_and_tools(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """Multiple steps with interleaved text and tool calls."""
        mock_run.side_effect = [
            # Step 1: text + tool
            _anthropic_response(text="Step 1: checking...", tool_calls=[_tool_call("bash", "tc_1")]),
            # Step 2: text + tool
            _anthropic_response(text="Step 2: building...", tool_calls=[_tool_call("bash", "tc_2")]),
            # Step 3: final text
            _anthropic_response(text="All steps complete."),
        ]
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        tool_executor = AsyncMock(return_value="ok")
        result = await run_raw_executor(
            agent,
            "multi-step task",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
            on_event=events.append,
        )
        assert result.text == "All steps complete."
        assert mock_run.call_count == 3

        intermediate_events = [e for e in events if e.get("type") == "intermediate_text"]
        assert len(intermediate_events) == 2
        assert intermediate_events[0]["text"] == "Step 1: checking..."
        assert intermediate_events[1]["text"] == "Step 2: building..."

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_no_continuation_prompt_injected(
        self,
        mock_run: AsyncMock,
        mock_client: AsyncMock,
        mock_raw_prompt: Any,
    ) -> None:
        """No continuation prompt should be injected into messages."""
        mock_run.side_effect = [
            _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]),
            _anthropic_response(text="Done."),
        ]
        agent = _make_agent()
        tool_executor = AsyncMock(return_value="ok")
        await run_raw_executor(
            agent,
            "do something",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
        )

        # Check no message contains a continuation prompt
        second_call_messages = mock_run.call_args_list[1].kwargs["messages"]
        for msg in second_call_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                assert "Continue working" not in content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assert "Continue working" not in block.get("text", "")
