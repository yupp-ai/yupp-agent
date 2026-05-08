"""Constants for Agent Harness Service."""

import os
import secrets
import uuid as _uuid
from typing import Any

from ypl.backend.config import settings

# Deprecated re-export — the canonical home for ``mcp_session_id_var`` is
# ``ypl.mcp_common.auth_context``. Tools should read
# ``current_request_context().ahs_session_id`` instead. Kept here for one
# release so existing imports keep working.
from ypl.mcp_common.auth_context import mcp_session_id_var  # noqa: F401

# Filesystem paths (configurable via env vars for local dev)
# In production: AHS_DATA_DIR=/data/ahs (mounted volume; ahs user's HOME).
# In local dev: falls back to /tmp/ahs when /data/ahs doesn't exist.
_DEFAULT_DATA_DIR = "/data/ahs"
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

# Default Slack channel for new agent projects (plain channel name, no "#" prefix or ID).
AHS_DEFAULT_PROJECT_SLACK_CHANNEL = os.environ.get("AHS_DEFAULT_PROJECT_SLACK_CHANNEL", "agentic-projects")

# Infrastructure subdirectories inside a session workspace that are NOT worktrees.
# Used by resolve_workspace, scan_session_worktrees, create_pr, and cleanup to
# distinguish session infrastructure from git worktrees.
SESSION_INFRA_DIRS = frozenset({"history", "attachments", "agent_memories", "tool-results"})

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

# Agent names that support per-user personalization (mirrors SAG constant).
PERSONAL_AGENT_PREFIXES: frozenset[str] = frozenset({"yuppclaw"})


def is_personal_agent(agent_name: str) -> bool:
    """Check if an agent name matches a personal agent pattern (e.g. yuppclaw-alice)."""
    return any(agent_name.startswith(f"{prefix}-") for prefix in PERSONAL_AGENT_PREFIXES)


# Default config template for newly created personal agents (yuppclaw-*).
# Mirrors the SRE agent config — harnessed executor, full tool access, sandbox enabled.
# Stored as a dict so it can be written to Agent.config JSONB in the DB.
PERSONAL_AGENT_DEFAULT_CONFIG: dict[str, Any] = {
    "default_repo": "yupp-agent",
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

# Lit (Streamlit console) base URL — used for session/project links in PR
# descriptions, Slack messages, and CLI/TUI output. Set this in the deployment's
# .env to the Streamlit console's public URL (e.g. ``https://lit.agcouch.com``).
# When empty, the Lit link is omitted from generated messages.
AHS_LIT_BASE_URL = os.environ.get("AHS_LIT_BASE_URL", "").rstrip("/")

# War Room (Next.js admin UI) base URL — used in Slack session-notice links.
# Set to your deployment's War Room public URL (e.g. ``https://war-room.agcouch.com``).
# When empty, the War Room link is omitted from generated messages.
AHS_WAR_ROOM_BASE_URL = os.environ.get("AHS_WAR_ROOM_BASE_URL", "").rstrip("/")

# Slack workspace subdomain — used to build Slack permalinks back to threads
# from Lit (e.g. ``yuppai`` or ``agentic-couch``). The full URL is built as
# ``https://{SLACK_WORKSPACE_DOMAIN_NAME}.slack.com/archives/<channel>/p<msg_ts>...``.
# When empty, Slack permalink generation returns None and callers should
# omit the link.
SLACK_WORKSPACE_DOMAIN_NAME = os.environ.get("SLACK_WORKSPACE_DOMAIN_NAME", "").strip()

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

# All harnessed executor model identifiers (used for validation & listing).
HARNESSED_MODELS: tuple[str, ...] = (
    HARNESS_CLAUDE_CODE_CLI,
    HARNESS_CLAUDE_SDK,
    HARNESS_CODEX_CLI,
    HARNESS_CODEX_APP_SERVER,
)

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
# These must match the actual keys in .mcp.json (and the dynamically
# injected ``harness``). ``settings.AGCOUCH_MCP_SERVER_NAME`` is resolved
# at import time so tests that override settings still see the default
# here; actual connection wiring in ``mcp_client.py`` re-reads the
# setting at runtime.
#
# TODO(phase-4): drop ``settings.AGCOUCH_MCP_SERVER_NAME`` from this list
# once the dev-token retirement also retires the ``allowed_servers``
# permission row referring to it. After PR #300 (phase-2) the AHS
# executor no longer connects to the agcouch mount, so the name is dead
# weight here, but ``SessionPermissions`` rows persisted with this in
# ``allowed_servers`` still need to read cleanly.
# TODO(phase-9): externally-registered MCP servers (from the DB registry)
# will be appended to this list at runtime when the session starts.
ALL_MCP_SERVERS: list[str] = ["harness", settings.AGCOUCH_MCP_SERVER_NAME]

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

# Harness MCP tools that previously lived on the agcouch mount and are now
# registered on harness via ``@shared_tool`` (PR #300 / phase-2). They reach
# yuppdb / agentdb / GCP logs / Sentry / Twitter / Linear / shared agent
# project + artifact + memory state, and were previously blocked for
# restricted (no-USE_MCP) sessions by the ``mcp__harness__*``
# wildcard. Now that the wildcard matches nothing, the security boundary
# has to be enumerated. ``runner.py`` adds ``mcp__harness__<name>`` for
# every entry below to ``--disallowedTools`` for restricted sessions.
#
# ``report_security_incident`` is intentionally *not* listed: SECURITY.md
# instructs every agent (regardless of USE_MCP) to call it on prompt
# injection / scope manipulation / etc. attempts.
SHARED_HARNESS_TOOLS_BLOCKED_FOR_RESTRICTED: frozenset[str] = frozenset(
    {
        # project_tasks (agentdb access — read + write)
        "get_project",
        "add_project",
        "update_project",
        "set_project_status",
        "set_project_state",
        "get_project_state",
        "list_projects",
        "get_task",
        "add_tasks",
        "add_task_sequence",
        "update_task",
        "set_task_status",
        "set_task_dependencies",
        "get_project_tasks",
        "get_ready_tasks",
        "claim_task",
        "resume_failed_task",
        "restart_task",
        # agent_artifacts
        "add_artifact",
        "update_artifact",
        "update_artifact_content",
        "read_artifact",
        "list_artifacts",
        "list_artifact_versions",
        "search_artifacts",
        "artifact_url",
        "archive_artifact",
        "archive_artifact_slug",
        # agent_schedules
        "list_ahs_agents",
        "create_agent_schedule",
        "create_recurring_agent_schedule",
        "cancel_agent_schedule",
        "edit_agent_schedule",
        "list_agent_schedules",
        # memory_artifacts
        "save_memory",
        "load_memory",
        "list_memory",
        "search_memory",
        # database — yuppdb / agentdb / bigquery
        "query_yuppdb",
        "query_agentdb",
        "query_bigquery",
        "query_bigquery_expensive",
        # gcp_logs / vercel_logs / alert metadata
        "search_gcp_logs",
        "search_vercel_logs",
        "get_gcp_alert_details",
        # sentry
        "get_sentry_issue_details",
        "get_sentry_issue_tag_values",
        "get_sentry_trace_details",
        "get_sentry_breadcrumbs",
        # twitter
        "search_twitter",
        "get_user_timeline",
        "get_tweet",
        # redis
        "get_redis_value",
        "scan_redis_keys",
        # slack — read-side
        "read_slack_thread",
        "search_slack",
        # linear_sync
        "list_linear_teams",
        "list_linear_projects",
        "resolve_linear_team",
        "resolve_linear_project",
        "import_project_from_linear",
        "link_project_to_linear",
        "export_project_to_linear",
        "push_task_status_to_linear",
        "sync_project_with_linear",
        "attach_link_to_linear_issue",
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
PROVIDER_CEREBRAS = "cerebras"
PROVIDER_MOONSHOT = "moonshot"
PROVIDER_OPENAI = "openai"
PROVIDER_ZAI = "zai"
PROVIDER_MINIMAX = "minimax"

# Default models per provider
DEFAULT_MODEL_ANTHROPIC = "anthropic/claude-sonnet-4-6"
DEFAULT_MODEL_CEREBRAS = "cerebras/gpt-oss-120b"
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

# ``mcp_session_id_var`` (which propagates the calling agent's AHS session
# ID through HTTP headers to MCP tool handlers) lives in
# ``ypl.mcp_common.auth_context`` and is re-exported at the top of this
# module for backwards compatibility. New code should read
# ``current_request_context().ahs_session_id`` instead.


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
