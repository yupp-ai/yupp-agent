"""FastMCP instance and shared session state for the harness MCP server.

This is the central module. All other tool files import ``mcp`` from here
and register their tools via ``@mcp.tool()``.  The module also owns every
module-level state dict so that ``clear_session_state()`` can clean up
state that lives in multiple tool modules without creating circular imports.
"""

from __future__ import annotations
import asyncio
import uuid as _uuid
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from sqlalchemy import text
from sqlmodel import select

from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.backend.db import get_async_session
from ypl.backend.utils.slack_utils import create_slack_link
from ypl.db.agent_harness import AgentSession, AgentTask
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# FastMCP instance — the single harness MCP app.
# All @mcp.tool() registrations in tool submodules use this object.
# ---------------------------------------------------------------------------

mcp = FastMCP("harness")

# ---------------------------------------------------------------------------
# Dependency injection: orchestration callbacks registered by server.py at
# startup. This avoids tools/ importing from the root wiring layer.
# ---------------------------------------------------------------------------

_run_subagent_fn: Callable[..., Any] | None = None
_route_model_stub_fn: Callable[..., list[str]] | None = None
_create_agent_fn: Callable[..., Any] | None = None


def register_orchestration_callbacks(
    run_subagent: Callable[..., Any],
    route_model_stub: Callable[..., list[str]],
    create_agent: Callable[..., Any] | None = None,
) -> None:
    """Called by server.py at startup to wire orchestration functions."""
    global _run_subagent_fn, _route_model_stub_fn, _create_agent_fn
    _run_subagent_fn = run_subagent
    _route_model_stub_fn = route_model_stub
    _create_agent_fn = create_agent


# ---------------------------------------------------------------------------
# Session sandbox registry
# Maps session_id → stack of bwrap_enabled values.
# Stack-based so nested scopes (parent → subagent) don't clobber each other.
# ---------------------------------------------------------------------------

_session_sandbox: dict[str, list[bool]] = {}


def set_session_sandbox(session_id: str, bwrap_enabled: bool) -> None:
    """Push a bwrap_enabled value onto the session's sandbox stack."""
    _session_sandbox.setdefault(session_id, []).append(bwrap_enabled)


def clear_session_sandbox(session_id: str) -> None:
    """Pop the top sandbox entry for a session. Remove key when stack is empty."""
    stack = _session_sandbox.get(session_id)
    if stack:
        stack.pop()
        if not stack:
            del _session_sandbox[session_id]


# ---------------------------------------------------------------------------
# Per-turn and per-session websearch call counters
# ---------------------------------------------------------------------------

_turn_websearch_count: dict[str, int] = {}
_session_websearch_count: dict[str, int] = {}
_session_websearch_locks: dict[str, asyncio.Lock] = {}


def reset_turn_websearch_count(session_id: str) -> None:
    """Reset the per-turn websearch counter for a new turn."""
    _turn_websearch_count.pop(session_id, None)


def clear_session_websearch_count(session_id: str) -> None:
    """Remove all websearch counters and lock for a session."""
    _turn_websearch_count.pop(session_id, None)
    _session_websearch_count.pop(session_id, None)
    _session_websearch_locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Current user tracking per session
# Set by service.py before each agent turn; read by MCP tools to identify
# the requester. Kept in-memory to avoid DB race conditions.
# ---------------------------------------------------------------------------

_session_current_user: dict[str, str] = {}


def set_session_current_user(session_id: str, user_id: str) -> None:
    """Set the current message sender for a session. Called before each turn."""
    _session_current_user[session_id] = user_id


async def _get_current_message_user_id(session_id: str) -> str | None:
    """Get the Yupp user_id of the user who sent the most recent message in this session.

    Reads from in-memory storage set by service.py before each turn.
    Falls back to session creator if not found (e.g., for older sessions).
    """
    user_id = _session_current_user.get(session_id)
    if user_id:
        return user_id

    # Fallback: look up session creator from DB
    # TODO: For subagent sessions, propagate the parent session's current_user_id to avoid
    # misattribution when subagent calls create_pr in a multi-user thread (see PR #10877).
    logger.warning("current_message_user_id not in memory, falling back to session creator", session_id=session_id)
    try:
        session_uuid = _uuid.UUID(session_id)
    except ValueError:
        logger.warning("session_id is not a valid UUID, cannot look up creator", session_id=session_id)
        return None
    async with get_async_session() as db:
        result = await db.execute(
            select(AgentSession.creator_user_id).where(AgentSession.agent_session_id == session_uuid)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Session metadata lookup
# Shared helper for subagent spawning and gateway tools.
# Lives here (not in gateway_tools or subagents) so both can import it without
# creating a cross-Layer-1-module dependency.
# ---------------------------------------------------------------------------


async def _resolve_parent_session(harness_session_id: str) -> dict[str, Any]:
    """Look up parent session metadata for subagent spawning.

    Args:
        harness_session_id: The harness session UUID.

    Returns:
        Dict with agent_name, model, workspace, and permissions (any may be None).
    """
    async with get_async_session() as db:
        result = await db.execute(
            text(
                "SELECT a.name, s.model, s.workspace, s.context FROM agent_sessions s "
                "JOIN agents a ON s.agent_id = a.agent_id "
                "WHERE s.agent_session_id = :sid"
            ),
            {"sid": harness_session_id},
        )
        row = result.fetchone()
        if not row:
            return {"agent_name": None, "model": None, "workspace": None, "permissions": None}
        context = row[3] or {}
        permissions: dict[str, Any] | None = context.get("permissions")
        subagent_depth: int = context.get("subagent_depth", 0)
        return {
            "agent_name": row[0],
            "model": row[1],
            "workspace": row[2],
            "permissions": permissions,
            "subagent_depth": subagent_depth,
        }


# ---------------------------------------------------------------------------
# PR attribution for AHS-driven sessions
# ---------------------------------------------------------------------------


async def _resolve_pr_attribution(session_id: str) -> str | None:
    """Build the PR attribution header for AHS-driven sessions.

    Returns a markdown attribution block — always including the agent name and
    the Lit session link, plus a project/task link when the session is task-
    triggered and a Slack thread link when the session was initiated from
    Slack.  The session link is what downstream review-fix loop tooling
    (e.g. master-reviewer) parses to route follow-up notifications back to the
    author session, so it must be emitted for every AHS-driven trigger
    (TASK, SLACK, AGENT, API, CRON), not just TASK.

    Returns None only when the session row cannot be located.
    """
    try:
        sid = _uuid.UUID(session_id)
    except ValueError:
        return None

    async with get_async_session() as db:
        # Fetch session context + agent name in one query.
        # ``s.trigger`` is intentionally not selected — attribution is now emitted for
        # every AHS-driven trigger, so we don't need to branch on it.
        result = await db.execute(
            text(
                "SELECT a.name, s.context FROM agent_sessions s "
                "JOIN agents a ON s.agent_id = a.agent_id "
                "WHERE s.agent_session_id = :sid"
            ),
            {"sid": str(sid)},
        )
        row = result.fetchone()
        if not row:
            return None

        agent_name: str = row[0] or "agent"
        context: dict[str, Any] = row[1] or {}

        task_id = context.get("task_id")
        project_id = context.get("project_id")
        project_name = context.get("project_name", "")
        user_name = context.get("user_name", "")
        slack_channel_id = context.get("slack_channel_id", "")
        slack_thread_ts = context.get("slack_thread_ts", "")

        # Fetch task title for the project/task link line.  Only relevant when
        # a task is actually associated with this session — otherwise the
        # query is skipped to avoid an unnecessary DB hit.
        task_title = ""
        if task_id:
            try:
                task_result = await db.execute(
                    select(AgentTask.title).where(AgentTask.agent_task_id == _uuid.UUID(task_id))
                )
                task_title = task_result.scalar_one_or_none() or ""
            except Exception:
                logger.warning("Failed to fetch task title for PR attribution", task_id=task_id)

    # Build attribution lines
    lines: list[str] = []

    # Line 1: agent + user (+ project/task link, when present).
    # The "for *<user>*" suffix is omitted when the session has no user_name
    # (AGENT-triggered sessions inherit user identity from the sender, but the
    # user_name context field may not be populated on every code path — emit a
    # cleaner header rather than "for **").
    attribution = f"\U0001f916 *{agent_name}*"
    if user_name:
        attribution += f" for *{user_name}*"
    if project_id and task_id:
        task_url = f"{AHS_LIT_BASE_URL}/agent_projects?project_id={project_id}&task_id={task_id}"
        label = (
            f"{project_name} / {task_title}" if project_name and task_title else task_title or project_name or "Task"
        )
        attribution += f" · \U0001f4cb [{label}]({task_url})"
    lines.append(attribution)

    # Line 2: session link (always) + Slack thread link (when the session was
    # initiated from Slack and the workspace domain is configured).  The
    # session link must be first — downstream tooling parses ``session_id=...``
    # from it to wire up review-fix notifications.
    session_url = f"{AHS_LIT_BASE_URL}/agent_harness_console?session_id={session_id}"
    line2 = f"\U0001f517 [Session]({session_url})"
    if slack_channel_id and slack_thread_ts:
        # Use the thread root ts for both the message ts (deep-link target)
        # and ``main_thread_ts`` (opens the side panel).  Returns None when
        # ``SLACK_WORKSPACE_DOMAIN_NAME`` is unset — silently omit the link
        # rather than emitting a broken URL.
        slack_url = create_slack_link(slack_channel_id, slack_thread_ts, slack_thread_ts)
        if slack_url:
            line2 += f" · [Slack]({slack_url})"
    lines.append(line2)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub auth terminal states and polling tasks
# Owned here so that clear_session_state() can clean them up without
# importing from github_auth (which would create a circular import).
# ---------------------------------------------------------------------------

_session_auth_terminal_states: dict[str, str] = {}
_session_polling_tasks: dict[str, asyncio.Task[None]] = {}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: str) -> str:
    """Validate that session_id is a proper UUID to prevent path traversal."""
    if not session_id:
        raise ValueError("session_id is required")
    try:
        _uuid.UUID(session_id)
    except ValueError:
        raise ValueError(f"session_id is not a valid UUID: {session_id!r}") from None
    return session_id


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def clear_session_state(session_id: str) -> None:
    """Clear all session-specific state when session ends.

    Cleans up all in-memory state keyed by session_id: sandbox stack,
    websearch counters, auth terminal states, current user tracking,
    and polling tasks.
    Note: User GitHub tokens persist across sessions (keyed by user_id, not session_id).
    """
    clear_session_sandbox(session_id)
    clear_session_websearch_count(session_id)
    _session_auth_terminal_states.pop(session_id, None)
    _session_current_user.pop(session_id, None)
    task = _session_polling_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
