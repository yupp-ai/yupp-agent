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
from sqlmodel import select

from ypl.backend.db import get_async_session
from ypl.db.agent_harness import AgentSession
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


def register_orchestration_callbacks(
    run_subagent: Callable[..., Any],
    route_model_stub: Callable[..., list[str]],
) -> None:
    """Called by server.py at startup to wire orchestration functions."""
    global _run_subagent_fn, _route_model_stub_fn
    _run_subagent_fn = run_subagent
    _route_model_stub_fn = route_model_stub


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

    Cleans up auth terminal states, current user tracking, and polling tasks.
    Note: User GitHub tokens persist across sessions (keyed by user_id, not session_id).
    """
    _session_auth_terminal_states.pop(session_id, None)
    _session_current_user.pop(session_id, None)
    task = _session_polling_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
