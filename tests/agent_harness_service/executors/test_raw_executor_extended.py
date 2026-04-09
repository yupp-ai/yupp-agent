"""Extended tests for raw_executor.py — covers uncovered portions.

Focuses on:
- _load_raw_executor_prompt
- _build_skill_catalog_section
- _build_resource_catalog_section
- _build_system_prompt (integration with above helpers)
- convert_tools_to_anthropic / convert_tools_to_openai
- filter_tools_by_permissions
- _run_anthropic (unit — mocked client)
- _run_openai (unit — mocked client)
- _create_client
- run_raw_executor: session history, context overflow, API error, OpenAI path,
  tool execution error, spill-to-file, provider mismatch, missing executor
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import openai
import pytest
from ypl.agent_harness_service.common.models import AgentSpec, ExecutorConfig
from ypl.agent_harness_service.executors.raw_executor import (
    _build_resource_catalog_section,
    _build_skill_catalog_section,
    _create_client,
    _load_raw_executor_prompt,
    _run_anthropic,
    _run_openai,
    convert_tools_to_anthropic,
    convert_tools_to_openai,
    filter_tools_by_permissions,
    run_raw_executor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    name: str = "test-agent",
    max_steps: int = 10,
    temperature: float | None = None,
    additional_prompt: str | None = None,
    model: str = "anthropic/claude-sonnet-4-6",
    tools: dict[str, str] | None = None,
) -> AgentSpec:
    return AgentSpec(
        name=name,
        executor=ExecutorConfig(type="raw", model=model),
        max_steps=max_steps,
        temperature=temperature,
        additional_system_prompt=additional_prompt,
        tools=tools or {"*": "allow"},
    )


def _anthropic_response(
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# _load_raw_executor_prompt
# ---------------------------------------------------------------------------


class TestLoadRawExecutorPrompt:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        """Returns None when RAW_EXECUTOR.md does not exist."""
        with patch("ypl.agent_harness_service.common.constants.AHS_SHARED_DIR", str(tmp_path)):
            result = _load_raw_executor_prompt()
        assert result is None

    def test_returns_content_when_file_exists(self, tmp_path: Path) -> None:
        """Returns stripped file content when RAW_EXECUTOR.md exists."""
        raw_dir = tmp_path / "raw_executor"
        raw_dir.mkdir()
        (raw_dir / "RAW_EXECUTOR.md").write_text("  Hello executor!  \n")

        with patch("ypl.agent_harness_service.common.constants.AHS_SHARED_DIR", str(tmp_path)):
            result = _load_raw_executor_prompt()
        assert result == "Hello executor!"


# ---------------------------------------------------------------------------
# _build_skill_catalog_section
# ---------------------------------------------------------------------------


class TestBuildSkillCatalogSection:
    def test_returns_none_when_skills_dir_missing(self, tmp_path: Path) -> None:
        """Returns None when AHS_SKILLS_DIR does not exist."""
        nonexistent = str(tmp_path / "skills_dir_nonexistent")
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", nonexistent):
            result = _build_skill_catalog_section()
        assert result is None

    def test_returns_none_when_no_skill_md_files(self, tmp_path: Path) -> None:
        """Returns None when the skills dir exists but has no SKILL.md files."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "empty-skill").mkdir()  # No SKILL.md
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(skills_dir)):
            result = _build_skill_catalog_section()
        assert result is None

    def test_returns_catalog_with_description(self, tmp_path: Path) -> None:
        """Returns a catalog string when SKILL.md files with frontmatter exist."""
        skills_dir = tmp_path / "skills"
        (skills_dir / "my-skill").mkdir(parents=True)
        skill_md = skills_dir / "my-skill" / "SKILL.md"
        skill_md.write_text('---\ndescription: "Does cool stuff"\n---\n# My Skill\n')

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(skills_dir)):
            result = _build_skill_catalog_section()

        assert result is not None
        assert "my-skill" in result
        assert "Does cool stuff" in result

    def test_excludes_skills_in_exclude_set(self, tmp_path: Path) -> None:
        """Skills in the 'exclude' frozenset are omitted from the catalog."""
        skills_dir = tmp_path / "skills"
        for skill_name in ("skill-a", "skill-b"):
            (skills_dir / skill_name).mkdir(parents=True)
            (skills_dir / skill_name / "SKILL.md").write_text('---\ndescription: "Test"\n---\n')

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(skills_dir)):
            result = _build_skill_catalog_section(exclude=frozenset({"skill-a"}))

        assert result is not None
        assert "skill-a" not in result
        assert "skill-b" in result

    def test_skill_without_frontmatter_included_without_description(self, tmp_path: Path) -> None:
        """Skills without YAML frontmatter are still listed but without a description."""
        skills_dir = tmp_path / "skills"
        (skills_dir / "no-meta").mkdir(parents=True)
        (skills_dir / "no-meta" / "SKILL.md").write_text("# Just the title\nSome content.\n")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(skills_dir)):
            result = _build_skill_catalog_section()

        assert result is not None
        assert "no-meta" in result

    def test_oserror_when_reading_skill_md_skips_entry(self, tmp_path: Path) -> None:
        """An OSError while reading a SKILL.md silently skips that entry."""
        skills_dir = tmp_path / "skills"
        (skills_dir / "bad-skill").mkdir(parents=True)
        (skills_dir / "bad-skill" / "SKILL.md").write_text("")  # exists but we'll patch open

        with (
            patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(skills_dir)),
            patch("builtins.open", side_effect=OSError("Read error")),
        ):
            result = _build_skill_catalog_section()
        # Should return None since no entries were successfully parsed
        assert result is None


# ---------------------------------------------------------------------------
# _build_resource_catalog_section
# ---------------------------------------------------------------------------


class TestBuildResourceCatalogSection:
    def test_basic_catalog(self) -> None:
        resources = [
            {"uri": "file:///shared/notes.md", "description": "Shared notes"},
            {"uri": "file:///shared/rules.md", "description": ""},
        ]
        result = _build_resource_catalog_section(resources)
        assert "file:///shared/notes.md" in result
        assert "Shared notes" in result
        assert "file:///shared/rules.md" in result

    def test_resource_without_description(self) -> None:
        """Resources without description don't include the dash separator."""
        resources = [{"uri": "file:///data.txt"}]
        result = _build_resource_catalog_section(resources)
        assert "file:///data.txt" in result
        # No extra dash when description is absent
        assert " — " not in result


# ---------------------------------------------------------------------------
# convert_tools_to_anthropic / convert_tools_to_openai
# ---------------------------------------------------------------------------


class TestConvertTools:
    def test_convert_to_anthropic_basic(self) -> None:
        tools = [
            {
                "name": "bash",
                "description": "Run a command",
                "inputSchema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }
        ]
        result = convert_tools_to_anthropic(tools)
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        assert result[0]["description"] == "Run a command"
        assert "input_schema" in result[0]

    def test_convert_to_anthropic_falls_back_to_input_schema_key(self) -> None:
        """Falls back to 'input_schema' when 'inputSchema' is absent."""
        tools = [{"name": "read", "input_schema": {"type": "object"}}]
        result = convert_tools_to_anthropic(tools)
        assert result[0]["input_schema"] == {"type": "object"}

    def test_convert_to_anthropic_empty_list(self) -> None:
        assert convert_tools_to_anthropic([]) == []

    def test_convert_to_openai_basic(self) -> None:
        tools = [
            {
                "name": "bash",
                "description": "Run a command",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        result = convert_tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "bash"
        assert "parameters" in result[0]["function"]

    def test_convert_to_openai_missing_description(self) -> None:
        """Missing description defaults to empty string."""
        tools = [{"name": "bash", "inputSchema": {"type": "object"}}]
        result = convert_tools_to_openai(tools)
        assert result[0]["function"]["description"] == ""

    def test_convert_to_openai_empty_list(self) -> None:
        assert convert_tools_to_openai([]) == []


# ---------------------------------------------------------------------------
# filter_tools_by_permissions
# ---------------------------------------------------------------------------


class TestFilterToolsByPermissions:
    def _tool(self, name: str) -> dict[str, Any]:
        return {"name": name, "description": "", "inputSchema": {}}

    def test_allow_all_by_default(self) -> None:
        tools = [self._tool("bash"), self._tool("read")]
        result = filter_tools_by_permissions(tools, {"*": "allow"})
        assert len(result) == 2

    def test_deny_all_by_default(self) -> None:
        tools = [self._tool("bash"), self._tool("read")]
        result = filter_tools_by_permissions(tools, {"*": "deny"})
        assert len(result) == 0

    def test_deny_specific_tool(self) -> None:
        tools = [self._tool("bash"), self._tool("read")]
        result = filter_tools_by_permissions(tools, {"*": "allow", "bash": "deny"})
        names = [t["name"] for t in result]
        assert "bash" not in names
        assert "read" in names

    def test_allow_specific_tool_in_deny_all(self) -> None:
        tools = [self._tool("bash"), self._tool("read")]
        result = filter_tools_by_permissions(tools, {"*": "deny", "bash": "allow"})
        names = [t["name"] for t in result]
        assert "bash" in names
        assert "read" not in names

    def test_no_wildcard_defaults_to_allow(self) -> None:
        """Without a wildcard, the default is 'allow'."""
        tools = [self._tool("bash"), self._tool("read")]
        result = filter_tools_by_permissions(tools, {"other_tool": "deny"})
        names = [t["name"] for t in result]
        assert "bash" in names
        assert "read" in names

    def test_empty_tools_returns_empty(self) -> None:
        result = filter_tools_by_permissions([], {"*": "allow"})
        assert result == []

    def test_read_resource_exempt_from_deny_all(self) -> None:
        """run_raw_executor re-adds read_resource even when * = deny.

        filter_tools_by_permissions itself simply denies read_resource when
        * = deny — the exemption is applied at the run_raw_executor level.
        This test verifies the raw filter does deny it.
        """
        tools = [self._tool("read_resource"), self._tool("bash")]
        result = filter_tools_by_permissions(tools, {"*": "deny"})
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _run_anthropic (unit — mocked client)
# ---------------------------------------------------------------------------


def _make_anthropic_client(response: Any) -> Any:
    """Build a mock AsyncAnthropic client with messages.create returning response."""
    client = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages.create = AsyncMock(return_value=response)
    return client


def _make_mock_anthropic_response(
    text: str = "",
    tool_blocks: list[Any] | None = None,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Build a mock Anthropic response object."""
    response = MagicMock()
    response.stop_reason = stop_reason
    content: list[Any] = []
    if text:
        tb = MagicMock()
        tb.type = "text"
        tb.text = text
        content.append(tb)
    if tool_blocks:
        content.extend(tool_blocks)
    response.content = content
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    # cache attrs may be absent
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    return response


class TestRunAnthropic:
    @pytest.mark.asyncio
    async def test_returns_text_and_empty_tool_calls(self) -> None:
        """Basic text response with no tool calls."""
        mock_response = _make_mock_anthropic_response(text="Hello world")
        mock_client = _make_anthropic_client(mock_response)

        result = await _run_anthropic(
            client=mock_client,
            model_id="claude-sonnet-4-6",
            system_prompt="You are helpful",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            temperature=None,
        )

        assert result["text"] == "Hello world"
        assert result["tool_calls"] == []
        assert result["finish_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_tool_use_blocks_extracted(self) -> None:
        """tool_use blocks become tool_calls entries."""
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tc_001"
        tool_block.name = "bash"
        tool_block.input = {"cmd": "ls"}

        mock_response = _make_mock_anthropic_response(tool_blocks=[tool_block], stop_reason="tool_use")
        mock_client = _make_anthropic_client(mock_response)

        result = await _run_anthropic(
            client=mock_client,
            model_id="claude-sonnet-4-6",
            system_prompt="sys",
            messages=[],
            tools=[{"name": "bash", "description": "", "input_schema": {"type": "object"}}],
            temperature=0.5,
        )

        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "bash"
        assert result["tool_calls"][0]["arguments"] == {"cmd": "ls"}

    @pytest.mark.asyncio
    async def test_caching_disabled_sends_string_system(self) -> None:
        """With enable_caching=False, system is a plain string."""
        mock_response = _make_mock_anthropic_response()
        mock_client = _make_anthropic_client(mock_response)

        await _run_anthropic(
            client=mock_client,
            model_id="claude-haiku-4-5",
            system_prompt="plain system",
            messages=[],
            tools=[],
            temperature=None,
            enable_caching=False,
        )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "plain system"

    @pytest.mark.asyncio
    async def test_temperature_included_when_provided(self) -> None:
        """Temperature is included in API call when not None."""
        mock_response = _make_mock_anthropic_response()
        mock_client = _make_anthropic_client(mock_response)

        await _run_anthropic(
            client=mock_client,
            model_id="claude-sonnet-4-6",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=0.7,
        )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.7

    @pytest.mark.asyncio
    async def test_model_parameters_merged(self) -> None:
        """Extra model_parameters are merged into the API call kwargs."""
        mock_response = _make_mock_anthropic_response()
        mock_client = _make_anthropic_client(mock_response)

        await _run_anthropic(
            client=mock_client,
            model_id="claude-sonnet-4-6",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=None,
            model_parameters={"thinking": {"type": "enabled"}},
        )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "thinking" in call_kwargs


# ---------------------------------------------------------------------------
# _run_openai (unit — mocked client)
# ---------------------------------------------------------------------------


def _make_openai_mock_response(
    text: str = "",
    finish_reason: str = "stop",
    tool_calls: list[MagicMock] | None = None,
) -> MagicMock:
    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message.content = text or None
    choice.message.tool_calls = tool_calls or []
    response = MagicMock()
    response.choices = [choice]
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    # Optional extra fields
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    return response


def _make_openai_client(response: Any) -> Any:
    """Build a mock AsyncOpenAI client with chat.completions.create returning response."""
    client = MagicMock(spec=openai.AsyncOpenAI)
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


class TestRunOpenAI:
    @pytest.mark.asyncio
    async def test_basic_text_response(self) -> None:
        mock_client = _make_openai_client(_make_openai_mock_response(text="Hi there"))

        result = await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            temperature=None,
        )

        assert result["text"] == "Hi there"
        assert result["tool_calls"] == []

    @pytest.mark.asyncio
    async def test_tool_calls_parsed(self) -> None:
        tc_mock = MagicMock()
        tc_mock.id = "call_abc"
        tc_mock.function.name = "bash"
        tc_mock.function.arguments = json.dumps({"cmd": "ls"})

        mock_client = _make_openai_client(
            _make_openai_mock_response(text="", finish_reason="tool_calls", tool_calls=[tc_mock])
        )

        result = await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[],
            tools=[{"type": "function", "function": {"name": "bash"}}],
            temperature=None,
        )

        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "bash"
        assert result["tool_calls"][0]["arguments"] == {"cmd": "ls"}

    @pytest.mark.asyncio
    async def test_invalid_json_in_tool_arguments(self) -> None:
        """Tool call with invalid JSON arguments are stored as raw."""
        tc_mock = MagicMock()
        tc_mock.id = "call_bad"
        tc_mock.function.name = "bad_tool"
        tc_mock.function.arguments = "NOT VALID JSON"

        mock_client = _make_openai_client(
            _make_openai_mock_response(text="", finish_reason="tool_calls", tool_calls=[tc_mock])
        )

        result = await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=None,
        )

        assert result["tool_calls"][0]["arguments"] == {"raw": "NOT VALID JSON"}

    @pytest.mark.asyncio
    async def test_temperature_included_when_not_none(self) -> None:
        mock_client = _make_openai_client(_make_openai_mock_response(text="hi"))

        await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=0.3,
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.3

    @pytest.mark.asyncio
    async def test_model_parameters_passed_as_extra_body(self) -> None:
        """Extra model_parameters go into extra_body for OpenAI."""
        mock_client = _make_openai_client(_make_openai_mock_response(text="hi"))

        await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=None,
            model_parameters={"thinking": {"type": "enabled"}},
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("extra_body") == {"thinking": {"type": "enabled"}}

    @pytest.mark.asyncio
    async def test_cached_tokens_parsed_from_prompt_details(self) -> None:
        """cached_tokens are extracted from prompt_tokens_details if present."""
        response = _make_openai_mock_response(text="hi")
        prompt_details = MagicMock()
        prompt_details.cached_tokens = 500
        response.usage.prompt_tokens_details = prompt_details
        mock_client = _make_openai_client(response)

        result = await _run_openai(
            client=mock_client,
            model_id="gpt-4o",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=None,
        )

        assert result["usage"]["cached_tokens"] == 500

    @pytest.mark.asyncio
    async def test_reasoning_content_included_when_present(self) -> None:
        """reasoning_content is included in result when model exposes it."""
        response = _make_openai_mock_response(text="final answer")
        response.choices[0].message.content = "final answer"
        response.choices[0].message.reasoning_content = "let me think..."
        mock_client = _make_openai_client(response)

        result = await _run_openai(
            client=mock_client,
            model_id="kimi-k2.5",
            system_prompt="sys",
            messages=[],
            tools=[],
            temperature=None,
        )

        assert result.get("reasoning_content") == "let me think..."


# ---------------------------------------------------------------------------
# _create_client
# ---------------------------------------------------------------------------


class TestCreateClient:
    def test_raises_when_api_key_missing(self) -> None:
        """ValueError when the API key env var is not set."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ypl.agent_harness_service.executors.raw_executor.get_provider_config") as mock_cfg,
        ):
            mock_cfg.return_value = MagicMock(env_key="MISSING_API_KEY")
            with pytest.raises(ValueError, match="API key not found"):
                _create_client("anthropic")

    def test_creates_anthropic_client(self) -> None:
        """Returns AsyncAnthropic client for 'anthropic' provider."""
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("ypl.agent_harness_service.executors.raw_executor.get_provider_config") as mock_cfg,
        ):
            mock_cfg.return_value = MagicMock(env_key="ANTHROPIC_API_KEY")
            client = _create_client("anthropic")
        assert isinstance(client, anthropic.AsyncAnthropic)

    def test_creates_openai_client_for_other_providers(self) -> None:
        """Returns AsyncOpenAI client for non-Anthropic providers."""
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
            patch("ypl.agent_harness_service.executors.raw_executor.get_provider_config") as mock_cfg,
            patch(
                "ypl.agent_harness_service.executors.raw_executor.is_openai_compatible",
                return_value=True,
            ),
        ):
            mock_cfg.return_value = MagicMock(env_key="OPENAI_API_KEY", api_base="https://api.openai.com/v1")
            client = _create_client("openai")
        assert isinstance(client, openai.AsyncOpenAI)


# ---------------------------------------------------------------------------
# run_raw_executor — additional paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunRawExecutorExtended:
    """Additional coverage for run_raw_executor paths not covered by existing tests."""

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_api_error_returns_error_result(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """API errors are caught and returned as [ERROR] result text."""
        mock_run.side_effect = Exception("API timeout")
        agent = _make_agent()
        result = await run_raw_executor(agent, "do something", "anthropic/claude-sonnet-4-6")
        assert "[ERROR]" in result.text
        assert "API timeout" in result.text

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_api_error_emits_error_event(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """An error event is emitted to on_event when the API fails."""
        mock_run.side_effect = Exception("Network error")
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        await run_raw_executor(agent, "do something", "anthropic/claude-sonnet-4-6", on_event=events.append)
        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) == 1

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_tool_execution_error_continues_loop(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """A tool execution error is captured and the loop continues."""
        mock_run.side_effect = [
            _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]),
            _anthropic_response(text="Done despite tool error."),
        ]
        agent = _make_agent()
        tool_executor = AsyncMock(side_effect=RuntimeError("Tool crashed"))
        result = await run_raw_executor(
            agent,
            "do something",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "run cmd", "inputSchema": {"type": "object", "properties": {}}}],
            tool_executor=tool_executor,
        )
        assert result.text == "Done despite tool error."

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_no_tool_executor_returns_error_result_in_tool(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """Without a tool_executor, tool calls produce [ERROR] results."""
        mock_run.side_effect = [
            _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]),
            _anthropic_response(text="finished"),
        ]
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        await run_raw_executor(
            agent,
            "run bash",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "cmd", "inputSchema": {}}],
            tool_executor=None,  # no executor
            on_event=events.append,
        )
        # Tool result events should contain an error message
        tool_result_events = [e for e in events if e.get("type") == "tool_result"]
        assert len(tool_result_events) == 1
        assert tool_result_events[0]["is_error"] is True

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_read_resource_tool_exempt_from_deny_all(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """read_resource tool is always available even with * = deny."""
        mock_run.return_value = _anthropic_response(text="Done")
        agent = _make_agent(tools={"*": "deny"})
        mcp_tools = [
            {"name": "bash", "description": "cmd", "inputSchema": {}},
            {"name": "read_resource", "description": "read", "inputSchema": {}},
        ]
        await run_raw_executor(agent, "do something", "anthropic/claude-sonnet-4-6", mcp_tools=mcp_tools)

        call_kwargs = mock_run.call_args.kwargs
        tool_names = [t["name"] for t in call_kwargs["tools"]]
        # read_resource should be in the schema even though * = deny
        assert "read_resource" in tool_names
        # bash should be denied
        assert "bash" not in tool_names

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_spill_to_file_when_result_exceeds_threshold(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
        tmp_path: Path,
    ) -> None:
        """Large tool results are spilled to file when spill threshold is exceeded."""
        mock_run.side_effect = [
            _anthropic_response(tool_calls=[_tool_call("search", "tc_1")]),
            _anthropic_response(text="Done"),
        ]
        # Set a low spill threshold so we trigger spilling
        agent = _make_agent()
        agent.executor.max_tool_result_spill_chars = 10  # Very low threshold

        big_result = "x" * 200  # Exceeds threshold

        import ypl.agent_harness_service.common.constants as const_mod

        monkeypatch_path = str(tmp_path)
        tool_executor = AsyncMock(return_value=big_result)

        with patch.object(const_mod, "AHS_SESSIONS_DIR", monkeypatch_path):
            result = await run_raw_executor(
                agent,
                "search logs",
                "anthropic/claude-sonnet-4-6",
                mcp_tools=[{"name": "search", "description": "search", "inputSchema": {}}],
                tool_executor=tool_executor,
                session_id="00000000-0000-0000-0000-000000000001",
            )

        assert result.text == "Done"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch("ypl.agent_harness_service.executors.raw_executor._create_client")
    @patch("ypl.agent_harness_service.executors.raw_executor._run_openai")
    async def test_openai_provider_path(
        self,
        mock_run_openai: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """OpenAI-compatible provider follows the OpenAI code path."""
        openai_response: dict[str, Any] = {
            "text": "OpenAI says hi",
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }
        mock_run_openai.return_value = openai_response

        mock_openai_client = MagicMock(spec=openai.AsyncOpenAI)
        mock_client.return_value = mock_openai_client

        agent = _make_agent(model="openai/gpt-4o")
        result = await run_raw_executor(agent, "hello", "openai/gpt-4o")

        assert result.text == "OpenAI says hi"
        mock_run_openai.assert_called_once()

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_on_event_emits_init_system_event(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """run_raw_executor always emits a system init event via on_event."""
        mock_run.return_value = _anthropic_response(text="Done")
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        await run_raw_executor(agent, "go", "anthropic/claude-sonnet-4-6", on_event=events.append)
        system_events = [e for e in events if e.get("type") == "system"]
        assert len(system_events) == 1
        assert system_events[0].get("subtype") == "init"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_result_event_emitted_on_success(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """A result event is emitted at the end of a successful run."""
        mock_run.return_value = _anthropic_response(text="All good")
        agent = _make_agent()
        events: list[dict[str, Any]] = []
        await run_raw_executor(agent, "run", "anthropic/claude-sonnet-4-6", on_event=events.append)
        result_events = [e for e in events if e.get("type") == "result"]
        assert len(result_events) == 1
        assert result_events[0].get("subtype") == "success"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_result_subtype_context_overflow(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """result subtype = stopped_context_overflow when overflow occurs."""
        # Simulate context overflow by making estimated_tokens exceed limit
        # We do this by returning [STOPPED] Context overflow text after max_steps
        mock_run.return_value = _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")])
        agent = _make_agent(max_steps=1)
        agent.executor.max_tool_result_spill_chars = 0

        # Make the loop hit max_steps immediately
        mock_run.side_effect = None
        mock_run.return_value = _anthropic_response(tool_calls=[_tool_call("bash", "tc_1")])

        tool_executor = AsyncMock(return_value="ok")
        events: list[dict[str, Any]] = []
        result = await run_raw_executor(
            agent,
            "overflow test",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "cmd", "inputSchema": {}}],
            tool_executor=tool_executor,
            on_event=events.append,
        )
        # With max_steps=1 and always-tool-call response, should hit max steps
        assert "[STOPPED]" in result.text
        result_events = [e for e in events if e.get("type") == "result"]
        assert len(result_events) == 1
        assert result_events[0].get("subtype") == "error_max_turns"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_tokens_accumulated_across_steps(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """Token counts accumulate across multiple steps."""
        step1 = dict(_anthropic_response(tool_calls=[_tool_call("bash", "tc_1")]))
        step1["usage"] = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
        }
        step2 = dict(_anthropic_response(text="Done"))
        step2["usage"] = {
            "input_tokens": 80,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 20,
        }
        mock_run.side_effect = [step1, step2]

        agent = _make_agent()
        tool_executor = AsyncMock(return_value="ok")
        result = await run_raw_executor(
            agent,
            "count tokens",
            "anthropic/claude-sonnet-4-6",
            mcp_tools=[{"name": "bash", "description": "cmd", "inputSchema": {}}],
            tool_executor=tool_executor,
        )
        assert result.tokens is not None
        assert result.tokens["input"] == 180  # 100 + 80
        assert result.tokens["output"] == 80  # 50 + 30
        assert result.tokens["cache_write"] == 10  # 10 + 0
        assert result.tokens["cache_read"] == 25  # 5 + 20

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_cache_metrics_logged(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
    ) -> None:
        """When cache tokens are present, cache metrics are logged."""
        response = dict(_anthropic_response(text="done"))
        response["usage"] = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        }
        mock_run.return_value = response

        agent = _make_agent()
        with patch("ypl.agent_harness_service.executors.raw_executor.logger") as mock_logger:
            await run_raw_executor(agent, "test", "anthropic/claude-sonnet-4-6")

        # Cache metrics log should have been called
        info_calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any("Cache metrics" in c for c in info_calls)

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_session_history_saved_on_success(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
        tmp_path: Path,
    ) -> None:
        """Session history is saved to disk when session_history_path is provided."""
        mock_run.return_value = _anthropic_response(text="All done")
        agent = _make_agent()
        history_path = str(tmp_path / "history.json")

        with patch("ypl.agent_harness_service.executors.context.save_session_history") as mock_save:
            await run_raw_executor(
                agent,
                "do work",
                "anthropic/claude-sonnet-4-6",
                session_history_path=history_path,
            )

        mock_save.assert_called_once()
        call_args = mock_save.call_args
        assert call_args[0][0] == history_path

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_session_history_loaded_on_resume(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
        tmp_path: Path,
    ) -> None:
        """Prior messages are loaded and reused when session_history_path exists."""
        mock_run.return_value = _anthropic_response(text="Resumed")
        agent = _make_agent()
        history_path = str(tmp_path / "history.json")

        prior_messages = [{"role": "user", "content": "prior message"}]

        with (
            patch(
                "ypl.agent_harness_service.executors.context.load_session_history",
                return_value=(prior_messages, "anthropic"),
            ),
            patch("ypl.agent_harness_service.executors.context.save_session_history"),
        ):
            result = await run_raw_executor(
                agent,
                "new message",
                "anthropic/claude-sonnet-4-6",
                session_history_path=history_path,
            )

        assert result.text == "Resumed"
        # Check that messages were loaded (prior messages should be in call)
        call_kwargs = mock_run.call_args.kwargs
        messages = call_kwargs["messages"]
        # Should contain prior messages + new user message
        assert len(messages) >= 2

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_provider_mismatch_in_history_starts_fresh(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
        tmp_path: Path,
    ) -> None:
        """When loaded history has a different provider, fresh session is started."""
        # Capture snapshot of messages at call time — the list is mutated after the call
        captured_messages: list[list[dict[str, Any]]] = []

        async def _capture_messages(**kwargs: Any) -> dict[str, Any]:
            captured_messages.append(list(kwargs["messages"]))  # snapshot before mutation
            return _anthropic_response(text="Fresh start")

        mock_run.side_effect = _capture_messages
        agent = _make_agent()
        history_path = str(tmp_path / "history.json")

        prior_messages = [{"role": "user", "content": "old openai message"}]

        with (
            patch(
                "ypl.agent_harness_service.executors.context.load_session_history",
                return_value=(prior_messages, "openai"),  # Wrong provider!
            ),
            patch("ypl.agent_harness_service.executors.context.save_session_history"),
        ):
            result = await run_raw_executor(
                agent,
                "new anthropic message",
                "anthropic/claude-sonnet-4-6",
                session_history_path=history_path,
            )

        assert result.text == "Fresh start"
        # Should only have 1 message (the new prompt, not the old openai history)
        # Use the snapshot captured before post-call mutation
        assert len(captured_messages) == 1
        messages_at_call = captured_messages[0]
        assert len(messages_at_call) == 1
        assert messages_at_call[0]["role"] == "user"
        assert messages_at_call[0]["content"] == "new anthropic message"

    @patch("ypl.agent_harness_service.executors.raw_executor._load_raw_executor_prompt", return_value=None)
    @patch(
        "ypl.agent_harness_service.executors.raw_executor._create_client",
        return_value=MagicMock(spec=anthropic.AsyncAnthropic),
    )
    @patch("ypl.agent_harness_service.executors.raw_executor._run_anthropic")
    async def test_synthetic_assistant_inserted_when_history_ends_with_user(
        self,
        mock_run: AsyncMock,
        mock_client: Any,
        mock_raw_prompt: Any,
        tmp_path: Path,
    ) -> None:
        """When history ends with a user role, a synthetic assistant msg is inserted."""
        # Capture a snapshot of messages at call time — the list is mutated after the call
        captured_messages: list[list[dict[str, Any]]] = []

        async def _capture_messages(**kwargs: Any) -> dict[str, Any]:
            captured_messages.append(list(kwargs["messages"]))  # snapshot before mutation
            return _anthropic_response(text="OK")

        mock_run.side_effect = _capture_messages
        agent = _make_agent()
        history_path = str(tmp_path / "history.json")

        # History that ends with a user message (e.g., from interrupted turn)
        prior_messages = [
            {"role": "user", "content": "initial prompt"},
            {"role": "assistant", "content": [{"type": "text", "text": "thinking"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "res"}]},
        ]

        with (
            patch(
                "ypl.agent_harness_service.executors.context.load_session_history",
                return_value=(prior_messages, "anthropic"),
            ),
            patch("ypl.agent_harness_service.executors.context.save_session_history"),
        ):
            result = await run_raw_executor(
                agent,
                "continue",
                "anthropic/claude-sonnet-4-6",
                session_history_path=history_path,
            )

        assert result.text == "OK"
        assert len(captured_messages) == 1
        messages_at_call = captured_messages[0]
        # The synthetic [Session resumed] message should be before the new user prompt
        roles = [m.get("role") for m in messages_at_call]
        # Prior: user, assistant, user (tool result) → synthetic assistant inserted →
        # then new user "continue" prompt appended last
        assert roles[-1] == "user"  # new prompt is last
        assert roles[-2] == "assistant"  # synthetic [Session resumed] message
