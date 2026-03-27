"""MCP tools for Linear ↔ AHS project sync.

Provides tools for bidirectional sync between AHS projects and Linear:

Lookup tools:
* ``list_linear_teams`` — list all Linear teams accessible to the user.
* ``list_linear_projects`` — list Linear projects accessible to the user.
* ``resolve_linear_team`` — resolve a team name/key to its UUID.
* ``resolve_linear_project`` — resolve a project name/slug to its UUID.

Import tools (Linear → AHS):
* ``import_project_from_linear`` — import a Linear project into AHS as a new
  AgentProject with corresponding AgentTasks.
* ``link_project_to_linear`` — link an existing AHS project to an existing
  Linear project without syncing data.

Export tools (AHS → Linear):
* ``export_project_to_linear`` — export an entire AHS project (and all its
  tasks) to Linear as a project + issues. Idempotent: re-running reuses the
  existing Linear project and updates issues in place.
* ``push_task_status_to_linear`` — push a single task's status to its linked
  Linear issue. Useful for immediate updates right after a task transitions
  to ``COMPLETED``, ``FAILED``, etc.

Bidirectional sync:
* ``sync_project_with_linear`` — synchronise an AHS project with its linked
  Linear project in the specified direction (``'bidirectional'``,
  ``'linear_to_ahs'``, or ``'ahs_to_linear'``).
"""

from __future__ import annotations
import asyncio
import uuid
from typing import Any

from sqlmodel import col, select

import ypl.agent_harness_service.tools.linear_sync.export_to_linear as export_to_linear
from ypl.agent_harness_service.tools.linear_sync.bidirectional import (
    ConflictResolution,
    sync_bidirectional,
)
from ypl.agent_harness_service.tools.linear_sync.import_from_linear import (
    import_project_from_linear as linear_import_project,
)
from ypl.agent_harness_service.tools.linear_sync.import_from_linear import (
    sync_tasks_from_linear,
)
from ypl.agent_harness_service.tools.linear_sync.linear_utils import (
    list_projects as linear_list_projects,
)
from ypl.agent_harness_service.tools.linear_sync.linear_utils import (
    list_teams as linear_list_teams,
)
from ypl.agent_harness_service.tools.linear_sync.linear_utils import (
    resolve_project_id,
    resolve_team_id,
)
from ypl.agent_harness_service.tools.linear_sync.mapping import map_ahs_status_to_linear
from ypl.agent_harness_service.tools.linear_sync.types import LinearProjectRef
from ypl.backend.db import get_async_session, retry_db
from ypl.backend.utils.linear import LinearClient
from ypl.db.agent_harness import AgentProject, AgentTask
from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email
from ypl.mcp_server.core import get_authenticated_user_email, get_requesting_user_id, mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_auth() -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the authenticated user ID, falling back to e-mail lookup.

    Returns ``(user_id_str, None)`` on success or ``(None, error_dict)`` on
    failure.
    """
    user_id = get_requesting_user_id()
    if user_id:
        return str(user_id), None

    auth_email = get_authenticated_user_email()
    if auth_email == "unknown":
        return None, {"success": False, "error": "Authentication required"}

    resolved_id, user_error = await resolve_user_id_from_email(auth_email)
    if user_error:
        return None, {"success": False, "error": user_error}

    return str(resolved_id), None


# ---------------------------------------------------------------------------
# Lookup Tools
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="list_linear_teams",
    description=(
        "List all Linear teams accessible to the authenticated user. "
        "Returns team UUIDs, names, and keys. Use this to find the team_id "
        "needed for import_project_from_linear or export_project_to_linear."
    ),
)
async def list_linear_teams() -> dict[str, Any]:
    """List all Linear teams.

    Returns:
        Dictionary with list of teams, each containing id, name, key, and description.
    """
    try:
        teams = await asyncio.to_thread(linear_list_teams)
        return {
            "success": True,
            "teams": teams,
        }
    except Exception as e:
        logger.error("Error listing Linear teams", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="list_linear_projects",
    description=(
        "List Linear projects accessible to the authenticated user. "
        "Returns project UUIDs, names, and slug IDs. Use this to find the project_id "
        "needed for import_project_from_linear."
    ),
)
async def list_linear_projects(limit: int = 50) -> dict[str, Any]:
    """List Linear projects.

    Args:
        limit: Maximum number of projects to return (default 50).

    Returns:
        Dictionary with list of projects, each containing id, name, and slug_id.
    """
    try:
        projects = await asyncio.to_thread(linear_list_projects, limit=limit)
        return {
            "success": True,
            "projects": projects,
        }
    except Exception as e:
        logger.error("Error listing Linear projects", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="resolve_linear_team",
    description=(
        "Resolve a Linear team name or key to its UUID. "
        "Accepts a team key (e.g., 'YUP'), team name (e.g., 'Yupp AI'), or UUID. "
        "If already a UUID, returns it unchanged. Use this before calling tools "
        "that require a linear_team_id parameter."
    ),
)
async def resolve_linear_team(team: str) -> dict[str, Any]:
    """Resolve a team identifier to its UUID.

    Args:
        team: Team UUID, key (e.g., 'YUP'), or name (e.g., 'Yupp AI').

    Returns:
        Dictionary with the resolved team_id, or error if not found.
    """
    try:
        team_id = await asyncio.to_thread(resolve_team_id, team)
        if team_id:
            return {
                "success": True,
                "team_id": team_id,
                "input": team,
            }
        return {
            "success": False,
            "error": f"Could not resolve team '{team}'. Use list_linear_teams to see available teams.",
        }
    except Exception as e:
        logger.error("Error resolving Linear team", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="resolve_linear_project",
    description=(
        "Resolve a Linear project name or slug to its UUID. "
        "Accepts a project name (e.g., 'Litweezer'), slug ID, or UUID. "
        "If already a UUID, returns it unchanged. Use this before calling tools "
        "that require a linear_project_id parameter."
    ),
)
async def resolve_linear_project(project: str) -> dict[str, Any]:
    """Resolve a project identifier to its UUID.

    Args:
        project: Project UUID, name (e.g., 'Litweezer'), or slug ID.

    Returns:
        Dictionary with the resolved project_id, or error if not found.
    """
    try:
        project_id = await asyncio.to_thread(resolve_project_id, project)
        if project_id:
            return {
                "success": True,
                "project_id": project_id,
                "input": project,
            }
        return {
            "success": False,
            "error": f"Could not resolve project '{project}'. Use list_linear_projects to see available projects.",
        }
    except Exception as e:
        logger.error("Error resolving Linear project", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Import Tools (Linear → AHS)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="import_project_from_linear",
    description=(
        "Import a Linear project into AHS by fetching its issues and creating a new AgentProject "
        "with corresponding AgentTasks. Parent/child relationships and blocker dependencies from "
        "Linear are preserved. The project starts in PAUSED status — use set_project_status to "
        "activate it. Requires authentication; the authenticated user becomes the project creator."
    ),
)
# Note: @retry_db is intentionally NOT used here. The underlying import_from_linear function
# makes Linear API calls and creates the project + tasks in a single DB session. If a retry
# occurred after partial work, it could create duplicate projects/tasks.
async def import_project_from_linear(
    linear_project_id: str,
    linear_team_id: str,
    include_completed: bool = False,
) -> dict[str, Any]:
    """Import a Linear project into AHS.

    Args:
        linear_project_id: UUID of the Linear project to import.
        linear_team_id: UUID of the Linear team that owns the project.
        include_completed: If True, also import completed/cancelled issues.
            Defaults to False (only active issues are imported).

    Returns:
        Dictionary with the created project details and sync stats.
    """
    try:
        creator_user_id = get_requesting_user_id()
        if not creator_user_id:
            auth_email = get_authenticated_user_email()
            if auth_email == "unknown":
                return {"success": False, "error": "Authentication required"}
            resolved_user_id, user_error = await resolve_user_id_from_email(auth_email)
            if user_error or not resolved_user_id:
                return {"success": False, "error": user_error or "Could not resolve user"}
            creator_user_id = resolved_user_id

        project, sync_result = await linear_import_project(
            linear_project_id=linear_project_id,
            linear_team_id=linear_team_id,
            creator_user_id=creator_user_id,
            include_completed=include_completed,
        )

        logger.info(
            "Linear project imported via MCP tool",
            project_id=str(project.agent_project_id),
            linear_project_id=linear_project_id,
            created=sync_result.created,
        )

        return {
            "success": True,
            "project": {
                "agent_project_id": str(project.agent_project_id),
                "name": project.name,
                "description": project.description,
                "status": project.status.value,
                "project_data": project.project_data,
                "created_at": project.created_at.isoformat() if project.created_at else None,
            },
            "sync_stats": {
                "created": sync_result.created,
                "updated": sync_result.updated,
                "skipped": sync_result.skipped,
                "errors": sync_result.errors,
            },
        }

    except ValueError as e:
        logger.warning("Linear import failed: not found", error=str(e))
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("Error importing Linear project", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="link_project_to_linear",
    description=(
        "Link an existing AHS project to an existing Linear project by storing the Linear project "
        "and team IDs in the project's project_data. No data is synced — use sync_tasks_from_linear "
        "or import_project_from_linear for actual data import. Requires authentication."
    ),
)
@retry_db
async def link_project_to_linear(
    project_id: str,
    linear_project_id: str,
    linear_team_id: str,
) -> dict[str, Any]:
    """Link an existing AHS project to a Linear project.

    Stores the Linear project and team IDs in the AHS project's project_data
    so that subsequent sync operations can reference the Linear project.
    Does not fetch or sync any data from Linear.

    Args:
        project_id: UUID of the existing AHS AgentProject.
        linear_project_id: UUID of the Linear project to link to.
        linear_team_id: UUID of the Linear team that owns the project.

    Returns:
        Dictionary with the updated project details.
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required"}

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id!r}"}

        async with get_async_session() as session:
            result = await session.execute(
                select(AgentProject)
                .where(col(AgentProject.agent_project_id) == proj_uuid)
                .where(col(AgentProject.deleted_at).is_(None))
            )
            project = result.scalars().first()
            if not project:
                return {"success": False, "error": f"Project not found: {project_id}"}

            # Check if project is already linked to a different Linear project
            # Supports both nested format (canonical) and legacy flat format
            existing_data = project.project_data or {}
            existing_ref = existing_data.get("linear_ref") or {}
            existing_linear_project_id = existing_ref.get("linear_project_id") or existing_data.get("linear_project_id")
            if existing_linear_project_id and existing_linear_project_id != linear_project_id:
                return {
                    "success": False,
                    "error": (
                        f"Project is already linked to Linear project {existing_linear_project_id}. "
                        "Unlink the existing project first before linking to a different one."
                    ),
                }

            # Set last_synced_at to None so the first sync will pull all issues.
            # Using datetime.now() here would mislead sync logic into skipping pre-link issues.
            linear_ref = LinearProjectRef(
                linear_project_id=linear_project_id,
                linear_team_id=linear_team_id,
                last_synced_at=None,
            )
            project_data = dict(project.project_data or {})
            project_data["linear_ref"] = linear_ref.model_dump(mode="json")
            project.project_data = project_data

            session.add(project)
            await session.commit()
            await session.refresh(project)

        logger.info(
            "AHS project linked to Linear",
            project_id=project_id,
            linear_project_id=linear_project_id,
            linear_team_id=linear_team_id,
        )

        return {
            "success": True,
            "project": {
                "agent_project_id": str(project.agent_project_id),
                "name": project.name,
                "description": project.description,
                "status": project.status.value,
                "project_data": project.project_data,
                "created_at": project.created_at.isoformat() if project.created_at else None,
            },
        }

    except ValueError as e:
        # Invalid input (e.g., bad UUID format) - don't retry
        logger.warning("Error linking project to Linear: invalid input", error=str(e))
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Export Tools (AHS → Linear)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="export_project_to_linear",
    description=(
        "Export an AHS project and all its tasks to Linear as a project + issues. "
        "Idempotent: if the project has already been exported the existing Linear project "
        "is reused and issues are updated in place. "
        "Requires an authenticated user session."
    ),
)
@retry_db
async def export_project_to_linear(
    project_id: str,
    linear_team_id: str,
    project_name: str | None = None,
) -> dict[str, Any]:
    """Export an AHS project to Linear.

    Args:
        project_id: UUID of the AHS project to export.
        linear_team_id: UUID of the Linear team that will own the new project.
        project_name: Override for the Linear project display name.
            Defaults to the AHS project's own name.

    Returns:
        Dictionary with ``linear_project_id`` and sync statistics (created /
        updated / errors counts).
    """
    try:
        user_id, auth_error = await _resolve_auth()
        if auth_error:
            return auth_error

        logger.info("export_project_to_linear: starting", user_id=user_id, project_id=project_id)

        try:
            uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id!r}"}

        try:
            uuid.UUID(linear_team_id)
        except ValueError:
            return {"success": False, "error": f"Invalid linear_team_id: {linear_team_id!r}"}

        linear_project_id, result = await export_to_linear.export_project_to_linear(
            project_id=project_id,
            linear_team_id=linear_team_id,
            linear_project_name=project_name,
        )

        return {
            "success": True,
            "linear_project_id": linear_project_id,
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors": result.errors,
        }

    except ValueError as exc:
        logger.warning("export_project_to_linear: not found", error=str(exc))
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.error("export_project_to_linear: unexpected error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


@mcp_server.tool(
    name="push_task_status_to_linear",
    description=(
        "Push a single AHS task's current status to its linked Linear issue. "
        "The task must already have a Linear issue linked (i.e. the project must "
        "have been exported via export_project_to_linear first). "
        "Useful for immediate status updates right after a task completes or fails. "
        "Requires an authenticated user session."
    ),
)
# Note: @retry_db is intentionally used here despite making external Linear API calls.
# The Linear update_issue call is idempotent (setting state to the same value is safe),
# so retrying after a transient DB error won't cause duplicate side effects.
@retry_db
async def push_task_status_to_linear(
    task_id: str,
) -> dict[str, Any]:
    """Push one task's status to its linked Linear issue.

    Fetches the task and its parent project from the AHS database, resolves the
    corresponding Linear issue and team workflow states, then calls
    ``LinearClient.update_issue`` with the mapped state ID.

    Note: This function uses @retry_db for DB resilience. The Linear API calls are
    idempotent (re-setting the same state is safe), so retries won't cause issues.

    Args:
        task_id: UUID of the AHS task whose status should be pushed.

    Returns:
        Dictionary with ``linear_issue_id``, ``linear_identifier``, and the
        resolved ``linear_state_id``.
    """
    try:
        user_id, auth_error = await _resolve_auth()
        if auth_error:
            return auth_error

        logger.info("push_task_status_to_linear: starting", user_id=user_id, task_id=task_id)

        try:
            task_uuid = uuid.UUID(task_id)
        except ValueError:
            return {"success": False, "error": f"Invalid task_id: {task_id!r}"}

        # ------------------------------------------------------------------
        # 1. Fetch task and project from DB
        # ------------------------------------------------------------------
        async with get_async_session() as session:
            task_row = await session.execute(select(AgentTask).where(col(AgentTask.agent_task_id) == task_uuid))
            task: AgentTask | None = task_row.scalars().first()

            if task is None:
                return {"success": False, "error": f"Task not found: {task_id}"}

            # ------------------------------------------------------------------
            # 2. Resolve linear_issue_id from task_data
            #    Supports both nested format (canonical) and legacy flat format
            # ------------------------------------------------------------------
            task_data: dict[str, Any] = task.task_data or {}
            linear_ref = task_data.get("linear_ref") or {}
            linear_issue_id: str | None = linear_ref.get("linear_issue_id") or task_data.get("linear_issue_id")
            linear_identifier: str = linear_ref.get("linear_identifier") or task_data.get("linear_identifier", "")

            if not linear_issue_id:
                return {
                    "success": False,
                    "error": (
                        f"Task {task_id} has no linked Linear issue. "
                        "Export the project first via export_project_to_linear."
                    ),
                }

            # ------------------------------------------------------------------
            # 3. Resolve team_id from the project's project_data
            # ------------------------------------------------------------------
            proj_row = await session.execute(
                select(AgentProject).where(col(AgentProject.agent_project_id) == task.agent_project_id)
            )
            project: AgentProject | None = proj_row.scalars().first()

            if project is None:
                return {"success": False, "error": f"Parent project not found for task {task_id}"}

            # Supports both nested format (canonical) and legacy flat format
            project_data: dict[str, Any] = project.project_data or {}
            project_linear_ref = project_data.get("linear_ref") or {}
            linear_team_id: str | None = project_linear_ref.get("linear_team_id") or project_data.get("linear_team_id")

            if not linear_team_id:
                return {
                    "success": False,
                    "error": (
                        f"Parent project {task.agent_project_id} has no linked Linear team. "
                        "Export the project first via export_project_to_linear."
                    ),
                }

            # Capture values needed outside the session
            ahs_status: str = task.status.name if task.status else "PENDING"

        # ------------------------------------------------------------------
        # 4. Fetch team workflow states and map AHS status → Linear state ID
        # ------------------------------------------------------------------

        client = LinearClient()
        states_response = await asyncio.to_thread(client.get_workflow_states, linear_team_id)

        # Check for GraphQL errors before extracting data
        gql_errors = states_response.get("errors")
        if gql_errors:
            error_msg = gql_errors[0].get("message", str(gql_errors)) if gql_errors else "Unknown error"
            logger.warning(
                "push_task_status_to_linear: failed to fetch workflow states",
                task_id=task_id,
                linear_team_id=linear_team_id,
                error=error_msg,
            )
            return {"success": False, "error": f"Failed to fetch Linear workflow states: {error_msg}"}

        team_states: list[dict[str, str]] = (
            (states_response.get("data") or {}).get("workflowStates", {}).get("nodes", [])
        )

        linear_state_id: str | None = map_ahs_status_to_linear(ahs_status, team_states)

        if linear_state_id is None:
            logger.warning(
                "push_task_status_to_linear: no matching Linear state",
                task_id=task_id,
                ahs_status=ahs_status,
            )
            return {
                "success": False,
                "error": (f"No matching Linear workflow state for AHS status '{ahs_status}' in team {linear_team_id}."),
            }

        # ------------------------------------------------------------------
        # 5. Push the state update to Linear
        # ------------------------------------------------------------------
        response = await asyncio.to_thread(
            client.update_issue,
            issue_id=linear_issue_id,
            state_id=linear_state_id,
        )
        errors = response.get("errors")
        if errors:
            raise RuntimeError(
                f"update_issue failed for issue {linear_issue_id}: {errors[0].get('message', str(errors))}"
            )
        update_result = (response.get("data") or {}).get("issueUpdate", {})
        if isinstance(update_result, dict) and not update_result.get("success"):
            raise RuntimeError(f"update_issue returned success=False for issue {linear_issue_id}. Response: {response}")

        logger.info(
            "push_task_status_to_linear: pushed",
            task_id=task_id,
            linear_issue_id=linear_issue_id,
            ahs_status=ahs_status,
            linear_state_id=linear_state_id,
        )

        return {
            "success": True,
            "task_id": task_id,
            "linear_issue_id": linear_issue_id,
            "linear_identifier": linear_identifier,
            "ahs_status": ahs_status,
            "linear_state_id": linear_state_id,
        }

    except Exception as exc:
        logger.error("push_task_status_to_linear: unexpected error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Bidirectional Sync
# ---------------------------------------------------------------------------

_VALID_DIRECTIONS = frozenset({"bidirectional", "linear_to_ahs", "ahs_to_linear"})


@mcp_server.tool(
    name="sync_project_with_linear",
    description=(
        "Synchronise an AHS project with its linked Linear project. "
        "Three directions are supported:\n"
        "  • 'bidirectional' (default) — compares timestamps and updates whichever side is stale; "
        "    conflict resolution can be controlled via the conflict_resolution parameter.\n"
        "  • 'linear_to_ahs' — pull all recent changes from Linear into AHS (incremental import).\n"
        "  • 'ahs_to_linear' — push all AHS task changes to their linked Linear issues.\n"
        "The project must already be linked to a Linear project "
        "(via export_project_to_linear or link_project_to_linear). "
        "Requires an authenticated user session."
    ),
)
async def sync_project_with_linear(
    project_id: str,
    direction: str = "bidirectional",
    conflict_resolution: str = "latest_wins",
) -> dict[str, Any]:
    """Synchronise an AHS project with its linked Linear project.

    Args:
        project_id: UUID of the AHS project to sync.
        direction: Sync direction — one of ``'bidirectional'`` (default),
            ``'linear_to_ahs'``, or ``'ahs_to_linear'``.
        conflict_resolution: How to resolve conflicts when direction is
            ``'bidirectional'`` and both sides were modified.  One of
            ``'latest_wins'`` (default), ``'linear_wins'``, ``'ahs_wins'``,
            or ``'skip'``.  Ignored for unidirectional syncs.

    Returns:
        Dictionary with ``direction``, ``conflict_resolution`` (when
        bidirectional), and sync statistics (created / updated / skipped /
        errors counts).
    """
    try:
        user_id, auth_error = await _resolve_auth()
        if auth_error:
            return auth_error

        logger.info(
            "sync_project_with_linear: starting",
            user_id=user_id,
            project_id=project_id,
            direction=direction,
            conflict_resolution=conflict_resolution,
        )

        try:
            proj_uuid = uuid.UUID(project_id)
        except ValueError:
            return {"success": False, "error": f"Invalid project_id: {project_id!r}"}

        if direction not in _VALID_DIRECTIONS:
            return {
                "success": False,
                "error": (f"Invalid direction {direction!r}. Must be one of: {', '.join(sorted(_VALID_DIRECTIONS))}"),
            }

        # ------------------------------------------------------------------
        # Dispatch to the appropriate sync function
        # ------------------------------------------------------------------
        if direction == "linear_to_ahs":
            result = await sync_tasks_from_linear(project_id=proj_uuid)
            return {
                "success": True,
                "direction": direction,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": result.errors,
            }

        if direction == "ahs_to_linear":
            result = await export_to_linear.sync_tasks_to_linear(project_id=project_id)
            return {
                "success": True,
                "direction": direction,
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": result.errors,
            }

        # direction == "bidirectional"
        try:
            cr = ConflictResolution(conflict_resolution)
        except ValueError:
            valid = ", ".join(f"'{m.value}'" for m in ConflictResolution)
            return {
                "success": False,
                "error": (f"Invalid conflict_resolution {conflict_resolution!r}. Must be one of: {valid}"),
            }

        result = await sync_bidirectional(project_id=project_id, conflict_resolution=cr)
        return {
            "success": True,
            "direction": direction,
            "conflict_resolution": cr.value,
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors": result.errors,
        }

    except ValueError as exc:
        logger.warning("sync_project_with_linear: not found or invalid input", error=str(exc))
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.error("sync_project_with_linear: unexpected error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}
