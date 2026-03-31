"""Data models for multi-model agent orchestration.

These models support the agent run loop, subagent spawning, and
multi-provider executor configuration.
"""

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ypl.agent_harness_service.common.constants import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_S,
    HARNESS_TO_CLI_TOOL_MAP,
    TOOLSET_PREFIX,
    TOOLSETS,
)

ToolPermission = Literal["allow", "deny", "ask"]


def _default_tools() -> dict[str, ToolPermission]:
    return {"*": "allow"}


def expand_tool_permissions(
    tool_permissions: dict[str, ToolPermission],
) -> dict[str, ToolPermission]:
    """Expand @toolset references in tool_permissions into individual tool entries.

    Example: {"*": "deny", "@readonly": "allow"}
         ->  {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "webfetch": "allow"}

    Explicit per-tool entries take precedence over toolset-expanded entries.
    Raises ValueError for unknown @toolset references.
    """
    expanded: dict[str, ToolPermission] = {}
    explicit: dict[str, ToolPermission] = {}

    for key, perm in tool_permissions.items():
        if key.startswith(TOOLSET_PREFIX):
            if key not in TOOLSETS:
                raise ValueError(f"Unknown toolset: {key!r}. Known toolsets: {sorted(TOOLSETS.keys())}")
            for tool in TOOLSETS[key]:
                expanded[tool] = perm
        else:
            explicit[key] = perm

    # Explicit per-tool entries override toolset-expanded entries
    expanded.update(explicit)
    return expanded


def tool_permissions_to_cli_flags(
    tool_permissions: dict[str, ToolPermission],
) -> tuple[list[str] | None, list[str] | None]:
    """Convert an expanded tool_permissions dict to (allowed_tools, disallowed_tools) for CLI.

    Maps generic tool names to Claude Code CLI names via HARNESS_TO_CLI_TOOL_MAP.
    Tools without a CLI mapping are skipped (they're MCP-only tools like new_task).

    Assumes input is already expanded (no @toolset references).
    Returns (allowed, disallowed) — at most one is non-None.

    Note: "ask" permissions are not representable in CLI flags (no --askTools equivalent).
    In default-deny mode, "ask" tools are effectively denied (not in the allowed list).
    In default-allow mode, "ask" tools are effectively allowed (not in the denied list).
    """
    wildcard = tool_permissions.get("*")

    if wildcard == "deny":
        # Default-deny: collect explicitly allowed tools, mapped to CLI names
        allowed = [
            HARNESS_TO_CLI_TOOL_MAP[k]
            for k, v in tool_permissions.items()
            if k != "*" and v == "allow" and k in HARNESS_TO_CLI_TOOL_MAP
        ]
        return (allowed, None) if allowed else ([], None)

    # Default-allow (wildcard == "allow" or absent): collect explicitly denied tools
    disallowed = [
        HARNESS_TO_CLI_TOOL_MAP[k]
        for k, v in tool_permissions.items()
        if k != "*" and v == "deny" and k in HARNESS_TO_CLI_TOOL_MAP
    ]
    return (None, disallowed) if disallowed else (None, None)


class CompactionConfig(BaseModel):
    """Configuration for within-turn context compaction (Phase 2)."""

    enabled: bool = True
    model: str = "anthropic/claude-haiku-4-5"
    prune_protect_steps: int = 2
    target_ratio: float = 0.7


class HistoryConfig(BaseModel):
    """Configuration for cross-turn session history persistence (Phase 3).

    When enabled, the raw executor saves/loads the full messages array
    (including all tool_use and tool_result blocks) to disk between turns.
    Context limits are handled by Phase 2 pruning/compaction.
    """

    enabled: bool = True


class RetryConfig(BaseModel):
    """Auto-retry configuration for silent CLI crashes.

    A "silent crash" is when the CLI subprocess exits with a non-zero return code
    and produces *zero meaningful stream events* — i.e. the only event emitted (if
    any) was the synthetic ``"error"`` event injected by the runner itself, with no
    real ``system``, ``assistant``, ``user``, or ``result`` events.  This pattern
    occurs during transient Anthropic API errors or network blips where the Claude
    Code CLI exits cleanly (code 1) without writing anything to stdout or stderr.

    The retry spawns a fresh subprocess with identical prompt and context.  Any
    pre-spawn optimisation from the first attempt has already been consumed, so the
    retry always uses the normal spawn path.  Error events from failed attempts are
    suppressed (not forwarded to the session consumer) so the DB only records the
    outcome of the final attempt.

    Attributes:
        max_retries: Maximum number of retry attempts after the initial run.
            ``0`` disables retries entirely.  Defaults to ``1`` so that a single
            transient silent crash does not permanently fail the session.
        on_empty_result: When ``True`` (the default), only retry when the run
            produced zero meaningful events (pure silent crash).  Set to ``False``
            to disable the trigger condition check and never auto-retry (equivalent
            to ``max_retries=0`` but kept separate for future trigger modes).
    """

    max_retries: int = 1
    on_empty_result: bool = True


class ExecutorConfig(BaseModel):
    """How an agent executes: raw (direct API) or harnessed (CLI wrapper).

    The ``model`` field has different semantics depending on ``type``:

    * **type="harnessed"** — ``model`` is the CLI harness name
      (e.g., ``"claude-code-cli"``, ``"codex-cli"``).
    * **type="raw"** — ``model`` is the LLM model in ``provider/model_id``
      format (e.g., ``"anthropic/claude-sonnet-4-6"``).
    """

    type: Literal["raw", "harnessed"] = "harnessed"
    model: str | None = None
    # For harnessed: which CLI harness to use. None for raw executors.
    harness: Literal["claude-code-cli", "codex-cli", "codex-app-server"] | None = None
    # Context management (Phase 1): truncate large tool results
    max_tool_result_chars: int = 30_000
    max_tool_result_lines: int = 1_000
    # Spill-to-file threshold: if a tool result exceeds this many characters,
    # write it to a file in the session workspace instead of truncating.
    # The agent receives the file path and is instructed to read the file.
    # Set to 0 to disable spilling (fall back to truncation only).
    max_tool_result_spill_chars: int = 100_000
    # Context management (Phase 2): within-turn pruning and compaction
    compaction: CompactionConfig = CompactionConfig()
    # Context management (Phase 3): cross-turn session history persistence
    history: HistoryConfig = HistoryConfig()
    # Auto-retry on silent CLI crashes (exit code != 0, zero meaningful stdout events)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @model_validator(mode="after")
    def _set_defaults(self) -> "ExecutorConfig":
        if self.type == "harnessed" and self.model is None:
            self.model = "claude-code-cli"
        return self

    @property
    def llm_model(self) -> str | None:
        """The LLM model identifier, or None for harnessed executors."""
        return self.model if self.type == "raw" else None


class AgentSpec(BaseModel):
    """Resolved runtime specification that executors consume.

    Design: AgentConfig (config.py) is the filesystem-level representation
    loaded from config.json. AgentSpec is the resolved, executor-ready form
    used by the raw executor and orchestration layer. AgentConfig is converted
    to AgentSpec at dispatch time (e.g., in RawExecutorRunner and _execute_raw).

    Both share ExecutorConfig for the executor type/model pair, ensuring
    a single source of truth for executor configuration.
    """

    name: str
    description: str | None = None
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    tools: dict[str, ToolPermission] = Field(default_factory=_default_tools)
    # Which agent types this agent can spawn via new_task. Empty = none. ["*"] = all.
    allowed_subagents: list[str] = Field(default_factory=list)
    max_steps: int | None = DEFAULT_MAX_STEPS
    timeout_s: int = DEFAULT_TIMEOUT_S
    temperature: float | None = None
    streaming: bool = True
    additional_system_prompt: str | None = None
    # Deferred MCP tools to pre-load via Phase 0 ToolSearch — see AgentConfig.required_tools.
    required_tools: list[str] = Field(default_factory=list)
    # Extra parameters passed directly to the LLM API (e.g. thinking mode).
    # For OpenAI-compatible providers these are merged into the request kwargs.
    # Example: {"thinking": {"type": "enabled"}} for GLM-5 / Kimi K2.5
    model_parameters: dict[str, Any] | None = None


class ExecutorResult(BaseModel):
    """Result from running an agent (raw or harnessed)."""

    text: str
    estimated_cost_usd: float | None = None
    duration_ms: int | None = None
    tokens: dict[str, int] | None = None  # {input, output, cache_read, cache_write, reasoning, num_turns}
    session_id: str | None = None  # For resumption


class SubagentSession(BaseModel):
    """In-memory tracking of a subagent's isolated session."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_session_id: str
    agent_name: str
    model: str  # Resolved model (e.g., "anthropic/claude-sonnet-4-6")
    executor_type: str  # "raw" or "harnessed"
    status: Literal["running", "completed", "error"] = "running"
    result: ExecutorResult | None = None
    time_created: float = Field(default_factory=time.time)
    time_completed: float | None = None
