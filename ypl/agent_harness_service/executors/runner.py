"""Agent runner abstraction and Claude Code CLI implementation.

The runner wraps agent execution. MVP uses ClaudeCodeRunner which spawns
`claude -p` with `--output-format stream-json`.

Future runners: GeminiCLIRunner, SingleInferenceRunner.
"""

import asyncio
import fcntl
import io
import json
import os
import pathlib
import platform
import shlex
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ypl.agent_harness_service.common.config import AgentConfig
from ypl.agent_harness_service.common.constants import (
    _CLI_TOOLS_SUPERSEDED_BY_MCP,
    AHS_DATA_DIR,
    BLOCKED_HARNESS_TOOLS,
    HARNESS_TO_CLI_TOOL_MAP,
    SHARED_HARNESS_TOOLS_BLOCKED_FOR_RESTRICTED,
)
from ypl.agent_harness_service.common.models import RetryConfig, tool_permissions_to_cli_flags
from ypl.agent_harness_service.common.types import SessionPermissions
from ypl.agent_harness_service.common.types import StreamEvent as StreamEvent  # re-export for compat
from ypl.agent_harness_service.executors.mcp_config import ensure_workspace_mcp_config
from ypl.agent_harness_service.executors.system_prompt import build_system_prompt
from ypl.backend.utils.monitoring import metric_inc, metric_record_with_labels
from ypl.structured_logger import get_logger

logger = get_logger()

AGENT_RESPONSE_DEBUG = os.environ.get("AGENT_RESPONSE_DEBUG", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Queue bottleneck detection
# ---------------------------------------------------------------------------
# Tracks sessions with queue_wait_ms > 30 s using a module-level sliding-window
# deque (monotonic timestamps). When ≥3 events fall within a 5-minute window,
# a structured logger.error fires with alert_type="session_queue_bottleneck" so
# that a Cloud Logging log-based alert can route the incident to Slack.
_QUEUE_BOTTLENECK_THRESHOLD_MS: int = 30_000  # 30 s
_QUEUE_BOTTLENECK_WINDOW_S: float = 300.0  # 5 minutes
_QUEUE_BOTTLENECK_MIN_COUNT: int = 3

_queue_bottleneck_lock: threading.Lock = threading.Lock()
_queue_bottleneck_events: deque[float] = deque()  # monotonic timestamps of over-threshold events


def _check_queue_bottleneck(queue_wait_ms: int, session_id: str) -> None:
    """Detect and log session_queue_bottleneck alerts.

    Fires logger.error exactly when the in-process count first reaches
    _QUEUE_BOTTLENECK_MIN_COUNT within a _QUEUE_BOTTLENECK_WINDOW_S rolling
    window.  Subsequent over-threshold events in the same window do NOT
    re-fire to prevent alert storms — the alert re-arms after the window clears.
    """
    if queue_wait_ms <= _QUEUE_BOTTLENECK_THRESHOLD_MS:
        return

    metric_inc("ahs/session_queue_bottleneck_event")

    now = time.monotonic()
    with _queue_bottleneck_lock:
        _queue_bottleneck_events.append(now)
        # Evict events that have aged out of the rolling window
        cutoff = now - _QUEUE_BOTTLENECK_WINDOW_S
        while _queue_bottleneck_events and _queue_bottleneck_events[0] < cutoff:
            _queue_bottleneck_events.popleft()
        count = len(_queue_bottleneck_events)

    # Fire exactly at the threshold crossing (not on every subsequent event)
    if count == _QUEUE_BOTTLENECK_MIN_COUNT:
        logger.error(
            "session_queue_bottleneck",
            session_id=session_id,
            queue_wait_ms=queue_wait_ms,
            bottleneck_count_in_window=count,
            window_seconds=int(_QUEUE_BOTTLENECK_WINDOW_S),
            alert_type="session_queue_bottleneck",
        )
        metric_inc("ahs/session_queue_bottleneck_alert")


# ---------------------------------------------------------------------------
# Pipe buffer tuning
# ---------------------------------------------------------------------------
# Linux pipe buffers default to 64 KB. Large tool results (GCP logs, DB query
# output, file reads) that exceed this trigger blocked writes and context
# switches, adding latency. We request up to 4 MB here, clamped to
# /proc/sys/fs/pipe-max-size (typically 1 MB unprivileged) to avoid EPERM.
# Falls back silently on non-Linux or restricted kernels.

_PIPE_BUF_SIZE = 4 * 1024 * 1024  # 4 MB requested; clamped to kernel max
# F_SETPIPE_SZ is Linux-specific (not in fcntl module constants on all platforms)
_F_SETPIPE_SZ: int = 1031


def _read_pipe_max_size() -> int | None:
    """Read the kernel's max allowed pipe buffer size for unprivileged users.

    Returns None if unavailable (non-Linux, restricted access).
    """
    try:
        return int(pathlib.Path("/proc/sys/fs/pipe-max-size").read_text().strip())
    except (OSError, ValueError):
        return None


def _try_enlarge_pipe_buf(proc: asyncio.subprocess.Process) -> None:
    """Attempt to increase OS pipe buffers for stdout and stderr.

    Requests up to 4 MB but clamps to /proc/sys/fs/pipe-max-size to avoid
    EPERM on unprivileged kernels (which reject, not clamp, oversized requests).

    Linux only — silently ignores errors for:
    - Non-Linux platforms (F_SETPIPE_SZ not available)
    - Kernels that block unprivileged pipe size changes
    - Internal asyncio API changes (accesses private transport attributes)
    """
    if platform.system() != "Linux":
        return

    pipe_max = _read_pipe_max_size()
    target_size = min(_PIPE_BUF_SIZE, pipe_max) if pipe_max else _PIPE_BUF_SIZE

    # asyncio.subprocess.Process._transport is _UnixSubprocessTransport.
    # get_pipe_transport(fd) returns the _UnixReadPipeTransport for that fd.
    # Each transport exposes _pipe, a file-like object whose fd we can resize.
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    get_pipe = getattr(transport, "get_pipe_transport", None)
    if get_pipe is None:
        return
    for fd in (1, 2):  # stdout, stderr
        try:
            pipe_transport = get_pipe(fd)
            if pipe_transport is None:
                continue
            pipe_file = getattr(pipe_transport, "_pipe", None)
            if pipe_file is None:
                continue
            fcntl.fcntl(pipe_file.fileno(), _F_SETPIPE_SZ, target_size)
        except (OSError, ValueError):
            # Kernel may block the operation entirely.
            # ValueError: fileno() on a closed file (private asyncio internals).
            pass


# ---------------------------------------------------------------------------
# Subprocess environment sandboxing
# ---------------------------------------------------------------------------
# Only explicitly approved env vars pass through to CLI subprocesses.
# This prevents agents from enumerating secrets (DB passwords, API keys, etc.).

_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        # System essentials
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        # Locale
        "LANG",
        "LANGUAGE",
        # Claude CLI
        "ANTHROPIC_API_KEY",
        # Claude Code's escape hatch for "running as root inside a container
        # is already sandboxed; trust me and allow --dangerously-skip-permissions".
        # Without forwarding this through the subprocess env, claude-code-cli
        # crashes on startup with "cannot be used with root/sudo privileges".
        "IS_SANDBOX",
        # Note: AHS no longer forwards ``AGCOUCH_MCP_TOKEN`` — agents reach
        # every shared / external-data tool via the harness MCP using
        # ``AHS_MCP_SECRET`` (handled inside ``mcp_config.resolve_mcp_servers``,
        # never exposed to the subprocess env). See
        # ``ypl/mcp_common/shared_tool.py``.
        # Git / GitHub
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        # OpenAI (Codex CLI)
        "OPENAI_API_KEY",
        # AHS workspace paths
        "AHS_DATA_DIR",
        "AHS_REPOS_DIR",
        "AHS_SESSIONS_DIR",
        "AHS_WORKSPACES_DIR",  # deprecated alias for AHS_SESSIONS_DIR
        # AHS_SHARED_DIR and AHS_AGENTS_DIR intentionally excluded — the CLI
        # does not need them; identity files are injected via --system-prompt.
        # Config
        "ENVIRONMENT",
        # Node.js runtime
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "UV_THREADPOOL_SIZE",
    }
)

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "XDG_",
    "LC_",
    "GIT_",
)

_BLOCKED_EXACT: frozenset[str] = frozenset(
    {
        "AGENT_HARNESS_SERVICE_API_KEY",
        "X_API_KEY",
        "AHS_MCP_SECRET",
        "GATEWAY_BASE_URL",
    }
)

_BLOCKED_PREFIXES: tuple[str, ...] = (
    "POSTGRES_",
    "SLACK_",
)

# Substrings that indicate a sensitive var regardless of naming convention.
_BLOCKED_SUBSTRINGS: tuple[str, ...] = (
    "PASSWORD",
    "SECRET",
)


def build_subprocess_env() -> dict[str, str]:
    """Build a filtered env dict for CLI subprocesses.

    Uses an allowlist approach: only explicitly approved env vars pass through.
    A secondary blocklist catches vars that must never leak even if someone
    accidentally adds them to the allowlist in the future.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        # Hard-block dangerous vars regardless of allowlist (case-insensitive)
        upper_key = key.upper()
        if key in _BLOCKED_EXACT:
            continue
        if upper_key.startswith(_BLOCKED_PREFIXES):
            continue
        if any(sub in upper_key for sub in _BLOCKED_SUBSTRINGS):
            continue

        # Allow by exact name or prefix match
        if key in _ALLOWED_EXACT or key.startswith(_ALLOWED_PREFIXES):
            env[key] = value

    # Ensure ~/.local/bin is in PATH so tools installed there (ruff, mypy, etc.)
    # are findable by bare command name without the agent having to search for them.
    # The systemd service does not inherit ~/.bashrc PATH augmentations, so
    # ~/.local/bin is absent from the inherited PATH even though bwrap already
    # mounts it read-only via _CLI_HOME_RO_BINDS in sandbox.py.
    home = env.get("HOME", os.path.expanduser("~"))
    local_bin = os.path.join(home, ".local", "bin")
    current_path = env.get("PATH", "")
    if local_bin not in current_path.split(":"):
        env["PATH"] = f"{local_bin}:{current_path}" if current_path else local_bin

    # Prepend the poetry venv bin so `python`, `mypy`, `ruff`, etc. resolve to
    # the venv-installed versions that have access to all project packages.
    # On VM deployments packages live in /opt/yupp-agent/.venv/, not in the
    # system Python — without this, mypy can't find any third-party imports.
    # The existence check makes this a no-op in Docker (where
    # `virtualenvs.create false` puts packages directly under /usr/local/).
    venv_bin = "/opt/yupp-agent/.venv/bin"
    if os.path.isdir(venv_bin) and venv_bin not in env["PATH"].split(":"):
        env["PATH"] = f"{venv_bin}:{env['PATH']}"

    return env


# CLI-prefixed versions of BLOCKED_HARNESS_TOOLS for --allowedTools / --disallowedTools.
#
# Two groups of denies, both prefixed with ``mcp__harness__``:
#
# 1. ``BLOCKED_HARNESS_TOOLS`` — high-privilege harness-internal tools
#    (request_write_access, create_pr, list_agents, schedule_agent_call …)
#    that require USE_MCP regardless of where they're mounted.
# 2. ``SHARED_HARNESS_TOOLS_BLOCKED_FOR_RESTRICTED`` — every shared /
#    external-data tool that previously lived on the agcouch mount and
#    was blocked for restricted sessions via the
#    ``mcp__harness__*`` wildcard. Post phase-2 (PR #300) those
#    tools live under ``mcp__harness__*`` so the wildcard no longer
#    matches; we enumerate the harness-prefixed names instead to
#    preserve the security boundary.
_BLOCKED_HARNESS_TOOLS_CLI = [f"mcp__harness__{t}" for t in BLOCKED_HARNESS_TOOLS] + [
    f"mcp__harness__{t}" for t in SHARED_HARNESS_TOOLS_BLOCKED_FOR_RESTRICTED
]
_RESTRICTED_HARNESS_TOOLS_CLI = [
    "mcp__harness__request_feedback",
    "mcp__harness__send_slack_message",
    "mcp__harness__new_task",
    "mcp__harness__route_model",
    "mcp__harness__create_agent",
]

# Claude Code built-in tools that are unconditionally denied regardless of
# session permissions, trigger, or executor config.  Their semantics require
# an interactive Claude Code UI (Code TUI / web app) to render output and
# capture user input; in headless ``claude -p`` mode (which is how AHS always
# invokes the CLI) they have no working surface and return cryptic errors
# when the model attempts to call them.
#
# AskUserQuestion is the canonical example — it surfaces a multi-option
# question dialog in Code TUI; in headless mode the call comes back with the
# bare string ``"Answer questions?"`` as the tool's error body, which is
# opaque to the model and indistinguishable from the session having gone
# dormant.  Denying it at the CLI flag level removes it from the model's
# tool list entirely so it never gets attempted.  Agents that need to ask a
# structured multiple-choice question should use the harness MCP equivalent
# (``mcp__harness__ask_question``) which renders clickable buttons in the
# Slack thread and routes the response back as the next turn; for non-Slack
# triggers the model falls back to asking inline in plain text.
_ALWAYS_DISALLOWED_CLI_TOOLS: list[str] = ["AskUserQuestion"]

# For harnessed (CLI) executors: MCP harness tools that are superseded by Claude Code's own
# native built-ins. Derived from HARNESS_TO_CLI_TOOL_MAP (harness name → CLI name).
# When the harness MCP is available, we deny these so the agent uses its native CLI tools
# (Bash, Read, Write, WebFetch, etc.) directly instead of the extra MCP round-trip.
_MCP_HARNESS_TOOLS_WITH_CLI_EQUIV = [
    f"mcp__harness__{tool}"
    for tool in HARNESS_TO_CLI_TOOL_MAP
    if HARNESS_TO_CLI_TOOL_MAP[tool] not in _CLI_TOOLS_SUPERSEDED_BY_MCP
]

# Top MCP tools to pre-declare explicitly in --allowedTools so Claude Code loads them
# eagerly (non-deferred) instead of lazily. Explicitly named tools are loaded at session
# start; wildcards cause deferred loading that requires ToolSearch before first use.
# Ordered by observed usage frequency across AHS daily performance reports.
# See: http://go/p/ahs-daily-performance for usage data.
_TOP_HARNESS_TOOLS_PREDECLARED: list[str] = [
    "mcp__harness__send_slack_message",
    "mcp__harness__authorize_github_user",
    "mcp__harness__check_github_auth_status",
    "mcp__harness__request_write_access",
    "mcp__harness__create_pr",
    "mcp__harness__list_available_repos",
]
# Tools migrated from the agcouch mount to the harness mount via
# ``@shared_tool`` (PR #300 / phase-2). Pre-declared here so Claude Code
# loads them eagerly instead of deferring discovery behind ``ToolSearch``.
_TOP_SHARED_TOOLS_PREDECLARED: list[str] = [
    "mcp__harness__query_yuppdb",
    "mcp__harness__search_gcp_logs",
    "mcp__harness__add_artifact",
    "mcp__harness__read_artifact",
    "mcp__harness__list_artifacts",
    "mcp__harness__search_artifacts",
    "mcp__harness__search_memory",
    "mcp__harness__load_memory",
    "mcp__harness__save_memory",
    "mcp__harness__list_memory",
    "mcp__harness__read_slack_thread",
    # Security incident reporting — pre-declared so SECURITY.md instructions work
    # without a ToolSearch round-trip. Fire-and-forget; never blocks a response.
    "mcp__harness__report_security_incident",
]


class RunContext(BaseModel):
    """Context passed to the runner for a single turn."""

    session_id: str
    workspace: str | None = None
    llm_session_id: str | None = None
    extra_dirs: list[str] = Field(default_factory=list)
    slack_session_id: str | None = None
    is_slack: bool = False
    is_task: bool = False
    session_context: dict[str, Any] | None = None
    # Timestamp when the AgentSession was created in the DB.  Used by
    # ClaudeCodeRunner to compute queue_wait_ms = now - created_at, which
    # measures the gap between session creation and when the CLI starts.
    session_created_at: datetime | None = None


class AgentRunner(ABC):
    """Abstract base for agent runners."""

    @abstractmethod
    def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Run the agent once and yield stream events.

        Subclasses implement this method.  The public ``run()`` method wraps it
        with auto-retry logic — do not call ``_run_once()`` directly from outside
        the runner hierarchy.

        Args:
            prompt: The enriched prompt (message + memory context)
            context: Run context with session info and workspace

        Yields:
            StreamEvent objects parsed from the agent output.
        """
        ...

    async def run(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Run the agent with auto-retry on silent CLI crashes.

        Wraps ``_run_once()`` in a retry loop.  A "silent crash" is detected when
        the entire run produces zero *meaningful* events — the only event emitted
        (if any) is the synthetic ``"error"`` event the runner itself injects on a
        non-zero subprocess exit, with no real ``system``, ``assistant``, ``user``,
        or ``result`` events.  This matches the pattern where the Claude Code CLI
        exits with code 1 and empty stderr/stdout due to transient API issues.

        Streaming behaviour: events are buffered until the first non-error event
        arrives.  Once a meaningful event is seen the buffer is flushed and all
        subsequent events stream directly to the consumer — so a successful run
        incurs virtually no extra latency (only the first ``system`` event is
        briefly buffered, typically <100 ms).  If the run ends with only error
        events the buffer is either discarded (retry available) or forwarded
        (final attempt), ensuring the consumer never sees intermediate failures.

        Retry configuration is read from ``self.config.executor_config.retry``
        (accessed via ``getattr`` so runners without a ``config`` attribute
        degrade gracefully to zero retries).

        Args:
            prompt: The enriched prompt (message + memory context)
            context: Run context with session info and workspace

        Yields:
            StreamEvent objects from the final (or only) run attempt.
        """
        retry_cfg: RetryConfig | None = None
        cfg = getattr(self, "config", None)
        exec_cfg = getattr(cfg, "executor_config", None)
        if exec_cfg is not None:
            retry_cfg = getattr(exec_cfg, "retry", None)
        max_retries: int = retry_cfg.max_retries if retry_cfg is not None else 0
        on_empty: bool = retry_cfg.on_empty_result if retry_cfg is not None else True

        for attempt in range(max_retries + 1):
            # Buffer events until we see the first non-error event.  Once a
            # meaningful event arrives we flush and switch to direct streaming.
            # This lets us suppress intermediate error events from failed attempts
            # without adding latency to successful runs.
            buffer: list[StreamEvent] = []
            streaming = False  # True once a non-error event has been observed
            only_errors = True  # False as soon as any non-error event is seen

            async for event in self._run_once(prompt, context):
                if streaming:
                    yield event
                else:
                    buffer.append(event)
                    if event.type != "error":
                        only_errors = False
                        # First meaningful event: flush buffer and stream directly
                        streaming = True
                        for buffered in buffer:
                            yield buffered
                        buffer.clear()

            # Determine whether to retry.
            is_empty_result = only_errors and on_empty
            has_retries_left = attempt < max_retries

            if not is_empty_result or not has_retries_left:
                # Successful run or retries exhausted — yield any remaining
                # buffered events (the last attempt's error events if all crashed).
                if is_empty_result and not has_retries_left and attempt > 0:
                    logger.error(
                        "CLI silent crash persisted after all retries",
                        session_id=context.session_id,
                        total_attempts=attempt + 1,
                    )
                for buffered in buffer:
                    yield buffered
                break

            # Silent crash on a non-final attempt: discard buffered error events
            # and retry with a fresh spawn.
            logger.warning(
                "CLI silent crash detected (zero meaningful events), retrying",
                session_id=context.session_id,
                attempt=attempt + 1,
                max_retries=max_retries,
            )


def extract_excerpt(event: StreamEvent, max_len: int = 200) -> str:
    """Extract a human-readable content excerpt from a stream event."""
    raw = event.raw

    if event.type == "assistant":
        text = event.text
        if text:
            return text[:max_len]
        # Tool-call-only assistant message — extract tool name from content blocks
        content = raw.get("message", {}).get("content", [])
        tool_names = [b.get("name", "") for b in content if b.get("type") == "tool_use"]
        if tool_names:
            return f"tool_call: {', '.join(tool_names)}"
        return "(no text content)"

    if event.type == "tool_use":
        tool_name = raw.get("tool_name", "") or raw.get("name", "")
        tool_input = str(raw.get("input", raw.get("tool_input", "")))
        return f"{tool_name}: {tool_input[:max_len]}"

    if event.type == "tool_result":
        content = str(raw.get("content", raw.get("output", "")))
        return content[:max_len]

    if event.type == "result":
        cost = raw.get("estimated_cost_usd") or raw.get("cost_usd")
        turns = raw.get("num_turns")
        duration = raw.get("duration_ms")
        return f"est_cost=${cost} turns={turns} duration={duration}ms"

    if event.type == "system":
        session_id = raw.get("session_id", "")
        return f"session={session_id}"

    if event.type == "error":
        return str(raw.get("error", ""))[:max_len]

    if event.type == "user":
        # Tool result fed back to Claude — extract content from message.content blocks
        blocks = raw.get("message", {}).get("content", [])
        parts = [str(b.get("content", "")) for b in blocks if b.get("content")]
        if parts:
            return "; ".join(parts)[:max_len]

    # Fallback: extract useful text from stderr/output/content fields
    for key in ("stderr", "output", "content", "text"):
        val = raw.get(key)
        if val and isinstance(val, str):
            return str(val).strip()[:max_len]

    return ""


def _handle_stream_limit_error(
    e: ValueError,
    cli_name: str,
    session_id: str,
    stream_limit: int,
) -> StreamEvent:
    """Handle ValueError from asyncio StreamReader when a line exceeds the limit.

    This typically occurs when the CLI outputs a single JSONL line larger than
    the StreamReader limit (e.g., large tool results, file reads, logs).

    Args:
        e: The ValueError exception (re-raised from LimitOverrunError)
        cli_name: Name of the CLI for logging (e.g., "Claude", "Codex")
        session_id: Session ID for log correlation
        stream_limit: The configured stream limit in bytes

    Returns:
        StreamEvent with error type and message
    """
    error_msg = str(e)
    if "chunk is longer than limit" in error_msg:
        logger.error(
            f"{cli_name} CLI output line exceeded stream limit",
            session_id=session_id,
            stream_limit_bytes=stream_limit,
            error=error_msg,
            hint="A single JSONL line from the CLI exceeded the StreamReader limit. "
            "This typically happens with large tool results (file reads, logs, etc.).",
            exc_info=e,
        )
    else:
        logger.error(
            f"Error during {cli_name} CLI execution",
            session_id=session_id,
            error=error_msg,
            exc_info=e,
        )
    return StreamEvent(type="error", raw={"error": error_msg})


class _SessionFileLogger:
    """Writes event log lines to /tmp/session-log-{llm_session_id}.log.

    Buffers lines until the llm_session_id is known (from the system event),
    then flushes the buffer and writes subsequent lines directly.
    """

    def __init__(self, initial_llm_session_id: str | None) -> None:
        self._buffer: list[str] = []
        self._file: io.TextIOWrapper | None = None
        if initial_llm_session_id:
            self._open(initial_llm_session_id)

    def _open(self, llm_session_id: str) -> None:
        log_dir = os.path.join(AHS_DATA_DIR, "session_logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"{llm_session_id}.log")
        self._file = open(path, "a")  # noqa: SIM115
        # Flush any buffered lines
        for line in self._buffer:
            self._file.write(line)
        self._buffer.clear()
        self._file.flush()

    def log(self, model: str, event_type: str, excerpt: str, llm_session_id: str | None = None) -> None:
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] Received message from {model}: [{event_type}] {excerpt}\n"

        # If we just learned the session id, open the file
        if llm_session_id and not self._file:
            self._open(llm_session_id)

        if self._file:
            self._file.write(line)
            self._file.flush()
        else:
            self._buffer.append(line)

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


class ClaudeCodeRunner(AgentRunner):
    """Runs Claude Code CLI as a subprocess with streaming JSON output."""

    def __init__(
        self,
        agent_config: AgentConfig,
        pre_proc_task: "asyncio.Task[asyncio.subprocess.Process] | None" = None,
    ) -> None:
        self.config = agent_config
        # Optional pre-spawned process task started concurrently with DB writes in
        # create_session().  When set, run() awaits this task instead of spawning
        # a fresh process, hiding the ~4.2s bwrap+CLI cold start behind other I/O.
        self._pre_proc_task = pre_proc_task

    def _build_args(self, prompt: str, context: RunContext) -> list[str]:
        """Build the CLI args for claude."""
        # Pass session_id to system prompt so MCP-enabled agents know their session
        session_id_for_prompt = context.session_id if self.config.has_mcp else None
        system_prompt = build_system_prompt(
            self.config.name,
            session_id=session_id_for_prompt,
            slack_session_id=context.slack_session_id,
            is_slack=context.is_slack,
            is_task=context.is_task,
            session_context=context.session_context,
            additional_system_prompt=self.config.additional_system_prompt,
            required_tools=self.config.required_tools or None,
            workspace=context.workspace,
        )

        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

        if self.config.model:
            args += ["--model", self.config.model]

        if system_prompt:
            args += ["--system-prompt", system_prompt]

        if context.llm_session_id:
            args += ["--resume", context.llm_session_id]

        # Identity files (ROLE.md, WORKSPACE.md) are read by the harness
        # and injected via --system-prompt. Repos and worktrees are inside the
        # session workspace (symlinked or as real dirs). No --add-dir needed.
        ctx = context.session_context or {}
        if "permissions" not in ctx:
            perms = SessionPermissions.restricted()  # fail-secure
        else:
            perms = SessionPermissions.from_context(ctx)
        has_full_access = perms.has_full_tool_access

        # Sandbox: skip permission prompts (agent runs unattended)
        if self.config.sandbox.enabled:
            args += ["--dangerously-skip-permissions"]

        # Derive --allowedTools / --disallowedTools from tool_permissions
        allowed, disallowed = tool_permissions_to_cli_flags(self.config.tool_permissions)

        if allowed is not None:
            # Allowlist mode ("*": "deny"): explicitly list allowed tools + MCP tools.
            # Exclude CLI tools superseded by MCP equivalents (e.g., WebFetch → mcp__harness__webfetch).
            if self.config.has_mcp:
                allowed = [t for t in allowed if t not in _CLI_TOOLS_SUPERSEDED_BY_MCP]
                if has_full_access:
                    # Pre-declare top tools explicitly so Claude Code loads them eagerly
                    # (non-deferred). Wildcards follow as catch-alls for the rest.
                    # Explicitly named tools skip ToolSearch discovery; wildcards still defer.
                    allowed.extend(_TOP_HARNESS_TOOLS_PREDECLARED)
                    allowed.extend(_TOP_SHARED_TOOLS_PREDECLARED)
                    allowed.append("mcp__harness__*")
                else:
                    allowed.extend(_RESTRICTED_HARNESS_TOOLS_CLI)
            # When the allowed list is empty, Claude CLI ignores --allowedTools ""
            # and falls back to allowing all built-in tools. Use a non-matching sentinel
            # so the allowlist filter is active but matches nothing.
            effective_allowed = allowed or ["__none__"]
            args += ["--allowedTools", ",".join(effective_allowed)]
            # Even in allowlist mode, deny MCP harness tools that have native CLI equivalents
            # (e.g., mcp__harness__* wildcard would otherwise include mcp__harness__bash).
            # Also deny ``_ALWAYS_DISALLOWED_CLI_TOOLS`` (e.g. AskUserQuestion) which have
            # no working surface in headless ``claude -p`` mode regardless of session shape.
            extra_denies = list(_ALWAYS_DISALLOWED_CLI_TOOLS)
            if self.config.has_mcp:
                extra_denies.extend(_MCP_HARNESS_TOOLS_WITH_CLI_EQUIV)
            args += ["--disallowedTools", ",".join(extra_denies)]
        elif disallowed is not None:
            # Denylist mode with explicit denies
            disallowed.extend(_ALWAYS_DISALLOWED_CLI_TOOLS)
            if self.config.has_mcp:
                disallowed.extend(_CLI_TOOLS_SUPERSEDED_BY_MCP)
                disallowed.extend(_MCP_HARNESS_TOOLS_WITH_CLI_EQUIV)
            if not has_full_access and self.config.has_mcp:
                disallowed.extend(_BLOCKED_HARNESS_TOOLS_CLI)
            args += ["--disallowedTools", ",".join(disallowed)]
        else:
            # Default-allow mode ("*": "allow" or no wildcard): all tools available.
            # MCP tools still appear as deferred in <available-deferred-tools>; agents
            # should use ToolSearch(query="select:tool1,tool2,...") to batch-load them.
            # See MCP_TOOLS.md in the shared prompt for the full tool manifest.
            deny_list: list[str] = list(_ALWAYS_DISALLOWED_CLI_TOOLS)
            if self.config.has_mcp:
                deny_list.extend(_CLI_TOOLS_SUPERSEDED_BY_MCP)
                deny_list.extend(_MCP_HARNESS_TOOLS_WITH_CLI_EQUIV)
            if not has_full_access and self.config.has_mcp:
                deny_list.extend(_BLOCKED_HARNESS_TOOLS_CLI)
            if deny_list:
                args += ["--disallowedTools", ",".join(deny_list)]

        args += ["--max-turns", str(self.config.max_turns)]

        return args

    @staticmethod
    def _ensure_workspace_mcp_config(
        workspace: str,
        session_id: str = "",
        session_context: dict[str, Any] | None = None,
        is_slack: bool = False,
        agent_name: str = "",
    ) -> None:
        """Write .mcp.json to the workspace root for MCP server discovery.

        Delegates to the shared ensure_workspace_mcp_config() so all harnessed
        executors (Claude Code, Codex, etc.) use the same MCP setup logic.
        """
        ensure_workspace_mcp_config(workspace, session_id, session_context, is_slack, agent_name=agent_name)

    async def pre_spawn(self, prompt: str, context: RunContext) -> asyncio.subprocess.Process:
        """Build args, write .mcp.json, and spawn the subprocess early.

        Designed to be called as an asyncio.Task so the bwrap+CLI cold start
        (~4.2s) overlaps with DB writes in create_session().  The caller must
        ensure the workspace directory and .claude symlink exist before calling.

        The returned Process has stdout/stderr pipes ready; run() will consume
        them when it awaits the pre-spawn task via self._pre_proc_task.
        """
        args = self._build_args(prompt, context)
        cwd = context.workspace

        # Write .mcp.json so Claude CLI discovers MCP servers at startup.
        if self.config.has_mcp and cwd:
            self._ensure_workspace_mcp_config(
                cwd,
                session_id=context.session_id,
                session_context=context.session_context,
                is_slack=context.is_slack,
                agent_name=self.config.name,
            )

        # Apply bwrap if enabled — mirrors the logic in run().
        from ypl.agent_harness_service.executors.sandbox import (
            build_bwrap_cli_command,
            bwrap_available,
            log_bwrap_details,
        )

        use_bwrap = self.config.sandbox.bwrap_enabled and cwd is not None and bwrap_available()
        if use_bwrap:
            assert cwd is not None
            args = build_bwrap_cli_command(args, cwd)
            log_bwrap_details(args, context.session_id, "Pre-spawning CLI in bwrap sandbox")
        elif self.config.sandbox.bwrap_enabled and not bwrap_available():
            # Loud signal: config wants the sandbox on, but the host can't
            # provide it. Previous behaviour was to silently run unsandboxed,
            # which hid exactly the failure mode that caused the prod-DB
            # orphan-revision incident.
            logger.error(
                "Sandbox requested but bwrap is not functional — agent "
                "will run WITHOUT filesystem / PID / namespace isolation. "
                "Fix the host (kernel.apparmor_restrict_unprivileged_userns "
                "on Ubuntu 24+) to restore sandboxing.",
                session_id=context.session_id,
                agent_name=self.config.name,
            )

        logger.info(
            "Pre-spawning Claude Code CLI",
            session_id=context.session_id,
            cwd=cwd,
            model=self.config.model or "(cli default)",
            bwrap=use_bwrap,
        )

        return await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            env=build_subprocess_env(),
        )

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> str:
        """Drain stderr in the background to prevent pipe deadlock."""
        assert proc.stderr is not None
        chunks = [chunk async for chunk in proc.stderr]
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Spawn claude CLI and yield parsed stream events."""
        args = self._build_args(prompt, context)
        cwd = context.workspace
        model_label = self.config.model or "(cli default)"
        cmd_str = shlex.join(args)
        cli_launch_time = time.monotonic()

        # Queue wait: time from AgentSession DB creation → runner start.
        # Measures AHS-internal scheduling/queue delay before CLI is launched.
        # Large values (>5s) indicate queue backup or slow session dispatch.
        _queue_wait_ms: float | None = None
        if context.session_created_at is not None:
            _queue_wait_ms = round((datetime.now(UTC) - context.session_created_at).total_seconds() * 1000, 1)
            logger.info(
                "Queue wait time",
                session_id=context.session_id,
                queue_wait_ms=_queue_wait_ms,
            )
            metric_record_with_labels("ahs/queue_wait_ms", int(_queue_wait_ms), {"agent": self.config.name})
            _check_queue_bottleneck(int(_queue_wait_ms), context.session_id)

        # Use a pre-spawned process if one was started concurrently with DB writes
        # in create_session() (first-turn optimization).  Fall back to a fresh spawn
        # if the task failed or is absent (follow-up turns, retries, attachments, etc.).
        if self._pre_proc_task is not None:
            pre_proc_task, self._pre_proc_task = self._pre_proc_task, None
            logger.info(
                "Awaiting pre-spawned Claude Code CLI",
                session_id=context.session_id,
                cwd=cwd,
                model=model_label,
                command=cmd_str,
            )
            try:
                proc = await pre_proc_task
            except Exception as spawn_err:
                logger.warning(
                    "Pre-spawn failed, falling back to normal spawn",
                    session_id=context.session_id,
                    error=str(spawn_err),
                )
                proc = None
        else:
            proc = None

        if proc is None:
            # Normal spawn path: write .mcp.json and launch the subprocess.
            if self.config.has_mcp and cwd:
                self._ensure_workspace_mcp_config(
                    cwd,
                    session_id=context.session_id,
                    session_context=context.session_context,
                    is_slack=context.is_slack,
                    agent_name=self.config.name,
                )

            # Wrap in bubblewrap if enabled and available
            from ypl.agent_harness_service.executors.sandbox import (
                build_bwrap_cli_command,
                bwrap_available,
                log_bwrap_details,
            )

            use_bwrap = self.config.sandbox.bwrap_enabled and cwd is not None and bwrap_available()
            if use_bwrap:
                assert cwd is not None  # mypy narrowing (guarded by use_bwrap)
                args = build_bwrap_cli_command(args, cwd)
                log_bwrap_details(args, context.session_id, "Wrapping CLI in bwrap sandbox")
                cmd_str = shlex.join(args)
            elif self.config.sandbox.bwrap_enabled and not bwrap_available():
                logger.error(
                    "Sandbox requested but bwrap is not functional — agent "
                    "will run WITHOUT filesystem / PID / namespace isolation.",
                    session_id=context.session_id,
                    agent_name=self.config.name,
                )

            logger.info(
                "Launching Claude Code CLI",
                session_id=context.session_id,
                cwd=cwd,
                model=model_label,
                bwrap=use_bwrap,
                command=cmd_str,
            )

            # Claude Code emits single JSON lines that can exceed asyncio's default
            # 64KB StreamReader limit (e.g. large tool results, file reads).
            # Raise to 10MB to avoid LimitOverrunError.
            stream_limit = 10 * 1024 * 1024

            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=stream_limit,
                env=build_subprocess_env(),
            )

        # Increase OS pipe buffers from 64 KB → 4 MB to reduce context switches
        # on large tool results (file reads, GCP logs, DB query output).
        _try_enlarge_pipe_buf(proc)

        # Drain stderr concurrently to prevent pipe buffer deadlock
        stderr_task = asyncio.create_task(self._drain_stderr(proc))

        # File logger — always writes to /tmp/session-log-{llm_session_id}.log
        file_logger = _SessionFileLogger(context.llm_session_id)

        # Track whether we've logged startup time yet (done on first event received)
        _startup_logged = False
        # first_token_ms: time from CLI launch → first text content event (true TTFT proxy).
        _first_token_ms: float | None = None

        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    raw = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line from Claude CLI", line=line_str[:200])
                    continue

                event_type = raw.get("type", "unknown")
                event = StreamEvent(type=event_type, raw=raw)
                excerpt = extract_excerpt(event)

                # Emit cli_startup_ms on the first [system] event — the canonical
                # "CLI is ready" signal from Claude Code.  Measuring to the system
                # event (rather than any first event) gives a precise, alertable
                # view of true startup cost including Node.js init and MCP handshake.
                # Values > 15 000 ms indicate a cold start (e.g. MCP server scaling).
                if not _startup_logged and event_type == "system":
                    cli_startup_ms = int((time.monotonic() - cli_launch_time) * 1000)
                    _startup_logged = True
                    slow = cli_startup_ms > 15_000
                    log_fn = logger.warning if slow else logger.info
                    log_fn(
                        "CLI startup time",
                        session_id=context.session_id,
                        cli_startup_ms=cli_startup_ms,
                        slow_startup=slow,
                    )
                    metric_record_with_labels(
                        "ahs/cli_startup_ms",
                        cli_startup_ms,
                        {"agent": self.config.name, "slow": str(slow).lower()},
                    )

                # Track first_token_ms: time from CLI launch → first text content event.
                # Only fires on assistant events with actual text (excludes tool_use and
                # other non-text events) to give a true TTFT for visible output.
                if _first_token_ms is None and event_type == "assistant" and event.text:
                    _first_token_ms = round((time.monotonic() - cli_launch_time) * 1000, 1)
                    logger.info(
                        "First token received",
                        session_id=context.session_id,
                        first_token_ms=_first_token_ms,
                    )
                    metric_record_with_labels(
                        "ahs/first_token_ms",
                        int(_first_token_ms),
                        {"agent": self.config.name},
                    )

                # Always log to session file
                file_logger.log(model_label, event_type, excerpt, llm_session_id=event.session_id)

                if AGENT_RESPONSE_DEBUG:
                    logger.info(
                        f"Received message from {model_label}: [{event_type}] {excerpt}",
                        session_id=context.session_id,
                    )

                yield event

            await proc.wait()
            stderr_text = await stderr_task

            logger.info(
                "Claude CLI process exited",
                session_id=context.session_id,
                returncode=proc.returncode,
                stderr_preview=stderr_text[:500] if stderr_text else None,
            )

            if proc.returncode and proc.returncode != 0:
                logger.error(
                    "Claude CLI exited with error",
                    session_id=context.session_id,
                    returncode=proc.returncode,
                    stderr=stderr_text[:1000],
                )
                yield StreamEvent(
                    type="error",
                    raw={"error": f"CLI exited with code {proc.returncode}", "stderr": stderr_text[:1000]},
                )

        except ValueError as e:
            yield _handle_stream_limit_error(e, "Claude", context.session_id, stream_limit)
        except Exception as e:
            logger.error(
                "Error during Claude CLI execution",
                session_id=context.session_id,
                error=str(e),
                exc_info=True,
            )
            yield StreamEvent(type="error", raw={"error": str(e)})
        finally:
            # Log startup time even if the CLI exited/failed before the [system] event.
            # This captures cold-start outliers where the process never reached readiness.
            if not _startup_logged:
                cli_startup_ms = int((time.monotonic() - cli_launch_time) * 1000)
                slow = cli_startup_ms > 15_000
                # Use WARNING here: a CLI that exits/crashes before emitting any
                # stdout is an abnormal condition (not a slow startup).  Keeping
                # this at WARNING ensures existing alerting on WARNING-level entries
                # continues to fire for these genuine failures.
                logger.warning(
                    "CLI startup time (no system event received)",
                    session_id=context.session_id,
                    cli_startup_ms=cli_startup_ms,
                    slow_startup=slow,
                    system_event_received=False,
                    returncode=proc.returncode,
                )
                metric_record_with_labels(
                    "ahs/cli_startup_ms",
                    cli_startup_ms,
                    {"agent": self.config.name, "slow": str(slow).lower(), "abnormal": "true"},
                )

            # Kill subprocess on any exit (including CancelledError from timeout)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stderr_task.cancel()
            file_logger.close()


class RawExecutorRunner(AgentRunner):
    """Runs agents via direct model API calls with MCP tool routing.

    Unlike ClaudeCodeRunner (which spawns a CLI subprocess), this runner
    calls the model API directly and routes tool calls through MCPToolAccess.
    Tool permissions are enforced at two levels:
    1. Agent-level: tool_permissions from config.json filter which MCP tools are exposed
    2. Session-level: SessionPermissions restrict which MCP servers and harness tools are available
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Run the raw executor and yield stream events.

        Wires an on_event callback into run_raw_executor so that every step
        (init, assistant responses, tool calls/results, final result) is
        yielded as a StreamEvent. This gives the service layer full visibility
        into raw executor execution — matching harnessed executor fidelity.
        """
        from ypl.agent_harness_service.common.constants import PERM_DENY
        from ypl.agent_harness_service.common.models import AgentSpec
        from ypl.agent_harness_service.executors.raw_executor import run_raw_executor
        from ypl.agent_harness_service.tools.mcp_client import MCPToolAccess

        session_id = context.session_id

        # Build an AgentSpec from AgentConfig (raw executor expects AgentSpec).
        # Reuse executor_config directly — it's already an ExecutorConfig instance.
        agent_spec = AgentSpec(
            name=self.config.name,
            description=self.config.description,
            executor=self.config.executor_config,
            tools=self.config.tool_permissions,
            allowed_subagents=self.config.allowed_subagents,
            max_steps=self.config.max_turns,
            timeout_s=self.config.timeout_s,
            streaming=False,
            additional_system_prompt=self.config.additional_system_prompt,
            required_tools=self.config.required_tools,
        )

        # Resolve permissions from context (with fail-secure defaults).
        # Must match the harnessed path logic in ensure_workspace_mcp_config().
        ctx = context.session_context or {}
        if "permissions" in ctx:
            perms = SessionPermissions.from_context(ctx)
        elif context.is_slack:
            # Slack session without any permission info — fail secure
            perms = SessionPermissions.restricted()
            logger.warning(
                "Slack session missing permissions, defaulting to restricted",
                session_id=session_id,
            )
        else:
            # Non-Slack sessions (API, cron, webhook) without permission info — fail secure.
            perms = SessionPermissions.restricted()
            logger.warning(
                "Session missing permissions, defaulting to restricted",
                session_id=session_id,
            )

        # Merge session-level restrictions into agent-level tool config.
        # Deny privileged harness tools not in allowed_harness_tools — matches the
        # harnessed path which adds BLOCKED_HARNESS_TOOLS to --disallowedTools.
        effective_tools: dict[str, str] = dict(self.config.tool_permissions)
        if not perms.has_full_tool_access:
            for tool in BLOCKED_HARNESS_TOOLS:
                if tool not in perms.allowed_harness_tools:
                    effective_tools[tool] = PERM_DENY
        allowed_servers = frozenset(perms.allowed_servers)

        # If agent denies all tools and has no MCP, skip MCP connections
        mock_session_id = f"raw-{uuid.uuid4()}"

        # Phase 3: Construct session history path for cross-turn persistence
        session_history_path: str | None = None
        if session_id and agent_spec.executor.history.enabled:
            from ypl.agent_harness_service.executors.context import get_session_history_path

            session_history_path = get_session_history_path(session_id)

        logger.info(
            "Launching raw executor",
            session_id=session_id,
            agent_name=self.config.name,
            model=self.config.model,
            tool_permissions=self.config.tool_permissions,
            allowed_servers=list(allowed_servers),
            session_history_path=session_history_path,
        )

        # Stream events in real-time via an asyncio.Queue bridge.
        # The sync _on_event callback pushes to the queue; this async generator
        # yields from it concurrently while the executor runs.
        _SKIP_EVENT_TYPES = {"assistant", "result"}
        _SENTINEL = object()
        event_queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()

        def _on_event(event: dict[str, Any]) -> None:
            event_type = event.get("type", "unknown")
            if event_type not in _SKIP_EVENT_TYPES:
                event_queue.put_nowait(event)

        # Yield system event (with session_id for service.py compatibility)
        yield StreamEvent(
            type="system",
            raw={"type": "system", "session_id": mock_session_id},
        )

        # Run the executor in a background task so we can yield events as they arrive.
        executor_result_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def _run_executor() -> None:
            try:
                ctx = context.session_context or {}
                requesting_user_id = ctx.get("current_turn_user_id") or ctx.get("user_id")
                async with MCPToolAccess(
                    session_id,
                    effective_tools,
                    allowed_servers=allowed_servers,
                    user_id=requesting_user_id,
                    agent_name=self.config.name,
                ) as mcp:
                    result = await run_raw_executor(
                        agent=agent_spec,
                        prompt=prompt,
                        model=self.config.model,
                        mcp_tools=mcp.mcp_tools,
                        tool_executor=mcp.call_tool,
                        session_history_path=session_history_path,
                        resource_catalog=mcp.resource_catalog or None,
                        on_event=_on_event,
                        session_id=session_id,
                        is_slack=context.is_slack,
                        is_task=context.is_task,
                        session_context=context.session_context,
                        slack_session_id=context.slack_session_id,
                    )
                executor_result_future.set_result(result)
            except Exception as exc:
                executor_result_future.set_exception(exc)
            finally:
                await event_queue.put(_SENTINEL)

        executor_task = asyncio.create_task(_run_executor())

        try:
            # Yield events from the queue as they arrive in real-time
            while True:
                item = await event_queue.get()
                if item is _SENTINEL:
                    break
                assert isinstance(item, dict)
                event_type = item.get("type", "unknown")
                yield StreamEvent(type=event_type, raw=item)

            # Executor finished — check for errors and yield final events
            if not executor_result_future.done():
                logger.error(
                    "Executor future unresolved (likely CancelledError)",
                    session_id=session_id,
                    agent_name=self.config.name,
                )
                yield StreamEvent(type="error", raw={"error": "Executor cancelled unexpectedly"})
            elif executor_result_future.exception():
                exc = executor_result_future.exception()
                logger.error(
                    "Raw executor failed",
                    session_id=session_id,
                    agent_name=self.config.name,
                    error=str(exc),
                    exc_info=exc,
                )
                yield StreamEvent(type="error", raw={"error": str(exc)})
            else:
                result = executor_result_future.result()
                # Yield the final assistant event with full response text.
                # This is what service.py uses for final_text and gateway delivery.
                if result.text:
                    yield StreamEvent(
                        type="assistant",
                        raw={
                            "type": "assistant",
                            "message": {"content": [{"type": "text", "text": result.text}]},
                        },
                    )

                # Derive subtype (mirrors raw_executor.py logic)
                if result.text and "[STOPPED] Context overflow" in result.text:
                    result_subtype = "stopped_context_overflow"
                elif result.text and result.text.startswith("[STOPPED]"):
                    result_subtype = "error_max_turns"
                else:
                    result_subtype = "success"

                # Yield result event (service.py extracts cost/duration/session_id from this)
                yield StreamEvent(
                    type="result",
                    raw={
                        "type": "result",
                        "subtype": result_subtype,
                        "session_id": result.session_id or mock_session_id,
                        "estimated_cost_usd": result.estimated_cost_usd,
                        "duration_ms": result.duration_ms,
                        "num_turns": result.tokens.get("num_turns") if result.tokens else None,
                    },
                )
        except BaseException:
            executor_task.cancel()
            try:
                await executor_task
            except (asyncio.CancelledError, Exception):
                pass
            raise


class MockRunner(AgentRunner):
    """Mock runner for testing that logs prompts without calling a real agent.

    Useful for:
    - Testing scheduled agent calls without incurring API costs
    - Dry-run mode to verify prompts and context
    - Local development without Claude CLI installed

    To use: set executor: "mock" in the agent config YAML.
    """

    def __init__(self, agent_config: AgentConfig) -> None:
        self.config = agent_config

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        """Log the prompt and yield fake events simulating a successful run."""
        mock_session_id = f"mock-{uuid.uuid4()}"

        logger.info(
            "MockRunner: received prompt",
            agent_name=self.config.name,
            session_id=context.session_id,
            mock_session_id=mock_session_id,
            prompt_length=len(prompt),
            prompt_preview=prompt[:500],
            workspace=context.workspace,
            is_slack=context.is_slack,
            slack_session_id=context.slack_session_id,
        )

        if context.session_context:
            logger.info(
                "MockRunner: session context",
                session_id=context.session_id,
                context=context.session_context,
            )

        # Yield system event with mock session ID
        yield StreamEvent(
            type="system",
            raw={
                "type": "system",
                "session_id": mock_session_id,
                "message": "MockRunner initialized",
            },
        )

        # Yield a fake assistant response
        mock_response = (
            f"[MockRunner] This is a simulated response for agent '{self.config.name}'.\n\n"
            f"Prompt received ({len(prompt)} chars):\n{prompt[:1000]}" + ("..." if len(prompt) > 1000 else "")
        )

        yield StreamEvent(
            type="assistant",
            raw={
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": mock_response}],
                },
            },
        )

        # Yield result event
        yield StreamEvent(
            type="result",
            raw={
                "type": "result",
                "session_id": mock_session_id,
                "estimated_cost_usd": 0.0,
                "duration_ms": 100,
                "num_turns": 1,
            },
        )

        logger.info(
            "MockRunner: completed",
            agent_name=self.config.name,
            session_id=context.session_id,
            mock_session_id=mock_session_id,
        )
