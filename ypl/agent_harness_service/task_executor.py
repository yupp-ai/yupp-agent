"""Task executor for Agent Harness Service.

This module provides functions for executing agent project tasks:
1. Recover stale IN_PROGRESS tasks on startup
2. Resolve task dependencies (PENDING → READY)
3. Claim and execute ready tasks
4. Update task completion status

The task executor is integrated into the scheduler polling loop and shares
its infrastructure for atomic claiming and graceful shutdown.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.projects.task_utils import are_dependencies_completed, complete_task
from ypl.agent_harness_service.service import create_session, has_execution_capacity
from ypl.backend.config import settings
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.redis_utils import RedisTokenBucketRateLimiter
from ypl.db.agent_harness import (
    Agent,
    AgentProject,
    AgentProjectStatus,
    AgentTask,
    AgentTaskStatus,
)
from ypl.structured_logger import get_logger


# Lit console base URL for session links in Slack notifications
def _get_lit_session_url(session_id: str) -> str:
    if settings.ENVIRONMENT == "production":
        base = "https://agent-streamlit-server-production-451082535721.us-east4.run.app"
    else:
        base = "https://agent-streamlit-server-staging-451082535721.us-east4.run.app"
    return f"{base}/agent_harness_console?session_id={session_id}"

logger = get_logger()

# Error subtypes that indicate the task can be resumed (session state is preserved).
# Note: stopped_context_overflow is NOT resumable — resuming adds more context, causing immediate re-overflow.
RESUMABLE_ERROR_SUBTYPES: frozenset[str] = frozenset(
    {
        "error_max_turns",  # Hit turn limit (both Claude CLI and raw executor use this)
        # TODO: Add budget limit subtype when implemented
    }
)

# Track in-flight task execution tasks for graceful shutdown
_task_execution_tasks: set[asyncio.Task] = set()

# Per-project in-flight count: incremented on claim, decremented on completion.
_project_in_flight: dict[uuid.UUID, int] = {}


def _parse_env_int(env_var: str, default: int) -> int:
    """Parse an integer from environment variable with fallback to default."""
    try:
        value = int(os.environ.get(env_var, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


# Configuration from environment
TASK_EXECUTOR_ENABLED = os.environ.get("AHS_TASK_EXECUTOR_ENABLED", "false").lower() == "true"
TASK_EXECUTOR_BATCH_SIZE = _parse_env_int("AHS_TASK_EXECUTOR_BATCH_SIZE", 1)
TASK_EXECUTOR_STALE_TIMEOUT_MINUTES = _parse_env_int("AHS_TASK_EXECUTOR_STALE_TIMEOUT_MINUTES", 30)
TASK_EXECUTOR_RATE_LIMIT = _parse_env_int("AHS_TASK_EXECUTOR_RATE_LIMIT", 30)
MAX_CONCURRENT_TASKS_PER_PROJECT = _parse_env_int("AHS_MAX_CONCURRENT_TASKS_PER_PROJECT", 3)

# Global rate limiter for all task execution (lazily initialized)
_rate_limiter: RedisTokenBucketRateLimiter | None = None


def _get_rate_limiter() -> RedisTokenBucketRateLimiter:
    """Get the global rate limiter for task execution."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RedisTokenBucketRateLimiter(
            limit=TASK_EXECUTOR_RATE_LIMIT,
            interval_seconds=3600,  # 1 hour
            redis_key_prefix="ahs:task_executor:rate_limit",
            burst_ratio=1.0,  # Strict limit, no burst allowance
        )
    return _rate_limiter


def _increment_project_in_flight(project_id: uuid.UUID) -> None:
    _project_in_flight[project_id] = _project_in_flight.get(project_id, 0) + 1


def _decrement_project_in_flight(project_id: uuid.UUID) -> None:
    count = _project_in_flight.get(project_id, 0) - 1
    if count <= 0:
        _project_in_flight.pop(project_id, None)
    else:
        _project_in_flight[project_id] = count


def has_project_capacity(project_id: uuid.UUID) -> bool:
    """Check if a project has capacity to start another task."""
    current = _project_in_flight.get(project_id, 0)
    if current < MAX_CONCURRENT_TASKS_PER_PROJECT:
        return True
    logger.info(
        "Project at task capacity, deferring",
        agent_project_id=str(project_id),
        active_count=current,
        max_per_project=MAX_CONCURRENT_TASKS_PER_PROJECT,
    )
    return False


async def recover_stale_in_progress_tasks() -> int:
    """Recover tasks that are stuck in IN_PROGRESS status.

    If the executor crashes or restarts while a task is IN_PROGRESS,
    it will remain stuck. This function resets stale IN_PROGRESS tasks
    back to READY so they can be retried.

    A task is considered stale if:
    - status is IN_PROGRESS
    - modified_at is older than TASK_EXECUTOR_STALE_TIMEOUT_MINUTES

    Returns the number of recovered tasks.
    """
    async with get_async_session() as session:
        stale_threshold = datetime.now(UTC) - timedelta(minutes=TASK_EXECUTOR_STALE_TIMEOUT_MINUTES)
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.status) == AgentTaskStatus.IN_PROGRESS)
            .where(col(AgentTask.modified_at) < stale_threshold)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update(skip_locked=True)
        )
        stale_tasks = list(result.scalars().all())

        if not stale_tasks:
            return 0

        # TODO: Add max retry limit to prevent infinite retry loops for consistently
        # failing tasks. Use len(task.assigned_session_ids) as retry counter and
        # mark as FAILED after MAX_TASK_RETRIES exceeded (see PR #10897).
        # TODO: Verify session is no longer active before requeueing to prevent
        # duplicate execution of long-running tasks. Could add heartbeat that
        # advances modified_at or check active sessions list (see PR #10897).
        for task in stale_tasks:
            task.status = AgentTaskStatus.READY
            logger.warning(
                "Recovered stale IN_PROGRESS task",
                agent_task_id=str(task.agent_task_id),
                agent_project_id=str(task.agent_project_id),
                modified_at=task.modified_at.isoformat() if task.modified_at else None,
            )

        await session.commit()
        return len(stale_tasks)


async def resolve_task_dependencies() -> int:
    """Transition PENDING/BLOCKED tasks to READY based on dependency status.

    A task transitions to:
    - READY: when all dependencies exist and have status=COMPLETED
    - (stays BLOCKED): when any dependency has status=FAILED or CANCELLED — left for human action

    Note: Tasks with dependencies are created with BLOCKED status (see project_tasks.py),
    while tasks without dependencies start as PENDING or READY.

    Returns the number of tasks transitioned.
    """
    async with get_async_session() as session:
        # Find PENDING or BLOCKED tasks (tasks with deps are created as BLOCKED)
        # Use skip_locked to avoid contention with concurrent schedulers
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.status).in_([AgentTaskStatus.PENDING, AgentTaskStatus.BLOCKED]))
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update(skip_locked=True)
        )
        pending_tasks = list(result.scalars().all())

        if not pending_tasks:
            return 0

        transitioned_count = 0
        for task in pending_tasks:
            # Check for failed dependencies — leave BLOCKED tasks as-is for human action
            has_failed_dep = await _has_failed_dependency(session, task)
            if has_failed_dep:
                # Log at debug level — tasks with deps are created as BLOCKED, so this state
                # is already known and expected. Logging at WARNING on every poll cycle (every
                # ~12s) generates tens of thousands of spurious warnings for tasks that are
                # legitimately waiting for human intervention.
                logger.debug(
                    "Task has failed dependency, leaving BLOCKED for human action",
                    agent_task_id=str(task.agent_task_id),
                    agent_project_id=str(task.agent_project_id),
                )
            elif await are_dependencies_completed(session, task):
                task.status = AgentTaskStatus.READY
                transitioned_count += 1
                logger.info(
                    "Task dependencies satisfied, marking READY",
                    agent_task_id=str(task.agent_task_id),
                    agent_project_id=str(task.agent_project_id),
                )

        if transitioned_count > 0:
            await session.commit()

        return transitioned_count


async def _has_failed_dependency(session: AsyncSession, task: AgentTask) -> bool:
    """Check if any dependency of a task has failed or been cancelled."""
    depends_on = task.depends_on
    if not depends_on:
        return False

    dep_ids = [uuid.UUID(dep_id) for dep_id in depends_on]
    result = await session.execute(
        select(sa.func.count())
        .select_from(AgentTask)
        .where(col(AgentTask.agent_task_id).in_(dep_ids))
        .where(col(AgentTask.status).in_([AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED]))
        .where(col(AgentTask.deleted_at).is_(None))
    )
    failed_count: Any = result.scalar_one()
    return bool(failed_count > 0)


def _task_pickup_project_condition() -> sa.ColumnElement[bool]:
    """Return the SQLAlchemy condition for project statuses that allow task pickup.

    A task may be picked up when the project is ACTIVE, or when the project is
    PAUSED and the task has the ``forced_pickup`` flag set in its task_data.
    Extracting this into a helper keeps ``get_ready_tasks`` and ``claim_task``
    in sync — both must use identical project-status logic so the executor
    never misses (or double-picks) a task.
    """
    return sa.or_(
        col(AgentProject.status) == AgentProjectStatus.ACTIVE,
        sa.and_(
            col(AgentProject.status) == AgentProjectStatus.PAUSED,
            col(AgentTask.task_data)["forced_pickup"].as_boolean().is_(True),
        ),
    )


async def get_ready_tasks(batch_size: int | None = None) -> list[AgentTask]:
    """Get tasks that are ready for execution.

    Returns tasks where:
    - status is READY
    - deleted_at is NULL
    - project status is ACTIVE OR task has forced_pickup flag

    The forced_pickup flag allows manually triggering tasks even when the
    project is paused. This is useful for selectively executing specific
    tasks without activating the entire project.

    Orders by priority (urgent first) then created_at ASC.

    Args:
        batch_size: Maximum number of tasks to return. Defaults to TASK_EXECUTOR_BATCH_SIZE.
    """
    if batch_size is None:
        batch_size = TASK_EXECUTOR_BATCH_SIZE

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTask)
            .join(AgentProject, col(AgentTask.agent_project_id) == col(AgentProject.agent_project_id))
            .where(col(AgentTask.status) == AgentTaskStatus.READY)
            .where(col(AgentTask.deleted_at).is_(None))
            .where(col(AgentProject.deleted_at).is_(None))
            .where(_task_pickup_project_condition())
            .order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
            .limit(batch_size)
        )
        return list(result.scalars().all())


async def claim_task(task_id: uuid.UUID) -> AgentTask | None:
    """Atomically claim a task for execution.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent multiple workers
    from executing the same task. Re-validates project status at claim time
    to handle race conditions between polling and claiming.

    Also allows claiming tasks with forced_pickup flag from PAUSED projects.
    The flag is cleared after successful claim to prevent re-execution if
    the task fails and is retried.

    Returns the task if successfully claimed, None otherwise.
    """
    async with get_async_session() as session:
        # Join with project to re-validate project state at claim time
        # This closes the race between get_ready_tasks() and claim_task()
        result = await session.execute(
            select(AgentTask)
            .join(AgentProject, col(AgentTask.agent_project_id) == col(AgentProject.agent_project_id))
            .where(col(AgentTask.agent_task_id) == task_id)
            .where(col(AgentTask.status) == AgentTaskStatus.READY)
            .where(col(AgentTask.deleted_at).is_(None))
            .where(col(AgentProject.deleted_at).is_(None))
            .where(_task_pickup_project_condition())
            .with_for_update(skip_locked=True)
        )
        task: AgentTask | None = result.scalar_one_or_none()
        if not task:
            return None  # Already claimed, status changed, or project no longer active

        task.status = AgentTaskStatus.IN_PROGRESS

        # Clear forced_pickup flag after claiming to prevent re-execution
        # if the task fails and is retried (user must explicitly re-trigger)
        if task.task_data and task.task_data.get("forced_pickup"):
            updated_data = dict(task.task_data)
            del updated_data["forced_pickup"]
            task.task_data = updated_data or None

        await session.commit()
        await session.refresh(task)
        return task


async def get_project(project_id: uuid.UUID) -> AgentProject | None:
    """Get a project by ID."""
    async with get_async_session() as session:
        return await session.get(AgentProject, project_id)


async def get_agent_by_id(agent_id: uuid.UUID) -> Agent | None:
    """Get an agent by ID."""
    async with get_async_session() as session:
        return await session.get(Agent, agent_id)


async def _ensure_updates_thread(project: AgentProject, agent_name: str) -> str | None:
    """Ensure the project has a Slack updates thread, creating one if needed.

    Reads ``updates_thread_ts`` from ``project.shared_state``.  If missing,
    acquires a row-level lock on the project, re-checks under the lock, posts
    a header message to ``project.slack_channel``, and persists the returned
    timestamp — all within the same transaction to prevent duplicate threads
    from concurrent task starts.

    Returns the thread_ts, or None if Slack is not configured or posting fails.
    All errors are caught and logged — this is best-effort.
    """
    slack_channel = project.slack_channel
    if not slack_channel:
        return None

    # Fast path: thread already exists in the snapshot we loaded.
    existing_ts: str | None = (project.shared_state or {}).get("updates_thread_ts")
    if existing_ts:
        return existing_ts

    try:
        from ypl.agent_harness_service.gateway import GatewayRegistry

        gateway = GatewayRegistry.get_instance().get("slack")
        if not gateway:
            logger.warning(
                "Slack gateway not available for updates thread init",
                agent_project_id=str(project.agent_project_id),
            )
            return None

        # Acquire a row lock BEFORE posting to Slack to prevent two workers
        # from both posting a header message and creating duplicate threads.
        # FOR UPDATE locks only this single project row — other projects are
        # unaffected. The Slack HTTP call is made while holding the lock, which
        # is acceptable because it is typically fast and the lock scope is narrow.
        async with get_async_session() as session:
            db_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == project.agent_project_id)
                .with_for_update()
            )
            db_project = db_result.scalar_one_or_none()
            if db_project is None:
                return None  # Project deleted between checks

            # Re-check under lock — another worker may have won the race already.
            current_state = dict(db_project.shared_state or {})
            existing = current_state.get("updates_thread_ts")
            if existing:
                return str(existing)

            # Read channel/name from the locked row to avoid stale snapshot data.
            locked_channel = db_project.slack_channel
            if not locked_channel:
                return None

            # Still missing: post the header message (lock is held during this call).
            result = await gateway.send_message(
                agent_name=agent_name,
                destination=locked_channel,
                text=f"📋 *{db_project.name}* — Project updates",
            )
            if not result.success or not result.message_id:
                logger.warning(
                    "Failed to post updates thread header",
                    agent_project_id=str(project.agent_project_id),
                    error=result.error,
                )
                return None

            thread_ts = result.message_id
            current_state["updates_thread_ts"] = thread_ts
            db_project.shared_state = current_state
            await session.commit()

    except Exception:
        logger.error(
            "Unexpected error creating updates thread",
            agent_project_id=str(project.agent_project_id),
            exc_info=True,
        )
        return None

    logger.info(
        "Created project updates thread",
        agent_project_id=str(project.agent_project_id),
        slack_channel=slack_channel,
        thread_ts=thread_ts,
    )
    return thread_ts


def _get_creator_mention(shared_state: dict[str, Any] | None) -> str:
    """Return a Slack @mention string for the project creator, or empty string."""
    creator_id = (shared_state or {}).get("creator_slack_user_id")
    if creator_id:
        return f" · <@{creator_id}>"
    return ""


async def _post_project_slack_update(
    project_id: uuid.UUID,
    agent_name: str,
    text: str,
    updates_thread_ts: str | None = None,
) -> None:
    """Post a best-effort lifecycle message to the project's Slack updates thread.

    Loads the project's ``updates_thread_ts`` from shared_state if not supplied.
    Silently swallows all errors so Slack issues never affect task execution.
    """
    try:
        from ypl.agent_harness_service.gateway import GatewayRegistry

        gateway = GatewayRegistry.get_instance().get("slack")
        if not gateway:
            return

        thread_ts = updates_thread_ts
        slack_channel: str | None = None

        async with get_async_session() as session:
            project = await session.get(AgentProject, project_id)
            if not project or not project.slack_channel:
                return
            slack_channel = project.slack_channel
            if not thread_ts:
                thread_ts = (project.shared_state or {}).get("updates_thread_ts")

        if not slack_channel or not thread_ts:
            return

        await gateway.send_message(
            agent_name=agent_name,
            destination=slack_channel,
            text=text,
            thread_id=thread_ts,
        )
    except Exception:
        logger.error(
            "Failed to post project Slack update (non-fatal)",
            agent_project_id=str(project_id),
            exc_info=True,
        )


async def add_session_to_task(task_id: uuid.UUID, session_id: str) -> None:
    """Record a session ID as having worked on a task."""
    async with get_async_session() as session:
        task = await session.get(AgentTask, task_id)
        if task:
            if task.assigned_session_ids is None:
                task.assigned_session_ids = []
            task.assigned_session_ids = [*task.assigned_session_ids, session_id]
            await session.commit()


async def execute_task(task_id: uuid.UUID) -> None:
    """Execute a task by creating an agent session.

    1. Claims the task atomically
    2. Loads project for default agent and shared state
    3. Creates session with task context
    4. Records session assignment on the task

    The session completion will be handled by hooks in service.py
    that call update_task_completion().
    """
    # Claim the task
    task = await claim_task(task_id)
    if not task:
        logger.debug("Task already claimed or not ready", agent_task_id=str(task_id))
        return

    _increment_project_in_flight(task.agent_project_id)

    # Check if this is a resumption (resume_session flag set by resume_task())
    task_data = dict(task.task_data or {})
    resume_session = task_data.pop("resume_session", False)
    session_to_resume: str | None = None
    if resume_session and task.assigned_session_ids:
        session_to_resume = task.assigned_session_ids[-1]

    logger.info(
        "Executing task",
        agent_task_id=str(task.agent_task_id),
        agent_project_id=str(task.agent_project_id),
        title=task.title,
        resuming_session=session_to_resume,
    )

    try:
        # Load project
        project = await get_project(task.agent_project_id)
        if not project:
            raise ValueError(f"Project not found: {task.agent_project_id}")

        # Determine agent (task-specific or project default)
        agent_id = task.agent_id or project.default_agent_id
        if not agent_id:
            raise ValueError(f"No agent configured for task {task.agent_task_id} or project {project.agent_project_id}")

        agent = await get_agent_by_id(agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {agent_id}")

        # Auto-init the project's Slack updates thread if not yet created.
        # This is best-effort; a None result just means Slack won't be used.
        updates_thread_ts = await _ensure_updates_thread(project, agent.name)

        # Build shared_state with updates_thread_ts pre-populated so the agent
        # doesn't need a separate get_project_state call to find it.
        shared_state = dict(project.shared_state or {})
        if updates_thread_ts:
            shared_state["updates_thread_ts"] = updates_thread_ts

        # Build context with project/task info
        # Note: task_data is nested to prevent shadowing project_id/task_id/shared_state
        context: dict = {
            "project_id": str(task.agent_project_id),
            "task_id": str(task.agent_task_id),
            "shared_state": shared_state,
            "task_data": task_data,  # Use the cleaned task_data (without resume_session)
            "slack_channel": project.slack_channel or "",
            "project_name": project.name,
        }

        # Build the message: use "continue" for resumption, full description for new tasks
        if session_to_resume:
            message = "Please continue with the task."
        else:
            message = task.description or task.title

        # Create the session (with optional resume)
        session_response = await create_session(
            SessionCreateRequest(
                agent_id=agent.name,
                trigger="task",
                user_id=project.creator_user_id,
                context=context,
                message=message,
                source="task_executor",
                session_id=session_to_resume,  # Pass session ID for resumption
            )
        )

        # Track session assignment on the task (skip if resuming same session to avoid duplicates)
        if session_response.session_id not in (task.assigned_session_ids or []):
            await add_session_to_task(task.agent_task_id, session_response.session_id)

        # Post "started" notice to the project's updates thread (best-effort).
        session_id_str = session_response.session_id
        short_id = session_id_str[:8]
        lit_url = _get_lit_session_url(session_id_str)
        creator_mention = _get_creator_mention(shared_state)
        await _post_project_slack_update(
            task.agent_project_id,
            agent.name,
            f"🔄 *{task.title}* started{creator_mention}\n📎 Session: <{lit_url}|{short_id}>",
            updates_thread_ts=updates_thread_ts,
        )

        # Clear resume flags from task_data now that session creation succeeded.
        # This is done AFTER success so setup failures don't lose resumability.
        if resume_session:
            async with get_async_session() as db_session:
                db_task = await db_session.get(AgentTask, task_id)
                if db_task and db_task.task_data:
                    updated_data = dict(db_task.task_data)
                    updated_data.pop("resume_session", None)
                    updated_data.pop("error_subtype", None)
                    db_task.task_data = updated_data or None
                    await db_session.commit()

        logger.info(
            "Task session created",
            agent_task_id=str(task.agent_task_id),
            session_id=session_response.session_id,
        )

    except Exception as e:
        # Setup failed before session was created — decrement here since
        # update_task_completion won't be called by the session completion hook.
        _decrement_project_in_flight(task.agent_project_id)
        error_msg = str(e)
        logger.error(
            "Task execution failed",
            agent_task_id=str(task.agent_task_id),
            error=error_msg,
            exc_info=True,
        )
        # Mark task as failed
        try:
            await update_task_completion(
                task_id=task.agent_task_id,
                session_id=None,
                success=False,
                result=None,
                error=error_msg,
            )
        except Exception:
            logger.error(
                "Failed to record task failure in DB",
                agent_task_id=str(task.agent_task_id),
                exc_info=True,
            )


async def update_task_completion(
    task_id: uuid.UUID,
    session_id: str | None,
    success: bool,
    result: dict | None,
    error: str | None,
    error_subtype: str | None = None,
) -> None:
    """Update task status after session completion.

    Args:
        task_id: The task being completed
        session_id: The session that executed the task (optional for errors before session creation)
        success: Whether the task succeeded
        result: Structured result data (for success)
        error: Error message (for failure)
        error_subtype: The error subtype (e.g., "error_max_turns") for resumable failures
    """
    async with get_async_session() as session:
        # Use FOR UPDATE to prevent race with recover_stale_in_progress_tasks
        # which could reset the task to READY between our read and write
        db_result = await session.execute(
            select(AgentTask).where(col(AgentTask.agent_task_id) == task_id).with_for_update()
        )
        task = db_result.scalar_one_or_none()
        if not task:
            logger.warning("Task not found for completion update", agent_task_id=str(task_id))
            return

        # Guard against status regression: only update if task is still IN_PROGRESS.
        # This prevents races where stale recovery has already reset the task to READY
        # and a new execution has claimed it, or where the task was manually cancelled.
        if task.status != AgentTaskStatus.IN_PROGRESS:
            logger.warning(
                "Task status changed since execution started, skipping completion update",
                agent_task_id=str(task_id),
                current_status=task.status.value,
                session_id=session_id,
            )
            return

        target_status = AgentTaskStatus.COMPLETED if success else AgentTaskStatus.FAILED
        task_result = result or ({"error": error} if error else None)

        # Manage error_subtype in task_data:
        # - Clear on success (task completed, no error to resume from)
        # - Store only if it's a resumable subtype (otherwise no point keeping it)
        task_data = dict(task.task_data or {})
        if success:
            task_data.pop("error_subtype", None)
        elif error_subtype and error_subtype in RESUMABLE_ERROR_SUBTYPES:
            task_data["error_subtype"] = error_subtype
        else:
            task_data.pop("error_subtype", None)  # Clear stale/non-resumable subtype
        task.task_data = task_data or None

        await complete_task(session, task, target_status, result=task_result)

        await session.commit()

        # Decrement per-project in-flight counter now that the session has finished.
        # (For setup failures, the decrement happens in execute_task's except block.)
        _decrement_project_in_flight(task.agent_project_id)

        # Capture fields needed for Slack notification before the session expires.
        _slack_project_id = task.agent_project_id
        _slack_agent_id = task.agent_id
        _slack_title = task.title
        _slack_summary: str | None = (task.result or {}).get("summary") if success else None

        logger.info(
            "Task completion updated",
            agent_task_id=str(task_id),
            agent_project_id=str(task.agent_project_id),
            success=success,
            session_id=session_id,
        )

    # Post completion/failure notice to the project's Slack updates thread (best-effort).
    try:
        _notify_project = await get_project(_slack_project_id)
        _notify_agent = await get_agent_by_id(_slack_agent_id) if _slack_agent_id else None
        if _notify_agent is None and _notify_project and _notify_project.default_agent_id:
            _notify_agent = await get_agent_by_id(_notify_project.default_agent_id)
        if _notify_agent:
            creator_mention = _get_creator_mention(_notify_project.shared_state if _notify_project else None)
            short_sid = session_id[:8] if session_id else ""
            session_suffix = (
                f"\n📎 Session: <{_get_lit_session_url(session_id)}|{short_sid}>" if session_id else ""
            )
            if success:
                summary_line = f"\n{_slack_summary}" if _slack_summary else ""
                notice = f"✅ *{_slack_title}*{creator_mention}{summary_line}{session_suffix}"
            else:
                error_line = f"\n{error}" if error else ""
                notice = f"❌ *{_slack_title}*{creator_mention}{error_line}{session_suffix}"
            await _post_project_slack_update(_slack_project_id, _notify_agent.name, notice)
    except Exception:
        logger.error(
            "Failed to post task completion Slack notice (non-fatal)",
            agent_task_id=str(task_id),
            exc_info=True,
        )

    # Trigger dependency resolution for downstream tasks
    # (this runs outside the transaction to avoid holding locks)
    if success:
        try:
            resolved = await resolve_task_dependencies()
            if resolved > 0:
                logger.info(
                    "Resolved task dependencies after completion", count=resolved, completed_task_id=str(task_id)
                )
        except Exception:
            logger.error(
                "Failed to resolve dependencies after task completion", agent_task_id=str(task_id), exc_info=True
            )


async def resume_task(task_id: uuid.UUID) -> dict[str, Any]:
    """Resume a failed task by resetting to READY and marking for session resumption.

    The task must:
    - Be in FAILED status
    - Have a resumable error_subtype (currently only "error_max_turns")
    - Have at least one previous session to resume

    Returns a dict with success status and details.
    """
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_id)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = result.scalar_one_or_none()

        if not task:
            return {"success": False, "error": "Task not found", "error_code": "NOT_FOUND"}

        if task.status != AgentTaskStatus.FAILED:
            return {
                "success": False,
                "error": f"Task is not in FAILED status (current: {task.status.value})",
                "error_code": "INVALID_STATUS",
            }

        # Check if error is resumable
        task_data = dict(task.task_data or {})
        error_subtype = task_data.get("error_subtype")
        if error_subtype not in RESUMABLE_ERROR_SUBTYPES:
            return {
                "success": False,
                "error": f"Task error is not resumable (subtype: {error_subtype})",
                "error_code": "NOT_RESUMABLE",
            }

        if not task.assigned_session_ids:
            return {
                "success": False,
                "error": "No previous session to resume",
                "error_code": "NO_SESSION",
            }

        # Mark for resumption: set status to READY and add resume_session flag.
        # Keep error_subtype until session creation succeeds (cleared in execute_task)
        # so setup failures don't lose resumability.
        session_to_resume = task.assigned_session_ids[-1]
        task.status = AgentTaskStatus.READY
        task.completed_at = None  # Clear terminal timestamp when reopening
        task_data["resume_session"] = True
        task.task_data = task_data

        await session.commit()

        logger.info(
            "Task queued for resumption",
            agent_task_id=str(task_id),
            session_to_resume=session_to_resume,
            error_subtype=error_subtype,
        )

        return {
            "success": True,
            "task_id": str(task_id),
            "session_to_resume": session_to_resume,
            "previous_error_subtype": error_subtype,
        }


def _task_done_callback(task: asyncio.Task) -> None:
    """Remove completed tasks from tracking set."""
    _task_execution_tasks.discard(task)


async def poll_and_execute_ready_tasks() -> None:
    """Query for ready tasks and spawn background tasks to execute them.

    This is called from the scheduler polling loop.
    """
    # First, resolve any PENDING/BLOCKED → READY transitions
    try:
        resolved = await resolve_task_dependencies()
        if resolved > 0:
            logger.info("Resolved task dependencies", count=resolved)
    except Exception as e:
        logger.error("Failed to resolve task dependencies", error=str(e), exc_info=True)

    # Then get and execute ready tasks
    # Over-fetch to handle rate-limited projects without starving other projects
    ready_tasks = await get_ready_tasks(batch_size=TASK_EXECUTOR_BATCH_SIZE * 3)
    if not ready_tasks:
        return

    logger.info("Found ready tasks", count=len(ready_tasks))

    # Rate limiter applies per-project (each project has its own bucket)
    rate_limiter = _get_rate_limiter()

    # TODO: Rate limit check happens before claim_task(). In multi-worker deployments,
    # another worker may claim the task between rate check and claim, wasting a token.
    # Consider moving rate check inside execute_task() after successful claim, though
    # that requires releasing the task if rate-limited. (see PR #10907)
    scheduled_count = 0
    for task in ready_tasks:
        # Check rate limit per project (project_id is the bucket identifier)
        project_id_str = str(task.agent_project_id)
        if not await rate_limiter.is_allowed(project_id_str):
            logger.warning(
                "Task execution rate limited",
                agent_task_id=str(task.agent_task_id),
                agent_project_id=project_id_str,
                tasks_per_hour=TASK_EXECUTOR_RATE_LIMIT,
            )
            # Continue to next task - other projects may not be rate limited
            continue

        # Check per-project parallelism limit
        if not has_project_capacity(task.agent_project_id):
            # Continue to next task - other projects may have capacity
            continue

        # Check global execution capacity before spawning
        if not has_execution_capacity():
            logger.info(
                "Deferring remaining tasks to next poll",
                deferred_count=len(ready_tasks) - ready_tasks.index(task),
            )
            break

        bg_task = create_background_task(execute_task(task.agent_task_id))
        _task_execution_tasks.add(bg_task)
        bg_task.add_done_callback(_task_done_callback)
        scheduled_count += 1

        # Stop once we've scheduled enough tasks
        if scheduled_count >= TASK_EXECUTOR_BATCH_SIZE:
            break


async def wait_for_in_flight_task_executions(timeout_seconds: float = 30.0) -> int:
    """Wait for in-flight task setup operations to complete.

    Called during shutdown. Note: this waits for execute_task() coroutines,
    which handle task claiming and session creation. The actual agent work
    runs in separate background tasks managed by service.py (wait_for_in_flight_tasks).

    Args:
        timeout_seconds: Maximum time to wait for setup tasks to complete.

    Returns:
        Number of setup tasks that were still running (and got cancelled) after timeout.
    """
    if not _task_execution_tasks:
        return 0

    pending_count = len(_task_execution_tasks)
    logger.info("Waiting for in-flight task executions", count=pending_count, timeout_seconds=timeout_seconds)

    # Wait for tasks with timeout
    done, pending = await asyncio.wait(
        _task_execution_tasks,
        timeout=timeout_seconds,
        return_when=asyncio.ALL_COMPLETED,
    )

    if pending:
        logger.warning("Cancelling remaining task executions after timeout", count=len(pending))
        for task in pending:
            task.cancel()
        # Wait briefly for cancellations to complete
        await asyncio.wait(pending, timeout=2.0)

    return len(pending)
