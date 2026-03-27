"""Per-parent-session queue for delivering subagent results back to the caller.

When a subagent completes (or errors), its result is pushed onto the parent
session's queue. A background drain task picks it up and injects it as a new
user-turn in the parent session so the parent agent can respond.

Queue lifecycle:
- Queues are created on first result push and destroyed when emptied.
- Queue contents are in-memory only — lost on AHS restart (acceptable).
- Each drain is async fire-and-forget (asyncio.ensure_future).
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from ypl.structured_logger import get_logger

logger = get_logger()

# Maximum nesting depth for subagent spawning.
# Root session = 0, child = 1, grandchild = 2.
# Agents at depth MAX_SUBAGENT_DEPTH cannot spawn further subagents.
MAX_SUBAGENT_DEPTH = 2

# Per-parent-session queues: parent_session_id (str) -> asyncio.Queue[SubagentResult]
_queues: dict[str, asyncio.Queue["SubagentResult"]] = {}

# Registered delivery callback (set by server.py at startup).
# Signature: async (parent_session_id: str, result: SubagentResult) -> None
_delivery_callback: Callable[[str, "SubagentResult"], Coroutine[Any, Any, None]] | None = None


@dataclass
class SubagentResult:
    """Result envelope delivered from a subagent to its parent session."""

    db_session_id: str
    agent_type: str
    model: str
    status: str  # "completed" | "error" | "timeout" | "cancelled"
    text: str
    duration_ms: int | None = None
    cost_usd: float | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def register_delivery_callback(
    fn: Callable[[str, "SubagentResult"], Coroutine[Any, Any, None]],
) -> None:
    """Register the async function called to deliver a result to the parent.

    Called by server.py at startup. The callback receives (parent_session_id, result)
    and is responsible for queuing or injecting the result into the parent session.
    """
    global _delivery_callback
    _delivery_callback = fn


def _get_or_create_queue(parent_session_id: str) -> asyncio.Queue["SubagentResult"]:
    if parent_session_id not in _queues:
        _queues[parent_session_id] = asyncio.Queue()
    return _queues[parent_session_id]


def put_result(parent_session_id: str, result: "SubagentResult") -> None:
    """Queue a subagent result for delivery to the parent session.

    Puts the result on the queue and schedules a drain task. Safe to call from
    any asyncio coroutine. The drain runs after the current coroutine yields.

    Args:
        parent_session_id: UUID string of the parent session.
        result: SubagentResult with completion details.
    """
    queue = _get_or_create_queue(parent_session_id)
    queue.put_nowait(result)
    asyncio.ensure_future(_drain(parent_session_id))


async def _drain(parent_session_id: str) -> None:
    """Drain all pending results for a parent session, calling the delivery callback."""
    queue = _queues.get(parent_session_id)
    if not queue:
        return

    while not queue.empty():
        try:
            result = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        if _delivery_callback is None:
            logger.error(
                "Subagent result delivery callback not registered — result dropped",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                db_session_id=result.db_session_id,
            )
            queue.task_done()
            continue

        try:
            await _delivery_callback(parent_session_id, result)
        except Exception:
            logger.error(
                "Failed to deliver subagent result to parent",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                db_session_id=result.db_session_id,
                exc_info=True,
            )
        finally:
            queue.task_done()

    # Cleanup empty queues to avoid unbounded growth
    if parent_session_id in _queues and _queues[parent_session_id].empty():
        del _queues[parent_session_id]
