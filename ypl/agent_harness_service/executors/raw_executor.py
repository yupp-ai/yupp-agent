"""Raw executor loop — direct model API calls with MCP tool routing.

Replaces the harnessed executor (Claude CLI / Codex) with a loop that:
1. Calls the model API directly (Anthropic or OpenAI)
2. Fetches tool schemas from MCP servers
3. Routes tool calls back to MCP
4. Manages conversation context

This gives full visibility into every tool call, token usage, and cost,
at the expense of not using the battle-tested CLI agent harness.
"""

import asyncio
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import openai

from ypl.agent_harness_service.common.constants import (
    DEFAULT_MAX_STEPS,
    PROVIDER_ANTHROPIC,
    get_session_dir,
)
from ypl.agent_harness_service.common.models import AgentSpec, ExecutorResult
from ypl.agent_harness_service.executors.providers import get_provider_config, is_openai_compatible, parse_model_string
from ypl.structured_logger import get_logger

logger = get_logger()


# Token cost per million tokens (approximate, for cost estimation).
# cache_read / cache_write / reasoning are optional; see _estimate_cost() for fallback logic.
_COST_PER_M_TOKENS: dict[str, dict[str, float]] = {
    # Anthropic — cache_read = 0.1x input, cache_write = 1.25x input
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.00},
    # OpenAI — cache_read = 0.5x input (gpt-4o), 0.25x (o3); cache_write = free (same as input)
    "gpt-4o": {"input": 2.50, "output": 10.0, "cache_read": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    "o3": {"input": 10.0, "output": 40.0, "cache_read": 2.50, "reasoning": 40.0},
    # Z.ai — cache_read ≈ 0.2x input
    "glm-5": {"input": 1.0, "output": 3.2, "cache_read": 0.20},
    # MiniMax — cache_read = 0.1x input, cache_write = 1.25x input
    "MiniMax-M2.5": {"input": 0.3, "output": 1.2, "cache_read": 0.03, "cache_write": 0.375},
    "MiniMax-M2.1": {"input": 0.3, "output": 1.2, "cache_read": 0.03, "cache_write": 0.375},
}

# Context window limits (for overflow detection)
_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3": 200_000,
    "glm-5": 128_000,
    "MiniMax-M2.5": 1_000_000,
    "MiniMax-M2.1": 1_000_000,
}

_RESERVED_BUFFER = 4_000  # Tokens reserved for response


def truncate_tool_result(text: str, max_chars: int, max_lines: int) -> str:
    """Truncate a tool result if it exceeds line or character limits.

    Checks line count first, then character count. Returns the original
    text unchanged if it's within both limits. The truncation marker may
    push the result slightly past max_chars (by ~50 chars) — this is
    acceptable since the marker itself must be visible.
    """
    original_len = len(text)
    lines = text.split("\n")
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n...[truncated {len(lines) - max_lines} lines]"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated to {max_chars} chars ({original_len} total)]"

    return text


def spill_tool_result_to_file(
    result: str,
    tool_name: str,
    tool_call_id: str,
    session_id: str,
) -> str:
    """Write a large tool result to a file and return a message pointing the agent to it.

    Creates ``tool-results/`` under the session workspace if needed, writes the
    full result to a timestamped file, and returns a short message instructing the
    agent to read the file using the Read tool.

    The path returned to the agent is workspace-relative (e.g.
    ``tool-results/foo.txt``) so the agent never needs to know the physical
    location of its session directory.

    Args:
        result: The full tool result string to spill.
        tool_name: Name of the tool that produced this result.
        tool_call_id: Unique ID for this tool call (included in the filename to prevent collisions).
        session_id: Active session ID — determines the workspace directory.

    Returns:
        A short string that replaces the large result in the conversation,
        telling the agent where to find the full output.
    """
    tool_results_dir = Path(get_session_dir(session_id)) / "tool-results"
    tool_results_dir.mkdir(parents=True, exist_ok=True)

    ts = int(time.time() * 1000)
    safe_name = tool_name.replace("/", "_").replace("..", "_")
    safe_id = tool_call_id.replace("/", "_").replace("..", "_")
    filename = f"{safe_name}-{safe_id}-{ts}.txt"
    file_path = tool_results_dir / filename
    file_path.write_text(result, encoding="utf-8")

    rel_path = f"tool-results/{filename}"
    return (
        f"Error: result ({len(result):,} characters) exceeds maximum allowed tokens. "
        f"Output has been saved to {rel_path}.\n"
        f"Use the Read tool to inspect the file contents directly."
    )


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using a simple heuristic.

    Uses ~4 chars per token with a 1.2x safety margin.
    """
    return math.ceil(len(text) / 4 * 1.2)


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate total token count for a conversation (messages + system prompt + tools)."""
    total = estimate_tokens(system_prompt)

    # Tool schemas sent with every API call can be substantial
    if tool_schemas:
        total += estimate_tokens(json.dumps(tool_schemas))

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # Anthropic format: list of content blocks
            for block in content:
                total += estimate_tokens(json.dumps(block))

        # OpenAI format: tool_calls on assistant messages carry function payloads
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(json.dumps(tool_calls))

        # Overhead for role/structure per message
        total += 4

    return total


def _estimate_cost(model_id: str, usage: dict[str, Any]) -> float:
    """Estimate cost from a usage dict with cache and reasoning awareness.

    Handles two different token reporting conventions:
    - Anthropic: input_tokens, cache_read_input_tokens, cache_creation_input_tokens
      are three non-overlapping buckets that sum to total input.
    - OpenAI-compatible: input_tokens includes cached_tokens as a subset.
    """
    rates = _COST_PER_M_TOKENS.get(model_id, {"input": 3.0, "output": 15.0})

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Anthropic cache fields (non-overlapping with input_tokens)
    anthropic_cache_read = usage.get("cache_read_input_tokens", 0)
    anthropic_cache_write = usage.get("cache_creation_input_tokens", 0)

    # OpenAI cache fields (subset of input_tokens)
    openai_cached = usage.get("cached_tokens", 0)

    # Reasoning tokens (o3 — subset of output_tokens)
    reasoning = usage.get("reasoning_tokens", 0)

    if anthropic_cache_read or anthropic_cache_write:
        # Anthropic convention: input_tokens is already the uncached portion
        uncached_input = input_tokens
        cache_read = anthropic_cache_read
        cache_write = anthropic_cache_write
    elif openai_cached:
        # OpenAI convention: input_tokens includes cached_tokens
        uncached_input = input_tokens - openai_cached
        cache_read = openai_cached
        cache_write = 0  # OpenAI cache writes are free (same as input rate)
    else:
        # No caching info — all input at full price
        uncached_input = input_tokens
        cache_read = 0
        cache_write = 0

    # Regular output = output - reasoning (reasoning priced separately for o3)
    regular_output = max(0, output_tokens - reasoning)

    return float(
        (
            uncached_input * rates.get("input", 3.0)
            + cache_read * rates.get("cache_read", rates.get("input", 3.0))
            + cache_write * rates.get("cache_write", rates.get("input", 3.0))
            + regular_output * rates.get("output", 15.0)
            + reasoning * rates.get("reasoning", rates.get("output", 15.0))
        )
        / 1_000_000
    )


def _load_raw_executor_prompt() -> str | None:
    """Load RAW_EXECUTOR.md from the shared directory if it exists."""
    from ypl.agent_harness_service.common.constants import AHS_SHARED_DIR

    path = os.path.join(AHS_SHARED_DIR, "raw_executor", "RAW_EXECUTOR.md")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _build_skill_catalog_section(exclude: frozenset[str] | None = None) -> str | None:
    """Build a system prompt section listing available skills.

    Scans ``AHS_SKILLS_DIR`` for SKILL.md files, extracts name and description
    from YAML frontmatter, and returns a compact catalog. The agent uses the
    ``load_skill()`` MCP tool to load a skill's full content on demand.

    Args:
        exclude: Skill directory names to omit from the catalog (e.g. skills
            whose content has already been inlined into the system prompt).
    """
    from ypl.agent_harness_service.common.constants import AHS_SKILLS_DIR

    if not os.path.isdir(AHS_SKILLS_DIR):
        return None

    entries: list[tuple[str, str]] = []
    for entry in sorted(os.listdir(AHS_SKILLS_DIR)):
        if exclude and entry in exclude:
            continue
        skill_md = os.path.join(AHS_SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        # Extract description from YAML frontmatter
        description = ""
        try:
            with open(skill_md) as f:
                lines = f.readlines()
            if lines and lines[0].strip() == "---":
                for line in lines[1:]:
                    if line.strip() == "---":
                        break
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("\"'")
        except OSError:
            continue
        entries.append((entry, description))

    if not entries:
        return None

    lines = [
        "## Available Skills",
        "Skills provide domain-specific guidance for common tasks. They are NOT loaded "
        "into your context by default — load them on demand when the task requires it.",
        "",
        "To load a skill, use the `load_skill` tool:",
        '  `load_skill(skill_name="<skill-name>")`',
        "",
    ]
    for name, desc in entries:
        line = f"- **{name}**"
        if desc:
            line += f" — {desc}"
        lines.append(line)
    return "\n".join(lines)


def _build_resource_catalog_section(resource_catalog: list[dict[str, str]]) -> str:
    """Build a system prompt section listing available MCP resources."""
    lines = ["## Available Resources", 'Use `read_resource(uri="...")` to load any of these on demand.', ""]
    for res in resource_catalog:
        desc = f" — {res['description']}" if res.get("description") else ""
        lines.append(f"- `{res['uri']}`{desc}")
    return "\n".join(lines)


def _build_system_prompt(
    agent: AgentSpec,
    provider: str,
    model_id: str,
    resource_catalog: list[dict[str, str]] | None = None,
    session_id: str | None = None,
    is_slack: bool = False,
    is_task: bool = False,
    session_context: dict[str, Any] | None = None,
    slack_session_id: str | None = None,
) -> str:
    """Build the system prompt for a raw executor agent.

    Reuses the shared prompt assembly (shared/*.md, agent identity files,
    session context) and appends raw-executor-specific environment context.

    Prompt ordering is designed to maximise Anthropic prompt-cache hit rates.
    The Anthropic API caches prompt *prefixes*, so all static/shared content
    must appear before any session-specific variable content:

        [static]  shared/*.md  →  ROLE.md  →  RAW_EXECUTOR.md
        [variable] session_context / task_context / session_id / slack_thread
                   → Model/WorkingDir/Date  →  resource_catalog

    Moving RAW_EXECUTOR.md before session context extends the cacheable
    prefix by ~200 tokens, improving cross-session cache reuse.
    """
    from ypl.agent_harness_service.executors.system_prompt import build_system_prompt

    # Prepend the static raw-executor environment instructions to
    # additional_system_prompt so they land *before* session-specific context
    # in the assembled prompt.  This keeps all static content in the cache
    # prefix and all variable content at the tail.
    raw_executor_prompt = _load_raw_executor_prompt()
    combined_additional = "\n\n".join(filter(None, [raw_executor_prompt, agent.additional_system_prompt]))

    base = build_system_prompt(
        name=agent.name,
        session_id=session_id,
        slack_session_id=slack_session_id,
        is_slack=is_slack,
        is_task=is_task,
        session_context=session_context,
        additional_system_prompt=combined_additional or None,
        has_native_skills=False,  # Raw executor uses load_skill() MCP tool
    )

    # Everything below is session-specific and intentionally placed after the
    # static prefix so it does not break prompt-cache hits across sessions.
    parts: list[str] = [base]

    parts.append(f"Model: {provider}/{model_id}")
    # For raw executors the process CWD is the harness server, not the session workspace.
    # Use the session workspace path so the agent sees its actual working context.
    working_dir = get_session_dir(session_id) if session_id else os.getcwd()
    parts.append(f"Working directory: {working_dir}")
    parts.append(f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}")

    if resource_catalog:
        parts.append(_build_resource_catalog_section(resource_catalog))

    # Skill catalog — compact listing so the agent knows what's available
    # without paying the full token cost of every skill upfront.
    # Exclude skill-backed files already inlined into the system prompt above
    # (has_native_skills=False path) to avoid listing them as load_skill() candidates.
    from ypl.agent_harness_service.executors.system_prompt import SKILL_BACKED_NAMES

    skill_catalog = _build_skill_catalog_section(exclude=SKILL_BACKED_NAMES)
    if skill_catalog:
        parts.append(skill_catalog)

    return "\n\n".join(parts)


# --- MCP Tool Schema Conversion ---


def convert_tools_to_anthropic(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP tool schemas to Anthropic format."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", t.get("input_schema", {"type": "object", "properties": {}})),
        }
        for t in mcp_tools
    ]


def convert_tools_to_openai(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP tool schemas to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", t.get("input_schema", {"type": "object", "properties": {}})),
            },
        }
        for t in mcp_tools
    ]


def filter_tools_by_permissions(
    tools: list[dict[str, Any]],
    permissions: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Filter tools based on agent permissions.

    Tools with 'deny' permission are excluded entirely (model never sees them).

    Args:
        tools: MCP tool schemas.
        permissions: Tool permission map (tool_name -> allow|deny|ask).

    Returns:
        Filtered list of tools the agent is allowed to use.
    """
    default = permissions.get("*", "allow")
    return [t for t in tools if permissions.get(t["name"], default) != "deny"]


# --- Anthropic Executor ---


async def _run_anthropic(
    client: anthropic.AsyncAnthropic,
    model_id: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float | None,
    max_tokens: int = 4096,
    enable_caching: bool = True,
) -> dict[str, Any]:
    """Call the Anthropic Messages API with optional prompt caching."""
    # System prompt: convert to content blocks with cache_control
    if enable_caching:
        system_param: str | list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_param = system_prompt

    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system_param,
        "messages": messages,
    }

    # Tools: add cache_control to the LAST tool definition
    if tools:
        if enable_caching:
            cached_tools = [*tools]  # shallow copy the list
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = cached_tools
        else:
            kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = await client.messages.create(**kwargs)

    # Extract response data
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                {
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                }
            )

    return {
        "text": "\n".join(text_parts),
        "tool_calls": tool_calls,
        "finish_reason": response.stop_reason,  # "end_turn", "tool_use", etc.
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        },
        "raw_content": [
            {
                "type": b.type,
                **({"text": b.text} if b.type == "text" else {"id": b.id, "name": b.name, "input": b.input}),
            }
            for b in response.content
        ],
    }


def _anthropic_tool_results(tool_calls: list[dict[str, Any]], results: dict[str, str]) -> list[dict[str, Any]]:
    """Build Anthropic tool_result content blocks."""
    return [
        {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": results.get(tc["id"], "[no result]"),
        }
        for tc in tool_calls
    ]


# --- OpenAI Executor ---


async def _run_openai(
    client: openai.AsyncOpenAI,
    model_id: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float | None,
) -> dict[str, Any]:
    """Call the OpenAI Chat Completions API."""
    full_messages = [{"role": "system", "content": system_prompt}, *messages]

    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": full_messages,
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    message = choice.message

    # Extract tool calls
    tool_calls: list[dict[str, Any]] = []
    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                arguments = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                arguments = {"raw": tc.function.arguments}
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": arguments,
                }
            )

    # Extract usage with cache and reasoning details
    usage: dict[str, int] = {
        "input_tokens": response.usage.prompt_tokens if response.usage else 0,
        "output_tokens": response.usage.completion_tokens if response.usage else 0,
    }

    # Cache metrics (auto-populated by OpenAI, Z.ai, MiniMax)
    if response.usage:
        prompt_details = getattr(response.usage, "prompt_tokens_details", None)
        if prompt_details:
            cached = getattr(prompt_details, "cached_tokens", 0)
            usage["cached_tokens"] = cached or 0

        # Reasoning tokens (o3/o1 series)
        completion_details = getattr(response.usage, "completion_tokens_details", None)
        if completion_details:
            reasoning = getattr(completion_details, "reasoning_tokens", 0)
            usage["reasoning_tokens"] = reasoning or 0

    return {
        "text": message.content or "",
        "tool_calls": tool_calls,
        "finish_reason": choice.finish_reason,  # "stop", "tool_calls", etc.
        "usage": usage,
    }


def _create_client(provider: str) -> anthropic.AsyncAnthropic | openai.AsyncOpenAI:
    """Create a provider API client, validating the API key is present."""
    provider_config = get_provider_config(provider)
    api_key = os.environ.get(provider_config.env_key)
    if not api_key:
        raise ValueError(f"API key not found in environment variable: {provider_config.env_key}")

    if provider == PROVIDER_ANTHROPIC:
        return anthropic.AsyncAnthropic(api_key=api_key)
    return openai.AsyncOpenAI(api_key=api_key, base_url=provider_config.api_base)


# --- Main Loop ---
# TODO (Phase 7): Raw executors currently have no local filesystem or shell access.
# They can only use MCP tools (harness, yuppster). To enable code editing and
# command execution, add file/shell MCP tools to the harness server.
# See ROADMAP.md §Phase 7.


async def run_raw_executor(
    agent: AgentSpec,
    prompt: str,
    model: str,
    mcp_tools: list[dict[str, Any]] | None = None,
    tool_executor: Any | None = None,
    session_history_path: str | None = None,
    resource_catalog: list[dict[str, str]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    session_id: str | None = None,
    is_slack: bool = False,
    is_task: bool = False,
    session_context: dict[str, Any] | None = None,
    slack_session_id: str | None = None,
) -> ExecutorResult:
    """Run the raw executor loop.

    Args:
        agent: Agent configuration.
        prompt: User prompt / task.
        model: Model string (e.g., 'anthropic/claude-sonnet-4-6').
        mcp_tools: Pre-fetched MCP tool schemas (optional).
        tool_executor: Callable to execute tool calls (async fn(name, args) -> str).
        session_history_path: Path to load/save session history for cross-turn
            resumption. If provided, loads prior messages from disk at start and
            saves the full messages array at end.
        resource_catalog: MCP resources available via read_resource tool.
            Injected into the system prompt so the model knows what's available.
        on_event: Optional callback invoked with structured event dicts for each
            step of execution (init, assistant responses, tool calls/results,
            final result). Used by runners to emit StreamEvents for logging
            and future streaming.
        session_id: Optional session ID for session context in the system prompt.
        is_slack: Whether the session was triggered from Slack.
        is_task: Whether the session is executing a project task.
        session_context: Optional dict of session metadata stored at creation time.
        slack_session_id: Optional Slack session ID (channel:thread_ts:app_id).

    Returns:
        ExecutorResult with the final text and metadata.
    """

    def _emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    provider, model_id = parse_model_string(model)

    # Create client once (reused across all steps)
    client = _create_client(provider)

    # Filter tools by agent permissions. read_resource is exempt — resources are
    # read-only reference data, not actions, so "* deny" should not block them.
    available_tools = mcp_tools or []
    filtered_tools = filter_tools_by_permissions(available_tools, agent.tools)
    read_resource_tool = next((t for t in available_tools if t["name"] == "read_resource"), None)
    if read_resource_tool and read_resource_tool not in filtered_tools:
        filtered_tools.append(read_resource_tool)

    system_prompt = _build_system_prompt(
        agent,
        provider,
        model_id,
        resource_catalog=resource_catalog,
        session_id=session_id,
        is_slack=is_slack,
        is_task=is_task,
        session_context=session_context,
        slack_session_id=slack_session_id,
    )

    # Convert to provider format
    if provider == PROVIDER_ANTHROPIC:
        tool_schemas = convert_tools_to_anthropic(filtered_tools)
    elif is_openai_compatible(provider):
        tool_schemas = convert_tools_to_openai(filtered_tools)
    else:
        tool_schemas = []

    max_steps = agent.max_steps or DEFAULT_MAX_STEPS
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_write_tokens = 0
    total_reasoning_tokens = 0
    total_cost = 0.0
    step = 0
    start_time = time.monotonic()

    # Emit init event
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "model": model,
            "tools": [t["name"] for t in filtered_tools],
            "max_steps": max_steps,
            "has_history": session_history_path is not None,
        }
    )

    # Lazy import to avoid circular dependency (context.py imports from raw_executor)
    from ypl.agent_harness_service.executors.context import (
        compact_messages,
        load_session_history,
        prune_tool_results,
        save_pre_compaction_snapshot,
        save_session_history,
    )

    # Phase 3: Load prior messages from disk if available
    messages: list[dict[str, Any]] = []
    if session_history_path:
        loaded = load_session_history(session_history_path)
        if loaded is not None:
            prior_messages, prior_provider = loaded
            if prior_provider == provider:
                messages = prior_messages
                logger.info(
                    "Resumed session from disk",
                    path=session_history_path,
                    prior_message_count=len(messages),
                )
            else:
                logger.warning(
                    "Provider mismatch in session history, starting fresh",
                    expected=provider,
                    found=prior_provider,
                    path=session_history_path,
                )

    # Append the new user message. If history was error-saved mid-turn (e.g., API
    # failure after tool results), it may end with a user-role message (tool results).
    # Anthropic rejects consecutive user messages, so insert a synthetic assistant
    # message to maintain alternation.
    if messages and messages[-1].get("role") == "user":
        if provider == PROVIDER_ANTHROPIC:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "[Session resumed]"}]})
        elif is_openai_compatible(provider):
            messages.append({"role": "assistant", "content": "[Session resumed]"})
    messages.append({"role": "user", "content": prompt})

    # Initialize response and final_text before loop
    response: dict[str, Any] = {}
    final_text = ""
    compacted_this_turn = False  # Only one compaction attempt per turn

    while True:
        step += 1
        if step > max_steps:
            logger.warning("Max steps reached", model=model, steps=step, max_steps=max_steps)
            final_text = f"[STOPPED] Max steps ({max_steps}) reached."
            break

        # Pre-emptive overflow check
        estimated_tokens = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)
        context_limit = _CONTEXT_LIMITS.get(model_id, 128_000)
        context_usage_pct = estimated_tokens / context_limit * 100

        logger.info(
            "Raw executor step",
            model=model,
            step=step,
            message_count=len(messages),
            estimated_context_tokens=estimated_tokens,
            context_usage_pct=round(context_usage_pct, 1),
        )

        if estimated_tokens > context_limit - _RESERVED_BUFFER:
            # Phase 2: Try pruning and compaction before giving up
            compaction_cfg = agent.executor.compaction
            recovered = False

            if compaction_cfg.enabled:
                # Step 1: Prune old tool results
                messages, pruned_count = prune_tool_results(
                    messages,
                    system_prompt,
                    context_limit,
                    target_ratio=compaction_cfg.target_ratio,
                    protect_steps=compaction_cfg.prune_protect_steps,
                    tool_schemas=tool_schemas,
                )
                if pruned_count:
                    estimated_tokens = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)
                    logger.info(
                        "Post-prune token estimate",
                        model=model,
                        estimated_tokens=estimated_tokens,
                        pruned_count=pruned_count,
                    )

                # Step 2: If still over, compact with LLM (only once per turn)
                if estimated_tokens > context_limit - _RESERVED_BUFFER and not compacted_this_turn:
                    # Snapshot pre-compaction messages to disk before they're replaced
                    if session_history_path:
                        save_pre_compaction_snapshot(session_history_path, messages, provider)

                    messages, compaction_stats = await compact_messages(
                        messages,
                        prompt,
                        system_prompt,
                        compaction_cfg,
                        tool_schemas=tool_schemas,
                    )
                    compacted_this_turn = True
                    total_cost += compaction_stats.get("compaction_cost_usd", 0.0)
                    estimated_tokens = estimate_messages_tokens(messages, system_prompt, tool_schemas=tool_schemas)
                    logger.info(
                        "Post-compaction token estimate",
                        model=model,
                        estimated_tokens=estimated_tokens,
                        **compaction_stats,
                    )

                if estimated_tokens <= context_limit - _RESERVED_BUFFER:
                    recovered = True
                    context_usage_pct = estimated_tokens / context_limit * 100

            if not recovered:
                logger.warning(
                    "Pre-emptive context overflow",
                    model=model,
                    estimated_tokens=estimated_tokens,
                    limit=context_limit,
                )
                overflow_msg = (
                    f"[STOPPED] Context overflow: ~{estimated_tokens} tokens estimated, limit is {context_limit}."
                )
                last_text = response.get("text", "")
                final_text = f"{last_text}\n\n{overflow_msg}" if last_text else overflow_msg
                break

        try:
            if provider == PROVIDER_ANTHROPIC:
                assert isinstance(client, anthropic.AsyncAnthropic)
                response = await _run_anthropic(
                    client=client,
                    model_id=model_id,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tool_schemas,
                    temperature=agent.temperature,
                    enable_caching=True,
                )
            elif is_openai_compatible(provider):
                assert isinstance(client, openai.AsyncOpenAI)
                response = await _run_openai(
                    client=client,
                    model_id=model_id,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tool_schemas,
                    temperature=agent.temperature,
                )
            else:
                return ExecutorResult(text=f"[ERROR] Unsupported provider: {provider}")
        except Exception as e:
            logger.error("Raw executor API call failed", model=model, step=step, error=str(e), exc_info=True)
            _emit(
                {
                    "type": "error",
                    "message": f"API call failed at step {step}: {e}",
                    "step": step,
                    "model": model,
                }
            )
            # Save history before early return so the user's message is not lost across turns
            if session_history_path:
                save_session_history(session_history_path, messages, provider)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            # Emit terminal result event so callback consumers get consistent
            # cost/token totals even on API failures.
            _emit(
                {
                    "type": "result",
                    "subtype": "error",
                    "num_turns": step,
                    "estimated_total_cost_usd": total_cost,
                    "duration_ms": elapsed_ms,
                    "model": model,
                    "modelUsage": {
                        model: {
                            "inputTokens": total_input_tokens,
                            "outputTokens": total_output_tokens,
                            "cacheReadTokens": total_cache_read_tokens,
                            "cacheWriteTokens": total_cache_write_tokens,
                            "reasoningTokens": total_reasoning_tokens,
                            "estimatedCostUSD": total_cost,
                        },
                    },
                }
            )
            return ExecutorResult(
                text=f"[ERROR] API call failed at step {step}: {e}",
                estimated_cost_usd=total_cost,
                duration_ms=elapsed_ms,
                tokens={
                    "input": total_input_tokens,
                    "output": total_output_tokens,
                    "cache_read": total_cache_read_tokens,
                    "cache_write": total_cache_write_tokens,
                    "reasoning": total_reasoning_tokens,
                    "num_turns": step,
                },
            )

        # Track tokens and cost
        usage = response.get("usage", {})
        step_input = usage.get("input_tokens", 0)
        step_output = usage.get("output_tokens", 0)
        step_cache_read = usage.get("cache_read_input_tokens", 0) or usage.get("cached_tokens", 0)
        step_cache_write = usage.get("cache_creation_input_tokens", 0)
        step_reasoning = usage.get("reasoning_tokens", 0)

        total_input_tokens += step_input
        total_output_tokens += step_output
        total_cache_read_tokens += step_cache_read
        total_cache_write_tokens += step_cache_write
        total_reasoning_tokens += step_reasoning

        step_cost = _estimate_cost(model_id, usage)
        total_cost += step_cost

        if step_cache_read or step_cache_write:
            logger.info(
                "Cache metrics",
                model=model,
                step=step,
                cache_read_tokens=step_cache_read,
                cache_write_tokens=step_cache_write,
            )

        # Check for tool calls
        tool_calls = response.get("tool_calls", [])

        # Emit assistant event for every API response (with full content blocks)
        assistant_content: list[dict[str, Any]] = []
        if response.get("text"):
            assistant_content.append({"type": "text", "text": response["text"]})
        assistant_content.extend(
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]} for tc in tool_calls
        )
        _emit(
            {
                "type": "assistant",
                "message": {
                    "content": assistant_content,
                    "model": model,
                    "usage": {
                        "input_tokens": step_input,
                        "output_tokens": step_output,
                        "cache_read_tokens": step_cache_read,
                        "cache_write_tokens": step_cache_write,
                        "reasoning_tokens": step_reasoning,
                    },
                },
                "step": step,
                "estimated_cost_usd": step_cost,
            }
        )

        # --- Standard tool loop: continue when tool_calls present, stop when not ---
        if not tool_calls:
            # No tool calls — model is done. Capture final text and break.
            final_text = response.get("text", "")
            if provider == PROVIDER_ANTHROPIC:
                raw_content = response.get("raw_content", [])
                if not raw_content and final_text:
                    raw_content = [{"type": "text", "text": final_text}]
                if raw_content:
                    messages.append({"role": "assistant", "content": raw_content})
            elif is_openai_compatible(provider):
                messages.append({"role": "assistant", "content": final_text or ""})
            break

        # Tool calls present — emit any intermediate text before executing tools.
        intermediate_text = response.get("text", "")
        if intermediate_text:
            _emit(
                {
                    "type": "intermediate_text",
                    "text": intermediate_text,
                    "step": step,
                }
            )

        # TODO (Phase 3): Doom loop detection — disabled for now.
        # Needs per-step signature comparison (not per-call) to avoid false positives
        # with parallel tool calls. See PHASE3_PLAN.md §3.7 for design.

        # Append assistant message to history.
        if provider == PROVIDER_ANTHROPIC:
            messages.append({"role": "assistant", "content": response.get("raw_content", [])})
        elif is_openai_compatible(provider):
            # Build OpenAI-format assistant message with tool calls
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if response["text"]:
                assistant_msg["content"] = response["text"]
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

        # Execute tool calls concurrently.
        # Emit all tool_use events upfront, run tools in parallel, then emit results.
        tool_results: dict[str, str] = {}

        for tc in tool_calls:
            _emit(
                {
                    "type": "tool_use",
                    "name": tc["name"],
                    "input": tc["arguments"],
                    "tool_use_id": tc["id"],
                    "step": step,
                }
            )

        async def _exec_tool(tc: dict[str, Any], step: int = step) -> tuple[str, str, str, bool, int]:
            """Execute a single tool call. Returns (id, name, result, is_error, duration_ms)."""
            t_name, t_args, t_id = tc["name"], tc["arguments"], tc["id"]
            logger.info("Executing tool call", tool=t_name, step=step)
            t_start = time.monotonic()
            t_error = False
            if tool_executor:
                try:
                    t_result = await tool_executor(t_name, t_args)
                except Exception as e:
                    t_result = f"[ERROR] Tool execution failed: {e}"
                    t_error = True
                    logger.error("Tool execution failed", tool=t_name, error=str(e), exc_info=True)
            else:
                t_result = f"[ERROR] No tool executor configured. Cannot execute '{t_name}'."
                t_error = True
            t_dur = int((time.monotonic() - t_start) * 1000)
            spill_chars = agent.executor.max_tool_result_spill_chars
            if not t_error and spill_chars > 0 and len(t_result) > spill_chars and session_id:
                t_truncated = spill_tool_result_to_file(t_result, t_name, t_id, session_id)
            else:
                t_truncated = truncate_tool_result(
                    t_result,
                    max_chars=agent.executor.max_tool_result_chars,
                    max_lines=agent.executor.max_tool_result_lines,
                )
            return t_id, t_name, t_truncated, t_error, t_dur

        completed = await asyncio.gather(*[_exec_tool(tc) for tc in tool_calls])

        for t_id, t_name, t_truncated, t_error, t_dur in completed:
            tool_results[t_id] = t_truncated
            _emit(
                {
                    "type": "tool_result",
                    "name": t_name,
                    "tool_use_id": t_id,
                    "output": t_truncated[:500],  # cap for event storage
                    "is_error": t_error,
                    "duration_ms": t_dur,
                    "step": step,
                }
            )

        # Append tool results to messages
        if provider == PROVIDER_ANTHROPIC:
            messages.append(
                {
                    "role": "user",
                    "content": _anthropic_tool_results(tool_calls, tool_results),
                }
            )
        elif is_openai_compatible(provider):
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_results.get(tc["id"], "[no result]"),
                }
                for tc in tool_calls
            )

        # TODO (Phase 3): Budget enforcement — check total_cost against agent.max_budget_usd.
        # Requires adding max_budget_usd to AgentSpec and loading from config.json.

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Phase 3: Save full messages array to disk for cross-turn resumption
    if session_history_path:
        save_session_history(session_history_path, messages, provider)

    # Emit final result event
    _emit(
        {
            "type": "result",
            "subtype": (
                "stopped_context_overflow"
                if "[STOPPED] Context overflow" in final_text
                else ("error_max_turns" if final_text.startswith("[STOPPED]") else "success")
            ),
            "num_turns": step,
            "estimated_total_cost_usd": total_cost,
            "duration_ms": elapsed_ms,
            "model": model,
            "modelUsage": {
                model: {
                    "inputTokens": total_input_tokens,
                    "outputTokens": total_output_tokens,
                    "cacheReadTokens": total_cache_read_tokens,
                    "cacheWriteTokens": total_cache_write_tokens,
                    "reasoningTokens": total_reasoning_tokens,
                    "estimatedCostUSD": total_cost,
                },
            },
        }
    )

    return ExecutorResult(
        text=final_text or "(no output)",
        estimated_cost_usd=total_cost,
        duration_ms=elapsed_ms,
        tokens={
            "input": total_input_tokens,
            "output": total_output_tokens,
            "cache_read": total_cache_read_tokens,
            "cache_write": total_cache_write_tokens,
            "reasoning": total_reasoning_tokens,
            "num_turns": step,
        },
    )
