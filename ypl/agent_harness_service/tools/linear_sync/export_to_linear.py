"""Export an AHS project (and its tasks) to Linear as a project + issues.

This module is the business-logic layer for the AHS → Linear push direction.
It can be invoked from two entry points:

1. **Agent-initiated (MCP)**: Called from the yuppster MCP server tool
   ``export_project_to_linear`` (``ypl/mcp_server/tools/linear_sync.py``),
   allowing agents to explicitly trigger a sync when needed.

2. **System-level (scheduler)**: Called from a periodic scheduler loop that
   automatically syncs projects based on their configuration (e.g., projects
   with ``linear_sync_enabled=True`` in ``project_data``).

Design
------
All AHS persistence (reads and writes) is done through direct async
SQLAlchemy queries — the same pattern used by
``ypl/mcp_server/tools/project_tasks.py``.

Linear API calls are made via :class:`~ypl.backend.utils.linear.LinearClient`
(the same client used by the harness MCP server Linear tools).  All
LinearClient calls are dispatched via :func:`asyncio.to_thread` since the
client is synchronous.

Idempotency
-----------
If ``project_data`` already contains a ``linear_project_id`` the project
creation step is skipped and the existing Linear project is reused.  Likewise
for tasks: if ``task_data`` already contains a ``linear_issue_id`` the issue
is updated (via :meth:`~.LinearClient.update_issue`) rather than duplicated.

Topological order
-----------------
Tasks are emitted to Linear in an order that ensures:

1. A parent task is created before any of its children (so the
   ``parentId`` field can be set immediately).
2. A dependency (blocker) task is created before any task that lists it in
   ``depends_on`` (so ``blocked_by`` relations can be set immediately).

Cycles in either graph are broken gracefully: the offending back-edge is
simply ignored and a warning is logged.
"""

from __future__ import annotations
import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select

from ypl.agent_harness_service.tools.linear_sync.mapping import (
    map_ahs_priority_to_linear,
    map_ahs_status_to_linear,
)
from ypl.agent_harness_service.tools.linear_sync.types import (
    LinearIssueRef,
    LinearProjectRef,
    SyncResult,
)
from ypl.backend.config import settings
from ypl.backend.db import get_async_session, retry_db
from ypl.backend.llm.constants import EMAIL_TO_LINEAR_NAME
from ypl.backend.utils.linear import LinearClient
from ypl.db.agent_harness import AgentProject, AgentTask
from ypl.db.users import User
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# User mapping helpers
# ---------------------------------------------------------------------------


async def _resolve_creator_linear_user_id(
    project: AgentProject,
    client: LinearClient,
) -> str | None:
    """Resolve the Linear user ID for the AHS project creator.

    Uses the TEAM_DIRECTORY mapping from constants to convert the creator's
    email → Linear name, then fetches Linear users to find the matching ID.

    Args:
        project: The AHS project record.
        client: LinearClient instance to fetch users.

    Returns:
        The Linear user ID if found, or None if the creator cannot be mapped.
    """
    if not project.creator_user_id:
        logger.info("Project has no creator_user_id, skipping assignee mapping")
        return None

    # Fetch creator's email from database (best-effort)
    try:
        async with get_async_session() as session:
            result = await session.execute(select(User).where(col(User.user_id) == project.creator_user_id))
            user = result.scalars().first()
            if not user or not user.email:
                logger.warning("Creator user not found or has no email", user_id=project.creator_user_id)
                return None
            creator_email = user.email
    except Exception as e:
        logger.warning("Failed to fetch creator from DB, skipping assignee mapping", error=str(e))
        return None

    # Map email → Linear name
    linear_name = EMAIL_TO_LINEAR_NAME.get(creator_email)
    if not linear_name:
        logger.warning("No Linear name mapping for creator email", email=creator_email)
        return None

    # Fetch Linear users and find by name (best-effort: API failure should not crash export)
    try:
        users_response = await asyncio.to_thread(client.get_all_users)
        users_data = (users_response.get("data") or {}).get("users", {}).get("nodes", [])

        for linear_user in users_data:
            # Match by name (case-insensitive) or displayName
            user_name = (linear_user.get("name") or "").lower()
            display_name = (linear_user.get("displayName") or "").lower()
            if linear_name.lower() in (user_name, display_name):
                linear_user_id = linear_user.get("id")
                logger.info(
                    "Resolved creator to Linear user",
                    creator_email=creator_email,
                    linear_name=linear_name,
                    linear_user_id=linear_user_id,
                )
                return str(linear_user_id) if linear_user_id else None

        logger.warning("Linear user not found by name", linear_name=linear_name)
    except Exception as e:
        logger.warning("Failed to fetch Linear users, skipping assignee mapping", error=str(e))
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def export_project_to_linear(
    project_id: str,
    linear_team_id: str,
    linear_project_name: str | None = None,
) -> tuple[str, SyncResult]:
    """Push an AHS project and all its tasks to Linear.

    Creates (or reuses) a Linear project and creates/updates a Linear issue
    for every AHS task in topological order — parents before children, blockers
    before the tasks they block.

    After a successful push, ``project_data`` on the AHS project record is
    updated with a :class:`~.types.LinearProjectRef`, and each task's
    ``task_data`` is updated with a :class:`~.types.LinearIssueRef`.

    Args:
        project_id: UUID of the AHS project to export.
        linear_team_id: UUID of the Linear team that will own the new project.
        linear_project_name: Override for the Linear project display name.
            Defaults to the AHS project name.

    Returns:
        ``(linear_project_id, SyncResult)`` — the Linear project UUID and
        per-item outcome counts.

    Raises:
        ValueError: If the AHS project is not found.
        RuntimeError: If a Linear API call fails.
    """
    result = SyncResult()
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # 1. Fetch AHS project
    # ------------------------------------------------------------------
    project = await _fetch_project(project_id)
    if project is None:
        raise ValueError(f"AHS project not found: {project_id}")

    name = linear_project_name or project.name
    logger.info(
        "Starting AHS→Linear export",
        project_id=project_id,
        project_name=name,
        linear_team_id=linear_team_id,
    )

    # ------------------------------------------------------------------
    # 2. Create or reuse the Linear project
    # ------------------------------------------------------------------
    existing_project_ref = _extract_project_ref(project)
    if existing_project_ref:
        linear_project_id = existing_project_ref.linear_project_id
        logger.info("Reusing existing Linear project", linear_project_id=linear_project_id)
    else:
        linear_project_id = await _create_linear_project(
            name=name,
            linear_team_id=linear_team_id,
        )
        project_ref = LinearProjectRef(
            linear_project_id=linear_project_id,
            linear_team_id=linear_team_id,
            last_synced_at=now,
        )
        await _save_project_ref(project_id, project_ref)
        logger.info("Created Linear project", linear_project_id=linear_project_id)

    # ------------------------------------------------------------------
    # 3. Fetch workflow states for status mapping (best-effort)
    # ------------------------------------------------------------------
    client = LinearClient()
    workflow_states: list[dict[str, str]] = []
    try:
        states_response = await asyncio.to_thread(client.get_workflow_states, linear_team_id)
        gql_errors = states_response.get("errors")
        if gql_errors:
            logger.warning(
                "Failed to fetch workflow states, status mapping will be skipped",
                error=gql_errors[0].get("message", str(gql_errors)),
            )
        else:
            workflow_states = (states_response.get("data") or {}).get("workflowStates", {}).get("nodes", [])
            logger.info("Fetched workflow states", team_id=linear_team_id, state_count=len(workflow_states))
    except Exception as e:
        logger.warning("Failed to fetch workflow states, status mapping will be skipped", error=str(e))

    # ------------------------------------------------------------------
    # 4. Resolve creator's Linear user ID for issue assignment
    # ------------------------------------------------------------------
    creator_linear_user_id = await _resolve_creator_linear_user_id(project, client)

    # ------------------------------------------------------------------
    # 5. Fetch all tasks and sort topologically
    # ------------------------------------------------------------------
    tasks = await _fetch_tasks(project_id)
    ordered_tasks = _topological_sort(tasks)

    # Running map from AHS task UUID string → Linear issue UUID string.
    # Populated as we create each issue so that parentId / blocked_by can
    # be resolved for later tasks in the sequence.
    task_id_to_linear_id: dict[str, str] = {}

    # Pre-populate from tasks that were previously exported.
    for task in tasks:
        ref = _extract_issue_ref(task)
        if ref:
            task_id_to_linear_id[str(task.agent_task_id)] = ref.linear_issue_id

    # ------------------------------------------------------------------
    # 6. Create / update issues in topological order
    # ------------------------------------------------------------------
    for task in ordered_tasks:
        task_id_str = str(task.agent_task_id)
        try:
            existing_ref = _extract_issue_ref(task)
            linear_issue_id, linear_identifier = await _upsert_linear_issue(
                task=task,
                linear_project_id=linear_project_id,
                linear_team_id=linear_team_id,
                task_id_to_linear_id=task_id_to_linear_id,
                workflow_states=workflow_states,
                assignee_id=creator_linear_user_id,
                existing_issue_id=existing_ref.linear_issue_id if existing_ref else None,
            )
            task_id_to_linear_id[task_id_str] = linear_issue_id

            issue_ref = LinearIssueRef(
                linear_issue_id=linear_issue_id,
                linear_identifier=linear_identifier,
                last_synced_at=now,
            )
            await _save_issue_ref(task_id_str, issue_ref)

            # Sync PR and session links as attachments (best-effort)
            try:
                await sync_task_links_to_linear(task, linear_issue_id)
            except Exception:
                logger.warning("Failed to sync links for task", task_id=task_id_str, exc_info=True)

            if existing_ref:
                result.updated += 1
            else:
                result.created += 1

        except Exception as exc:
            logger.warning(
                "Failed to export task to Linear",
                task_id=task_id_str,
                error=str(exc),
                exc_info=True,
            )
            result.errors += 1

    logger.info(
        "AHS→Linear export complete",
        project_id=project_id,
        linear_project_id=linear_project_id,
        created=result.created,
        updated=result.updated,
        errors=result.errors,
    )
    return linear_project_id, result


async def sync_tasks_to_linear(
    project_id: str,
) -> SyncResult:
    """Push AHS task updates to their linked Linear issues.

    For each task that already has a ``linear_issue_id`` in ``task_data``,
    calls :meth:`~.LinearClient.update_issue` to push title, description,
    and priority changes.  Tasks without a ``linear_issue_id`` are created as
    new issues (same as calling :func:`export_project_to_linear` for the first
    time).

    Args:
        project_id: UUID of the AHS project to sync.

    Returns:
        :class:`~.types.SyncResult` with per-item outcome counts.

    Raises:
        ValueError: If the project has not been exported yet (no
            ``linear_project_id`` in ``project_data``).
    """
    project = await _fetch_project(project_id)
    if project is None:
        raise ValueError(f"AHS project not found: {project_id}")

    project_ref = _extract_project_ref(project)
    if not project_ref:
        raise ValueError(f"Project {project_id} has no linked Linear project. Call export_project_to_linear first.")

    tasks = await _fetch_tasks(project_id)
    result = SyncResult()
    now = datetime.now(UTC)

    # Fetch workflow states for status mapping (best-effort)
    client = LinearClient()
    workflow_states: list[dict[str, str]] = []
    try:
        states_response = await asyncio.to_thread(client.get_workflow_states, project_ref.linear_team_id)
        gql_errors = states_response.get("errors")
        if gql_errors:
            logger.warning(
                "Failed to fetch workflow states, status mapping will be skipped",
                error=gql_errors[0].get("message", str(gql_errors)),
            )
        else:
            workflow_states = (states_response.get("data") or {}).get("workflowStates", {}).get("nodes", [])
    except Exception as e:
        logger.warning("Failed to fetch workflow states, status mapping will be skipped", error=str(e))

    # Resolve creator's Linear user ID for issue assignment
    creator_linear_user_id = await _resolve_creator_linear_user_id(project, client)

    # Build current AHS→Linear map for dependency resolution
    task_id_to_linear_id: dict[str, str] = {}
    for task in tasks:
        ref = _extract_issue_ref(task)
        if ref:
            task_id_to_linear_id[str(task.agent_task_id)] = ref.linear_issue_id

    # Topologically sort tasks so new issues are created in dependency order
    # (parents/blockers first). This ensures parentId/blocked_by can be
    # resolved when creating new issues.
    ordered_tasks = _topological_sort(tasks)

    for task in ordered_tasks:
        task_id_str = str(task.agent_task_id)
        try:
            existing_ref = _extract_issue_ref(task)
            if existing_ref:
                # Update the existing Linear issue (priority, title, desc, state).
                # Note: We intentionally pass assignee_id=None to preserve any
                # manual reassignments made in Linear.
                await _update_linear_issue(
                    task=task,
                    linear_issue_id=existing_ref.linear_issue_id,
                    workflow_states=workflow_states,
                    assignee_id=None,
                )
                # Refresh last_synced_at
                updated_ref = LinearIssueRef(
                    linear_issue_id=existing_ref.linear_issue_id,
                    linear_identifier=existing_ref.linear_identifier,
                    last_synced_at=now,
                )
                await _save_issue_ref(task_id_str, updated_ref)
                result.updated += 1
            else:
                # Issue not yet in Linear — create it
                linear_issue_id, linear_identifier = await _upsert_linear_issue(
                    task=task,
                    linear_project_id=project_ref.linear_project_id,
                    linear_team_id=project_ref.linear_team_id,
                    task_id_to_linear_id=task_id_to_linear_id,
                    workflow_states=workflow_states,
                    assignee_id=creator_linear_user_id,
                    existing_issue_id=None,
                )
                task_id_to_linear_id[task_id_str] = linear_issue_id
                new_ref = LinearIssueRef(
                    linear_issue_id=linear_issue_id,
                    linear_identifier=linear_identifier,
                    last_synced_at=now,
                )
                await _save_issue_ref(task_id_str, new_ref)
                result.created += 1

        except Exception as exc:
            logger.warning(
                "Failed to sync task to Linear",
                task_id=task_id_str,
                error=str(exc),
                exc_info=True,
            )
            result.errors += 1

    logger.info(
        "sync_tasks_to_linear complete",
        project_id=project_id,
        created=result.created,
        updated=result.updated,
        errors=result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Linear API helpers
# ---------------------------------------------------------------------------


async def _create_linear_project(
    name: str,
    linear_team_id: str,
) -> str:
    """Create a new Linear project via LinearClient.

    Args:
        name: Display name for the Linear project.
        linear_team_id: UUID of the Linear team that owns the project.

    Returns:
        The UUID of the newly created project.

    Raises:
        RuntimeError: If the API call fails or returns no ``id``.
    """
    client = LinearClient()
    response = await asyncio.to_thread(client.create_project, name=name, team_ids=[linear_team_id])
    errors = response.get("errors")
    if errors:
        raise RuntimeError(f"create_project failed: {errors[0].get('message', str(errors))}")
    proj_result = (response.get("data") or {}).get("projectCreate", {})
    if not proj_result.get("success"):
        raise RuntimeError(f"create_project returned success=False for project '{name}'. Response: {response}")
    project_id = (proj_result.get("project") or {}).get("id")
    if not project_id:
        raise RuntimeError(f"create_project returned no id for project '{name}'. Response: {response}")
    return str(project_id)


async def _upsert_linear_issue(
    task: AgentTask,
    linear_project_id: str,
    linear_team_id: str,
    task_id_to_linear_id: dict[str, str],
    workflow_states: list[dict[str, str]],
    assignee_id: str | None,
    existing_issue_id: str | None,
) -> tuple[str, str]:
    """Create or update a Linear issue for an AHS task.

    When creating a new issue, sets ``projectId``, ``parentId``, ``stateId``,
    ``assigneeId``, and ``blocked_by`` relations.  When updating an existing
    issue, pushes title, description, priority, state, and assignee changes.

    Args:
        task: The AHS task record.
        linear_project_id: Linear project UUID to assign the issue to.
        linear_team_id: Linear team UUID required when creating new issues.
        task_id_to_linear_id: Map of AHS task ID → Linear issue ID for
            already-created issues (used to resolve parentId / blocked_by).
        workflow_states: List of Linear workflow states for the team, used to
            map AHS task status to Linear state ID.
        assignee_id: Linear user ID to assign the issue to, or None to leave
            unassigned.
        existing_issue_id: If not ``None``, update this issue in place rather
            than creating a new one.

    Returns:
        ``(linear_issue_id, linear_identifier)`` — the Linear issue UUID
        and human-readable identifier (e.g. ``"ENG-42"``).

    Raises:
        RuntimeError: If the API call fails or returns no ``id``.
    """
    client = LinearClient()
    priority = map_ahs_priority_to_linear(task.priority.name)
    ahs_status = task.status.name if task.status else "READY"
    state_id = map_ahs_status_to_linear(ahs_status, workflow_states)

    if existing_issue_id:
        # Update existing issue — title, description, priority, state.
        # Note: We intentionally omit assignee_id to preserve any manual
        # reassignments made in Linear.
        response = await asyncio.to_thread(
            client.update_issue,
            issue_id=existing_issue_id,
            title=task.title,
            description=task.description or "",
            priority=priority,
            state_id=state_id,
            assignee_id=None,
        )
        errors = response.get("errors")
        if errors:
            raise RuntimeError(f"update_issue failed for task '{task.title}': {errors[0].get('message', str(errors))}")
        update_result = (response.get("data") or {}).get("issueUpdate", {})
        if not update_result.get("success"):
            raise RuntimeError(f"update_issue returned success=False for task '{task.title}'. Response: {response}")
        issue = update_result.get("issue") or {}
        issue_id = str(issue.get("id", existing_issue_id))
        identifier = str(issue.get("identifier", ""))
        return issue_id, identifier

    # Create new issue
    # Resolve parentId: use the Linear ID of the AHS parent task (if any).
    parent_linear_id: str | None = None
    if task.parent_task_id:
        parent_linear_id = task_id_to_linear_id.get(str(task.parent_task_id))

    # Resolve blocked_by IDs from depends_on.
    blocked_by_ids: list[str] = []
    for dep_id in task.depends_on or []:
        dep_linear_id = task_id_to_linear_id.get(dep_id)
        if dep_linear_id:
            blocked_by_ids.append(dep_linear_id)

    response = await asyncio.to_thread(
        client.create_issue,
        title=task.title,
        team_id=linear_team_id,
        description=task.description or "",
        state_id=state_id,
        assignee_id=assignee_id,
        priority=priority,
        project_id=linear_project_id,
        parent_id=parent_linear_id,
    )
    errors = response.get("errors")
    if errors:
        raise RuntimeError(f"create_issue failed for task '{task.title}': {errors[0].get('message', str(errors))}")
    create_result = (response.get("data") or {}).get("issueCreate", {})
    if not create_result.get("success"):
        raise RuntimeError(f"create_issue returned success=False for task '{task.title}'. Response: {response}")
    issue = create_result.get("issue") or {}
    raw_issue_id = issue.get("id")
    if not raw_issue_id:
        raise RuntimeError(f"create_issue returned no id for task '{task.title}'. Response: {response}")
    issue_id = str(raw_issue_id)
    identifier = str(issue.get("identifier", ""))

    # Set blocked_by relations after creation
    if blocked_by_ids:
        await asyncio.to_thread(client.set_issue_blocked_by, issue_id, blocked_by_ids)

    return issue_id, identifier


async def _update_linear_issue(
    task: AgentTask,
    linear_issue_id: str,
    workflow_states: list[dict[str, str]],
    assignee_id: str | None,
) -> None:
    """Push title, description, priority, state, and assignee updates to an existing Linear issue.

    Args:
        task: AHS task with the current field values.
        linear_issue_id: UUID of the existing Linear issue to update.
        workflow_states: List of Linear workflow states for the team, used to
            map AHS task status to Linear state ID.
        assignee_id: Linear user ID to assign the issue to, or None to leave
            assignee unchanged.
    """
    client = LinearClient()
    priority = map_ahs_priority_to_linear(task.priority.name)
    ahs_status = task.status.name if task.status else "READY"
    state_id = map_ahs_status_to_linear(ahs_status, workflow_states)
    response = await asyncio.to_thread(
        client.update_issue,
        issue_id=linear_issue_id,
        title=task.title,
        description=task.description or "",
        priority=priority,
        state_id=state_id,
        assignee_id=assignee_id,
    )
    errors = response.get("errors")
    if errors:
        raise RuntimeError(f"update_issue failed for issue {linear_issue_id}: {errors[0].get('message', str(errors))}")
    update_result = (response.get("data") or {}).get("issueUpdate", {})
    if isinstance(update_result, dict) and not update_result.get("success"):
        raise RuntimeError(f"update_issue returned success=False for issue {linear_issue_id}. Response: {response}")


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def _topological_sort(tasks: list[AgentTask]) -> list[AgentTask]:
    """Return *tasks* in an order that respects parent and dependency edges.

    Parents come before their children; blockers come before the tasks that
    depend on them.  Cycles are broken by ignoring the back-edge that would
    cause infinite recursion (a warning is logged).

    Args:
        tasks: Unordered list of AHS task objects for a single project.

    Returns:
        A new list in topological order.
    """
    id_to_task: dict[str, AgentTask] = {str(t.agent_task_id): t for t in tasks}
    visited: set[str] = set()
    visiting: set[str] = set()  # DFS path — used to detect cycles
    ordered: list[AgentTask] = []

    def _visit(task: AgentTask) -> None:
        tid = str(task.agent_task_id)
        if tid in visited:
            return
        if tid in visiting:
            logger.warning(
                "Cycle detected in task graph; breaking back-edge",
                task_id=tid,
                title=task.title,
            )
            return

        visiting.add(tid)

        # Visit parent before child
        if task.parent_task_id:
            parent = id_to_task.get(str(task.parent_task_id))
            if parent:
                _visit(parent)

        # Visit each dependency before this task
        for dep_id in task.depends_on or []:
            dep = id_to_task.get(dep_id)
            if dep:
                _visit(dep)

        visiting.discard(tid)
        visited.add(tid)
        ordered.append(task)

    for task in tasks:
        _visit(task)

    return ordered


# ---------------------------------------------------------------------------
# AHS persistence helpers
# ---------------------------------------------------------------------------


@retry_db
async def _fetch_project(project_id: str) -> AgentProject | None:
    """Fetch an AHS project by UUID.

    Uses primary reads (not replica) to ensure we see the latest
    ``linear_project_id`` written by prior runs, avoiding duplicate
    creation due to replica lag.

    Args:
        project_id: UUID string of the project.

    Returns:
        The :class:`~ypl.db.agent_harness.AgentProject` record, or ``None``
        if not found.
    """
    proj_uuid = uuid.UUID(project_id)
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        return result.scalars().first()


@retry_db
async def _fetch_tasks(project_id: str) -> list[AgentTask]:
    """Fetch all non-deleted tasks for a project, ordered by priority then creation time.

    Uses primary reads (not replica) to ensure we see the latest
    ``linear_issue_id`` written by prior runs, avoiding duplicate
    creation due to replica lag.

    Args:
        project_id: UUID string of the project.

    Returns:
        List of :class:`~ypl.db.agent_harness.AgentTask` records.
    """
    proj_uuid = uuid.UUID(project_id)
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_project_id) == proj_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .order_by(col(AgentTask.priority).asc(), col(AgentTask.created_at).asc())
        )
        return list(result.scalars().all())


@retry_db
async def _save_project_ref(project_id: str, ref: LinearProjectRef) -> None:
    """Merge a :class:`~.types.LinearProjectRef` into a project's ``project_data``.

    Uses a shallow merge so any other existing ``project_data`` keys are
    preserved.

    Args:
        project_id: UUID string of the AHS project.
        ref: The :class:`~.types.LinearProjectRef` to store.
    """
    proj_uuid = uuid.UUID(project_id)
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
            .with_for_update()
        )
        project = result.scalars().first()
        if not project:
            logger.warning("Project not found when saving project ref", project_id=project_id)
            return

        existing: dict[str, Any] = dict(project.project_data or {})
        # Use nested linear_ref format (consistent with import_from_linear)
        existing["linear_ref"] = {
            "linear_project_id": ref.linear_project_id,
            "linear_team_id": ref.linear_team_id,
            "last_synced_at": ref.last_synced_at.isoformat() if ref.last_synced_at else None,
        }
        # Remove legacy flat-format keys if present (migration to nested format)
        existing.pop("linear_project_id", None)
        existing.pop("linear_team_id", None)
        existing.pop("last_synced_at", None)
        project.project_data = existing
        # Explicitly mark the JSONB column as modified so SQLAlchemy flushes it.
        flag_modified(project, "project_data")
        session.add(project)
        await session.commit()


@retry_db
async def _save_issue_ref(task_id: str, ref: LinearIssueRef) -> None:
    """Merge a :class:`~.types.LinearIssueRef` into a task's ``task_data``.

    Uses a shallow merge so any other existing ``task_data`` keys are
    preserved.

    Args:
        task_id: UUID string of the AHS task.
        ref: The :class:`~.types.LinearIssueRef` to store.
    """
    task_uuid = uuid.UUID(task_id)
    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        task = result.scalars().first()
        if not task:
            logger.warning("Task not found when saving issue ref", task_id=task_id)
            return

        existing: dict[str, Any] = dict(task.task_data or {})
        # Use nested linear_ref format (consistent with import_from_linear)
        existing["linear_ref"] = {
            "linear_issue_id": ref.linear_issue_id,
            "linear_identifier": ref.linear_identifier,
            "last_synced_at": ref.last_synced_at.isoformat() if ref.last_synced_at else None,
        }
        # Remove legacy flat-format keys if present (migration to nested format)
        existing.pop("linear_issue_id", None)
        existing.pop("linear_identifier", None)
        existing.pop("last_synced_at", None)
        task.task_data = existing
        # Explicitly mark the JSONB column as modified so SQLAlchemy flushes it.
        flag_modified(task, "task_data")
        session.add(task)
        await session.commit()


# ---------------------------------------------------------------------------
# Ref extraction helpers
# ---------------------------------------------------------------------------


def _extract_project_ref(project: AgentProject) -> LinearProjectRef | None:
    """Extract a :class:`~.types.LinearProjectRef` from ``project_data``, if present.

    Reads from nested format: ``{linear_ref: {linear_project_id, linear_team_id, last_synced_at}}``

    Args:
        project: AHS project record.

    Returns:
        A :class:`~.types.LinearProjectRef` or ``None`` if the project has
        not been linked to Linear yet.

    Raises:
        ValueError: If ``linear_project_id`` exists but required fields
            (``linear_team_id``) are missing — indicating data corruption.
    """
    data = project.project_data or {}
    linear_ref = data.get("linear_ref")
    if not linear_ref or not isinstance(linear_ref, dict):
        return None

    project_id = linear_ref.get("linear_project_id")
    if not project_id:
        return None

    team_id = linear_ref.get("linear_team_id")
    if not team_id:
        raise ValueError(
            f"project_data.linear_ref has linear_project_id but missing linear_team_id: "
            f"project={project.agent_project_id}"
        )
    return LinearProjectRef(
        linear_project_id=project_id,
        linear_team_id=team_id,
        last_synced_at=_parse_dt(linear_ref.get("last_synced_at")),
    )


def _extract_issue_ref(task: AgentTask) -> LinearIssueRef | None:
    """Extract a :class:`~.types.LinearIssueRef` from ``task_data``, if present.

    Reads from nested format: ``{linear_ref: {linear_issue_id, linear_identifier, last_synced_at}}``

    Args:
        task: AHS task record.

    Returns:
        A :class:`~.types.LinearIssueRef` or ``None`` if the task has not
        been linked to a Linear issue yet.

    Raises:
        ValueError: If ``linear_issue_id`` exists but required fields
            (``linear_identifier``) are missing — indicating data corruption.
    """
    data = task.task_data or {}
    linear_ref = data.get("linear_ref")
    if not linear_ref or not isinstance(linear_ref, dict):
        return None

    issue_id = linear_ref.get("linear_issue_id")
    if not issue_id:
        return None

    identifier = linear_ref.get("linear_identifier")
    if not identifier:
        raise ValueError(
            f"task_data.linear_ref has linear_issue_id but missing linear_identifier: task={task.agent_task_id}"
        )
    return LinearIssueRef(
        linear_issue_id=issue_id,
        linear_identifier=identifier,
        last_synced_at=_parse_dt(linear_ref.get("last_synced_at")),
    )


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a timezone-aware datetime, or return None.

    Args:
        value: ISO 8601 datetime string, or ``None``.

    Returns:
        Parsed :class:`datetime` (UTC), or ``None``.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Link attachment sync
# ---------------------------------------------------------------------------


def _get_lit_console_url(session_id: str) -> str:
    """Construct a Lit Console URL for a session."""
    if settings.ENVIRONMENT == "production":
        base = "https://agent-streamlit-server-production-451082535721.us-east4.run.app"
    else:
        base = "https://agent-streamlit-server-staging-451082535721.us-east4.run.app"
    return f"{base}/agent_harness_console?session_id={session_id}"


async def sync_task_links_to_linear(task: AgentTask, linear_issue_id: str) -> int:
    """Attach PR and session links from an AHS task to its Linear issue.

    Fetches existing attachments first to avoid duplicates.  Only attaches
    links that are not already present (matched by URL).

    Args:
        task: The AHS task with ``result`` and ``assigned_session_ids``.
        linear_issue_id: The Linear issue UUID to attach links to.

    Returns:
        Number of new links attached.
    """
    # Collect links to sync
    links: list[tuple[str, str]] = []  # (url, title)

    # PR link from result
    result = task.result
    if isinstance(result, dict):
        pr_url = result.get("pr_url")
        if isinstance(pr_url, str) and pr_url.startswith("https://"):
            pr_num = ""
            if "/pull/" in pr_url:
                pr_num = pr_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
            title = f"PR #{pr_num}" if pr_num.isdigit() else "Pull Request"
            links.append((pr_url, title))

    # Session links
    links.extend((_get_lit_console_url(sid), f"Session {sid[:8]}") for sid in task.assigned_session_ids or [])

    if not links:
        return 0

    client = LinearClient()

    # Fetch existing attachments to deduplicate
    existing_urls: set[str] = set()
    try:
        response = await asyncio.to_thread(client.list_attachments, linear_issue_id)
        nodes = ((response.get("data") or {}).get("issue") or {}).get("attachments", {}).get("nodes", [])
        existing_urls = {a.get("url", "") for a in nodes}
    except Exception:
        logger.warning("Failed to fetch existing attachments, may create duplicates", issue_id=linear_issue_id)

    attached = 0
    for url, title in links:
        if url in existing_urls:
            continue
        try:
            response = await asyncio.to_thread(client.attach_link, linear_issue_id, url, title)
            attach_result = (response.get("data") or {}).get("attachmentLinkURL", {})
            if attach_result.get("success"):
                attached += 1
            else:
                logger.warning("attach_link returned success=False", issue_id=linear_issue_id, url=url)
        except Exception:
            logger.warning("Failed to attach link to Linear issue", issue_id=linear_issue_id, url=url, exc_info=True)

    if attached > 0:
        logger.info("Attached links to Linear issue", issue_id=linear_issue_id, count=attached)

    return attached
