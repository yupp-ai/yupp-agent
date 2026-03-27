"""Import Linear projects and issues into the AHS project/task system.

Provides two public functions:
- import_project_from_linear: Create a new AHS project from a Linear project.
- sync_tasks_from_linear: Pull incremental updates from Linear into an existing project.

Both functions use :class:`~ypl.backend.utils.linear.LinearClient` (the same
client used by the harness MCP server Linear tools) to call the Linear GraphQL
API.  All LinearClient calls are dispatched via :func:`asyncio.to_thread` since
the client is synchronous.
"""

from __future__ import annotations
import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

from sqlmodel import col, select

from ypl.agent_harness_service.tools.linear_sync.mapping import map_linear_priority_to_ahs, map_linear_status_to_ahs
from ypl.agent_harness_service.tools.linear_sync.types import LinearIssueRef, LinearProjectRef, SyncResult
from ypl.backend.db import get_async_session
from ypl.backend.utils.linear import LinearClient
from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Internal Linear API helpers  (thin wrappers around LinearClient)
# ---------------------------------------------------------------------------


async def _fetch_project(linear_project_id: str) -> dict[str, Any]:
    """Fetch a Linear project's metadata.

    Args:
        linear_project_id: UUID of the Linear project.

    Returns:
        Dict with id, name, description, state fields.

    Raises:
        ValueError: If the project is not found.
        RuntimeError: On Linear API errors.
    """
    client = LinearClient()
    response = await asyncio.to_thread(client.get_project, linear_project_id)
    errors = response.get("errors")
    if errors:
        raise RuntimeError(f"Linear get_project failed: {errors[0].get('message', str(errors))}")
    project = (response.get("data") or {}).get("project")
    if not project:
        raise ValueError(f"Linear project not found: {linear_project_id}")
    return dict(project)


async def _fetch_issues(
    linear_project_id: str,
    include_completed: bool = False,
    updated_after: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch all Linear issues for a project, handling pagination.

    Uses :meth:`~ypl.backend.utils.linear.LinearClient.list_project_issues`
    which includes the ``parent`` and ``relations`` fields needed to
    reconstruct the AHS dependency graph.

    Args:
        linear_project_id: Linear project UUID.
        include_completed: Include issues in completed/cancelled states.
        updated_after: If set, only return issues updated after this timestamp.

    Returns:
        Flat list of issue dicts.
    """
    client = LinearClient()
    updated_after_str = updated_after.isoformat() if updated_after is not None else None
    return await asyncio.to_thread(
        client.list_project_issues,
        project_id=linear_project_id,
        include_completed=include_completed,
        updated_after=updated_after_str,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_blocked_by(issue: dict[str, Any]) -> list[str]:
    """Return the list of Linear issue IDs that block *issue*."""
    blocked_by: list[str] = []
    for rel in issue.get("relations", {}).get("nodes", []):
        if rel.get("type") == "blocked_by":
            related = rel.get("relatedIssue")
            if related:
                blocked_by.append(related["id"])
    return blocked_by


def _topological_sort(
    issue_ids: list[str],
    parent_map: dict[str, str | None],
    blocked_by_map: dict[str, list[str]],
) -> list[str]:
    """Return a topological ordering of *issue_ids* respecting parent and blocker edges.

    Edges represent "must create before" relationships:
      - parent → child  (we need the parent's DB UUID to set parent_task_id)
      - blocker → blocked  (we need the blocker's DB UUID to store in depends_on)

    If the graph contains a cycle, the cyclic nodes are appended at the end in
    their original order so that creation still proceeds (depends_on will simply
    be incomplete for those nodes, which is safer than aborting the whole import).

    Args:
        issue_ids: All issue IDs to sort.
        parent_map: Maps issue_id → parent_issue_id (or None).
        blocked_by_map: Maps issue_id → list of issue_ids that block it.

    Returns:
        Ordered list of issue IDs.
    """
    id_set = set(issue_ids)

    # Build in-degree and adjacency list (predecessor → successor)
    in_degree: dict[str, int] = dict.fromkeys(issue_ids, 0)
    successors: dict[str, list[str]] = {i: [] for i in issue_ids}

    def _add_edge(pred: str, succ: str) -> None:
        if pred not in id_set or succ not in id_set:
            return  # Ignore edges to issues outside our set
        if succ in successors[pred]:
            return  # Deduplicate
        successors[pred].append(succ)
        in_degree[succ] += 1

    for issue_id in issue_ids:
        parent_id = parent_map.get(issue_id)
        if parent_id:
            _add_edge(parent_id, issue_id)
        for blocker_id in blocked_by_map.get(issue_id, []):
            _add_edge(blocker_id, issue_id)

    # Kahn's algorithm
    queue: deque[str] = deque(i for i in issue_ids if in_degree[i] == 0)
    ordered: list[str] = []

    while queue:
        node = queue.popleft()
        ordered.append(node)
        for succ in successors[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    # Append any remaining nodes (cycle members) preserving original order
    remaining = [i for i in issue_ids if i not in set(ordered)]
    if remaining:
        logger.warning(
            "Cycle detected in Linear issue graph — affected issues appended at end",
            cycle_node_count=len(remaining),
        )
    return ordered + remaining


def _ahs_priority(linear_priority: int | None) -> AgentTaskPriority:
    """Map a Linear priority integer to AgentTaskPriority."""
    name = map_linear_priority_to_ahs(linear_priority or 0)
    try:
        return AgentTaskPriority[name]
    except KeyError:
        return AgentTaskPriority.NORMAL


def _ahs_status(issue: dict[str, Any], has_unmet_deps: bool) -> AgentTaskStatus:
    """Determine the initial AHS status for an imported task.

    If the task has unmet dependencies it is BLOCKED regardless of the Linear state.
    Otherwise the status is derived from the Linear workflow-state type.
    """
    if has_unmet_deps:
        return AgentTaskStatus.BLOCKED

    state = issue.get("state") or {}
    ahs_status_name = map_linear_status_to_ahs(state)
    try:
        return AgentTaskStatus[ahs_status_name]
    except KeyError:
        return AgentTaskStatus.READY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def import_project_from_linear(
    linear_project_id: str,
    linear_team_id: str,
    creator_user_id: str,
    include_completed: bool = False,
) -> tuple[AgentProject, SyncResult]:
    """Create a new AHS project mirroring a Linear project.

    Steps:
    1. Fetch the Linear project and its issues (excluding completed by default).
    2. Create an AgentProject with ``project_data`` containing a LinearProjectRef.
    3. Build parent/child and blocker/blocked dependency graphs.
    4. Topologically sort issues so parents and blockers are created first.
    5. Create AgentTask rows in that order, recording each task's UUID so that
       subsequent tasks can reference it in ``parent_task_id`` / ``depends_on``.

    Args:
        linear_project_id: UUID of the Linear project to import.
        linear_team_id: UUID of the Linear team that owns the project.
        creator_user_id: AHS user ID to set as project creator.
        include_completed: If True, also import completed/cancelled issues.

    Returns:
        A tuple of (AgentProject, SyncResult).

    Raises:
        ValueError: If the Linear project cannot be found.
        RuntimeError: On Linear API errors.
    """
    logger.info(
        "Starting Linear project import",
        linear_project_id=linear_project_id,
        linear_team_id=linear_team_id,
        include_completed=include_completed,
    )

    # ---- Step 1: Fetch Linear data ------------------------------------------
    linear_project = await _fetch_project(linear_project_id)
    issues = await _fetch_issues(linear_project_id, include_completed=include_completed)

    logger.info("Fetched Linear issues", issue_count=len(issues))

    # ---- Step 2: Build look-up structures -----------------------------------
    issue_ids = [i["id"] for i in issues]
    issues_by_id = {i["id"]: i for i in issues}

    parent_map: dict[str, str | None] = {}
    blocked_by_map: dict[str, list[str]] = {}

    for issue in issues:
        issue_id = issue["id"]
        parent_info = issue.get("parent")
        parent_map[issue_id] = parent_info["id"] if parent_info else None
        blocked_by_map[issue_id] = _extract_blocked_by(issue)

    # ---- Step 3: Topological sort -------------------------------------------
    ordered_ids = _topological_sort(issue_ids, parent_map, blocked_by_map)

    # ---- Step 4: Persist to DB ----------------------------------------------
    sync_result = SyncResult()
    now = datetime.now(UTC)

    project_ref = LinearProjectRef(
        linear_project_id=linear_project_id,
        linear_team_id=linear_team_id,
        last_synced_at=now,
    )

    async with get_async_session() as db:
        # Create the project
        project = AgentProject(
            name=linear_project["name"],
            description=linear_project.get("description"),
            status=AgentProjectStatus.PAUSED,
            creator_user_id=creator_user_id,
            project_data={"linear_ref": project_ref.model_dump(mode="json")},
        )
        db.add(project)
        await db.flush()  # Populate agent_project_id

        project_id = project.agent_project_id

        # Create tasks in topological order
        # Maps linear_issue_id → newly-created agent_task_id (UUID)
        issue_to_task_id: dict[str, uuid.UUID] = {}

        for linear_id in ordered_ids:
            issue = issues_by_id[linear_id]

            # Resolve parent_task_id
            parent_linear_id = parent_map.get(linear_id)
            parent_task_id: uuid.UUID | None = None
            if parent_linear_id and parent_linear_id in issue_to_task_id:
                parent_task_id = issue_to_task_id[parent_linear_id]

            # Resolve depends_on (from blockedBy)
            dep_task_ids = [
                str(issue_to_task_id[b]) for b in blocked_by_map.get(linear_id, []) if b in issue_to_task_id
            ]
            depends_on: list[str] | None = dep_task_ids or None

            # Determine whether any recorded deps are non-COMPLETED (we can't easily
            # check statuses here without extra queries, so we use presence of deps as
            # the heuristic — tasks with any deps start as BLOCKED and the scheduler
            # will unblock them once predecessors complete).
            has_deps = bool(depends_on)
            status = _ahs_status(issue, has_unmet_deps=has_deps)
            priority = _ahs_priority(issue.get("priority"))

            issue_ref = LinearIssueRef(
                linear_issue_id=linear_id,
                linear_identifier=issue.get("identifier", ""),
                last_synced_at=now,
            )

            task = AgentTask(
                agent_project_id=project_id,
                parent_task_id=parent_task_id,
                title=issue["title"],
                description=issue.get("description"),
                status=status,
                priority=priority,
                depends_on=depends_on,
                task_data={
                    "linear_issue_id": linear_id,
                    "linear_ref": issue_ref.model_dump(mode="json"),
                },
            )
            db.add(task)
            await db.flush()  # Populate agent_task_id

            issue_to_task_id[linear_id] = task.agent_task_id
            sync_result.created += 1

        await db.commit()
        await db.refresh(project)

    logger.info(
        "Linear project import complete",
        project_id=str(project_id),
        linear_project_id=linear_project_id,
        created=sync_result.created,
    )
    return project, sync_result


async def sync_tasks_from_linear(
    project_id: uuid.UUID,
    since: datetime | None = None,
) -> SyncResult:
    """Pull incremental updates from Linear into an existing AHS project.

    Fetches all Linear issues that were updated after *since* (or after
    ``project_data.linear_ref.last_synced_at`` if *since* is not given) and
    either creates new tasks or updates existing ones.  The project's
    ``last_synced_at`` is updated on success.

    The watermark written to ``last_synced_at`` is captured *before* the Linear
    API call so that any issues updated during the fetch window are included in
    the next sync rather than being silently skipped.

    Args:
        project_id: UUID of the existing AHS project.
        since: Only fetch issues updated after this timestamp.  Defaults to the
            project's recorded ``last_synced_at``.

    Returns:
        SyncResult with counts of created/updated/skipped/errors.

    Raises:
        ValueError: If the project is not found or has no Linear reference.
        RuntimeError: On Linear API errors.
    """
    sync_result = SyncResult()

    # ---- Fetch project from DB ----------------------------------------------
    async with get_async_session() as db:
        proj_result = await db.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == project_id)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        project = proj_result.scalars().first()
        if not project:
            raise ValueError(f"AHS project not found: {project_id}")

        project_data = project.project_data or {}
        linear_ref_data = project_data.get("linear_ref")
        if not linear_ref_data:
            raise ValueError(
                f"Project {project_id} has no Linear reference in project_data. "
                "Use import_project_from_linear to create the link."
            )
        linear_ref = LinearProjectRef.model_validate(linear_ref_data)

    effective_since = since or linear_ref.last_synced_at

    logger.info(
        "Starting Linear sync",
        project_id=str(project_id),
        linear_project_id=linear_ref.linear_project_id,
        since=effective_since.isoformat() if effective_since else None,
    )

    # Capture the watermark BEFORE the API call so that any Linear updates that
    # occur during the fetch window are not silently skipped — they will be
    # caught by the next sync whose lower bound starts here.
    now = datetime.now(UTC)

    # ---- Fetch updated issues -----------------------------------------------
    issues = await _fetch_issues(
        linear_ref.linear_project_id,
        include_completed=True,
        updated_after=effective_since,
    )

    if not issues:
        logger.info("No issues updated since last sync", project_id=str(project_id))
        return sync_result

    # ---- Apply updates -------------------------------------------------------
    # TODO: To prevent duplicate task creation from concurrent syncs, consider
    # taking a FOR UPDATE lock on the project row at the start of this session
    # (or adding a partial unique index on agent_project_id + task_data->>'linear_issue_id').
    # See PR #11007 review thread for details.
    async with get_async_session() as db:
        # Batch-fetch all existing tasks for the updated issue IDs in a single
        # query to avoid N+1 database round-trips inside the update loop.
        # Query using nested format: task_data->'linear_ref'->>'linear_issue_id'
        linear_issue_ids = [issue["id"] for issue in issues]
        existing_tasks_result = await db.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_project_id) == project_id)
            .where(
                AgentTask.task_data["linear_ref"]["linear_issue_id"].astext.in_(linear_issue_ids)  # type: ignore[index]
            )
            .where(col(AgentTask.deleted_at).is_(None))
        )
        tasks_by_linear_id: dict[str, AgentTask] = {
            task.task_data["linear_ref"]["linear_issue_id"]: task for task in existing_tasks_result.scalars()
        }

        for issue in issues:
            linear_issue_id = issue["id"]

            try:
                existing_task = tasks_by_linear_id.get(linear_issue_id)

                if existing_task:
                    # Update mutable fields from Linear.  Always propagate
                    # description, including None, so deletions in Linear clear
                    # the AHS field rather than leaving stale text behind.
                    existing_task.title = issue["title"]
                    existing_task.description = issue.get("description")

                    # Update status only if not in an executor-owned or terminal
                    # AHS state.  Map the Linear state directly — do NOT apply
                    # the has_unmet_deps heuristic here because the dep-check was
                    # designed for initial import bootstrapping.  During sync,
                    # the existing task's AHS dep state is already established;
                    # forcing BLOCKED based on depends_on presence would regress
                    # tasks whose deps have since completed.  Terminal states
                    # (COMPLETED, FAILED, CANCELLED) are also skipped to prevent
                    # regressing a finished task if someone edits its Linear issue.
                    _skip_status_override = {
                        AgentTaskStatus.IN_PROGRESS,
                        AgentTaskStatus.IN_REVIEW,
                        AgentTaskStatus.COMPLETED,
                        AgentTaskStatus.FAILED,
                        AgentTaskStatus.CANCELLED,
                        AgentTaskStatus.BLOCKED,  # preserve BLOCKED — deps resolved by task_executor
                    }
                    if existing_task.status not in _skip_status_override:
                        existing_task.status = _ahs_status(issue, has_unmet_deps=False)

                    existing_task.priority = _ahs_priority(issue.get("priority"))

                    # Refresh last_synced_at in task_data
                    task_data = dict(existing_task.task_data or {})
                    issue_ref = LinearIssueRef(
                        linear_issue_id=linear_issue_id,
                        linear_identifier=issue.get("identifier", ""),
                        last_synced_at=now,
                    )
                    task_data["linear_ref"] = issue_ref.model_dump(mode="json")
                    existing_task.task_data = task_data

                    db.add(existing_task)
                    sync_result.updated += 1
                else:
                    # New issue discovered during incremental sync.
                    # TODO: Map depends_on and parent_task_id for newly discovered
                    # issues (requires fetching their relations and resolving against
                    # existing tasks). For now tasks are created as PENDING so the
                    # scheduler can promote them; a follow-up full re-import via
                    # import_project_from_linear is the authoritative way to
                    # establish the full dependency graph.
                    #
                    # Cap the initial status at READY: never create a synced task
                    # as IN_PROGRESS because it has never been claimed by the
                    # executor — stale-task recovery would reset it to READY
                    # anyway and could trigger unintended re-execution of work
                    # that Linear already considers in progress.
                    raw_status = _ahs_status(issue, has_unmet_deps=False)
                    capped_status = AgentTaskStatus.READY if raw_status == AgentTaskStatus.IN_PROGRESS else raw_status
                    issue_ref = LinearIssueRef(
                        linear_issue_id=linear_issue_id,
                        linear_identifier=issue.get("identifier", ""),
                        last_synced_at=now,
                    )
                    task = AgentTask(
                        agent_project_id=project_id,
                        title=issue["title"],
                        description=issue.get("description"),
                        status=capped_status,
                        priority=_ahs_priority(issue.get("priority")),
                        task_data={
                            "linear_issue_id": linear_issue_id,
                            "linear_ref": issue_ref.model_dump(mode="json"),
                        },
                    )
                    db.add(task)
                    sync_result.created += 1

            except Exception as exc:
                logger.error(
                    "Error syncing Linear issue",
                    linear_issue_id=linear_issue_id,
                    error=str(exc),
                    exc_info=True,
                )
                sync_result.errors += 1
                continue

        # Update last_synced_at on the project only if there were no errors.
        # If any issues failed to sync, keep the old watermark so they fall
        # within the next sync window.
        if sync_result.errors == 0:
            proj_result = await db.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == project_id)
                .where(col(AgentProject.deleted_at).is_(None))
                .with_for_update()
            )
            project = proj_result.scalars().first()
            if project:
                project_data = dict(project.project_data or {})
                existing_ref = project_data.get("linear_ref", {})
                existing_ref["last_synced_at"] = now.isoformat()
                project_data["linear_ref"] = existing_ref
                project.project_data = project_data
                db.add(project)

        await db.commit()

    logger.info(
        "Linear sync complete",
        project_id=str(project_id),
        created=sync_result.created,
        updated=sync_result.updated,
        skipped=sync_result.skipped,
        errors=sync_result.errors,
    )
    return sync_result
