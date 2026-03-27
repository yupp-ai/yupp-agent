"""Bidirectional synchronisation between AHS project tasks and Linear issues.

This module is the business-logic layer for the two-way sync direction.  It can
be invoked from the ``sync_project_with_linear`` MCP tool in
``ypl/mcp_server/tools/linear_sync.py`` (direction='bidirectional').

Algorithm
---------
1. Fetch all AHS tasks for the project and their linked Linear issue IDs.
2. Fetch all Linear issues for the linked Linear project.
3. Build a mapping of ``linear_issue_id → (AHS task, Linear issue)`` pairs.
4. For **matched pairs** (present in both systems), compare ``modified_at``
   (AHS) vs ``updatedAt`` (Linear) and apply the chosen
   :class:`ConflictResolution` strategy.
5. Handle **orphans**:
   - AHS tasks with no linked Linear issue → push to Linear.
   - Linear issues with no AHS task → create an AHS task.
6. Stamp ``last_synced_at`` on the project (only when there are zero errors).
7. Return a :class:`~.types.SyncResult` with aggregate counts.
"""

from __future__ import annotations
import asyncio
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, select

from ypl.agent_harness_service.tools.linear_sync.export_to_linear import (
    _extract_issue_ref,
    _extract_project_ref,
    _fetch_tasks,
    _parse_dt,
    _save_issue_ref,
    _upsert_linear_issue,
)
from ypl.agent_harness_service.tools.linear_sync.import_from_linear import (
    _ahs_priority,
    _ahs_status,
    _fetch_issues,
)
from ypl.agent_harness_service.tools.linear_sync.mapping import (
    map_ahs_priority_to_linear,
    map_ahs_status_to_linear,
)
from ypl.agent_harness_service.tools.linear_sync.types import LinearIssueRef, SyncResult
from ypl.backend.db import get_async_session, retry_db
from ypl.backend.utils.linear import LinearClient
from ypl.db.agent_harness import AgentProject, AgentTask, AgentTaskStatus
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# ConflictResolution enum
# ---------------------------------------------------------------------------


class ConflictResolution(StrEnum):
    """Strategy for resolving conflicts when both AHS and Linear were modified.

    A "conflict" arises when both the AHS task and the corresponding Linear
    issue have been updated since the last sync.  The resolution strategy
    controls which side's changes win.
    """

    LINEAR_WINS = "linear_wins"
    """Always overwrite AHS with Linear's state, regardless of timestamps."""

    AHS_WINS = "ahs_wins"
    """Always overwrite Linear with AHS state, regardless of timestamps."""

    LATEST_WINS = "latest_wins"
    """Compare ``modified_at`` (AHS) and ``updatedAt`` (Linear); the more
    recently modified side wins.  Items with identical timestamps are skipped."""

    SKIP = "skip"
    """Skip all conflicting pairs; only process orphans (items present in
    exactly one system)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def sync_bidirectional(
    project_id: str | uuid.UUID,
    conflict_resolution: ConflictResolution = ConflictResolution.LATEST_WINS,
) -> SyncResult:
    """Synchronise an AHS project with its linked Linear project in both directions.

    Args:
        project_id: UUID of the AHS project (string or :class:`uuid.UUID`).
        conflict_resolution: Strategy to apply when both sides have been
            modified since the last sync.  Defaults to
            :attr:`ConflictResolution.LATEST_WINS`.

    Returns:
        :class:`~.types.SyncResult` with ``created``, ``updated``,
        ``skipped``, and ``errors`` counts covering both sides.

    Raises:
        ValueError: If the project is not found or has no linked Linear
            project.
        RuntimeError: On unrecoverable Linear API errors.
    """
    from ypl.agent_harness_service.tools.linear_sync.export_to_linear import _fetch_project

    proj_id_str = str(project_id)
    now = datetime.now(UTC)
    result = SyncResult()

    # ------------------------------------------------------------------
    # 1. Load AHS project and verify it is linked to Linear
    # ------------------------------------------------------------------
    project = await _fetch_project(proj_id_str)
    if project is None:
        raise ValueError(f"AHS project not found: {project_id}")

    project_ref = _extract_project_ref(project)
    if not project_ref:
        raise ValueError(
            f"Project {project_id} has no linked Linear project. "
            "Use export_project_to_linear or link_project_to_linear first."
        )

    logger.info(
        "Starting bidirectional sync",
        project_id=proj_id_str,
        linear_project_id=project_ref.linear_project_id,
        conflict_resolution=conflict_resolution.value,
    )

    # ------------------------------------------------------------------
    # 2. Fetch AHS tasks and Linear issues in parallel
    # ------------------------------------------------------------------
    ahs_tasks, linear_issues = await asyncio.gather(
        _fetch_tasks(proj_id_str),
        _fetch_issues(project_ref.linear_project_id, include_completed=True),
    )

    # ------------------------------------------------------------------
    # 3. Build mappings
    # ------------------------------------------------------------------
    # linear_issue_id → AgentTask (tasks already linked to a Linear issue)
    tasks_by_linear_id: dict[str, AgentTask] = {}
    # AHS tasks that have never been pushed to Linear (orphans on AHS side)
    ahs_orphans: list[AgentTask] = []

    for task in ahs_tasks:
        try:
            ref = _extract_issue_ref(task)
        except ValueError as e:
            # Imported tasks may have partial data; treat as orphan if extraction fails
            logger.warning(
                "Failed to extract issue ref from task, treating as orphan",
                task_id=str(task.agent_task_id),
                error=str(e),
            )
            ref = None
        if ref:
            tasks_by_linear_id[ref.linear_issue_id] = task
        else:
            ahs_orphans.append(task)

    # linear_issue_id → issue dict
    linear_issues_by_id: dict[str, dict[str, Any]] = {i["id"]: i for i in linear_issues}

    # Linear issues that have no matching AHS task (orphans on Linear side)
    linear_orphan_ids = set(linear_issues_by_id) - set(tasks_by_linear_id)

    # Running AHS task ID → Linear issue ID map (for dependency resolution
    # when creating new issues for AHS orphans).
    # Reuse the mapping built in the first loop to avoid redundant extraction
    # and potential ValueError from malformed refs.
    task_id_to_linear_id: dict[str, str] = {
        str(task.agent_task_id): linear_id for linear_id, task in tasks_by_linear_id.items()
    }

    # ------------------------------------------------------------------
    # 4. Fetch Linear team workflow states once (needed for AHS→Linear
    #    status mapping in matched-pair and orphan processing).
    # ------------------------------------------------------------------
    team_statuses = await _fetch_team_statuses(project_ref.linear_team_id)

    # ------------------------------------------------------------------
    # 5. Resolve matched pairs
    # ------------------------------------------------------------------
    for linear_id, task in tasks_by_linear_id.items():
        issue = linear_issues_by_id.get(linear_id)
        if issue is None:
            # Issue was deleted from Linear — don't delete the AHS task,
            # just skip and let a human decide.
            logger.info(
                "Linear issue no longer present in project; skipping AHS task",
                linear_issue_id=linear_id,
                task_id=str(task.agent_task_id),
            )
            result.skipped += 1
            continue

        try:
            # Extract last_synced_at to use as baseline for LATEST_WINS comparison.
            # We compare whether each side was modified AFTER the last sync, not
            # raw modified_at vs updatedAt (which causes feedback loops since sync
            # itself updates these timestamps).
            ref = _extract_issue_ref(task)
            last_synced = ref.last_synced_at if ref else None
            ahs_modified = task.modified_at
            linear_updated = _parse_dt(issue.get("updatedAt"))

            direction = _resolve_direction(
                conflict_resolution=conflict_resolution,
                ahs_modified=ahs_modified,
                linear_updated=linear_updated,
                last_synced=last_synced,
            )

            if direction == "skip":
                result.skipped += 1
            elif direction == "linear_to_ahs":
                await _apply_linear_to_ahs(task, issue, now)
                result.updated += 1
            else:  # "ahs_to_linear"
                await _apply_ahs_to_linear(task, team_statuses, now)
                result.updated += 1

        except Exception as exc:
            logger.error(
                "Error resolving sync pair",
                linear_issue_id=linear_id,
                task_id=str(task.agent_task_id),
                error=str(exc),
                exc_info=True,
            )
            result.errors += 1

    # ------------------------------------------------------------------
    # 6a. Handle AHS orphans → push to Linear
    # ------------------------------------------------------------------
    for task in ahs_orphans:
        try:
            linear_issue_id, linear_identifier = await _upsert_linear_issue(
                task=task,
                linear_project_id=project_ref.linear_project_id,
                linear_team_id=project_ref.linear_team_id,
                task_id_to_linear_id=task_id_to_linear_id,
                workflow_states=team_statuses,
                assignee_id=None,  # Let Linear auto-assign or leave unassigned
                existing_issue_id=None,
            )
            task_id_to_linear_id[str(task.agent_task_id)] = linear_issue_id
            issue_ref = LinearIssueRef(
                linear_issue_id=linear_issue_id,
                linear_identifier=linear_identifier,
                last_synced_at=now,
            )
            await _save_issue_ref(str(task.agent_task_id), issue_ref)
            result.created += 1

        except Exception as exc:
            logger.error(
                "Error pushing AHS orphan task to Linear",
                task_id=str(task.agent_task_id),
                error=str(exc),
                exc_info=True,
            )
            result.errors += 1

    # ------------------------------------------------------------------
    # 6b. Handle Linear orphans → create in AHS
    # ------------------------------------------------------------------
    for linear_id in linear_orphan_ids:
        issue = linear_issues_by_id[linear_id]
        try:
            await _create_ahs_task_from_linear(
                project_id=proj_id_str,
                issue=issue,
                now=now,
            )
            result.created += 1

        except Exception as exc:
            logger.error(
                "Error creating AHS task from Linear orphan",
                linear_issue_id=linear_id,
                error=str(exc),
                exc_info=True,
            )
            result.errors += 1

    # ------------------------------------------------------------------
    # 7. Stamp last_synced_at on the project (only when error-free)
    # ------------------------------------------------------------------
    if result.errors == 0:
        await _update_project_last_synced(proj_id_str, now)

    logger.info(
        "Bidirectional sync complete",
        project_id=proj_id_str,
        conflict_resolution=conflict_resolution.value,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Conflict-resolution helper
# ---------------------------------------------------------------------------


def _resolve_direction(
    conflict_resolution: ConflictResolution,
    ahs_modified: datetime | None,
    linear_updated: datetime | None,
    last_synced: datetime | None = None,
) -> str:
    """Return the update direction for a matched pair.

    Args:
        conflict_resolution: The chosen strategy.
        ahs_modified: When the AHS task was last modified (``modified_at``).
        linear_updated: When the Linear issue was last updated (``updatedAt``).
        last_synced: When the task was last synced (from ``task_data.last_synced_at``).
            Used by LATEST_WINS to determine which side changed since the last sync.

    Returns:
        One of ``"linear_to_ahs"``, ``"ahs_to_linear"``, or ``"skip"``.
    """
    if conflict_resolution == ConflictResolution.LINEAR_WINS:
        return "linear_to_ahs"
    if conflict_resolution == ConflictResolution.AHS_WINS:
        return "ahs_to_linear"
    if conflict_resolution == ConflictResolution.SKIP:
        return "skip"

    # LATEST_WINS: check if each side was modified since last sync.
    # This avoids feedback loops where sync itself updates modified_at/updatedAt.
    if last_synced is None:
        # No prior sync — fall back to simple timestamp comparison
        if ahs_modified is None and linear_updated is None:
            return "skip"
        if ahs_modified is None:
            return "linear_to_ahs"
        if linear_updated is None:
            return "ahs_to_linear"

        ahs_ts = ahs_modified if ahs_modified.tzinfo else ahs_modified.replace(tzinfo=UTC)
        lin_ts = linear_updated if linear_updated.tzinfo else linear_updated.replace(tzinfo=UTC)

        if ahs_ts > lin_ts:
            return "ahs_to_linear"
        if lin_ts > ahs_ts:
            return "linear_to_ahs"
        return "skip"

    # Normalize timestamps for comparison
    sync_ts = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=UTC)
    ahs_ts_norm: datetime | None = (
        (ahs_modified if ahs_modified.tzinfo else ahs_modified.replace(tzinfo=UTC)) if ahs_modified else None
    )
    lin_ts_norm: datetime | None = (
        (linear_updated if linear_updated.tzinfo else linear_updated.replace(tzinfo=UTC)) if linear_updated else None
    )

    ahs_changed = ahs_ts_norm is not None and ahs_ts_norm > sync_ts
    lin_changed = lin_ts_norm is not None and lin_ts_norm > sync_ts

    if ahs_changed and lin_changed:
        # Both changed since last sync — use most recent; skip if identical timestamps
        if ahs_ts_norm and lin_ts_norm:
            if ahs_ts_norm > lin_ts_norm:
                return "ahs_to_linear"
            if lin_ts_norm > ahs_ts_norm:
                return "linear_to_ahs"
            return "skip"  # Identical timestamps — skip per docstring
        return "skip"
    if ahs_changed:
        return "ahs_to_linear"
    if lin_changed:
        return "linear_to_ahs"
    return "skip"  # Neither changed since last sync


# ---------------------------------------------------------------------------
# Direction-specific update helpers
# ---------------------------------------------------------------------------


async def _apply_linear_to_ahs(
    task: AgentTask,
    issue: dict[str, Any],
    now: datetime,
) -> None:
    """Overwrite an AHS task's mutable fields with the corresponding Linear issue.

    Updates ``title``, ``description``, ``status``, and ``priority``.
    Status is not overridden for in-progress or terminal tasks (same guard
    used in :func:`~.import_from_linear.sync_tasks_from_linear`).

    Args:
        task: AHS task to update.
        issue: Linear issue dict (source of truth).
        now: UTC timestamp used to stamp ``last_synced_at``.
    """
    _skip_status_override = {
        AgentTaskStatus.IN_PROGRESS,
        AgentTaskStatus.IN_REVIEW,
        AgentTaskStatus.COMPLETED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
        AgentTaskStatus.BLOCKED,
    }

    task_uuid = task.agent_task_id
    async with get_async_session() as session:
        db_result = await session.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_task_id) == task_uuid)
            .where(col(AgentTask.deleted_at).is_(None))
            .with_for_update()
        )
        db_task = db_result.scalars().first()
        if not db_task:
            logger.warning("Task not found for Linear→AHS update", task_id=str(task_uuid))
            return

        db_task.title = issue["title"]
        db_task.description = issue.get("description")

        if db_task.status not in _skip_status_override:
            db_task.status = _ahs_status(issue, has_unmet_deps=False)

        db_task.priority = _ahs_priority(issue.get("priority"))

        # Store Linear ref in nested format (consistent with _save_issue_ref)
        task_data = dict(db_task.task_data or {})
        task_data["linear_ref"] = {
            "linear_issue_id": issue["id"],
            "linear_identifier": issue.get("identifier", ""),
            "last_synced_at": now.isoformat(),
        }
        db_task.task_data = task_data
        flag_modified(db_task, "task_data")

        session.add(db_task)
        await session.commit()


async def _apply_ahs_to_linear(
    task: AgentTask,
    team_statuses: list[dict[str, str]],
    now: datetime,
) -> None:
    """Overwrite a Linear issue with the AHS task's current state.

    Pushes ``title``, ``description``, ``priority``, and ``stateId``
    (resolved from AHS status via :func:`~.mapping.map_ahs_status_to_linear`).

    Args:
        task: AHS task (source of truth).
        team_statuses: Linear team workflow states (id, type, name) used for
            status mapping.  If empty, ``stateId`` is omitted from the update.
        now: UTC timestamp used to stamp ``last_synced_at``.
    """
    issue_ref = _extract_issue_ref(task)
    if not issue_ref:
        logger.warning(
            "No linear_issue_id on task for AHS→Linear update",
            task_id=str(task.agent_task_id),
        )
        return

    client = LinearClient()
    priority = map_ahs_priority_to_linear(task.priority.name)
    state_id = map_ahs_status_to_linear(task.status.value, team_statuses)

    update_kwargs: dict[str, Any] = {
        "issue_id": issue_ref.linear_issue_id,
        "title": task.title,
        "description": task.description or "",
        "priority": priority,
    }
    if state_id:
        update_kwargs["state_id"] = state_id

    response = await asyncio.to_thread(client.update_issue, **update_kwargs)
    errors = response.get("errors")
    if errors:
        raise RuntimeError(f"update_issue failed for task '{task.title}': {errors[0].get('message', str(errors))}")
    update_result = (response.get("data") or {}).get("issueUpdate", {})
    if isinstance(update_result, dict) and not update_result.get("success"):
        raise RuntimeError(f"update_issue returned success=False for task '{task.title}'. Response: {response}")

    updated_ref = LinearIssueRef(
        linear_issue_id=issue_ref.linear_issue_id,
        linear_identifier=issue_ref.linear_identifier,
        last_synced_at=now,
    )
    await _save_issue_ref(str(task.agent_task_id), updated_ref)


# ---------------------------------------------------------------------------
# Orphan helpers
# ---------------------------------------------------------------------------


async def _create_ahs_task_from_linear(
    project_id: str,
    issue: dict[str, Any],
    now: datetime,
) -> None:
    """Create a new AHS task from a Linear issue that has no AHS counterpart.

    Status is capped at ``READY``; the task is never created as
    ``IN_PROGRESS`` because it has never been claimed by an executor.

    Args:
        project_id: UUID string of the AHS project to add the task to.
        issue: Linear issue dict.
        now: UTC timestamp used to stamp ``last_synced_at``.
    """
    proj_uuid = uuid.UUID(project_id)
    linear_issue_id = issue["id"]

    raw_status = _ahs_status(issue, has_unmet_deps=False)
    capped_status = AgentTaskStatus.READY if raw_status == AgentTaskStatus.IN_PROGRESS else raw_status

    async with get_async_session() as session:
        # Store Linear ref in nested format (consistent with _apply_linear_to_ahs
        # and _save_issue_ref). _extract_issue_ref reads nested format first.
        task = AgentTask(
            agent_project_id=proj_uuid,
            title=issue["title"],
            description=issue.get("description"),
            status=capped_status,
            priority=_ahs_priority(issue.get("priority")),
            task_data={
                "linear_ref": {
                    "linear_issue_id": linear_issue_id,
                    "linear_identifier": issue.get("identifier", ""),
                    "last_synced_at": now.isoformat(),
                },
            },
        )
        session.add(task)
        await session.commit()


# ---------------------------------------------------------------------------
# Linear API helpers
# ---------------------------------------------------------------------------


async def _fetch_team_statuses(linear_team_id: str) -> list[dict[str, str]]:
    """Fetch workflow states for a Linear team.

    Uses :meth:`~ypl.backend.utils.linear.LinearClient.get_workflow_states`
    and normalises the response to a flat list of ``{id, type, name}`` dicts
    suitable for :func:`~.mapping.map_ahs_status_to_linear`.

    Args:
        linear_team_id: UUID of the Linear team.

    Returns:
        List of state dicts.  Returns an empty list on API errors (callers
        will then skip status mapping rather than failing the whole sync).
    """
    client = LinearClient()
    response = await asyncio.to_thread(client.get_workflow_states, linear_team_id)
    errors = response.get("errors")
    if errors:
        logger.warning(
            "Failed to fetch Linear team workflow states; status mapping will be skipped",
            linear_team_id=linear_team_id,
            error=errors[0].get("message", str(errors)),
        )
        return []
    nodes = ((response.get("data") or {}).get("workflowStates") or {}).get("nodes", [])
    return [{"id": s["id"], "type": s.get("type", ""), "name": s.get("name", "")} for s in nodes]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


@retry_db
async def _update_project_last_synced(project_id: str, now: datetime) -> None:
    """Stamp ``last_synced_at`` on the project's ``linear_ref`` in ``project_data``.

    Args:
        project_id: UUID string of the AHS project.
        now: Timestamp to record.
    """
    proj_uuid = uuid.UUID(project_id)
    async with get_async_session() as session:
        db_result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == proj_uuid)
            .where(col(AgentProject.deleted_at).is_(None))
            .with_for_update()
        )
        project = db_result.scalars().first()
        if not project:
            logger.warning("Project not found when updating last_synced_at", project_id=project_id)
            return

        # Update last_synced_at in nested format (canonical) with legacy fallback.
        # _extract_project_ref reads nested format first, so we must update there.
        project_data = dict(project.project_data or {})
        linear_ref = project_data.get("linear_ref")
        if isinstance(linear_ref, dict):
            linear_ref["last_synced_at"] = now.isoformat()
            project_data["linear_ref"] = linear_ref
        else:
            # Legacy flat format
            project_data["last_synced_at"] = now.isoformat()
        project.project_data = project_data
        flag_modified(project, "project_data")
        session.add(project)
        await session.commit()
