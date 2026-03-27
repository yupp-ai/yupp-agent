"""Scheduler for executing agent schedules.

This module provides a background polling loop that:
1. Queries for due agent schedules (SCHEDULED and RECURRING)
2. Claims them atomically and creates a run record
3. Executes them by creating sessions and sending messages
4. Updates run status and parent schedule status based on success/failure
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlmodel import col, select

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.service import create_session, has_execution_capacity
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.db.agent_harness import (
    Agent,
    AgentSchedule,
    AgentScheduleRun,
    AgentScheduleRunStatus,
    AgentScheduleStatus,
    AgentScheduleType,
)
from ypl.mcp_common.scheduled_agent_call_helpers import compute_next_run_for_cron
from ypl.structured_logger import get_logger

logger = get_logger()

# Track in-flight execution tasks for graceful shutdown
_execution_tasks: set[asyncio.Task] = set()


def _parse_env_int(env_var: str, default: int) -> int:
    """Parse an integer from environment variable with fallback to default."""
    try:
        value = int(os.environ.get(env_var, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


# Configuration from environment
SCHEDULER_ENABLED = os.environ.get("AHS_SCHEDULER_ENABLED", "true").lower() == "true"
SCHEDULER_POLL_INTERVAL_SECONDS = _parse_env_int("AHS_SCHEDULER_POLL_INTERVAL", 10)
SCHEDULER_BATCH_SIZE = _parse_env_int("AHS_SCHEDULER_BATCH_SIZE", 10)
# Timeout for stale IN_PROGRESS schedules (if a schedule is IN_PROGRESS for longer than this, it's considered stale)
SCHEDULER_STALE_TIMEOUT_MINUTES = _parse_env_int("AHS_SCHEDULER_STALE_TIMEOUT_MINUTES", 30)


async def recover_stale_in_progress_schedules() -> int:
    """Recover schedules that are stuck in IN_PROGRESS status.

    If the scheduler crashes or restarts while a schedule is IN_PROGRESS,
    it will remain stuck. This function resets stale IN_PROGRESS schedules
    back to PENDING so they can be retried.

    A schedule is considered stale if:
    - status is IN_PROGRESS
    - modified_at is older than SCHEDULER_STALE_TIMEOUT_MINUTES

    Returns the number of recovered schedules.
    """
    from datetime import timedelta

    async with get_async_session() as session:
        stale_threshold = datetime.now(UTC) - timedelta(minutes=SCHEDULER_STALE_TIMEOUT_MINUTES)
        # Use FOR UPDATE SKIP LOCKED to prevent concurrent recovery by multiple workers
        result = await session.execute(
            select(AgentSchedule)
            .where(col(AgentSchedule.status) == AgentScheduleStatus.IN_PROGRESS)
            .where(col(AgentSchedule.modified_at) < stale_threshold)
            .where(col(AgentSchedule.deleted_at).is_(None))
            .with_for_update(skip_locked=True)
        )
        stale_schedules = list(result.scalars().all())

        if not stale_schedules:
            return 0

        recovery_error = f"Recovered from stale IN_PROGRESS (timeout: {SCHEDULER_STALE_TIMEOUT_MINUTES}m)"
        now = datetime.now(UTC)

        for schedule in stale_schedules:
            schedule.last_error = recovery_error

            # Also mark orphaned runs as FAILED and increment run_count
            orphaned_runs_result = await session.execute(
                select(AgentScheduleRun)
                .where(col(AgentScheduleRun.agent_schedule_id) == schedule.agent_schedule_id)
                .where(col(AgentScheduleRun.status) == AgentScheduleRunStatus.IN_PROGRESS)
            )
            orphaned_runs = list(orphaned_runs_result.scalars().all())
            for run in orphaned_runs:
                run.status = AgentScheduleRunStatus.FAILED
                run.completed_at = now
                run.error = recovery_error
                # Increment run_count for each orphaned run to keep denormalized counter accurate
                schedule.run_count += 1
                logger.warning(
                    "Marked orphaned run as FAILED",
                    agent_schedule_run_id=str(run.agent_schedule_run_id),
                    agent_schedule_id=str(schedule.agent_schedule_id),
                )

            # Check if max_runs reached after incrementing - set terminal status instead of requeueing
            if schedule.max_runs is not None and schedule.run_count >= schedule.max_runs:
                schedule.status = AgentScheduleStatus.FAILED
                logger.info(
                    "Stale schedule reached max_runs during recovery",
                    agent_schedule_id=str(schedule.agent_schedule_id),
                    run_count=schedule.run_count,
                    max_runs=schedule.max_runs,
                )
            else:
                schedule.status = AgentScheduleStatus.PENDING

            logger.warning(
                "Recovered stale IN_PROGRESS schedule",
                agent_schedule_id=str(schedule.agent_schedule_id),
                modified_at=schedule.modified_at.isoformat() if schedule.modified_at else None,
                orphaned_runs_count=len(orphaned_runs),
                final_status=schedule.status.value,
            )

        await session.commit()
        return len(stale_schedules)


async def get_due_agent_schedules() -> list[AgentSchedule]:
    """Get all agent schedules that are due for execution.

    Returns schedules where:
    - status is PENDING
    - next_run_at <= now
    - deleted_at is NULL

    Orders by next_run_at ASC and limits to batch size.
    """
    async with get_async_session() as session:
        now = datetime.now(UTC)
        result = await session.execute(
            select(AgentSchedule)
            .where(col(AgentSchedule.status) == AgentScheduleStatus.PENDING)
            .where(col(AgentSchedule.next_run_at) <= now)
            .where(col(AgentSchedule.deleted_at).is_(None))
            .order_by(col(AgentSchedule.next_run_at).asc())
            .limit(SCHEDULER_BATCH_SIZE)
        )
        return list(result.scalars().all())


async def claim_agent_schedule(schedule_id: uuid.UUID) -> tuple[AgentSchedule | None, AgentScheduleRun | None]:
    """Atomically claim an agent schedule and create a run record.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent multiple workers
    from executing the same schedule.

    Returns (schedule, run) tuple, or (None, None) if the schedule was already claimed.
    """
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentSchedule)
            .where(col(AgentSchedule.agent_schedule_id) == schedule_id)
            .where(col(AgentSchedule.status) == AgentScheduleStatus.PENDING)
            .with_for_update(skip_locked=True)
        )
        schedule: AgentSchedule | None = result.scalar_one_or_none()
        if not schedule:
            return None, None  # Already claimed or status changed

        # Derive run_number from MAX(run_number) + 1 to avoid duplicates after crash recovery
        # (run_count only increments on success/failure, but recovery resets to PENDING without incrementing)
        max_run_result = await session.execute(
            select(sa.func.coalesce(sa.func.max(AgentScheduleRun.run_number), 0)).where(
                col(AgentScheduleRun.agent_schedule_id) == schedule.agent_schedule_id
            )
        )
        max_run_number = max_run_result.scalar_one()
        run_number = max_run_number + 1

        # Create a run record
        run = AgentScheduleRun(
            agent_schedule_id=schedule.agent_schedule_id,
            run_number=run_number,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        session.add(run)

        # Update parent schedule status
        schedule.status = AgentScheduleStatus.IN_PROGRESS

        await session.commit()
        await session.refresh(schedule)
        await session.refresh(run)
        return schedule, run


async def get_agent_by_id(agent_id: uuid.UUID) -> Agent | None:
    """Look up an agent by ID."""
    async with get_async_session() as session:
        return await session.get(Agent, agent_id)


async def _update_run_completion(
    schedule: AgentSchedule,
    run: AgentScheduleRun,
    *,
    success: bool,
    session_id: str | None = None,
    error: str | None = None,
) -> None:
    """Update a run record and parent schedule after execution completes.

    Args:
        schedule: The parent schedule record (may be stale snapshot).
        run: The run record to update.
        success: Whether the execution succeeded.
        session_id: The session ID (required for success).
        error: The error message (required for failure).
    """
    async with get_async_session() as db_session:
        now = datetime.now(UTC)

        # Update the run record
        db_run = await db_session.get(AgentScheduleRun, run.agent_schedule_run_id)
        if db_run:
            db_run.status = AgentScheduleRunStatus.COMPLETED if success else AgentScheduleRunStatus.FAILED
            db_run.completed_at = now
            if success and session_id:
                db_run.session_id = uuid.UUID(session_id)
            elif not success and error:
                db_run.error = error

        # Update the parent schedule
        db_schedule = await db_session.get(AgentSchedule, schedule.agent_schedule_id)
        if not db_schedule:
            logger.warning(
                "Agent schedule not found for completion update",
                agent_schedule_id=str(schedule.agent_schedule_id),
                success=success,
            )
            await db_session.commit()
            return

        db_schedule.last_run_at = now
        db_schedule.run_count += 1
        if success:
            db_schedule.last_error = None
            if session_id:
                db_schedule.last_session_id = uuid.UUID(session_id)
        else:
            db_schedule.last_error = error

        # Determine terminal status based on schedule type
        terminal_status = AgentScheduleStatus.COMPLETED if success else AgentScheduleStatus.FAILED

        if db_schedule.schedule_type == AgentScheduleType.SCHEDULED:
            db_schedule.status = terminal_status
        else:  # RECURRING
            if db_schedule.max_runs is not None and db_schedule.run_count >= db_schedule.max_runs:
                db_schedule.status = terminal_status
                logger.info(
                    "Recurring schedule reached max_runs",
                    agent_schedule_id=str(db_schedule.agent_schedule_id),
                    run_count=db_schedule.run_count,
                    max_runs=db_schedule.max_runs,
                    final_status=terminal_status.value,
                )
            else:
                # Schedule next run
                db_schedule.next_run_at = compute_next_run_for_cron(
                    db_schedule.cron_expression,  # type: ignore[arg-type]
                    db_schedule.cron_timezone,
                )
                db_schedule.status = AgentScheduleStatus.PENDING

        await db_session.commit()


async def update_run_success(
    schedule: AgentSchedule,
    run: AgentScheduleRun,
    session_id: str,
) -> None:
    """Update a run record and parent schedule after successful execution."""
    await _update_run_completion(schedule, run, success=True, session_id=session_id)


async def update_run_failure(
    schedule: AgentSchedule,
    run: AgentScheduleRun,
    error: str,
) -> None:
    """Update a run record and parent schedule after failed execution."""
    await _update_run_completion(schedule, run, success=False, error=error)


async def execute_agent_schedule(schedule_id: uuid.UUID) -> None:
    """Execute an agent schedule.

    1. Atomically claims the schedule and creates a run record
    2. Loads the agent config
    3. Creates a session and sends message via create_session()
    4. Updates run and parent schedule status based on result

    Note: create_session() with message kicks off the agent runner in the background
    and returns after dispatching. The "success" status means the message was
    successfully dispatched, not that the agent task completed successfully.
    Agent task failures are handled by the runner and recorded in the session.
    """
    # Claim the schedule and create run record
    schedule, run = await claim_agent_schedule(schedule_id)
    if not schedule or not run:
        logger.debug(
            "Agent schedule already claimed or not pending",
            agent_schedule_id=str(schedule_id),
        )
        return

    logger.info(
        "Executing agent schedule",
        agent_schedule_id=str(schedule.agent_schedule_id),
        agent_schedule_run_id=str(run.agent_schedule_run_id),
        run_number=run.run_number,
        schedule_type=schedule.schedule_type.value,
        agent_id=str(schedule.agent_id),
    )

    try:
        # Get agent name
        agent = await get_agent_by_id(schedule.agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {schedule.agent_id}")

        # Create session with the schedule creator's user_id.
        # Permission check happens inside create_session using user_id.
        context = dict(schedule.context) if schedule.context else {}
        session_response = await create_session(
            SessionCreateRequest(
                agent_id=agent.name,
                trigger="cron",
                user_id=schedule.created_by_user,
                context=context,
                message=schedule.message,
                source="scheduler",
            )
        )

        # Update success state
        await update_run_success(schedule, run, session_response.session_id)

        logger.info(
            "Agent schedule executed successfully",
            agent_schedule_id=str(schedule.agent_schedule_id),
            agent_schedule_run_id=str(run.agent_schedule_run_id),
            session_id=session_response.session_id,
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(
            "Agent schedule execution failed",
            agent_schedule_id=str(schedule.agent_schedule_id),
            agent_schedule_run_id=str(run.agent_schedule_run_id),
            error=error_msg,
            exc_info=True,
        )
        try:
            await update_run_failure(schedule, run, error_msg)
        except Exception:
            # If we can't update the DB, log the error but don't crash.
            # The stale recovery mechanism will eventually reset this schedule.
            logger.error(
                "Failed to record schedule failure in DB",
                agent_schedule_id=str(schedule.agent_schedule_id),
                agent_schedule_run_id=str(run.agent_schedule_run_id),
                exc_info=True,
            )


def _task_done_callback(task: asyncio.Task) -> None:
    """Remove completed tasks from tracking set."""
    _execution_tasks.discard(task)


async def poll_and_execute_due_schedules() -> None:
    """Query for due schedules and spawn background tasks to execute them."""
    due_schedules = await get_due_agent_schedules()
    if not due_schedules:
        return

    logger.info("Found due agent schedules", count=len(due_schedules))

    for schedule in due_schedules:
        if not has_execution_capacity():
            logger.info(
                "Deferring remaining schedules to next poll",
                deferred_count=len(due_schedules) - due_schedules.index(schedule),
            )
            break
        task = create_background_task(execute_agent_schedule(schedule.agent_schedule_id))
        _execution_tasks.add(task)
        task.add_done_callback(_task_done_callback)


async def wait_for_in_flight_tasks(timeout_seconds: float = 30.0) -> int:
    """Wait for in-flight execution tasks to complete.

    Called during shutdown to allow graceful completion of running tasks.

    Args:
        timeout_seconds: Maximum time to wait for tasks to complete.

    Returns:
        Number of tasks that were still running (and got cancelled) after timeout.
    """
    if not _execution_tasks:
        return 0

    pending_count = len(_execution_tasks)
    logger.info("Waiting for in-flight execution tasks", count=pending_count, timeout_seconds=timeout_seconds)

    # Wait for tasks with timeout
    done, pending = await asyncio.wait(
        _execution_tasks,
        timeout=timeout_seconds,
        return_when=asyncio.ALL_COMPLETED,
    )

    if pending:
        logger.warning("Cancelling remaining execution tasks after timeout", count=len(pending))
        for task in pending:
            task.cancel()
        # Wait briefly for cancellations to complete
        await asyncio.wait(pending, timeout=2.0)

    return len(pending)


async def run_scheduler() -> None:
    """Background loop that polls for and executes due agent schedules and tasks.

    Runs indefinitely, polling every SCHEDULER_POLL_INTERVAL_SECONDS.
    Catches and logs exceptions to prevent the loop from dying.

    On startup and periodically, recovers stale IN_PROGRESS schedules and tasks
    that may have been orphaned due to crashes or restarts.
    """
    from ypl.agent_harness_service.task_executor import (
        TASK_EXECUTOR_ENABLED,
        poll_and_execute_ready_tasks,
        recover_stale_in_progress_tasks,
    )

    logger.info(
        "Scheduler started",
        poll_interval_seconds=SCHEDULER_POLL_INTERVAL_SECONDS,
        batch_size=SCHEDULER_BATCH_SIZE,
        stale_timeout_minutes=SCHEDULER_STALE_TIMEOUT_MINUTES,
        task_executor_enabled=TASK_EXECUTOR_ENABLED,
    )

    # Recover stale schedules on startup
    try:
        recovered = await recover_stale_in_progress_schedules()
        if recovered > 0:
            logger.info("Recovered stale IN_PROGRESS schedules on startup", count=recovered)
    except Exception as e:
        logger.error("Failed to recover stale schedules on startup", error=str(e), exc_info=True)

    # Recover stale tasks on startup
    if TASK_EXECUTOR_ENABLED:
        try:
            recovered = await recover_stale_in_progress_tasks()
            if recovered > 0:
                logger.info("Recovered stale IN_PROGRESS tasks on startup", count=recovered)
        except Exception as e:
            logger.error("Failed to recover stale tasks on startup", error=str(e), exc_info=True)

    poll_count = 0
    while True:
        try:
            await poll_and_execute_due_schedules()

            # Also poll for ready tasks
            if TASK_EXECUTOR_ENABLED:
                try:
                    await poll_and_execute_ready_tasks()
                except Exception as e:
                    logger.error("Task executor poll error", error=str(e), exc_info=True)

            # Periodically check for stale schedules/tasks (every ~10 minutes at default poll interval)
            poll_count += 1
            if poll_count >= 60:  # 60 polls * 10s = ~10 minutes
                poll_count = 0
                try:
                    recovered = await recover_stale_in_progress_schedules()
                    if recovered > 0:
                        logger.info("Recovered stale IN_PROGRESS schedules", count=recovered)
                except Exception as e:
                    logger.error("Failed to recover stale schedules", error=str(e), exc_info=True)

                if TASK_EXECUTOR_ENABLED:
                    try:
                        recovered = await recover_stale_in_progress_tasks()
                        if recovered > 0:
                            logger.info("Recovered stale IN_PROGRESS tasks", count=recovered)
                    except Exception as e:
                        logger.error("Failed to recover stale tasks", error=str(e), exc_info=True)

        except Exception as e:
            logger.error("Scheduler poll error", error=str(e), exc_info=True)

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL_SECONDS)
