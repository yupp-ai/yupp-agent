"""Constants for Agent Harness Service."""

import os
import secrets
import uuid as _uuid
from contextvars import ContextVar
from typing import Any

# Filesystem paths (configurable via env vars for local dev)
# In production: AHS_DATA_DIR=/data (mounted volume).
# In local dev: falls back to /tmp/ahs when /data doesn't exist.
_DEFAULT_DATA_DIR = "/data"
_LOCAL_DATA_DIR = os.path.join("/tmp", "ahs")
AHS_DATA_DIR = os.environ.get(
    "AHS_DATA_DIR",
    _DEFAULT_DATA_DIR if os.path.isdir(_DEFAULT_DATA_DIR) else _LOCAL_DATA_DIR,
)

# Agent configs: production uses AHS_DATA_DIR/agents, local dev uses deploy/agent_configs/ in-tree.
_DEPLOY_AGENTS_DIR = os.path.join(os.path.dirname(__file__), "..", "deploy", "agent_configs")
AHS_AGENTS_DIR = os.environ.get(
    "AHS_AGENTS_DIR",
    os.path.join(AHS_DATA_DIR, "agents") if os.path.isdir(os.path.join(AHS_DATA_DIR, "agents")) else _DEPLOY_AGENTS_DIR,
)
# Shared prompt files: production uses AHS_DATA_DIR/shared, local dev uses deploy/shared/ in-tree.
_DEPLOY_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "deploy", "shared")
AHS_SHARED_DIR = os.environ.get(
    "AHS_SHARED_DIR",
    os.path.join(AHS_DATA_DIR, "shared") if os.path.isdir(os.path.join(AHS_DATA_DIR, "shared")) else _DEPLOY_SHARED_DIR,
)
AHS_REPOS_DIR = os.environ.get("AHS_REPOS_DIR", os.path.join(AHS_DATA_DIR, "repos"))

# Skills directory: contains SKILL.md files loaded on-demand by Claude Code.
# For non-Claude-Code executors, build_system_prompt() inlines skill content
# into the system prompt since those executors cannot invoke skills.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AHS_SKILLS_DIR = os.environ.get(
    "AHS_SKILLS_DIR",
    os.path.join(_REPO_ROOT, ".agents", "skills"),
)
# Deprecated alias — use AHS_SESSIONS_DIR instead.
AHS_WORKSPACES_DIR = os.environ.get("AHS_WORKSPACES_DIR", os.path.join(AHS_DATA_DIR, "sessions"))
AHS_SESSIONS_DIR = os.environ.get("AHS_SESSIONS_DIR", AHS_WORKSPACES_DIR)

# Infrastructure subdirectories inside a session workspace that are NOT worktrees.
# Used by resolve_workspace, scan_session_worktrees, create_pr, and cleanup to
# distinguish session infrastructure from git worktrees.
SESSION_INFRA_DIRS = frozenset({"history", "attachments", "agent_memories", "tool-results"})

# Per-agent persistent memory directory (survives across sessions).
AHS_MEMORIES_DIR = os.environ.get("AHS_MEMORIES_DIR", os.path.join(AHS_DATA_DIR, "memories"))

# ---------------------------------------------------------------------------
# Bwrapped Command Handler (BCH) settings
# ---------------------------------------------------------------------------

# Seconds of inactivity before the BCH proxy process is killed.
# The next call_tool() transparently restarts it (Go binary restarts in ~2 ms).
AHS_BCH_IDLE_TIMEOUT_SECONDS: int = int(os.environ.get("AHS_BCH_IDLE_TIMEOUT_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# Codex App Server settings
# ---------------------------------------------------------------------------

# TCP port for the supervisord-managed `codex app-server` sidecar.
# Used by the health monitoring background task in server.py.
# Note: CodexAppServerRunner spawns per-session servers on dynamic ports;
# this port is for the C1 supervisord-managed shared sidecar health check.
CODEX_APP_SERVER_PORT: int = int(os.environ.get("CODEX_APP_SERVER_PORT", "8765"))

# GCS bucket and prefix for persisting agent memory directories.
# Layout: gs://{bucket}/{prefix}/{agent_name}/...
AHS_GCS_MEMORY_BUCKET = os.environ.get("AHS_GCS_MEMORY_BUCKET", "yupp-agents")
AHS_GCS_MEMORY_PREFIX = os.environ.get("AHS_GCS_MEMORY_PREFIX", "agent_memory")

# Agent names that support per-user personalization (mirrors SAG constant).
PERSONAL_AGENT_PREFIXES: frozenset[str] = frozenset({"yuppclaw"})


def is_personal_agent(agent_name: str) -> bool:
    """Check if an agent name matches a personal agent pattern (e.g. yuppclaw-alice)."""
    return any(agent_name.startswith(f"{prefix}-") for prefix in PERSONAL_AGENT_PREFIXES)


# Default config template for newly created personal agents (yuppclaw-*).
# Mirrors the SRE agent config — harnessed executor, full tool access, sandbox enabled.
# Stored as a dict so it can be written to Agent.config JSONB in the DB.
PERSONAL_AGENT_DEFAULT_CONFIG: dict[str, Any] = {
    "default_repo": "yupp-mind",
    "max_turns": 50,
    "max_budget_usd": 3.0,
    "has_mcp": True,
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "bwrapEnabled": True,
    },
    "executor_config": {
        "type": "harnessed",
    },
    "tool_permissions": {
        "*": "allow",
    },
    "allowed_subagents": [],
    "feedback_probability": 0.2,
    "feedback_min_turns": 5,
    "timeout_s": 36000,
    "allowed_gateways": ["*"],
}

# GCS bucket and prefix for persisting session workspace data (attachments, history).
# Layout: gs://{bucket}/sessions/{environment}/{session_id}/...
# Persistence is disabled in local dev (checked in session_persistence.py).
AHS_GCS_SESSION_BUCKET = os.environ.get("AHS_GCS_SESSION_BUCKET", "yupp-agents")
_AHS_ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
AHS_GCS_SESSION_PREFIX = os.environ.get("AHS_GCS_SESSION_PREFIX", f"sessions/{_AHS_ENVIRONMENT}")

# MCP server base URL (used to generate per-workspace .mcp.json configs)
AHS_MCP_BASE_URL = os.environ.get("AHS_MCP_BASE_URL", "http://127.0.0.1:8090")

# MCP endpoint auth token. Falls back to a random value for local dev.
AHS_MCP_SECRET = os.environ.get("AHS_MCP_SECRET", secrets.token_urlsafe(32))

# --- Executor types (executor_config.type) ---
EXECUTOR_TYPE_RAW = "raw"
EXECUTOR_TYPE_HARNESSED = "harnessed"

# --- CLI identifiers for harnessed executors (executor_config.model when type="harnessed") ---
# When type="harnessed", the model field specifies which CLI wrapper to use.
HARNESS_CLAUDE_CODE_CLI = "claude-code-cli"
HARNESS_CODEX_CLI = "codex-cli"
# Codex app-server runner: connects to a persistent `codex app-server` sidecar via WebSocket.
HARNESS_CODEX_APP_SERVER = "codex-app-server"
# In-process SDK runner: no subprocess, uses Anthropic Python SDK directly.
HARNESS_CLAUDE_SDK = "claude-agent-sdk"

# Tool permission values
PERM_ALLOW = "allow"
PERM_DENY = "deny"
PERM_ASK = "ask"

# Predefined toolsets: shorthand for groups of workspace tools.
# Tool names are generic harness-level names (lowercase), matching MCP tool names.
# For harnessed (CLI) executors, these are mapped to CLI-specific names via
# HARNESS_TO_CLI_TOOL_MAP before generating --allowedTools/--disallowedTools flags.
TOOLSET_PREFIX = "@"
TOOLSETS: dict[str, list[str]] = {
    "@readonly": ["read", "glob", "grep", "webfetch", "websearch"],
    "@readwrite": ["bash", "read", "write", "edit", "glob", "grep", "webfetch", "websearch"],
}

# Maps generic harness tool names → Claude Code CLI tool names.
# Used by tool_permissions_to_cli_flags() when generating CLI flags for harnessed executors.
# For every tool in this map, the native CLI variant is preferred and the MCP harness
# duplicate (mcp__harness__<tool>) is denied at runtime via _MCP_HARNESS_TOOLS_WITH_CLI_EQUIV.
HARNESS_TO_CLI_TOOL_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
}

# Claude Code built-in tools that are superseded by our MCP equivalents.
# These are always denied when the harness MCP server is available,
# forcing traffic through our MCP implementations instead.
_CLI_TOOLS_SUPERSEDED_BY_MCP: list[str] = []

# All known MCP server names that a session can be granted access to.
# These must match the actual keys in .mcp.json (and the dynamically injected "harness").
ALL_MCP_SERVERS: list[str] = ["harness", "yuppster-mcp-server"]

# Harness MCP tools that require USE_MCP permission (bare names, no CLI prefix).
# Used by both the harnessed path (runner.py, with mcp__harness__ prefix) and
# the raw executor path (mcp_client.py, bare names) to enforce consistent restrictions.
BLOCKED_HARNESS_TOOLS = frozenset(
    {
        "request_write_access",
        "list_available_repos",
        "create_pr",
        "list_agents",
        "schedule_agent_call",
        "schedule_recurring_agent_call",
    }
)

# Harness tools available to sessions with restricted access (no USE_MCP).
# These are safe tools that don't expose sensitive data or privileged actions.
RESTRICTED_HARNESS_TOOLS: list[str] = [
    "request_feedback",
    "send_slack_message",
    "new_task",
    "route_model",
    "create_agent",
]

# User-facing notice sent when the agent exhausts its turn quota.
TURN_LIMIT_NOTICE = (
    "_[AHS] ⏳ I've used all my turns for this round. Send me another message to continue where I left off!_"
)

# User-facing notice sent when the agent is stopped due to context window overflow.
CONTEXT_OVERFLOW_NOTICE = (
    "_[AHS] 💥 I ran out of context window space. Send me another message to continue in a fresh context!_"
)

# Per-turn and per-session tool call limits (no global limit)
MAX_WEBSEARCH_CALLS_PER_TURN = 20
MAX_WEBSEARCH_CALLS_PER_SESSION = 100

# CLI defaults
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_STEPS = 50
DEFAULT_TIMEOUT_S = 36000  # 10 hours (max_turns is the real guard)

# Compaction: max chars to keep when building the summarization input
COMPACTION_TOOL_RESULT_PREVIEW_CHARS = 200
COMPACTION_CONTENT_PREVIEW_CHARS = 500
DEFAULT_TEMPERATURE = None

# Provider identifiers
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_MOONSHOT = "moonshot"
PROVIDER_OPENAI = "openai"
PROVIDER_ZAI = "zai"
PROVIDER_MINIMAX = "minimax"

# Default models per provider
DEFAULT_MODEL_ANTHROPIC = "anthropic/claude-sonnet-4-6"
DEFAULT_MODEL_MOONSHOT = "moonshot/kimi-k2.5"
DEFAULT_MODEL_OPENAI = "openai/gpt-4o"
DEFAULT_MODEL_ZAI = "zai/glm-5"
DEFAULT_MODEL_MINIMAX = "minimax/MiniMax-M2.5"

# Session status values
SESSION_STATUS_ACTIVE = "ACTIVE"
SESSION_STATUS_COMPLETED = "COMPLETED"
SESSION_STATUS_STALE = "STALE"

# Auto-stale sweep configuration
# AHS_AUTO_STALE_INTERVAL_S: How often the background sweep runs (default 1800 = 30 min)
# AHS_SESSION_STALE_TIMEOUT_HOURS: How long a session can be idle before being marked STALE (default 6)
AHS_AUTO_STALE_INTERVAL_S = int(os.environ.get("AHS_AUTO_STALE_INTERVAL_S", "1800"))
AHS_SESSION_STALE_TIMEOUT_HOURS = float(os.environ.get("AHS_SESSION_STALE_TIMEOUT_HOURS", "6"))

# Scheduler configuration (for scheduled agent calls)
# AHS_SCHEDULER_ENABLED: Set to "false" to disable the scheduler
# AHS_SCHEDULER_POLL_INTERVAL: Polling interval in seconds (default: 10)
# AHS_SCHEDULER_BATCH_SIZE: Max calls to process per poll (default: 10)

# ContextVar: propagates the calling agent's session ID from HTTP headers to MCP tool handlers.
# Set by the auth middleware from the X-AHS-Session-ID header. MCP tools (new_task) read this
# to enforce allowed_subagents without relying on the LLM to pass session_id.
mcp_session_id_var: ContextVar[str] = ContextVar("mcp_session_id", default="")


# ---------------------------------------------------------------------------
# Session directory helpers
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: str) -> None:
    """Validate that session_id is a proper UUID to prevent path traversal."""
    if not session_id:
        raise ValueError("session_id is required")
    try:
        _uuid.UUID(session_id)
    except ValueError:
        raise ValueError(f"session_id is not a valid UUID: {session_id!r}") from None


def get_session_dir(session_id: str) -> str:
    """Return the absolute path to the session workspace root.

    This is the canonical helper for harness internals that need the session
    directory without manually constructing ``AHS_SESSIONS_DIR / session_id``.

    Args:
        session_id: Active session ID (must be a valid UUID).

    Returns:
        Absolute path to the session workspace root.

    Raises:
        ValueError: If ``session_id`` is not a valid UUID.
    """
    _validate_session_id(session_id)
    return os.path.join(AHS_SESSIONS_DIR, session_id)
