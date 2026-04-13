"""Agent configuration loading from filesystem.

Each agent has a directory under AHS_AGENTS_DIR with:
- config.json: tool permissions, model, limits
- ROLE.md: agent's role, expertise, and personality
"""

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ypl.agent_harness_service.common.constants import (
    AHS_AGENTS_DIR,
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_S,
    HARNESS_TO_CLI_TOOL_MAP,
)
from ypl.agent_harness_service.common.models import (
    ExecutorConfig,
    ToolPermission,
    _default_tools,
    expand_tool_permissions,
)
from ypl.structured_logger import get_logger

logger = get_logger()


class SandboxConfig(BaseModel):
    enabled: bool = True
    auto_allow_bash_if_sandboxed: bool = True
    bwrap_enabled: bool = True  # Bubblewrap sandboxing for bash commands (default on)


class AgentConfig(BaseModel):
    """Parsed agent configuration from config.json + identity files.

    This is the filesystem-level config (loaded from config.json). For orchestration,
    AgentSpec (in models.py) is the resolved runtime specification that executors
    actually consume. AgentConfig is converted to AgentSpec at dispatch time.
    """

    name: str
    config_dir: str
    display_name: str = ""
    description: str | None = None

    # Executor configuration: type (raw/harnessed) and model (CLI name or LLM model).
    executor_config: ExecutorConfig = Field(default_factory=ExecutorConfig)
    # LLM model override for harnessed executors (passed as --model to CLI).
    # Only set programmatically (e.g., by _build_runner_config during subagent spawning).
    llm_model: str | None = None

    @property
    def model(self) -> str:
        """The LLM model to use (for CLI --model flag or raw API calls).

        For harnessed executors: returns llm_model override (or "" for CLI default).
        For raw executors: returns executor_config.model (provider/model_id format).
        """
        if self.llm_model:
            return self.llm_model
        if self.executor_config.type == "raw":
            return self.executor_config.model or ""
        return ""

    # permissions
    tool_permissions: dict[str, ToolPermission] = Field(default_factory=_default_tools)
    allowed_subagents: list[str] = Field(default_factory=list)
    # TODO: deprecate this as we are moving to use direct tool setup.
    has_mcp: bool = False

    default_repo: str = "yupp-agent"
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    max_turns: int = DEFAULT_MAX_TURNS
    max_budget_usd: float = 2.0
    feedback_probability: float = 0.2  # 0.0 = disabled, 0.2 = 20% chance
    feedback_min_turns: int = 5  # Minimum completed turns before auto-feedback
    timeout_s: int = DEFAULT_TIMEOUT_S
    allowed_gateways: list[str] = Field(default_factory=lambda: ["*"])

    # Additional system prompt appended after on-disk identity files.
    # Used by DB-only agents (e.g., personal agents created via create_agent MCP tool).
    additional_system_prompt: str | None = None

    # Agent-to-Agent (A2A) messaging authorization.
    # Lists the agent names that this agent is permitted to send messages to.
    # Use ``'*'`` as a wildcard to allow messaging any agent (unrestricted).
    # Omitting the field (or providing an empty list) enforces deny-by-default:
    # the agent cannot initiate messages to any other agent.
    #
    # Example (config.json):
    #   "allowed_to_message": ["eng-raccoon", "sre-james"]
    #   "allowed_to_message": ["*"]   # unrestricted outbound messaging
    allowed_to_message: list[str] = Field(default_factory=list)

    # Deferred MCP tools to pre-load at session start via a Phase 0 ToolSearch call.
    # When non-empty, build_system_prompt() injects a "## Phase 0: Load Tools" section
    # that instructs the agent to call ToolSearch(query="select:<tools>") as its very
    # first action — eliminating per-session tool-discovery turns.
    # Format: list of fully-qualified tool names, e.g.
    #   ["mcp__yuppster-mcp-server__query_agentdb", "mcp__harness__send_slack_message"]
    required_tools: list[str] = Field(default_factory=list)


_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_agent_name(name: str) -> None:
    """Raise ValueError if the agent name contains path-traversal characters."""
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"Invalid agent name: {name!r}. Must match [a-z0-9-]+.")


def read_file_if_exists(path: str) -> str | None:
    """Read a file's contents, returning None if it doesn't exist."""
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


@lru_cache(maxsize=32)
def load_agent_config(name: str) -> AgentConfig | None:
    """Load agent configuration from its directory.

    Args:
        name: Agent name (e.g., 'sre-james')

    Returns:
        AgentConfig if the directory and config.json exist, None otherwise.
    """
    validate_agent_name(name)
    config_dir = os.path.join(AHS_AGENTS_DIR, name)
    config_path = os.path.join(config_dir, "config.json")

    if not os.path.isfile(config_path):
        logger.warning("Agent config not found", name=name, config_path=config_path)
        return None

    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load agent config", name=name, error=str(e))
        return None

    sandbox_raw = raw.get("sandbox", {})
    sandbox = SandboxConfig(
        enabled=sandbox_raw.get("enabled", True),
        auto_allow_bash_if_sandboxed=sandbox_raw.get("autoAllowBashIfSandboxed", True),
        bwrap_enabled=sandbox_raw.get("bwrapEnabled", True),
    )

    # Resolve tool_permissions: prefer explicit field, fall back from legacy allowed_tools
    tool_perms: dict[str, ToolPermission]
    try:
        raw_tool_perms = raw.get("tool_permissions")
        if raw_tool_perms is not None:
            tool_perms = expand_tool_permissions(raw_tool_perms)
        elif "allowed_tools" in raw and raw["allowed_tools"]:
            # Backward compat: convert legacy allowed_tools list → dict.
            # DEPRECATED: allowed_tools is replaced by tool_permissions. Do not use in new configs.
            # This fallback will be removed in a future release.
            logger.warning(
                "Legacy allowed_tools detected, convert to tool_permissions",
                name=name,
            )
            # Reverse map: PascalCase CLI names → lowercase generic names
            cli_to_generic = {v: k for k, v in HARNESS_TO_CLI_TOOL_MAP.items()}
            tool_perms = {"*": "deny"}
            for tool in raw["allowed_tools"]:
                normalized = cli_to_generic.get(tool, tool.lower())
                tool_perms[normalized] = "allow"
        else:
            tool_perms = {"*": "allow"}
    except ValueError as e:
        logger.error("Invalid tool permissions in config", name=name, error=str(e))
        return None

    # Resolve executor_config: Pydantic validates type/model.
    # Default is ExecutorConfig(type="harnessed", model="claude-code-cli").
    exec_cfg_raw = raw.get("executor_config", {})

    config = AgentConfig(
        name=name,
        config_dir=config_dir,
        display_name=raw.get("display_name", ""),
        description=raw.get("description"),
        executor_config=exec_cfg_raw,
        default_repo=raw.get("default_repo", "yupp-agent"),
        tool_permissions=tool_perms,
        sandbox=sandbox,
        max_turns=raw.get("max_turns", DEFAULT_MAX_TURNS),
        max_budget_usd=raw.get("max_budget_usd", 2.0),
        has_mcp=raw.get("has_mcp", False),
        feedback_probability=raw.get("feedback_probability", 0.2),
        feedback_min_turns=raw.get("feedback_min_turns", 5),
        timeout_s=raw.get("timeout_s", DEFAULT_TIMEOUT_S),
        allowed_subagents=raw.get("allowed_subagents", []),
        allowed_gateways=raw.get("allowed_gateways", ["*"]),
        required_tools=raw.get("required_tools", []),
        allowed_to_message=raw.get("allowed_to_message", []),
    )

    logger.info("Loaded agent config", name=name, model=config.model, max_turns=config.max_turns)
    return config


def load_agent_config_from_db(agent: Any) -> AgentConfig:
    """Build an AgentConfig from a DB Agent record (for agents without on-disk config).

    Args:
        agent: An Agent DB model instance with .name, .config (JSONB), and .additional_system_prompt.

    Returns:
        AgentConfig built from the DB record's config JSONB.
    """
    raw = agent.config or {}

    sandbox_raw = raw.get("sandbox", {})
    sandbox = SandboxConfig(
        enabled=sandbox_raw.get("enabled", True),
        auto_allow_bash_if_sandboxed=sandbox_raw.get("autoAllowBashIfSandboxed", True),
        bwrap_enabled=sandbox_raw.get("bwrapEnabled", True),
    )

    raw_tool_perms = raw.get("tool_permissions")
    tool_perms: dict[str, ToolPermission]
    if raw_tool_perms is not None:
        tool_perms = expand_tool_permissions(raw_tool_perms)
    else:
        tool_perms = {"*": "allow"}

    exec_cfg_raw = raw.get("executor_config", {})
    config_dir = os.path.join(AHS_AGENTS_DIR, agent.name)

    return AgentConfig(
        name=agent.name,
        config_dir=config_dir,
        display_name=agent.display_name or agent.name,
        description=agent.description,
        executor_config=exec_cfg_raw,
        default_repo=raw.get("default_repo", "yupp-agent"),
        tool_permissions=tool_perms,
        sandbox=sandbox,
        max_turns=raw.get("max_turns", DEFAULT_MAX_TURNS),
        max_budget_usd=raw.get("max_budget_usd", 2.0),
        has_mcp=raw.get("has_mcp", True),
        feedback_probability=raw.get("feedback_probability", 0.2),
        feedback_min_turns=raw.get("feedback_min_turns", 5),
        timeout_s=raw.get("timeout_s", DEFAULT_TIMEOUT_S),
        allowed_subagents=raw.get("allowed_subagents", []),
        allowed_gateways=raw.get("allowed_gateways", ["*"]),
        additional_system_prompt=agent.additional_system_prompt,
        required_tools=raw.get("required_tools", []),
        allowed_to_message=raw.get("allowed_to_message", []),
    )


@lru_cache
def discover_agents() -> dict[str, AgentConfig]:
    """Discover all agent configs under AHS_AGENTS_DIR.

    Returns:
        Dict mapping name to AgentConfig.
    """
    agents: dict[str, AgentConfig] = {}
    agents_dir = Path(AHS_AGENTS_DIR)

    if not agents_dir.is_dir():
        logger.warning("Agents directory not found", path=AHS_AGENTS_DIR)
        return agents

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue
        config = load_agent_config(entry.name)
        if config:
            agents[entry.name] = config

    logger.info("Discovered agents", count=len(agents), names=list(agents.keys()))
    return agents


def clear_config_cache() -> None:
    """Clear the configuration cache. Useful for testing."""
    discover_agents.cache_clear()
    load_agent_config.cache_clear()
