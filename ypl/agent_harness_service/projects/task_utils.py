"""Shared utilities for task status validation and transitions.

This module provides validation logic used by both:
- ypl/mcp_server/tools/project_tasks.py (MCP tools for task management)
- ypl/agent_harness_service/task_executor.py (task execution engine)
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.db.agent_harness import AgentSessionMessage, AgentTask, AgentTaskStatus

# Terminal statuses that indicate a task is finished
TERMINAL_TASK_STATUSES: set[AgentTaskStatus] = {
    AgentTaskStatus.COMPLETED,
    AgentTaskStatus.FAILED,
    AgentTaskStatus.CANCELLED,
}

# Valid status transitions for agent tasks.
# Key: current status, Value: set of allowed target statuses.
# Note: PENDING is allowed from any status for task reset/recovery.
VALID_TASK_TRANSITIONS: dict[AgentTaskStatus, set[AgentTaskStatus]] = {
    AgentTaskStatus.PENDING: {AgentTaskStatus.BLOCKED, AgentTaskStatus.READY, AgentTaskStatus.CANCELLED},
    AgentTaskStatus.BLOCKED: {AgentTaskStatus.READY, AgentTaskStatus.PENDING, AgentTaskStatus.CANCELLED},
    AgentTaskStatus.READY: {
        AgentTaskStatus.IN_PROGRESS,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.PENDING,
    },
    AgentTaskStatus.IN_PROGRESS: {
        AgentTaskStatus.COMPLETED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.READY,
        AgentTaskStatus.PENDING,
        AgentTaskStatus.IN_REVIEW,  # Agent finished, awaiting human review
    },
    AgentTaskStatus.IN_REVIEW: {
        AgentTaskStatus.COMPLETED,  # Review passed
        AgentTaskStatus.FAILED,  # Review failed
        AgentTaskStatus.CANCELLED,  # Cancelled during review
        AgentTaskStatus.PENDING,  # Reset task for re-execution
    },
    AgentTaskStatus.COMPLETED: {AgentTaskStatus.PENDING, AgentTaskStatus.FAILED},  # Allow restart/mark as failed
    AgentTaskStatus.FAILED: {AgentTaskStatus.PENDING, AgentTaskStatus.READY},
    AgentTaskStatus.CANCELLED: {AgentTaskStatus.PENDING},
}


def validate_task_status_transition(current: AgentTaskStatus, target: AgentTaskStatus) -> str | None:
    """Validate that a task status transition is allowed.

    Args:
        current: The task's current status.
        target: The desired target status.

    Returns:
        None if the transition is valid, otherwise an error message string.
    """
    allowed = VALID_TASK_TRANSITIONS.get(current, set())
    if target not in allowed:
        return f"Cannot transition from {current.value} to {target.value}. Allowed: {sorted(s.value for s in allowed)}"
    return None


async def are_dependencies_completed(session: AsyncSession, task: AgentTask) -> bool:
    """Check if all dependencies of a task are COMPLETED.

    Args:
        session: SQLAlchemy async session.
        task: The task to check dependencies for.

    Returns:
        True if the task has no dependencies or all dependencies are COMPLETED.
        False if any dependency is not COMPLETED or doesn't exist.
    """
    if not task.depends_on:
        return True

    dep_ids = [uuid.UUID(d) for d in task.depends_on]
    result = await session.execute(
        select(sa.func.count())
        .select_from(AgentTask)
        .where(col(AgentTask.agent_task_id).in_(dep_ids))
        .where(col(AgentTask.status) == AgentTaskStatus.COMPLETED)
        .where(col(AgentTask.deleted_at).is_(None))
    )
    completed_count: int = result.scalar_one()
    return completed_count == len(dep_ids)


async def validate_task_status_semantics(
    session: AsyncSession,
    task: AgentTask,
    target_status: AgentTaskStatus,
) -> str | None:
    """Validate that a target status is semantically correct for the task.

    This checks dependency-based constraints:
    - READY: All dependencies must be COMPLETED.
    - BLOCKED: Task must have dependencies (otherwise use PENDING/READY).

    Args:
        session: SQLAlchemy async session.
        task: The task being updated.
        target_status: The desired target status.

    Returns:
        None if the status is semantically valid, otherwise an error message string.
    """
    if target_status == AgentTaskStatus.READY:
        if not await are_dependencies_completed(session, task):
            return (
                "Cannot set status to READY: not all dependencies are COMPLETED. "
                "Use BLOCKED for tasks with unmet dependencies."
            )
    elif target_status == AgentTaskStatus.BLOCKED:
        if not task.depends_on:
            return (
                "Cannot set status to BLOCKED: task has no dependencies. "
                "Use PENDING or READY for tasks without dependencies."
            )
    return None


async def validate_task_status_change(
    session: AsyncSession,
    task: AgentTask,
    target_status: AgentTaskStatus,
) -> str | None:
    """Validate both transition and semantic constraints for a status change.

    This is a convenience function that combines:
    1. validate_task_status_transition() - checks if transition path is allowed
    2. validate_task_status_semantics() - checks dependency constraints

    Args:
        session: SQLAlchemy async session.
        task: The task being updated.
        target_status: The desired target status.

    Returns:
        None if the status change is valid, otherwise an error message string.
    """
    # Check transition is allowed
    transition_error = validate_task_status_transition(task.status, target_status)
    if transition_error:
        return transition_error

    # Check semantic constraints
    return await validate_task_status_semantics(session, task, target_status)


async def aggregate_task_spending(session: AsyncSession, task: AgentTask) -> Decimal | None:
    """Aggregate spending from all sessions that worked on a task.

    Sums up cost_usd from AgentSessionMessage for all sessions in task.assigned_session_ids.

    Args:
        session: SQLAlchemy async session.
        task: The task to aggregate spending for.

    Returns:
        Total spending as Decimal, or None if no spending recorded.
    """
    if not task.assigned_session_ids:
        return None

    session_uuids = [uuid.UUID(sid) for sid in task.assigned_session_ids]
    result = await session.execute(
        select(sa.func.sum(AgentSessionMessage.cost_usd)).where(
            col(AgentSessionMessage.agent_session_id).in_(session_uuids)
        )
    )
    total = result.scalar_one_or_none()
    return Decimal(str(total)) if total is not None else None


async def complete_task(
    session: AsyncSession,
    task: AgentTask,
    status: AgentTaskStatus,
    result: dict[str, Any] | None = None,
    *,
    aggregate_spending: bool = True,
) -> None:
    """Complete a task by setting terminal status and aggregating spending.

    This is the canonical way to transition a task to a terminal state (COMPLETED, FAILED, CANCELLED).
    It handles:
    1. Setting the status
    2. Setting completed_at timestamp
    3. Setting the result (if provided)
    4. Aggregating actual_spending_usd from assigned sessions

    Note: This function accepts an AsyncSession rather than creating one internally because
    callers often need transactional atomicity with other operations (e.g., validation checks,
    cascading status changes to dependent tasks). The caller is responsible for committing.

    Args:
        session: SQLAlchemy async session (caller must commit).
        task: The task to complete.
        status: Target status (must be COMPLETED, FAILED, or CANCELLED).
        result: Optional result dict to store on the task.
        aggregate_spending: Whether to aggregate spending from sessions (default True).

    Raises:
        ValueError: If status is not a terminal status or transition is invalid.
    """
    if status not in TERMINAL_TASK_STATUSES:
        raise ValueError(f"complete_task requires terminal status, got {status.value}")

    transition_error = validate_task_status_transition(task.status, status)
    if transition_error:
        raise ValueError(transition_error)

    task.status = status
    task.completed_at = datetime.now(UTC)

    if result is not None:
        task.result = result

    if aggregate_spending:
        spending = await aggregate_task_spending(session, task)
        if spending is not None:
            task.actual_spending_usd = spending

    session.add(task)


# Statuses that should not be restarted due to executor race conditions
NON_RESTARTABLE_STATUSES: set[AgentTaskStatus] = {
    AgentTaskStatus.IN_PROGRESS,  # Executor may be actively working on this task
}


async def restart_task(task_id: uuid.UUID) -> tuple[bool, str]:
    """Restart a task by resetting it to PENDING status.

    This is the canonical way to restart a task. It handles:
    1. Fetching the task with FOR UPDATE lock
    2. Blocking restart for IN_PROGRESS tasks (executor race condition)
    3. Validating the transition is allowed
    4. Resetting status to PENDING
    5. Clearing completed_at, result, and assigned_session_ids
    6. Committing the transaction

    Args:
        task_id: The UUID of the task to restart.

    Returns:
        A tuple of (success, message).
    """
    from ypl.backend.db import get_async_session

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_id)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = result.scalar_one_or_none()
        if task is None:
            return False, "Task not found"

        # Block restart for tasks that may have active executors
        if task.status in NON_RESTARTABLE_STATUSES:
            return (
                False,
                f"Cannot restart task with status {task.status.value}. Cancel or wait for the task to complete first.",
            )

        # Validate transition is allowed
        transition_error = validate_task_status_transition(task.status, AgentTaskStatus.PENDING)
        if transition_error:
            return False, transition_error

        old_status = task.status

        # Reset task state
        task.status = AgentTaskStatus.PENDING
        task.completed_at = None
        task.result = None
        task.assigned_session_ids = None

        session.add(task)
        await session.commit()
        return True, f"Task restarted (was {old_status.value})"
