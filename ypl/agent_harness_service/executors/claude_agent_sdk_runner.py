"""Claude Agent SDK runner — in-process streaming replacement for Claude Code CLI.

Uses the Anthropic Python SDK to make direct HTTPS API calls, eliminating the
3–6 s Node.js + CLI cold-start overhead of ClaudeCodeRunner.

Architecture (Section 3.2 of AHS Executor v2 design doc):
  ClaudeAgentSdkRunner.run()
    ├─ anthropic.AsyncAnthropic().messages.create(...)
    │     ──── HTTPS ────▶ Anthropic API
    │     ◀─── response ────
    └─ on tool_use block:
         ├─ local disk tools (Bash, Read, Write, Edit, Glob, Grep) → BCH (CommandHandlerManager)
         └─ MCP tools (harness, yuppster) → MCPToolAccess.call_tool()

Tool list:
  - Local disk tools are declared as inline JSON Schema definitions (not via MCP server URL).
  - Harness + yuppster MCP tools are fetched from the running FastMCP servers and declared
    as MCP tool schemas. SessionPermissions.tool_permissions filtering is applied before
    declaring the tool list to the SDK.

Conversation resumption:
  - Message history is persisted to disk after each turn using the same format as
    RawExecutorRunner (context.py helpers).
  - context.llm_session_id is the SDK session UUID: None on first turn (runner
    generates one), then stored by service.py and passed back on subsequent turns.

Abort / cancellation:
  - No subprocess to kill. task.cancel() propagates CancelledError into
    ``await client.messages.create()``, which closes the underlying aiohttp
    connection automatically.
  - The finally block cleans up any partial state (no process handle to release).

Warm pool:
  - Not used. At session create, service.py checks
    ``executor_config.model == HARNESS_CLAUDE_SDK`` and skips pre-spawn.
"""

from __future__ import annotations
import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import anthropic

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.common.constants import (
    BLOCKED_HARNESS_TOOLS,
    PERM_DENY,
)
from ypl.agent_harness_service.common.models import expand_tool_permissions
from ypl.agent_harness_service.common.types import SessionPermissions, StreamEvent
from ypl.agent_harness_service.executors.context import (
    get_session_history_path,
    load_session_history,
    save_session_history,
)
from ypl.agent_harness_service.executors.raw_executor import (
    _estimate_cost,
    convert_tools_to_anthropic,
    filter_tools_by_permissions,
)
from ypl.agent_harness_service.executors.runner import (
    AGENT_RESPONSE_DEBUG,
    AgentRunner,
    RunContext,
)
from ypl.agent_harness_service.executors.system_prompt import build_system_prompt
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default Anthropic model when agent config does not specify llm_model.
# Override via env var for environment-specific defaults.
_DEFAULT_SDK_MODEL = os.environ.get("AHS_SDK_DEFAULT_MODEL", "claude-opus-4-5")

# Max response tokens per API call. Intentionally larger than the raw
# executor default (4096) to allow longer tool-heavy agent responses.
_MAX_TOKENS = 8096

# Local disk tools dispatched directly to BCH; all other tools go via MCP.
_LOCAL_DISK_TOOLS: frozenset[str] = frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})

# ---------------------------------------------------------------------------
# Inline JSON Schema definitions for local disk tools.
# These are sent as tool definitions directly in the Anthropic API request,
# not via an MCP server URL. The runner routes them to BCH on tool_use events.
# ---------------------------------------------------------------------------

_LOCAL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "Bash": {
        "name": "Bash",
        "description": (
            "Run a bash command in the session workspace sandbox. "
            "Output is capped at 100 KB; use file redirection for larger output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120)",
                },
                "description": {
                    "type": "string",
                    "description": "Optional human-readable description of what this command does",
                },
            },
            "required": ["command"],
        },
    },
    "Read": {
        "name": "Read",
        "description": "Read the contents of a file. Returns up to 2000 lines by default.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read",
                },
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "name": "Write",
        "description": "Write content to a file, creating it if it does not exist. Overwrites existing content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to write to",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "name": "Edit",
        "description": (
            "Replace a specific string in a file with a new string. "
            "old_string must match exactly (including indentation and whitespace). "
            "Will fail if old_string is not unique in the file unless replace_all=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to modify",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement string",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false)",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "Glob": {
        "name": "Glob",
        "description": "Find files matching a glob pattern, sorted by modification time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files (e.g. '**/*.py', 'src/**/*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current working directory)",
                },
            },
            "required": ["pattern"],
        },
    },
    "Grep": {
        "name": "Grep",
        "description": "Search for a regex pattern in files using ripgrep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Output format (default: files_with_matches)",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context to show before and after each match",
                },
                "-A": {
                    "type": "integer",
                    "description": "Lines to show after each match",
                },
                "-B": {
                    "type": "integer",
                    "description": "Lines to show before each match",
                },
            },
            "required": ["pattern"],
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_local_tool_list(agent_config: AgentConfig) -> list[dict[str, Any]]:
    """Return the subset of local disk tool schemas permitted by the agent config.

    Applies ``tool_permissions`` filtering: only include tools not denied.
    Uses the same expand_tool_permissions logic as the CLI runner.

    Args:
        agent_config: Agent configuration with tool_permissions.

    Returns:
        List of Anthropic-format tool definition dicts for local disk tools.
    """
    expanded = expand_tool_permissions(agent_config.tool_permissions)
    wildcard = expanded.get("*", "allow")
    result = []
    for tool_name, schema in _LOCAL_TOOL_SCHEMAS.items():
        harness_name = tool_name.lower()
        perm = expanded.get(harness_name, wildcard)
        if perm != PERM_DENY:
            result.append(schema)
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ClaudeAgentSdkRunner(AgentRunner):
    """In-process runner using the Anthropic Python SDK (``claude-agent-sdk``).

    Replaces ``ClaudeCodeRunner`` (spawns ``claude -p`` subprocess, 3–6 s cold)
    with a direct HTTPS call to the Anthropic Messages API, eliminating all
    Node.js and bwrap subprocess overhead for the executor layer.

    Identified by ``executor_config.model == "claude-agent-sdk"``; the actual
    LLM model (e.g. "claude-opus-4-5") is taken from ``agent_config.llm_model``.
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Run the agent loop and yield stream events.

        Implements the full agentic loop:
        1. Build system prompt and tool list.
        2. Connect to MCP servers (harness + yuppster) via MCPToolAccess.
        3. Repeatedly call ``client.messages.create()`` until ``stop_reason != "tool_use"``.
        4. Dispatch each tool_use block: local disk tools → BCH; MCP tools → MCPToolAccess.
        5. Persist message history for cross-turn resumption.
        6. Yield ``result`` event so service.py can store llm_session_id, cost, etc.

        Abort:
          task.cancel() → CancelledError propagates into ``await client.messages.create()``,
          closing the underlying aiohttp connection.  The finally block handles cleanup.

        Args:
            prompt: User message (enriched prompt from service.py).
            context: Run context (session_id, workspace, llm_session_id, …).

        Yields:
            StreamEvent objects: system, assistant, tool_use, tool_result, result, error.
        """
        from ypl.agent_harness_service.tools.mcp_client import MCPToolAccess

        session_id = context.session_id
        start_time = time.monotonic()

        # ------------------------------------------------------------------ #
        # Resolve permissions (fail-secure defaults).                          #
        # Must mirror the logic in ClaudeCodeRunner._build_args().            #
        # ------------------------------------------------------------------ #
        ctx = context.session_context or {}
        if "permissions" in ctx:
            perms = SessionPermissions.from_context(ctx)
        elif context.is_slack:
            perms = SessionPermissions.restricted()
            logger.warning(
                "SDK runner: Slack session missing permissions, defaulting to restricted",
                session_id=session_id,
            )
        else:
            perms = SessionPermissions.restricted()
            logger.warning(
                "SDK runner: Session missing permissions, defaulting to restricted",
                session_id=session_id,
            )

        # Merge session-level harness restrictions into agent-level tool config.
        # Deny privileged harness tools not in allowed_harness_tools — matches
        # the harnessed path which adds BLOCKED_HARNESS_TOOLS to --disallowedTools.
        effective_tools: dict[str, str] = dict(self.config.tool_permissions)
        if not perms.has_full_tool_access:
            for tool in BLOCKED_HARNESS_TOOLS:
                if tool not in perms.allowed_harness_tools:
                    effective_tools[tool] = PERM_DENY
        allowed_servers = frozenset(perms.allowed_servers)

        # ------------------------------------------------------------------ #
        # Conversation resumption.                                             #
        # context.llm_session_id is None on first turn; we generate a UUID.  #
        # Subsequent turns pass the stored UUID so we can load history.       #
        # ------------------------------------------------------------------ #
        sdk_session_id: str = context.llm_session_id or str(uuid.uuid4())
        history_path = get_session_history_path(session_id)
        messages: list[dict[str, Any]] = []

        if context.llm_session_id:
            loaded = load_session_history(history_path)
            if loaded:
                messages, _ = loaded
                logger.info(
                    "SDK runner: loaded conversation history",
                    session_id=session_id,
                    sdk_session_id=sdk_session_id,
                    message_count=len(messages),
                )
            else:
                logger.warning(
                    "SDK runner: history file missing or invalid, starting fresh",
                    session_id=session_id,
                    sdk_session_id=sdk_session_id,
                )

        # ------------------------------------------------------------------ #
        # Emit system event.  service.py reads session_id from this event     #
        # on the first turn to initialise llm_session_id in the DB.          #
        # ------------------------------------------------------------------ #
        yield StreamEvent(
            type="system",
            raw={"type": "system", "session_id": sdk_session_id},
        )

        # ------------------------------------------------------------------ #
        # Build system prompt.                                                 #
        # ------------------------------------------------------------------ #
        system_prompt = build_system_prompt(
            self.config.name,
            session_id=session_id if self.config.has_mcp else None,
            slack_session_id=context.slack_session_id,
            is_slack=context.is_slack,
            is_task=context.is_task,
            session_context=context.session_context,
            additional_system_prompt=self.config.additional_system_prompt,
            # SDK runner inlines skill content (same as raw executor) because
            # there is no native /skill command for this executor type.
            has_native_skills=False,
        )

        # ------------------------------------------------------------------ #
        # Resolve LLM model.                                                   #
        # agent_config.llm_model is the Anthropic model ID (e.g. "claude-opus-4-5").
        # executor_config.model = "claude-agent-sdk" is the executor type
        # identifier, NOT the model passed to the API.                        #
        # ------------------------------------------------------------------ #
        llm_model: str = self.config.llm_model or _DEFAULT_SDK_MODEL
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.AsyncAnthropic(api_key=api_key)

        # ------------------------------------------------------------------ #
        # Telemetry accumulators                                               #
        # ------------------------------------------------------------------ #
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read = 0
        total_cache_write = 0
        num_agent_turns = 0
        result_subtype = "success"
        requesting_user_id = ctx.get("current_turn_user_id") or ctx.get("user_id")

        # ------------------------------------------------------------------ #
        # Main agent loop                                                      #
        # ------------------------------------------------------------------ #
        try:
            async with MCPToolAccess(
                session_id,
                effective_tools,
                allowed_servers=allowed_servers,
                user_id=requesting_user_id,
                agent_name=self.config.name,
            ) as mcp:
                # Build MCP tools in Anthropic format and apply permission filtering.
                mcp_tool_schemas = convert_tools_to_anthropic(mcp.mcp_tools)
                mcp_tool_schemas = filter_tools_by_permissions(mcp_tool_schemas, effective_tools)

                # Local disk tools (inline JSON schema; not via MCP server URL).
                local_tool_schemas = _build_local_tool_list(self.config)

                # Full tool list: local tools first so identical names (if any) prefer local.
                all_tools: list[dict[str, Any]] = [*local_tool_schemas, *mcp_tool_schemas]

                # Set of currently-allowed local tool names for dispatch routing.
                local_tool_names: frozenset[str] = frozenset(s["name"] for s in local_tool_schemas)

                logger.info(
                    "SDK runner starting agent loop",
                    session_id=session_id,
                    agent_name=self.config.name,
                    model=llm_model,
                    local_tools=sorted(local_tool_names),
                    mcp_tool_count=len(mcp_tool_schemas),
                    sdk_session_id=sdk_session_id,
                    resuming=bool(context.llm_session_id),
                )

                # Append the incoming user message to the conversation.
                messages.append({"role": "user", "content": prompt})

                # Agent loop: run up to max_turns Anthropic API calls per AHS turn.
                for _turn in range(self.config.max_turns):
                    num_agent_turns += 1

                    response = await client.messages.create(
                        model=llm_model,
                        max_tokens=_MAX_TOKENS,
                        system=system_prompt,
                        messages=cast(list[anthropic.types.MessageParam], messages),
                        tools=all_tools or anthropic.NOT_GIVEN,  # type: ignore[arg-type]
                    )

                    # Accumulate token usage for cost estimation.
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    total_cache_read += getattr(response.usage, "cache_read_input_tokens", 0) or 0
                    total_cache_write += getattr(response.usage, "cache_creation_input_tokens", 0) or 0

                    # Parse assistant response into text + tool_use blocks.
                    assistant_content: list[dict[str, Any]] = []
                    text_parts: list[str] = []
                    tool_use_blocks: list[dict[str, Any]] = []

                    for block in response.content:
                        if block.type == "text":
                            text_parts.append(block.text)
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            tool_use_blocks.append(
                                {
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                }
                            )
                            assistant_content.append(
                                {
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                }
                            )

                    # Append the assistant turn to history before dispatching tools.
                    messages.append({"role": "assistant", "content": assistant_content})

                    # Yield assistant event (text chunks).
                    if text_parts:
                        text = "\n".join(text_parts)
                        yield StreamEvent(
                            type="assistant",
                            raw={
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": text}],
                                },
                            },
                        )
                        if AGENT_RESPONSE_DEBUG:
                            logger.debug(
                                "SDK runner assistant text",
                                session_id=session_id,
                                turn=_turn,
                                excerpt=text[:200],
                            )

                    # Stop condition: model returned end_turn or did not request tools.
                    if response.stop_reason != "tool_use" or not tool_use_blocks:
                        break

                    # -------------------------------------------------------- #
                    # Dispatch tool calls                                       #
                    # -------------------------------------------------------- #
                    from ypl.agent_harness_service.tools.workspace_tools import get_command_handler_manager

                    bch_manager = get_command_handler_manager(session_id)
                    tool_result_blocks: list[dict[str, Any]] = []

                    for tc in tool_use_blocks:
                        tool_id: str = tc["id"]
                        tool_name: str = tc["name"]
                        tool_input: dict[str, Any] = tc["input"]

                        # Yield tool_use event (service.py records these for billing/logging).
                        yield StreamEvent(
                            type="tool_use",
                            raw={
                                "type": "tool_use",
                                "tool_use_id": tool_id,
                                "tool_name": tool_name,
                                "input": tool_input,
                            },
                        )

                        if AGENT_RESPONSE_DEBUG:
                            logger.debug(
                                "SDK runner tool call",
                                session_id=session_id,
                                tool=tool_name,
                                turn=_turn,
                            )

                        # Route dispatch:
                        #   local disk tool → BCH (CommandHandlerManager)
                        #   everything else → MCPToolAccess (FastMCP HTTP)
                        try:
                            if tool_name in local_tool_names:
                                if bch_manager is not None:
                                    tool_result = await bch_manager.call_tool(tool_name, tool_input)
                                else:
                                    # BCH not registered — AHS configuration error.
                                    tool_result = (
                                        f"[ERROR] BCH (CommandHandlerManager) is not registered for "
                                        f"session {session_id!r}. Cannot execute local tool '{tool_name}'. "
                                        "This is an AHS configuration error; please report it."
                                    )
                                    logger.error(
                                        "SDK runner: BCH manager not registered",
                                        session_id=session_id,
                                        tool=tool_name,
                                    )
                            else:
                                tool_result = await mcp.call_tool(tool_name, tool_input)
                        except asyncio.CancelledError:
                            raise  # Propagate cancellation immediately
                        except Exception as exc:
                            tool_result = f"[ERROR] Tool '{tool_name}' raised an exception: {exc}"
                            logger.error(
                                "SDK runner: tool dispatch error",
                                session_id=session_id,
                                tool=tool_name,
                                error=str(exc),
                                exc_info=exc,
                            )

                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": tool_result,
                            }
                        )

                        # Yield tool_result event.
                        yield StreamEvent(
                            type="tool_result",
                            raw={
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "tool_name": tool_name,
                                "content": tool_result,
                            },
                        )

                    # Append all tool results as a user message (Anthropic format).
                    messages.append({"role": "user", "content": tool_result_blocks})

                else:
                    # Loop exhausted without a clean end_turn — agent hit max_turns.
                    result_subtype = "error_max_turns"
                    logger.warning(
                        "SDK runner: hit max_turns without end_turn",
                        session_id=session_id,
                        max_turns=self.config.max_turns,
                        num_agent_turns=num_agent_turns,
                    )

        except asyncio.CancelledError:
            # Task was cancelled (e.g. stop_session() called while streaming).
            # The underlying aiohttp connection is cancelled automatically by asyncio.
            # Save partial history before re-raising so the next turn can resume.
            logger.info(
                "SDK runner: task cancelled, saving partial history",
                session_id=session_id,
                turns_completed=num_agent_turns,
            )
            if messages:
                save_session_history(history_path, messages, "anthropic")
            raise  # Re-raise CancelledError — do NOT yield a result event here
        except Exception as exc:
            logger.error(
                "SDK runner: unhandled error in agent loop",
                session_id=session_id,
                error=str(exc),
                exc_info=exc,
            )
            yield StreamEvent(type="error", raw={"error": str(exc)})
            result_subtype = "error"
        finally:
            # ---------------------------------------------------------------- #
            # Cleanup (no subprocess to kill).                                  #
            # aiohttp cancels in-flight requests when the task is cancelled.   #
            # This block is intentionally minimal — no process handle to       #
            # release, no temp files to unlink.                                #
            # ---------------------------------------------------------------- #
            pass

        # ------------------------------------------------------------------ #
        # Persist conversation history for next turn.                         #
        # ------------------------------------------------------------------ #
        save_session_history(history_path, messages, "anthropic")

        # ------------------------------------------------------------------ #
        # Emit result event.                                                   #
        # service.py reads session_id (→ llm_session_id), cost_usd,          #
        # duration_ms, and num_turns from this event.                         #
        # ------------------------------------------------------------------ #
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        usage_dict: dict[str, Any] = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cache_creation_input_tokens": total_cache_write,
            "cache_read_input_tokens": total_cache_read,
        }
        cost_usd = _estimate_cost(llm_model, usage_dict)

        logger.info(
            "SDK runner finished",
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            num_agent_turns=num_agent_turns,
            result_subtype=result_subtype,
            cost_usd=round(cost_usd, 6),
            duration_ms=elapsed_ms,
        )

        yield StreamEvent(
            type="result",
            raw={
                "type": "result",
                "subtype": result_subtype,
                "session_id": sdk_session_id,
                "cost_usd": cost_usd,
                "estimated_cost_usd": cost_usd,
                "duration_ms": elapsed_ms,
                "num_turns": num_agent_turns,
                "usage": usage_dict,
            },
        )
