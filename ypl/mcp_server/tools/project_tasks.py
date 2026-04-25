"""MCP tools for project and task management.

Provides tools for creating projects, adding tasks with dependencies,
querying task status, and managing lifecycle transitions with automatic
dependency cascade.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select

from ypl.agent_harness_service.common.constants import AHS_DEFAULT_PROJECT_SLACK_CHANNEL
from ypl.agent_harness_service.projects.task_utils import (
    TERMINAL_TASK_STATUSES,
    are_dependencies_completed,
    complete_task,
    validate_task_status_change,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.soul_utils import has_permission_by_user_id_cached
from ypl.db.agent_harness import (
    Agent,
    AgentProject,
    AgentProjectStatus,
    AgentTask,
    AgentTaskPriority,
    AgentTaskStatus,
)
from ypl.db.rbac import Permission
from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email
from ypl.mcp_server.authorization import resolve_caller_user_id
from ypl.mcp_server.core import get_authenticated_user_email, get_requesting_user_id, mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()

# NOTE: Read-only tools (get_project, get_task, get_project_tasks, list_projects)
# still expose any caller's USE_MCP access to browse all projects; tightening
# those is a follow-up. The mutating tools below enforce
# ``caller == project.creator_user_id OR caller has MANAGE_AGENT_PROJECTS``.


# ============================================================================
# Helpers
# ============================================================================


async def _resolve_caller_for_project_auth(auth_email: str) -> tuple[str | None, bool, str | None]:
    """Resolve ``(caller_user_id, is_project_admin, error)``.

    ``is_project_admin`` is True iff the caller holds
    ``MANAGE_AGENT_PROJECTS`` — meaning they may mutate any project.
    Otherwise, per-resource mutation is limited to resources they own.
    """
    caller_user_id, err = await resolve_caller_user_id(auth_email)
    if err or caller_user_id is None:
        return None, False, err or "Could not resolve caller"
    is_admin = await has_permission_by_user_id_cached(caller_user_id, Permission.MANAGE_AGENT_PROJECTS)
    return caller_user_id, is_admin, None


def _check_project_ownership(
    project: AgentProject,
    caller_user_id: str,
    is_project_admin: bool,
) -> str | None:
    """Return an error message if the caller may not mutate this project, else None.

    Allowed when the caller is the project's ``creator_user_id`` OR holds
    ``MANAGE_AGENT_PROJECTS`` (``is_project_admin``).
    """
    if str(project.creator_user_id) == caller_user_id:
        return None
    if is_project_admin:
        return None
    return "Not authorized: you do not own this project and lack MANAGE_AGENT_PROJECTS permission"


async def _load_project_for_task(session: Any, task: AgentTask) -> AgentProject | None:
    """Load the parent project for a task, honoring soft-deletion."""
    result = await session.execute(
        select(AgentProject)
        .where(col(AgentProject.agent_project_id) == task.agent_project_id)
        .where(col(AgentProject.deleted_at).is_(None))
    )
    project: AgentProject | None = result.scalars().first()
    return project


async def _resolve_agent_id_by_name(session: Any, agent_name: str) -> uuid.UUID | None:
    """Resolve an agent name to its agent_id within the given session. Returns None if not found."""
    result = await session.execute(
        select(Agent).where(Agent.name == agent_name).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
    )
    agent = result.scalars().first()
    return agent.agent_id if agent else None


async def _resolve_agent_name_by_id(session: Any, agent_id: uuid.UUID) -> str | None:
    """Resolve an agent_id to its name within the given session. Returns None if not found."""
    result = await session.execute(
        select(Agent.name).where(col(Agent.agent_id) == agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
    )
    row = result.first()
    return row.name if row else None


async def _batch_resolve_agent_names(session: Any, agent_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Batch-resolve agent_ids to names. Returns a dict mapping agent_id -> name."""
    if not agent_ids:
        return {}
    result = await session.execute(
        select(Agent.agent_id, Agent.name)
        .where(col(Agent.agent_id).in_(list(agent_ids)))
        .where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
    )
    return {row.agent_id: row.name for row in result.all()}


async def _get_task_status_summary(session: Any, project_id: uuid.UUID) -> dict[str, int]:
    """Get counts of tasks by status for a project."""
    result = await session.execute(
        select(AgentTask.status, sa.func.count())
        .where(col(AgentTask.agent_project_id) == project_id)
        .where(col(AgentTask.deleted_at).is_(None))
        .group_by(AgentTask.status)
    )
    summary: dict[str, int] = {s.value: 0 for s in AgentTaskStatus}
    total = 0
    for status_val, count in result.all():
        summary[status_val.value] = count
        total += count
    summary["total"] = total
    return summary


async def _promote_dependents_to_ready(session: Any, completed_task_id: uuid.UUID, project_id: uuid.UUID) -> list[str]:
    """After a task completes, promote dependent tasks from BLOCKED/PENDING → READY if all deps are met.

    Returns list of newly-ready task ID strings.
    """
    task_id_str = str(completed_task_id)

    result = await session.execute(
        select(AgentTask)
        .where(col(AgentTask.agent_project_id) == project_id)
        .where(col(AgentTask.status).in_([AgentTaskStatus.BLOCKED, AgentTaskStatus.PENDING]))
        .where(col(AgentTask.deleted_at).is_(None))
        .where(col(AgentTask.depends_on).isnot(None))
    )
    candidates = result.scalars().all()

    # TODO: batch dep status check to avoid N+1 queries (see PR #10887)
    newly_ready: list[str] = []
    for task in candidates:
        if not task.depends_on or task_id_str not in task.depends_on:
            continue

        dep_ids = [uuid.UUID(d) for d in task.depends_on]
        dep_result = await session.execute(
            select(AgentTask.status)
            .where(col(AgentTask.agent_task_id).in_(dep_ids))
            .where(col(AgentTask.deleted_at).is_(None))
        )
        dep_rows = dep_result.all()
        if len(dep_rows) == len(dep_ids) and all(row.status == AgentTaskStatus.COMPLETED for row in dep_rows):
            task.status = AgentTaskStatus.READY
            session.add(task)
            newly_ready.append(str(task.agent_task_id))

    return newly_ready


def _format_task_row(task: AgentTask, agent_name: str | None) -> dict[str, Any]:
    """Format an AgentTask + resolved agent_name into a response dict."""
    return {
        "agent_task_id": str(task.agent_task_id),
        "agent_project_id": str(task.agent_project_id),
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.name,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "depends_on": task.depends_on,
        "agent_name": agent_name,
        "result": task.result,
        "task_data": task.task_data,
        "estimated_effort": task.estimated_effort,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _format_project(project: AgentProject, default_agent_name: str | None = None) -> dict[str, Any]:
    """Format an AgentProject into a response dict."""
    return {
        "agent_project_id": str(project.agent_project_id),
        "name": project.name,
        "description": project.description,
        "status": project.status.value,
        "slack_channel": project.slack_channel,
        "default_agent_id": str(project.default_agent_id) if project.default_agent_id else None,
        "default_agent_name": default_agent_name,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }


def _parse_priority(value: str, context: str) -> tuple[AgentTaskPriority | None, dict[str, Any] | None]:
    """Parse a priority string. Returns (priority, None) on success or (None, error_dict) on failure."""
    try:
        return AgentTaskPriority[value], None
    except KeyError:
        return None, {
            "success": False,
            "error": f"Invalid priority '{value}' {context}. Valid values: {[p.name for p in AgentTaskPriority]}",
        }


# ============================================================================
# MCP Tool Functions
# ============================================================================


@mcp_server.tool(
    name="add_project",
    description=(
        "Create a new agent project. Projects group related tasks with dependencies "
        "and track progress toward a common goal. Projects start in PAUSED status; "
        "set to ACTIVE via set_project_status to begin task execution. "
        "slack_channel defaults to 'agentic-projects' if not specified. "
        "Prefer a plain channel name (e.g. 'agentic-projects'); channel IDs are also accepted."
    ),
)
@retry_db
async def add_project(
    name: str,
    description: str | None = None,
    slack_channel: str | None = None,
) -> dict[str, Any]:
    """Create a new agent project.

    Args:
        name: Human-readable project name (e.g. "Q3 model migration")
        description: Goal description and completion criteria — what "done" looks like
        slack_channel: Slack channel for progress updates (e.g. "agentic-projects").
            Prefer a plain channel name; channel IDs are also accepted. Defaults to "agentic-projects" if not specified.

    Returns:
        Dictionary with project ID and metadata
    """
    try:
        creator_user_id = get_requesting_user_id()
        if not creator_user_id:
            auth_email = get_authenticated_user_email()
            if auth_email == "unknown":
                return {"success": False, "error": "Authentication required"}
            creator_user_id, user_error = await resolve_user_id_from_email(auth_email)
            if user_error:
                return {"success": False, "error": user_error}

        effective_channel = slack_channel.strip() if slack_channel else AHS_DEFAULT_PROJECT_SLACK_CHANNEL

        project = AgentProject(
            name=name,
            description=description,
            creator_user_id=creator_user_id,
            slack_channel=effective_channel,
        )

        async with get_async_session() as session:
            session.add(project)
            await session.commit()
            await session.refresh(project)

            logger.info("Created agent project", project_id=str(project.agent_project_id), name=name)

            return {
                "success": True,
                **_format_project(project),
            }

    except Exception as e:
        logger.error("Error creating agent project", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="add_task_sequence",
    description=(
        "Add a linear sequence of tasks to a project where each task depends on the previous one. "
        "The first task starts as READY; subsequent tasks are BLOCKED until their predecessor completes. "
        "Optionally nest all tasks under a parent task for organizational grouping."
    ),
)
@retry_db
async def add_task_sequence(
    project_id: str,
    tasks: str,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    """Add a linear sequence of dependent tasks.

    Args:
        project_id: UUID of the project to add tasks to
        tasks: JSON array of task definitions, executed in order. Each element:
            {"title": "Step name", "description": "What to do",
             "priority": "NORMAL|HIGH|URGENT|LOW", "agent_name": "optional-agent-to-run-this"}
            Only "title" is required.
        parent_task_id: Optional UUID of an existing task to nest all new tasks under
            (organizational grouping only — does NOT create a dependency on the parent)

    Returns:
        Dictionary with created task IDs and their dependency chain
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        parent_uuid: uuid.UUID | None = None
        if parent_task_id:
            try:
                parent_uuid = uuid.UUID(parent_task_id)
            except ValueError:
                return {"success": False, "error": f"Invalid parent_task_id: {parent_task_id}"}

        try:
            task_defs = json.loads(tasks)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid tasks JSON: {e}"}

        if not isinstance(task_defs, list) or len(task_defs) == 0:
            return {"success": False, "error": "tasks must be a non-empty JSON array"}

        async with get_async_session() as session:
            # Verify project exists
            proj_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            if not proj_result.scalars().first():
                return {"success": False, "error": f"Project not found: {project_id}"}

            # Verify parent task exists and belongs to this project
            if parent_uuid:
                parent_result = await session.execute(
                    select(AgentTask)
                    .where(col(AgentTask.agent_task_id) == parent_uuid)
                    .where(col(AgentTask.deleted_at).is_(None))
                )
                parent = parent_result.scalars().first()
                if not parent:
                    return {"success": False, "error": f"Parent task not found: {parent_task_id}"}
                if parent.agent_project_id != proj_uuid:
                    return {"success": False, "error": "Parent task belongs to a different project"}

            created_tasks = []
            prev_task_id: uuid.UUID | None = None

            for i, task_def in enumerate(task_defs):
                if not isinstance(task_def, dict) or "title" not in task_def:
                    return {"success": False, "error": f"Task at index {i} must be an object with 'title'"}

                agent_id: uuid.UUID | None = None
                if task_def.get("agent_name"):
                    agent_id = await _resolve_agent_id_by_name(session, task_def["agent_name"])
                    if agent_id is None:
                        return {"success": False, "error": f"Agent not found: {task_def['agent_name']}"}

                priority = AgentTaskPriority.NORMAL
                if "priority" in task_def:
                    parsed_priority, err = _parse_priority(task_def["priority"], f"at index {i}")
                    if err:
                        return err
                    priority = parsed_priority  # type: ignore[assignment]

                depends_on = [str(prev_task_id)] if prev_task_id else None
                status = AgentTaskStatus.READY if prev_task_id is None else AgentTaskStatus.BLOCKED

                task = AgentTask(
                    agent_project_id=proj_uuid,
                    parent_task_id=parent_uuid,
                    title=task_def["title"],
                    description=task_def.get("description"),
                    status=status,
                    priority=priority,
                    agent_id=agent_id,
                    depends_on=depends_on,
                    task_data=task_def.get("task_data"),
                )
                session.add(task)
                await session.flush()

                created_tasks.append(
                    {
                        "agent_task_id": str(task.agent_task_id),
                        "title": task.title,
                        "status": task.status.value,
                        "depends_on": depends_on,
                    }
                )
                prev_task_id = task.agent_task_id

            await session.commit()

            logger.info("Created task sequence", project_id=project_id, task_count=len(created_tasks))

            return {
                "success": True,
                "project_id": project_id,
                "task_count": len(created_tasks),
                "tasks": created_tasks,
            }

    except Exception as e:
        logger.error("Error creating task sequence", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="add_tasks",
    description=(
        "Add tasks with arbitrary dependencies to a project. Each task has a local 'name' "
        "used for dependency resolution within the batch. Dependencies can reference other "
        "tasks in the same batch by name, or existing tasks by UUID. "
        "Priority values: URGENT, HIGH, NORMAL, LOW (default: NORMAL)."
    ),
)
@retry_db
async def add_tasks(
    project_id: str,
    tasks: str,
) -> dict[str, Any]:
    """Add tasks with an explicit dependency graph.

    Args:
        project_id: UUID of the project to add tasks to
        tasks: JSON array of task definitions. Each element:
            {"name": "local-ref-name",
             "title": "Human-readable task title",
             "description": "Detailed instructions for the agent",
             "depends_on": ["other-name", "existing-task-uuid"],
             "priority": "NORMAL|HIGH|URGENT|LOW",
             "agent_name": "agent-to-execute",
             "parent_task_id": "optional-uuid-for-grouping"}
            "name" and "title" are required. "name" is a local key for dependency
            resolution only — it is NOT stored in the database.
            "depends_on" entries can be names of other tasks in this batch or UUIDs
            of tasks that already exist in the project.

    Returns:
        Dictionary with created task IDs and resolved dependencies
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        try:
            task_defs = json.loads(tasks)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid tasks JSON: {e}"}

        if not isinstance(task_defs, list) or len(task_defs) == 0:
            return {"success": False, "error": "tasks must be a non-empty JSON array"}

        # Validate required fields and unique names
        names_seen: set[str] = set()
        for i, td in enumerate(task_defs):
            if not isinstance(td, dict) or "title" not in td:
                return {"success": False, "error": f"Task at index {i} must have 'title'"}
            name = td.get("name")
            if not name:
                return {"success": False, "error": f"Task at index {i} must have 'name' for dependency resolution"}
            if name in names_seen:
                return {"success": False, "error": f"Duplicate task name: '{name}'"}
            names_seen.add(name)

        # Cycle detection via DFS
        name_to_local_deps: dict[str, list[str]] = {}
        for td in task_defs:
            deps = td.get("depends_on") or []
            name_to_local_deps[td["name"]] = [d for d in deps if d in names_seen]

        UNVISITED, VISITING, VISITED = 0, 1, 2
        visit_state: dict[str, int] = dict.fromkeys(names_seen, UNVISITED)

        def _has_cycle(node: str) -> bool:
            if visit_state[node] == VISITING:
                return True
            if visit_state[node] == VISITED:
                return False
            visit_state[node] = VISITING
            for dep in name_to_local_deps.get(node, []):
                if dep in visit_state and _has_cycle(dep):
                    return True
            visit_state[node] = VISITED
            return False

        for name in names_seen:
            if _has_cycle(name):
                return {"success": False, "error": f"Dependency cycle detected involving task '{name}'"}

        async with get_async_session() as session:
            # Verify project exists
            proj_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            if not proj_result.scalars().first():
                return {"success": False, "error": f"Project not found: {project_id}"}

            # Validate external UUID dependencies exist in this project
            external_dep_uuids: set[uuid.UUID] = set()
            for td in task_defs:
                for dep_ref in td.get("depends_on") or []:
                    if dep_ref not in names_seen:
                        try:
                            external_dep_uuids.add(uuid.UUID(dep_ref))
                        except ValueError:
                            pass  # Will be caught in second pass

            if external_dep_uuids:
                existing_result = await session.execute(
                    select(AgentTask.agent_task_id)
                    .where(col(AgentTask.agent_task_id).in_(list(external_dep_uuids)))
                    .where(col(AgentTask.agent_project_id) == proj_uuid)
                    .where(col(AgentTask.deleted_at).is_(None))
                )
                existing_ids = {row.agent_task_id for row in existing_result.all()}
                missing = external_dep_uuids - existing_ids
                if missing:
                    return {
                        "success": False,
                        "error": f"External dependency tasks not found in project: {[str(m) for m in missing]}",
                    }

            # First pass: create all tasks to get UUIDs
            name_to_uuid: dict[str, uuid.UUID] = {}
            task_objects: list[AgentTask] = []

            for td in task_defs:
                agent_id: uuid.UUID | None = None
                if td.get("agent_name"):
                    agent_id = await _resolve_agent_id_by_name(session, td["agent_name"])
                    if agent_id is None:
                        return {"success": False, "error": f"Agent not found: {td['agent_name']}"}

                priority = AgentTaskPriority.NORMAL
                if "priority" in td:
                    parsed_priority, err = _parse_priority(td["priority"], f"for task '{td['name']}'")
                    if err:
                        return err
                    priority = parsed_priority  # type: ignore[assignment]

                parent_uuid: uuid.UUID | None = None
                if td.get("parent_task_id"):
                    try:
                        parent_uuid = uuid.UUID(td["parent_task_id"])
                    except ValueError:
                        return {"success": False, "error": f"Invalid parent_task_id for task '{td['name']}'"}
                    parent_result = await session.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_task_id) == parent_uuid)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    parent_task = parent_result.scalars().first()
                    if not parent_task:
                        return {"success": False, "error": f"Parent task not found: {td['parent_task_id']}"}
                    if parent_task.agent_project_id != proj_uuid:
                        return {
                            "success": False,
                            "error": f"Parent task belongs to a different project (task '{td['name']}')",
                        }

                task = AgentTask(
                    agent_project_id=proj_uuid,
                    parent_task_id=parent_uuid,
                    title=td["title"],
                    description=td.get("description"),
                    priority=priority,
                    agent_id=agent_id,
                    task_data=td.get("task_data"),
                    status=AgentTaskStatus.PENDING,  # Updated in second pass
                )
                session.add(task)
                await session.flush()
                name_to_uuid[td["name"]] = task.agent_task_id
                task_objects.append(task)

            # Second pass: resolve dependencies and set status
            for td, task in zip(task_defs, task_objects, strict=True):
                raw_deps = td.get("depends_on") or []
                resolved_deps: list[str] = []
                for dep_ref in raw_deps:
                    if dep_ref in name_to_uuid:
                        resolved_deps.append(str(name_to_uuid[dep_ref]))
                    else:
                        # Assume it's an existing task UUID
                        try:
                            uuid.UUID(dep_ref)
                            resolved_deps.append(dep_ref)
                        except ValueError:
                            return {
                                "success": False,
                                "error": f"Unknown dependency '{dep_ref}' for task '{td['name']}'. "
                                "Must be a name in this batch or an existing task UUID.",
                            }

                if resolved_deps:
                    task.depends_on = resolved_deps
                    task.status = AgentTaskStatus.BLOCKED
                else:
                    task.depends_on = None
                    task.status = AgentTaskStatus.READY
                session.add(task)

            await session.commit()

            created_tasks = [
                {
                    "agent_task_id": str(task.agent_task_id),
                    "name": td["name"],
                    "title": task.title,
                    "status": task.status.value,
                    "depends_on": task.depends_on,
                }
                for td, task in zip(task_defs, task_objects, strict=True)
            ]

            logger.info("Created tasks with dependencies", project_id=project_id, task_count=len(created_tasks))

            return {
                "success": True,
                "project_id": project_id,
                "task_count": len(created_tasks),
                "tasks": created_tasks,
            }

    except Exception as e:
        logger.error("Error creating tasks", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="get_ready_tasks",
    description=(
        "Get all tasks in a project that are ready to be executed. Also automatically promotes "
        "PENDING/BLOCKED tasks whose dependencies are all completed to READY status."
    ),
)
@retry_db
async def get_ready_tasks(
    project_id: str,
) -> dict[str, Any]:
    """Get tasks ready for execution in a project.

    Args:
        project_id: UUID of the project

    Returns:
        Dictionary with ready tasks ordered by priority (URGENT first), then creation time
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        async with get_async_session() as session:
            # Promote eligible tasks whose deps are all completed
            candidates_result = await session.execute(
                select(AgentTask)
                .where(col(AgentTask.agent_project_id) == proj_uuid)
                .where(col(AgentTask.status).in_([AgentTaskStatus.PENDING, AgentTaskStatus.BLOCKED]))
                .where(col(AgentTask.deleted_at).is_(None))
            )
            candidates = candidates_result.scalars().all()

            promoted = 0
            for task in candidates:
                if not task.depends_on:
                    task.status = AgentTaskStatus.READY
                    session.add(task)
                    promoted += 1
                    continue

                dep_ids = [uuid.UUID(d) for d in task.depends_on]
                dep_result = await session.execute(
                    select(AgentTask.status)
                    .where(col(AgentTask.agent_task_id).in_(dep_ids))
                    .where(col(AgentTask.deleted_at).is_(None))
                )
                dep_rows = dep_result.all()
                if len(dep_rows) == len(dep_ids) and all(row.status == AgentTaskStatus.COMPLETED for row in dep_rows):
                    task.status = AgentTaskStatus.READY
                    session.add(task)
                    promoted += 1

            if promoted:
                await session.commit()

            # Fetch all READY tasks
            ready_result = await session.execute(
                select(AgentTask, col(Agent.name).label("agent_name"))
                .outerjoin(Agent, col(AgentTask.agent_id) == col(Agent.agent_id))
                .where(col(AgentTask.agent_project_id) == proj_uuid)
                .where(col(AgentTask.status) == AgentTaskStatus.READY)
                .where(col(AgentTask.deleted_at).is_(None))
                .order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
            )

            tasks = [_format_task_row(row.AgentTask, row.agent_name) for row in ready_result.all()]

            return {
                "success": True,
                "project_id": project_id,
                "promoted_count": promoted,
                "task_count": len(tasks),
                "tasks": tasks,
            }

    except Exception as e:
        logger.error("Error getting ready tasks", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="set_task_status",
    description=(
        "Update a task's status. Status values: PENDING, BLOCKED, READY, IN_PROGRESS, "
        "IN_REVIEW, COMPLETED, FAILED, CANCELLED. "
        "When a task is marked COMPLETED, dependent tasks are "
        "automatically promoted to READY if all their dependencies are met. "
        "Valid transitions: PENDING→READY/BLOCKED/CANCELLED, BLOCKED→READY/PENDING/CANCELLED, "
        "READY→IN_PROGRESS/CANCELLED/BLOCKED/PENDING, "
        "IN_PROGRESS→COMPLETED/FAILED/CANCELLED/READY/PENDING/IN_REVIEW, "
        "IN_REVIEW→COMPLETED/FAILED/CANCELLED/PENDING, FAILED→PENDING/READY, CANCELLED→PENDING. "
        "Semantic validation: READY requires all dependencies to be COMPLETED; "
        "BLOCKED requires the task to have dependencies."
    ),
)
@retry_db
async def set_task_status(
    task_id: str,
    status: str,
    result: str | None = None,
) -> dict[str, Any]:
    """Update a task's status.

    Args:
        task_id: UUID of the task to update
        status: Target status — one of PENDING, BLOCKED, READY, IN_PROGRESS, COMPLETED, FAILED, CANCELLED
        result: Optional JSON string with structured output data (typically set when COMPLETED or FAILED)

    Returns:
        Dictionary with updated status and list of any dependent tasks that became READY
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id}"}

        try:
            target_status = AgentTaskStatus(status)
        except ValueError:
            valid = [s.value for s in AgentTaskStatus]
            return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid}"}

        result_data: dict[str, Any] | None = None
        if result:
            try:
                result_data = json.loads(result)
            except json.JSONDecodeError as e:
                return {"success": False, "error": f"Invalid result JSON: {e}"}

        caller_user_id, is_admin, auth_err = await _resolve_caller_for_project_auth(auth_email)
        if auth_err or caller_user_id is None:
            return {"success": False, "error": auth_err or "Could not authorize"}

        async with get_async_session() as session:
            # Lock the row to serialize concurrent status transitions on the same task
            task_result = await session.execute(
                select(AgentTask)
                .where(col(AgentTask.agent_task_id) == task_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
                .with_for_update()
            )
            task = task_result.scalars().first()
            if not task:
                return {"success": False, "error": f"Task not found: {task_id}"}

            if not is_admin:
                project = await _load_project_for_task(session, task)
                if project is None:
                    return {"success": False, "error": f"Project not found for task: {task_id}"}
                ownership_err = _check_project_ownership(project, caller_user_id, False)
                if ownership_err:
                    return {"success": False, "error": ownership_err}

            # Validate transition and semantic constraints
            validation_error = await validate_task_status_change(session, task, target_status)
            if validation_error:
                return {"success": False, "error": validation_error}

            if target_status in TERMINAL_TASK_STATUSES:
                # Use complete_task for terminal statuses to aggregate spending
                await complete_task(session, task, target_status, result=result_data)
            else:
                # Non-terminal status change
                task.status = target_status
                if result_data is not None:
                    task.result = result_data
                # Clear completed_at when reopening a task (e.g. FAILED→READY, CANCELLED→PENDING)
                task.completed_at = None
                session.add(task)

            # Cascade: promote dependents if this task completed
            newly_ready: list[str] = []
            if target_status == AgentTaskStatus.COMPLETED:
                newly_ready = await _promote_dependents_to_ready(session, task_uuid, task.agent_project_id)

            await session.commit()

            logger.info(
                "Updated task status",
                task_id=task_id,
                status=target_status.value,
                newly_ready_count=len(newly_ready),
            )

            return {
                "success": True,
                "agent_task_id": task_id,
                "status": target_status.value,
                "newly_ready_tasks": newly_ready,
            }

    except Exception as e:
        logger.error("Error setting task status", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="restart_task",
    description=(
        "Restart a task by resetting it to READY (or PENDING if it has unmet dependencies). "
        "Clears the task's result, completed_at, actual_spending_usd, and assigned_session_ids. "
        "Works from any status. Use this when a task needs to be re-executed from scratch."
    ),
)
@retry_db
async def restart_task(
    task_id: str,
) -> dict[str, Any]:
    """Restart a task, clearing all execution state.

    Args:
        task_id: UUID of the task to restart

    Returns:
        Dictionary with the reset task details
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id}"}

        async with get_async_session() as session:
            task_result = await session.execute(
                select(AgentTask)
                .where(col(AgentTask.agent_task_id) == task_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
                .with_for_update()
            )
            task = task_result.scalars().first()
            if not task:
                return {"success": False, "error": f"Task not found: {task_id}"}

            old_status = task.status.value

            # Clear execution state
            task.result = None
            task.completed_at = None
            task.actual_spending_usd = None
            task.assigned_session_ids = None

            # Set to READY if deps are met, otherwise PENDING
            if await are_dependencies_completed(session, task):
                task.status = AgentTaskStatus.READY
            else:
                task.status = AgentTaskStatus.PENDING

            session.add(task)
            await session.commit()

            agent_name = await _resolve_agent_name_by_id(session, task.agent_id) if task.agent_id else None

            logger.info(
                "Restarted task",
                task_id=task_id,
                old_status=old_status,
                new_status=task.status.value,
            )

            return {
                "success": True,
                "task": _format_task_row(task, agent_name),
            }

    except Exception as e:
        logger.error("Error restarting task", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="set_project_status",
    description=(
        "Update a project's status. Status values: ACTIVE, PAUSED, COMPLETED, ARCHIVED. "
        "Returns a task summary showing how many tasks are in each state. "
        "New projects start as PAUSED; set to ACTIVE to begin task execution."
    ),
)
@retry_db
async def set_project_status(
    project_id: str,
    status: str,
) -> dict[str, Any]:
    """Update a project's status.

    Args:
        project_id: UUID of the project to update
        status: Target status — one of ACTIVE, PAUSED, COMPLETED, ARCHIVED

    Returns:
        Dictionary with updated status and task_summary (counts per task status)
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        try:
            target_status = AgentProjectStatus(status)
        except ValueError:
            valid = [s.value for s in AgentProjectStatus]
            return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid}"}

        caller_user_id, is_admin, auth_err = await _resolve_caller_for_project_auth(auth_email)
        if auth_err or caller_user_id is None:
            return {"success": False, "error": auth_err or "Could not authorize"}

        async with get_async_session() as session:
            proj_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            project = proj_result.scalars().first()
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            ownership_err = _check_project_ownership(project, caller_user_id, is_admin)
            if ownership_err:
                return {"success": False, "error": ownership_err}

            project.status = target_status
            session.add(project)

            task_summary = await _get_task_status_summary(session, proj_uuid)

            await session.commit()

            logger.info("Updated project status", project_id=project_id, status=target_status.value)

            return {
                "success": True,
                "agent_project_id": project_id,
                "status": target_status.value,
                "task_summary": task_summary,
            }

    except Exception as e:
        logger.error("Error setting project status", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="get_project",
    description=(
        "Get project details by ID or name. Includes a task_summary with counts per task status. "
        "If searching by name, returns all matching projects (names are not unique)."
    ),
)
@retry_db
async def get_project(
    project_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Get project details.

    Args:
        project_id: UUID of the project (use this for exact lookup)
        name: Project name to search for (returns all matches, newest first)
            Provide exactly one of project_id or name.

    Returns:
        Dictionary with project details and task_summary
    """
    try:
        if not project_id and not name:
            return {"success": False, "error": "Must provide either project_id or name"}

        async with get_async_session_read_replica() as session:
            if project_id:
                try:
                    proj_uuid = uuid.UUID(project_id)
                except ValueError:
                    return {"success": False, "error": f"Invalid project_id: {project_id}"}

                result = await session.execute(
                    select(AgentProject)
                    .where(col(AgentProject.agent_project_id) == proj_uuid)
                    .where(col(AgentProject.deleted_at).is_(None))
                )
                project = result.scalars().first()
                if not project:
                    return {"success": False, "error": f"Project not found: {project_id}"}

                task_summary = await _get_task_status_summary(session, proj_uuid)
                default_agent_name = None
                if project.default_agent_id:
                    default_agent_name = await _resolve_agent_name_by_id(session, project.default_agent_id)

                return {
                    "success": True,
                    "project": _format_project(project, default_agent_name),
                    "task_summary": task_summary,
                }

            # Search by name
            result = await session.execute(
                select(AgentProject)
                .where(AgentProject.name == name)
                .where(col(AgentProject.deleted_at).is_(None))
                .order_by(col(AgentProject.created_at).desc())
            )
            projects = result.scalars().all()
            if not projects:
                return {"success": False, "error": f"No projects found with name: {name}"}

            # Batch-resolve agent names to avoid N+1 queries
            agent_ids = {p.default_agent_id for p in projects if p.default_agent_id}
            agent_names_map = await _batch_resolve_agent_names(session, agent_ids)

            project_list = []
            for proj in projects:
                task_summary = await _get_task_status_summary(session, proj.agent_project_id)
                default_agent_name = agent_names_map.get(proj.default_agent_id) if proj.default_agent_id else None
                project_list.append(
                    {
                        **_format_project(proj, default_agent_name),
                        "task_summary": task_summary,
                    }
                )

            return {
                "success": True,
                "count": len(project_list),
                "projects": project_list,
            }

    except Exception as e:
        logger.error("Error getting project", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="get_task",
    description=(
        "Get task details by ID or title. If searching by title, returns all matches "
        "(optionally scoped to a project). Includes dependency info, result data, and timestamps."
    ),
)
@retry_db
async def get_task(
    task_id: str | None = None,
    title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Get task details.

    Args:
        task_id: UUID of the task (use this for exact lookup)
        title: Task title to search for (returns all matches, newest first)
            Provide exactly one of task_id or title.
        project_id: Optional UUID to scope a title search to a specific project

    Returns:
        Dictionary with task details including status, dependencies, and result
    """
    try:
        if not task_id and not title:
            return {"success": False, "error": "Must provide either task_id or title"}

        async with get_async_session_read_replica() as session:
            base_query: Any = (
                select(AgentTask, col(Agent.name).label("agent_name"))
                .outerjoin(Agent, col(AgentTask.agent_id) == col(Agent.agent_id))
                .where(col(AgentTask.deleted_at).is_(None))
            )

            if task_id:
                try:
                    task_uuid = uuid.UUID(task_id)
                except ValueError:
                    return {"success": False, "error": f"Invalid task_id: {task_id}"}

                result = await session.execute(base_query.where(col(AgentTask.agent_task_id) == task_uuid))
                row = result.first()
                if not row:
                    return {"success": False, "error": f"Task not found: {task_id}"}

                return {
                    "success": True,
                    "task": _format_task_row(row.AgentTask, row.agent_name),
                }

            # Search by title
            query = base_query.where(AgentTask.title == title)
            if project_id:
                try:
                    proj_uuid = uuid.UUID(project_id)
                except ValueError:
                    return {"success": False, "error": f"Invalid project_id: {project_id}"}
                query = query.where(col(AgentTask.agent_project_id) == proj_uuid)

            query = query.order_by(col(AgentTask.created_at).desc())
            result = await session.execute(query)
            rows = result.all()
            if not rows:
                return {"success": False, "error": f"No tasks found with title: {title}"}

            return {
                "success": True,
                "count": len(rows),
                "tasks": [_format_task_row(row.AgentTask, row.agent_name) for row in rows],
            }

    except Exception as e:
        logger.error("Error getting task", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="get_project_tasks",
    description=(
        "Get all tasks in a project with optional status filter. "
        "Filter values: PENDING, BLOCKED, READY, IN_PROGRESS, IN_REVIEW, COMPLETED, FAILED, CANCELLED. "
        "Returns the full task list ordered by priority and a summary of counts by status."
    ),
)
@retry_db
async def get_project_tasks(
    project_id: str,
    status: str | None = None,
) -> dict[str, Any]:
    """Get all tasks for a project.

    Args:
        project_id: UUID of the project
        status: Optional filter — one of PENDING, BLOCKED, READY, IN_PROGRESS, COMPLETED, FAILED, CANCELLED

    Returns:
        Dictionary with tasks (ordered by priority) and task_summary (counts per status)
    """
    try:
        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        status_filter: AgentTaskStatus | None = None
        if status:
            try:
                status_filter = AgentTaskStatus(status)
            except ValueError:
                valid = [s.value for s in AgentTaskStatus]
                return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid}"}

        async with get_async_session_read_replica() as session:
            query: Any = (
                select(AgentTask, col(Agent.name).label("agent_name"))
                .outerjoin(Agent, col(AgentTask.agent_id) == col(Agent.agent_id))
                .where(col(AgentTask.agent_project_id) == proj_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
            )
            if status_filter:
                query = query.where(col(AgentTask.status) == status_filter)

            query = query.order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
            result = await session.execute(query)
            rows = result.all()

            tasks = [_format_task_row(row.AgentTask, row.agent_name) for row in rows]
            task_summary = await _get_task_status_summary(session, proj_uuid)

            return {
                "success": True,
                "project_id": project_id,
                "task_count": len(tasks),
                "tasks": tasks,
                "task_summary": task_summary,
            }

    except Exception as e:
        logger.error("Error getting project tasks", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="list_projects",
    description=(
        "List projects with optional filters. Filter values: ACTIVE, PAUSED, COMPLETED, ARCHIVED. "
        "Returns projects sorted by most recently created. "
        "Use this to discover existing projects when starting a new session."
    ),
)
@retry_db
async def list_projects(
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List projects with optional status filter.

    Args:
        status: Optional filter — one of ACTIVE, PAUSED, COMPLETED, ARCHIVED
        limit: Maximum number of projects to return (default 20, max 100)

    Returns:
        Dictionary with matching projects and their task summaries
    """
    try:
        status_filter: AgentProjectStatus | None = None
        if status:
            try:
                status_filter = AgentProjectStatus(status)
            except ValueError:
                valid = [s.value for s in AgentProjectStatus]
                return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid}"}

        limit = min(max(limit, 1), 100)

        async with get_async_session_read_replica() as session:
            query: Any = select(AgentProject).where(col(AgentProject.deleted_at).is_(None))
            if status_filter:
                query = query.where(col(AgentProject.status) == status_filter)
            query = query.order_by(col(AgentProject.created_at).desc()).limit(limit)

            result = await session.execute(query)
            projects = result.scalars().all()

            # Batch-resolve agent names to avoid N+1 queries
            agent_ids = {p.default_agent_id for p in projects if p.default_agent_id}
            agent_names_map = await _batch_resolve_agent_names(session, agent_ids)

            project_list = []
            for proj in projects:
                task_summary = await _get_task_status_summary(session, proj.agent_project_id)
                default_agent_name = agent_names_map.get(proj.default_agent_id) if proj.default_agent_id else None
                project_list.append(
                    {
                        **_format_project(proj, default_agent_name),
                        "task_summary": task_summary,
                    }
                )

            return {
                "success": True,
                "count": len(project_list),
                "projects": project_list,
            }

    except Exception as e:
        logger.error("Error listing projects", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="update_task",
    description=(
        "Update a task's mutable fields (title, description, priority, agent, task_data, estimated_effort). "
        "Priority values: URGENT, HIGH, NORMAL, LOW. "
        "Only provided fields are updated; omitted fields are left unchanged. "
        "Cannot update status — use set_task_status for that. "
        "Cannot update dependencies — use set_task_dependencies for that."
    ),
)
@retry_db
async def update_task(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    agent_name: str | None = None,
    task_data: str | None = None,
    estimated_effort: str | None = None,
) -> dict[str, Any]:
    """Update a task's fields.

    Args:
        task_id: UUID of the task to update
        title: New title (if provided)
        description: New description / instructions (if provided)
        priority: New priority — URGENT, HIGH, NORMAL, LOW (if provided)
        agent_name: New agent assignment by name (if provided). Use empty string to unassign.
        task_data: New task_data as JSON string (if provided). Merged with existing data.
        estimated_effort: New effort estimate, e.g. "small", "medium", "large" (if provided)

    Returns:
        Dictionary with updated task details
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id}"}

        caller_user_id, is_admin, auth_err = await _resolve_caller_for_project_auth(auth_email)
        if auth_err or caller_user_id is None:
            return {"success": False, "error": auth_err or "Could not authorize"}

        async with get_async_session() as session:
            task_result = await session.execute(
                select(AgentTask)
                .where(col(AgentTask.agent_task_id) == task_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
                .with_for_update()
            )
            task = task_result.scalars().first()
            if not task:
                return {"success": False, "error": f"Task not found: {task_id}"}

            if not is_admin:
                # Owner check: load the project and compare creator_user_id.
                project = await _load_project_for_task(session, task)
                if project is None:
                    return {"success": False, "error": f"Project not found for task: {task_id}"}
                ownership_err = _check_project_ownership(project, caller_user_id, False)
                if ownership_err:
                    return {"success": False, "error": ownership_err}

            if title is not None:
                task.title = title
            if description is not None:
                task.description = description
            if estimated_effort is not None:
                task.estimated_effort = estimated_effort

            if priority is not None:
                parsed_priority, err = _parse_priority(priority, "")
                if err:
                    return err
                task.priority = parsed_priority

            if agent_name is not None:
                if agent_name == "":
                    task.agent_id = None
                else:
                    agent_id = await _resolve_agent_id_by_name(session, agent_name)
                    if agent_id is None:
                        return {"success": False, "error": f"Agent not found: {agent_name}"}
                    task.agent_id = agent_id

            if task_data is not None:
                try:
                    new_data = json.loads(task_data)
                except json.JSONDecodeError as e:
                    return {"success": False, "error": f"Invalid task_data JSON: {e}"}
                # Merge with existing task_data
                if task.task_data:
                    merged = {**task.task_data, **new_data}
                else:
                    merged = new_data
                task.task_data = merged

            session.add(task)
            await session.commit()
            await session.refresh(task)

            # Resolve agent name for response
            resolved_agent_name: str | None = None
            if task.agent_id:
                agent_result = await session.execute(
                    select(Agent.name).where(col(Agent.agent_id) == task.agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
                )
                row = agent_result.first()
                resolved_agent_name = row.name if row else None

            logger.info("Updated task", task_id=task_id)

            return {
                "success": True,
                "task": _format_task_row(task, resolved_agent_name),
            }

    except Exception as e:
        logger.error("Error updating task", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="set_task_dependencies",
    description=(
        "Set or clear a task's dependency list. Replaces the entire depends_on list with the "
        "provided task IDs. Pass an empty array to remove all dependencies. "
        "Automatically updates the task's status: BLOCKED if it has unmet dependencies, "
        "READY if all dependencies are COMPLETED (or none remain). "
        "All referenced dependency tasks must exist in the same project."
    ),
)
@retry_db
async def set_task_dependencies(
    task_id: str,
    depends_on: str,
) -> dict[str, Any]:
    """Set a task's dependencies.

    Args:
        task_id: UUID of the task to update
        depends_on: JSON array of task ID strings that this task depends on.
            Example: '["uuid-1", "uuid-2"]'. Pass '[]' to clear all dependencies.

    Returns:
        Dictionary with updated task details including new depends_on and status
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id}"}

        try:
            dep_ids_raw = json.loads(depends_on)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid depends_on JSON: {e}"}

        if not isinstance(dep_ids_raw, list):
            return {"success": False, "error": "depends_on must be a JSON array of task ID strings"}

        # Validate all IDs are valid UUIDs
        dep_uuids: list[uuid.UUID] = []
        for dep_str in dep_ids_raw:
            if not isinstance(dep_str, str):
                return {"success": False, "error": f"Each dependency must be a string UUID, got: {dep_str}"}
            try:
                dep_uuids.append(uuid.UUID(dep_str))
            except ValueError:
                return {"success": False, "error": f"Invalid dependency UUID: {dep_str}"}

        # Prevent self-dependency
        if task_uuid in dep_uuids:
            return {"success": False, "error": "A task cannot depend on itself"}

        async with get_async_session() as session:
            # Lock the task
            task_result = await session.execute(
                select(AgentTask)
                .where(col(AgentTask.agent_task_id) == task_uuid)
                .where(col(AgentTask.deleted_at).is_(None))
                .with_for_update()
            )
            task = task_result.scalars().first()
            if not task:
                return {"success": False, "error": f"Task not found: {task_id}"}

            # Verify all dependency tasks exist in the same project
            if dep_uuids:
                existing_result = await session.execute(
                    select(AgentTask.agent_task_id, AgentTask.status)
                    .where(col(AgentTask.agent_task_id).in_(dep_uuids))
                    .where(col(AgentTask.agent_project_id) == task.agent_project_id)
                    .where(col(AgentTask.deleted_at).is_(None))
                )
                existing_rows = list(existing_result.all())
                existing_ids = {row.agent_task_id for row in existing_rows}
                missing = [str(d) for d in dep_uuids if d not in existing_ids]
                if missing:
                    return {
                        "success": False,
                        "error": f"Dependency tasks not found in project: {missing}",
                    }

                # Cycle detection: check if any dep transitively depends on this task
                all_project_tasks = await session.execute(
                    select(AgentTask.agent_task_id, AgentTask.depends_on)
                    .where(col(AgentTask.agent_project_id) == task.agent_project_id)
                    .where(col(AgentTask.deleted_at).is_(None))
                )
                # Build adjacency: task -> deps (what it depends on)
                dep_graph: dict[str, list[str]] = {}
                for row in all_project_tasks.all():
                    dep_graph[str(row.agent_task_id)] = row.depends_on or []
                # Apply proposed change
                dep_graph[task_id] = [str(d) for d in dep_uuids]

                # DFS to check if task_id is reachable from itself
                def _has_cycle(start: str) -> bool:
                    visited: set[str] = set()
                    stack = list(dep_graph.get(start, []))
                    while stack:
                        node = stack.pop()
                        if node == start:
                            return True
                        if node in visited:
                            continue
                        visited.add(node)
                        stack.extend(dep_graph.get(node, []))
                    return False

                if _has_cycle(task_id):
                    return {
                        "success": False,
                        "error": "Setting these dependencies would create a cycle",
                    }

                # Auto-update status based on dependency state
                all_completed = all(row.status == AgentTaskStatus.COMPLETED for row in existing_rows)
            else:
                all_completed = True  # No deps = all met

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

            # Resolve agent name for response
            resolved_agent_name: str | None = None
            if task.agent_id:
                agent_result = await session.execute(
                    select(Agent.name).where(col(Agent.agent_id) == task.agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
                )
                agent_row = agent_result.first()
                resolved_agent_name = agent_row.name if agent_row else None

            logger.info(
                "Updated task dependencies",
                task_id=task_id,
                dep_count=len(dep_uuids),
                new_status=task.status.value,
            )

            return {
                "success": True,
                "task": _format_task_row(task, resolved_agent_name),
            }

    except Exception as e:
        logger.error("Error updating task dependencies", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="update_project",
    description=(
        "Update a project's mutable fields (name, description, slack_channel, default_agent_name, "
        "budget_usd, project_data). "
        "Only provided fields are updated. Cannot update status — use set_project_status for that."
    ),
)
@retry_db
async def update_project(
    project_id: str,
    name: str | None = None,
    description: str | None = None,
    slack_channel: str | None = None,
    default_agent_name: str | None = None,
    budget_usd: float | None = None,
    project_data: str | None = None,
) -> dict[str, Any]:
    """Update a project's fields.

    Args:
        project_id: UUID of the project to update
        name: New project name (if provided)
        description: New description (if provided)
        slack_channel: Plain channel name (no "#" prefix, no ID). Use empty string to clear.
        default_agent_name: New default agent by name (if provided). Use empty string to clear.
        budget_usd: New budget cap in USD (if provided). Use 0 to clear.
        project_data: New project-level data as JSON string (if provided). Merged with existing data.

    Returns:
        Dictionary with updated project details
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        caller_user_id, is_admin, auth_err = await _resolve_caller_for_project_auth(auth_email)
        if auth_err or caller_user_id is None:
            return {"success": False, "error": auth_err or "Could not authorize"}

        async with get_async_session() as session:
            proj_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            project = proj_result.scalars().first()
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}
            ownership_err = _check_project_ownership(project, caller_user_id, is_admin)
            if ownership_err:
                return {"success": False, "error": ownership_err}

            if name is not None:
                project.name = name
            if description is not None:
                project.description = description
            if slack_channel is not None:
                project.slack_channel = slack_channel or None
            if default_agent_name is not None:
                if default_agent_name:
                    agent_id = await _resolve_agent_id_by_name(session, default_agent_name)
                    if not agent_id:
                        return {"success": False, "error": f"Agent not found: {default_agent_name}"}
                    project.default_agent_id = agent_id
                else:
                    project.default_agent_id = None
            if budget_usd is not None:
                if budget_usd < 0:
                    return {"success": False, "error": "budget_usd must be non-negative (use 0 to clear)"}
                project.budget_usd = Decimal(str(budget_usd)) if budget_usd > 0 else None
            if project_data is not None:
                try:
                    new_data = json.loads(project_data)
                except json.JSONDecodeError as e:
                    return {"success": False, "error": f"Invalid project_data JSON: {e}"}
                if not isinstance(new_data, dict):
                    return {
                        "success": False,
                        "error": f"project_data must be a JSON object (dict), not {type(new_data).__name__}",
                    }
                if project.project_data:
                    merged = {**project.project_data, **new_data}
                else:
                    merged = new_data
                project.project_data = merged

            session.add(project)
            await session.commit()
            await session.refresh(project)

            task_summary = await _get_task_status_summary(session, proj_uuid)

            # Resolve agent name for response - avoid extra DB call if we already know it
            resolved_agent_name: str | None = None
            if default_agent_name is not None:
                # If agent name was provided in this update, reuse it (or None if clearing)
                resolved_agent_name = default_agent_name or None
            elif project.default_agent_id:
                # Agent name wasn't updated, look it up from existing ID
                resolved_agent_name = await _resolve_agent_name_by_id(session, project.default_agent_id)

            logger.info("Updated project", project_id=project_id)

            return {
                "success": True,
                "project": _format_project(project, resolved_agent_name),
                "task_summary": task_summary,
            }

    except Exception as e:
        logger.error("Error updating project", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="claim_task",
    description=(
        "Atomically claim a READY task by transitioning it to IN_PROGRESS. "
        "If task_id is provided, claims that specific task (fails if not READY). "
        "If only project_id is provided, claims the highest-priority READY task. "
        "Returns the claimed task details or an error if no task is available."
    ),
)
@retry_db
async def claim_task(
    project_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Atomically claim a task for execution.

    Args:
        project_id: UUID of the project
        task_id: UUID of a specific task to claim (optional — if omitted, claims highest-priority READY task)

    Returns:
        Dictionary with claimed task details, or error if no READY task available
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        task_uuid: uuid.UUID | None = None
        if task_id:
            try:
                task_uuid = uuid.UUID(task_id)
            except ValueError:
                return {"success": False, "error": f"Invalid task_id: {task_id}"}

        async with get_async_session() as session:
            if task_uuid:
                # Claim specific task with row-level lock
                task_result = await session.execute(
                    select(AgentTask)
                    .where(col(AgentTask.agent_task_id) == task_uuid)
                    .where(col(AgentTask.agent_project_id) == proj_uuid)
                    .where(col(AgentTask.status) == AgentTaskStatus.READY)
                    .where(col(AgentTask.deleted_at).is_(None))
                    .with_for_update(skip_locked=True)
                )
                task = task_result.scalars().first()
                if not task:
                    return {
                        "success": False,
                        "error": f"Task {task_id} is not READY or does not exist in project {project_id}",
                    }
            else:
                # Claim highest-priority READY task with row-level lock
                task_result = await session.execute(
                    select(AgentTask)
                    .where(col(AgentTask.agent_project_id) == proj_uuid)
                    .where(col(AgentTask.status) == AgentTaskStatus.READY)
                    .where(col(AgentTask.deleted_at).is_(None))
                    .order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                task = task_result.scalars().first()
                if not task:
                    return {
                        "success": False,
                        "error": "No READY tasks available in this project",
                    }

            task.status = AgentTaskStatus.IN_PROGRESS
            session.add(task)
            await session.commit()
            await session.refresh(task)

            # Resolve agent name for response
            resolved_agent_name: str | None = None
            if task.agent_id:
                agent_result = await session.execute(
                    select(Agent.name).where(col(Agent.agent_id) == task.agent_id).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
                )
                row = agent_result.first()
                resolved_agent_name = row.name if row else None

            logger.info("Claimed task", task_id=str(task.agent_task_id), project_id=project_id)

            return {
                "success": True,
                "task": _format_task_row(task, resolved_agent_name),
            }

    except Exception as e:
        logger.error("Error claiming task", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="get_project_state",
    description=(
        "Read the project's shared state — a key-value store accessible to all tasks and sessions. "
        "If key is provided, returns just that value. Otherwise returns the entire state dict."
    ),
)
@retry_db
async def get_project_state(
    project_id: str,
    key: str | None = None,
) -> dict[str, Any]:
    """Read project shared state.

    Args:
        project_id: UUID of the project
        key: Optional specific key to read (returns just that value)

    Returns:
        Dictionary with the shared state (full dict or single key's value)
    """
    try:
        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        async with get_async_session_read_replica() as session:
            proj_result = await session.execute(
                select(AgentProject.shared_state)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            row = proj_result.first()
            if not row:
                return {"success": False, "error": f"Project not found: {project_id}"}

            state = row.shared_state or {}

            if key is not None:
                if key not in state:
                    return {"success": True, "key": key, "value": None, "exists": False}
                return {"success": True, "key": key, "value": state[key], "exists": True}

            return {"success": True, "state": state}

    except Exception as e:
        logger.error("Error getting project state", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="set_project_state",
    description=(
        "Write to the project's shared state. Sets a single key-value pair, "
        "merging with existing state. Use this for cross-session coordination — "
        "e.g. tracking which items have been processed, storing intermediate results."
    ),
)
@retry_db
async def set_project_state(
    project_id: str,
    key: str,
    value: str,
) -> dict[str, Any]:
    """Write a key-value pair to project shared state.

    Args:
        project_id: UUID of the project
        key: State key to set
        value: Value as JSON string (will be parsed and stored as structured data).
            For simple strings, wrap in quotes: '"hello"'. For objects: '{"count": 5}'.

    Returns:
        Dictionary with the updated full state
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id}"}

        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid value JSON: {e}. Wrap strings in quotes."}

        async with get_async_session() as session:
            # Use FOR UPDATE to prevent concurrent read-modify-write races
            proj_result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
                .with_for_update()
            )
            project = proj_result.scalars().first()
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}

            state = dict(project.shared_state) if project.shared_state else {}
            state[key] = parsed_value
            project.shared_state = state
            session.add(project)
            await session.commit()

            logger.info("Updated project state", project_id=project_id, key=key)

            return {
                "success": True,
                "key": key,
                "state": state,
            }

    except Exception as e:
        logger.error("Error setting project state", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="resume_failed_task",
    description=(
        "Resume a failed task that can be retried. The task must be in FAILED status with a "
        "resumable error type (hit turn limit). This resets the task to READY and marks it "
        "to resume the previous session instead of starting fresh. "
        "Resumable error types: error_max_turns."
    ),
)
@retry_db
async def resume_failed_task(
    task_id: str,
) -> dict[str, Any]:
    """Resume a failed task by resetting it to READY with session resumption.

    Args:
        task_id: UUID of the failed task to resume

    Returns:
        Dictionary with success status and session resumption details
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id}"}

        # Import here to avoid circular dependency
        from ypl.agent_harness_service.task_executor import resume_task

        result = await resume_task(task_uuid)

        if result.get("success"):
            logger.info(
                "Resumed failed task",
                task_id=task_id,
                session_to_resume=result.get("session_to_resume"),
            )
        else:
            logger.warning(
                "Failed to resume task",
                task_id=task_id,
                error=result.get("error"),
            )

        return result

    except Exception as e:
        logger.error("Error resuming failed task", exc_info=True)
        return {"success": False, "error": str(e)}
