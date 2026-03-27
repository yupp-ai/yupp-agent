"""Service layer for Agent Project & Task REST API.

Contains the business logic for project/task CRUD, status transitions,
and dependency management. Routes call these functions directly.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select

from ypl.agent_harness_service.projects.project_types import (
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectResponse,
    ProjectStatusRequest,
    ProjectUpdateRequest,
    TaskDependenciesRequest,
    TaskListResponse,
    TaskResponse,
    TaskResumeResponse,
    TaskStatusRequest,
    TaskStatusResponse,
    TaskSummary,
    TaskUpdateRequest,
)
from ypl.agent_harness_service.projects.task_utils import (
    TERMINAL_TASK_STATUSES,
    complete_task,
    validate_task_status_change,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.db.agent_harness import (
    Agent,
    AgentProject,
    AgentProjectStatus,
    AgentTask,
    AgentTaskPriority,
    AgentTaskStatus,
)
from ypl.db.users import User
from ypl.structured_logger import get_logger

logger = get_logger()


# ============================================================================
# Helpers
# ============================================================================


async def _get_task_summary(session: Any, project_id: uuid.UUID) -> TaskSummary:
    """Get task counts by status for a project."""
    result = await session.execute(
        select(AgentTask.status, sa.func.count())
        .where(col(AgentTask.agent_project_id) == project_id)
        .where(col(AgentTask.deleted_at).is_(None))
        .group_by(AgentTask.status)
    )
    summary_dict: dict[str, int] = {s.value: 0 for s in AgentTaskStatus}
    total = 0
    for status_val, count in result.all():
        summary_dict[status_val.value] = count
        total += count
    summary_dict["total"] = total
    return TaskSummary(**summary_dict)


async def _get_task_summaries_batch(session: Any, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, TaskSummary]:
    """Get task counts by status for multiple projects in a single query."""
    if not project_ids:
        return {}

    result = await session.execute(
        select(AgentTask.agent_project_id, AgentTask.status, sa.func.count())
        .where(col(AgentTask.agent_project_id).in_(project_ids))
        .where(col(AgentTask.deleted_at).is_(None))
        .group_by(col(AgentTask.agent_project_id), AgentTask.status)
    )

    summaries: dict[uuid.UUID, dict[str, int]] = {pid: {s.value: 0 for s in AgentTaskStatus} for pid in project_ids}
    totals: dict[uuid.UUID, int] = dict.fromkeys(project_ids, 0)

    for proj_id, status_val, count in result.all():
        summaries[proj_id][status_val.value] = count
        totals[proj_id] += count

    return {pid: TaskSummary(**{**summaries[pid], "total": totals[pid]}) for pid in project_ids}


def _format_project(
    project: AgentProject,
    user_name: str | None,
    task_summary: TaskSummary,
    *,
    include_shared_state: bool = False,
) -> ProjectDetailResponse | ProjectResponse:
    """Format project into response model."""
    base = {
        "agent_project_id": str(project.agent_project_id),
        "name": project.name,
        "description": project.description,
        "status": project.status.value,
        "creator_user_id": project.creator_user_id,
        "creator_user_name": user_name,
        "slack_channel": project.slack_channel,
        "budget_usd": project.budget_usd,
        "budget_spent_usd": project.budget_spent_usd,
        "created_at": project.created_at,
        "task_summary": task_summary,
    }
    if include_shared_state:
        return ProjectDetailResponse(**base, shared_state=project.shared_state)
    return ProjectResponse(**base)


def _format_task(
    task: AgentTask,
    agent_name: str | None,
    creator_user_id: str | None,
    creator_user_name: str | None,
) -> TaskResponse:
    """Format task into response model."""
    return TaskResponse(
        agent_task_id=str(task.agent_task_id),
        agent_project_id=str(task.agent_project_id),
        title=task.title,
        description=task.description,
        status=task.status.value,
        priority=task.priority.name,
        parent_task_id=str(task.parent_task_id) if task.parent_task_id else None,
        depends_on=task.depends_on,
        agent_name=agent_name,
        agent_id=str(task.agent_id) if task.agent_id else None,
        assigned_session_ids=task.assigned_session_ids,
        result=task.result,
        task_data=task.task_data,
        estimated_effort=task.estimated_effort,
        actual_spending_usd=task.actual_spending_usd,
        completed_at=task.completed_at,
        created_at=task.created_at,
        creator_user_id=creator_user_id,
        creator_user_name=creator_user_name,
    )


async def _resolve_agent_id_by_name(session: Any, agent_name: str) -> uuid.UUID | None:
    """Resolve agent name to agent_id. Returns None if not found."""
    result = await session.execute(
        select(Agent).where(Agent.name == agent_name).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
    )
    agent = result.scalars().first()
    return agent.agent_id if agent else None


async def _promote_dependents_to_ready(session: Any, completed_task_id: uuid.UUID, project_id: uuid.UUID) -> list[str]:
    """After a task completes, promote dependent tasks from BLOCKED/PENDING -> READY if all deps met."""
    task_id_str = str(completed_task_id)

    result = await session.execute(
        select(AgentTask)
        .where(col(AgentTask.agent_project_id) == project_id)
        .where(col(AgentTask.status).in_([AgentTaskStatus.BLOCKED, AgentTaskStatus.PENDING]))
        .where(col(AgentTask.deleted_at).is_(None))
        .where(col(AgentTask.depends_on).isnot(None))
    )
    candidates = result.scalars().all()

    # Filter to tasks that actually depend on the completed task
    relevant_tasks = [task for task in candidates if task.depends_on and task_id_str in task.depends_on]
    if not relevant_tasks:
        return []

    # Gather all unique dependency IDs across all relevant tasks
    all_dep_ids: set[uuid.UUID] = set()
    for task in relevant_tasks:
        all_dep_ids.update(uuid.UUID(d) for d in task.depends_on)  # depends_on checked in filter above

    # Batch-fetch all dependency statuses in a single query
    dep_status_result = await session.execute(
        select(AgentTask.agent_task_id, AgentTask.status).where(col(AgentTask.agent_task_id).in_(list(all_dep_ids)))
    )
    dep_statuses: dict[uuid.UUID, AgentTaskStatus] = {row.agent_task_id: row.status for row in dep_status_result.all()}

    newly_ready: list[str] = []
    for task in relevant_tasks:
        dep_ids = [uuid.UUID(d) for d in task.depends_on]  # depends_on checked in filter above
        # Treat deleted/missing deps as satisfied (they can't block forever)
        if all(dep_statuses.get(d, AgentTaskStatus.COMPLETED) == AgentTaskStatus.COMPLETED for d in dep_ids):
            task.status = AgentTaskStatus.READY
            session.add(task)
            newly_ready.append(str(task.agent_task_id))

    return newly_ready


# ============================================================================
# Projects
# ============================================================================


@retry_db
async def list_projects_service(
    status: str | None = None,
    creator_user_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ProjectListResponse:
    """List projects with optional filters and pagination."""
    status_filter: AgentProjectStatus | None = None
    if status:
        try:
            status_filter = AgentProjectStatus(status)
        except ValueError:
            valid = [s.value for s in AgentProjectStatus]
            raise ValueError(f"Invalid status: {status}. Must be one of: {valid}") from None

    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)

    async with get_async_session_read_replica() as session:
        # Build query with user join
        query: Any = (
            select(AgentProject, col(User.name).label("user_name"))
            .outerjoin(User, col(AgentProject.creator_user_id) == col(User.user_id))
            .where(col(AgentProject.deleted_at).is_(None))
        )
        if status_filter:
            query = query.where(col(AgentProject.status) == status_filter)
        if creator_user_id:
            query = query.where(col(AgentProject.creator_user_id) == creator_user_id)

        # Count total
        count_inner: Any = select(AgentProject.agent_project_id).where(col(AgentProject.deleted_at).is_(None))
        if status_filter:
            count_inner = count_inner.where(col(AgentProject.status) == status_filter)
        if creator_user_id:
            count_inner = count_inner.where(col(AgentProject.creator_user_id) == creator_user_id)
        count_query: Any = select(sa.func.count()).select_from(count_inner.subquery())

        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Fetch page
        query = query.order_by(col(AgentProject.created_at).desc()).offset(offset).limit(limit)
        result = await session.execute(query)
        rows = result.all()

        # Batch-fetch task summaries for all projects on this page
        project_ids = [row.AgentProject.agent_project_id for row in rows]
        summaries = await _get_task_summaries_batch(session, project_ids)

        items: list[ProjectResponse] = []
        for row in rows:
            project = row.AgentProject
            user_name = row.user_name
            task_summary = summaries.get(project.agent_project_id, TaskSummary())
            response = _format_project(project, user_name, task_summary)
            assert isinstance(response, ProjectResponse)
            items.append(response)

        return ProjectListResponse(items=items, total=total, offset=offset, limit=limit)


@retry_db
async def get_project_service(project_id: str) -> ProjectDetailResponse:
    """Get a single project by ID with shared_state and task summary."""
    proj_uuid = uuid.UUID(project_id)

    async with get_async_session_read_replica() as session:
        result = await session.execute(
            select(AgentProject, col(User.name).label("user_name"))
            .outerjoin(User, col(AgentProject.creator_user_id) == col(User.user_id))
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        row = result.first()
        if not row:
            raise LookupError(f"Project not found: {project_id}")

        project = row.AgentProject
        task_summary = await _get_task_summary(session, proj_uuid)
        response = _format_project(project, row.user_name, task_summary, include_shared_state=True)
        assert isinstance(response, ProjectDetailResponse)
        return response


@retry_db
async def update_project_service(project_id: str, request: ProjectUpdateRequest) -> ProjectDetailResponse:
    """Update a project's mutable fields."""
    proj_uuid = uuid.UUID(project_id)

    async with get_async_session() as session:
        proj_result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
            .with_for_update()
        )
        project = proj_result.scalars().first()
        if not project:
            raise LookupError(f"Project not found: {project_id}")

        if request.name is not None:
            project.name = request.name
        if request.description is not None:
            project.description = request.description
        if request.slack_channel is not None:
            project.slack_channel = request.slack_channel or None

        session.add(project)
        await session.commit()
        await session.refresh(project)

        # Resolve user name
        user_name: str | None = None
        if project.creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == project.creator_user_id))
            user_row = user_result.first()
            user_name = user_row.name if user_row else None

        task_summary = await _get_task_summary(session, proj_uuid)
        response = _format_project(project, user_name, task_summary, include_shared_state=True)
        assert isinstance(response, ProjectDetailResponse)
        return response


@retry_db
async def set_project_status_service(project_id: str, request: ProjectStatusRequest) -> ProjectDetailResponse:
    """Change a project's status."""
    proj_uuid = uuid.UUID(project_id)

    try:
        target_status = AgentProjectStatus(request.status)
    except ValueError:
        valid = [s.value for s in AgentProjectStatus]
        raise ValueError(f"Invalid status: {request.status}. Must be one of: {valid}") from None

    async with get_async_session() as session:
        proj_result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
            .with_for_update()
        )
        project = proj_result.scalars().first()
        if not project:
            raise LookupError(f"Project not found: {project_id}")

        # Project status transitions are intentionally free-form (any → any).
        # Unlike tasks, projects have a simple lifecycle and admins may need
        # to reactivate archived projects or undo status changes.
        project.status = target_status
        session.add(project)

        # Resolve user name
        user_name: str | None = None
        if project.creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == project.creator_user_id))
            user_row = user_result.first()
            user_name = user_row.name if user_row else None

        task_summary = await _get_task_summary(session, proj_uuid)
        await session.commit()

        response = _format_project(project, user_name, task_summary, include_shared_state=True)
        assert isinstance(response, ProjectDetailResponse)

        logger.info("Updated project status via REST", project_id=project_id, status=target_status.value)
        return response


# ============================================================================
# Tasks
# ============================================================================


@retry_db
async def list_tasks_service(
    project_id: str,
    status: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> TaskListResponse:
    """List tasks in a project with optional filters and pagination."""
    proj_uuid = uuid.UUID(project_id)

    status_filter: AgentTaskStatus | None = None
    if status:
        try:
            status_filter = AgentTaskStatus(status)
        except ValueError:
            valid = [s.value for s in AgentTaskStatus]
            raise ValueError(f"Invalid status: {status}. Must be one of: {valid}") from None

    parent_uuid: uuid.UUID | None = None
    if parent_task_id:
        parent_uuid = uuid.UUID(parent_task_id)

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)

    async with get_async_session_read_replica() as session:
        # Verify project exists
        proj_result = await session.execute(
            select(AgentProject.agent_project_id, AgentProject.creator_user_id)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        proj_row = proj_result.first()
        if not proj_row:
            raise LookupError(f"Project not found: {project_id}")

        # Resolve project creator
        creator_user_id = proj_row.creator_user_id
        creator_user_name: str | None = None
        if creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == creator_user_id))
            user_row = user_result.first()
            creator_user_name = user_row.name if user_row else None

        # Build task query
        query: Any = (
            select(AgentTask, col(Agent.name).label("agent_name"))
            .outerjoin(Agent, col(AgentTask.agent_id) == col(Agent.agent_id))
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
        )
        if status_filter:
            query = query.where(col(AgentTask.status) == status_filter)
        if parent_uuid is not None:
            query = query.where(col(AgentTask.parent_task_id) == parent_uuid)

        # Count total matching tasks
        count_inner: Any = (
            select(AgentTask.agent_task_id)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
        )
        if status_filter:
            count_inner = count_inner.where(col(AgentTask.status) == status_filter)
        if parent_uuid is not None:
            count_inner = count_inner.where(col(AgentTask.parent_task_id) == parent_uuid)
        count_query = select(sa.func.count()).select_from(count_inner.subquery())
        total = (await session.execute(count_query)).scalar() or 0

        # Fetch page
        query = query.order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
        query = query.offset(offset).limit(limit)
        result = await session.execute(query)
        rows = result.all()

        items = [_format_task(row.AgentTask, row.agent_name, creator_user_id, creator_user_name) for row in rows]
        task_summary = await _get_task_summary(session, proj_uuid)

        return TaskListResponse(items=items, total=total, offset=offset, limit=limit, task_summary=task_summary)


@retry_db
async def get_task_service(project_id: str, task_id: str) -> TaskResponse:
    """Get a single task by ID, scoped to a project."""
    proj_uuid = uuid.UUID(project_id)
    task_uuid = uuid.UUID(task_id)

    async with get_async_session_read_replica() as session:
        # Fetch task with agent name
        result = await session.execute(
            select(AgentTask, col(Agent.name).label("agent_name"))
            .outerjoin(Agent, col(AgentTask.agent_id) == col(Agent.agent_id))
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
        )
        row = result.first()
        if not row:
            raise LookupError(f"Task not found: {task_id} in project {project_id}")

        # Resolve project creator
        proj_result = await session.execute(
            select(AgentProject.creator_user_id).where(col(AgentProject.agent_project_id) == proj_uuid)
        )
        proj_row = proj_result.first()
        creator_user_id = proj_row.creator_user_id if proj_row else None

        creator_user_name: str | None = None
        if creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == creator_user_id))
            user_row = user_result.first()
            creator_user_name = user_row.name if user_row else None

        return _format_task(row.AgentTask, row.agent_name, creator_user_id, creator_user_name)


@retry_db
async def update_task_service(project_id: str, task_id: str, request: TaskUpdateRequest) -> TaskResponse:
    """Update a task's mutable fields."""
    proj_uuid = uuid.UUID(project_id)
    task_uuid = uuid.UUID(task_id)

    async with get_async_session() as session:
        task_result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = task_result.scalars().first()
        if not task:
            raise LookupError(f"Task not found: {task_id} in project {project_id}")

        if request.title is not None:
            task.title = request.title
        if request.description is not None:
            task.description = request.description
        if request.estimated_effort is not None:
            task.estimated_effort = request.estimated_effort

        if request.priority is not None:
            try:
                task.priority = AgentTaskPriority[request.priority]
            except KeyError:
                valid = [p.name for p in AgentTaskPriority]
                raise ValueError(f"Invalid priority: {request.priority}. Must be one of: {valid}") from None

        if request.agent_name is not None:
            if request.agent_name == "":
                task.agent_id = None
            else:
                agent_id = await _resolve_agent_id_by_name(session, request.agent_name)
                if agent_id is None:
                    raise ValueError(f"Agent not found: {request.agent_name}")
                task.agent_id = agent_id

        if request.task_data is not None:
            if task.task_data:
                merged = {**task.task_data, **request.task_data}
            else:
                merged = request.task_data
            task.task_data = merged

        session.add(task)
        await session.commit()
        await session.refresh(task)

        # Resolve agent name
        resolved_agent_name: str | None = None
        if task.agent_id:
            agent_result = await session.execute(
                select(Agent.name).where(col(Agent.agent_id) == task.agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
            )
            agent_row = agent_result.first()
            resolved_agent_name = agent_row.name if agent_row else None

        # Resolve project creator
        proj_result = await session.execute(
            select(AgentProject.creator_user_id).where(col(AgentProject.agent_project_id) == proj_uuid)
        )
        proj_row = proj_result.first()
        creator_user_id = proj_row.creator_user_id if proj_row else None

        creator_user_name: str | None = None
        if creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == creator_user_id))
            user_row = user_result.first()
            creator_user_name = user_row.name if user_row else None

        logger.info("Updated task via REST", task_id=task_id, project_id=project_id)
        return _format_task(task, resolved_agent_name, creator_user_id, creator_user_name)


@retry_db
async def set_task_status_service(project_id: str, task_id: str, request: TaskStatusRequest) -> TaskStatusResponse:
    """Change a task's status with validation and dependency cascade."""
    proj_uuid = uuid.UUID(project_id)
    task_uuid = uuid.UUID(task_id)

    try:
        target_status = AgentTaskStatus(request.status)
    except ValueError:
        valid = [s.value for s in AgentTaskStatus]
        raise ValueError(f"Invalid status: {request.status}. Must be one of: {valid}") from None

    async with get_async_session() as session:
        task_result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = task_result.scalars().first()
        if not task:
            raise LookupError(f"Task not found: {task_id} in project {project_id}")

        # Validate transition
        validation_error = await validate_task_status_change(session, task, target_status)
        if validation_error:
            raise ValueError(validation_error)

        if target_status in TERMINAL_TASK_STATUSES:
            await complete_task(session, task, target_status, result=request.result)
        else:
            task.status = target_status
            if request.result is not None:
                task.result = request.result
            task.completed_at = None
            session.add(task)

        # Cascade: promote dependents if completed
        newly_ready: list[str] = []
        if target_status == AgentTaskStatus.COMPLETED:
            newly_ready = await _promote_dependents_to_ready(session, task_uuid, proj_uuid)

        await session.commit()

        logger.info(
            "Updated task status via REST",
            task_id=task_id,
            project_id=project_id,
            status=target_status.value,
            newly_ready_count=len(newly_ready),
        )

        return TaskStatusResponse(
            agent_task_id=task_id,
            status=target_status.value,
            newly_ready_tasks=newly_ready,
        )


@retry_db
async def set_task_dependencies_service(
    project_id: str, task_id: str, request: TaskDependenciesRequest
) -> TaskResponse:
    """Replace a task's dependency list."""
    proj_uuid = uuid.UUID(project_id)
    task_uuid = uuid.UUID(task_id)

    seen: set[str] = set()
    dep_uuids: list[uuid.UUID] = []
    for dep_str in request.depends_on:
        if dep_str in seen:
            continue
        seen.add(dep_str)
        try:
            dep_uuids.append(uuid.UUID(dep_str))
        except ValueError:
            raise ValueError(f"Invalid dependency UUID: {dep_str}") from None

    # Prevent self-dependency
    if task_uuid in dep_uuids:
        raise ValueError("A task cannot depend on itself")

    async with get_async_session() as session:
        task_result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = task_result.scalars().first()
        if not task:
            raise LookupError(f"Task not found: {task_id} in project {project_id}")

        all_completed = True

        if dep_uuids:
            # Verify deps exist in same project
            existing_result = await session.execute(
                select(AgentTask.agent_task_id, AgentTask.status)
                .where(col(AgentTask.agent_task_id).in_(dep_uuids))
                .where(col(AgentTask.agent_project_id) == proj_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
            )
            existing_rows = list(existing_result.all())
            existing_ids = {row.agent_task_id for row in existing_rows}
            missing = [str(d) for d in dep_uuids if d not in existing_ids]
            if missing:
                raise ValueError(f"Dependency tasks not found in project: {missing}")

            # Cycle detection
            all_project_tasks = await session.execute(
                select(AgentTask.agent_task_id, AgentTask.depends_on)
                .where(col(AgentTask.agent_project_id) == proj_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
            )
            dep_graph: dict[str, list[str]] = {}
            for row in all_project_tasks.all():
                dep_graph[str(row.agent_task_id)] = row.depends_on or []
            dep_graph[task_id] = [str(d) for d in dep_uuids]

            visited: set[str] = set()
            stack = list(dep_graph.get(task_id, []))
            while stack:
                node = stack.pop()
                if node == task_id:
                    raise ValueError("Setting these dependencies would create a cycle")
                if node in visited:
                    continue
                visited.add(node)
                stack.extend(dep_graph.get(node, []))

            all_completed = all(row.status == AgentTaskStatus.COMPLETED for row in existing_rows)

        # Set dependencies
        task.depends_on = [str(d) for d in dep_uuids] if dep_uuids else None

        # Auto-adjust status if task is in a non-terminal, non-in-progress, non-in-review state
        _no_auto_adjust = TERMINAL_TASK_STATUSES | {AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.IN_REVIEW}
        if task.status not in _no_auto_adjust:
            if dep_uuids and not all_completed:
                task.status = AgentTaskStatus.BLOCKED
            else:
                task.status = AgentTaskStatus.READY

        session.add(task)
        await session.commit()
        await session.refresh(task)

        # Resolve agent name
        resolved_agent_name: str | None = None
        if task.agent_id:
            agent_result = await session.execute(
                select(Agent.name).where(col(Agent.agent_id) == task.agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
            )
            agent_row = agent_result.first()
            resolved_agent_name = agent_row.name if agent_row else None

        # Resolve project creator
        proj_result = await session.execute(
            select(AgentProject.creator_user_id).where(col(AgentProject.agent_project_id) == proj_uuid)
        )
        proj_row = proj_result.first()
        creator_user_id = proj_row.creator_user_id if proj_row else None

        creator_user_name: str | None = None
        if creator_user_id:
            user_result = await session.execute(select(User.name).where(col(User.user_id) == creator_user_id))
            user_row = user_result.first()
            creator_user_name = user_row.name if user_row else None

        logger.info(
            "Updated task dependencies via REST",
            task_id=task_id,
            project_id=project_id,
            dep_count=len(dep_uuids),
            new_status=task.status.value,
        )

        return _format_task(task, resolved_agent_name, creator_user_id, creator_user_name)


@retry_db
async def resume_task_service(project_id: str, task_id: str) -> TaskResumeResponse:
    """Resume a failed task."""
    proj_uuid = uuid.UUID(project_id)
    task_uuid = uuid.UUID(task_id)

    # Verify task belongs to project
    async with get_async_session_read_replica() as session:
        task_result = await session.execute(
            select(AgentTask.agent_project_id)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
        )
        row = task_result.first()
        if not row:
            raise LookupError(f"Task not found: {task_id}")
        if row.agent_project_id != proj_uuid:
            raise LookupError(f"Task {task_id} does not belong to project {project_id}")

    from ypl.agent_harness_service.task_executor import resume_task

    result = await resume_task(task_uuid)

    if not result.get("success"):
        error_code = result.get("error_code")
        error_message = result.get("error", "Failed to resume task")
        if error_code == "NOT_FOUND":
            raise LookupError(error_message)
        raise ValueError(error_message)

    logger.info("Resumed failed task via REST", task_id=task_id, project_id=project_id)

    return TaskResumeResponse(
        agent_task_id=task_id,
        status=result.get("status", "READY"),
        session_to_resume=result.get("session_to_resume"),
    )
