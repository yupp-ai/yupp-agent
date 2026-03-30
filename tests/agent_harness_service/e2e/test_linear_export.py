#!/usr/bin/env python3
"""Quick e2e test script for Linear project export.

Usage:
    # Make sure .env has LINEAR_API_KEY set
    conda activate ys-dev
    poetry run python tests/agent_harness_service/e2e/test_linear_export.py <command> [args]

Commands:
    list-projects           List AHS projects available for export
    export <project_id> <team>  Export an AHS project to Linear
    sync <project_id>       Sync task updates to Linear (project must be exported first)
    push-status <task_id>   Push a single task's status to Linear

Examples:
    # List AHS projects
    poetry run python tests/agent_harness_service/e2e/test_linear_export.py list-projects

    # Export an AHS project to Linear (team can be name, key, or UUID)
    poetry run python tests/agent_harness_service/e2e/test_linear_export.py export \\
        0d69dd47-90f8-49c9-ac47-466edd520b70 YUP

    # Sync task updates to Linear
    poetry run python tests/agent_harness_service/e2e/test_linear_export.py sync \\
        0d69dd47-90f8-49c9-ac47-466edd520b70

    # Push a single task's status to Linear
    poetry run python tests/agent_harness_service/e2e/test_linear_export.py push-status \\
        a1b2c3d4-e5f6-7890-abcd-ef1234567890
"""

from __future__ import annotations
import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path


def _load_environment() -> None:
    """Load environment variables from .env file.

    This is wrapped in a function to avoid side effects at import time,
    which would cause issues during pytest collection.
    """
    from dotenv import load_dotenv

    # Try to load .env from current directory or repo root
    env_path = Path(".env")
    if not env_path.exists():
        # Script is in tests/agent_harness_service/e2e/, so go up 3 levels to repo root
        env_path = Path(__file__).parent.parent.parent.parent / ".env"

    if env_path.exists():
        print(f"Loading .env from: {env_path.resolve()}")
        load_dotenv(env_path)
    else:
        print(f"WARNING: No .env file found at {env_path.resolve()}")
        load_dotenv()  # Try default locations

    # Debug: Check if LINEAR_API_KEY was loaded
    api_key_from_env = os.environ.get("LINEAR_API_KEY")
    if api_key_from_env:
        print("LINEAR_API_KEY configured: True")
    else:
        print("WARNING: LINEAR_API_KEY not found in environment!")


async def list_ahs_projects(limit: int = 20) -> None:
    """List AHS projects available for export."""
    from sqlalchemy import func
    from sqlmodel import col, select
    from ypl.backend.db import get_async_session
    from ypl.db.agent_harness import AgentProject, AgentTask

    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"\n=== AHS Projects (limit {limit}) ===")

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentProject)
            .where(col(AgentProject.deleted_at).is_(None))
            .order_by(col(AgentProject.created_at).desc())
            .limit(limit)
        )
        projects = list(result.scalars().all())

        if not projects:
            print("  No projects found")
            return

        for proj in projects:
            # Get task count using COUNT query (avoids fetching all task objects)
            count_result = await session.execute(
                select(func.count())
                .select_from(AgentTask)
                .where(col(AgentTask.agent_project_id) == proj.agent_project_id)
                .where(col(AgentTask.deleted_at).is_(None))
            )
            task_count = count_result.scalar() or 0

            # Check if already linked to Linear (supports both nested and legacy flat format)
            project_data = proj.project_data or {}
            linear_ref = project_data.get("linear_ref") or {}
            linear_project_id = linear_ref.get("linear_project_id") or project_data.get("linear_project_id")
            linear_status = f" [Linear: {linear_project_id[:8]}...]" if linear_project_id else ""

            print(f"  {proj.agent_project_id}")
            print(f"    Name: {proj.name}")
            print(f"    Status: {proj.status.value if proj.status else 'N/A'}")
            print(f"    Tasks: {task_count}{linear_status}")
            print()


async def run_export(
    project_id: str,
    linear_team_input: str,
    project_name: str | None = None,
) -> None:
    """Run the export and print results."""
    from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
    from ypl.agent_harness_service.tools.linear_sync.linear_utils import resolve_team_id
    from ypl.backend.config import settings
    from ypl.backend.utils.linear import LinearClient

    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"Linear API Key configured: {'Yes' if settings.LINEAR_API_KEY else 'No'}")
    print(f"Database URL: {settings.POSTGRES_HOST[:50]}..." if settings.POSTGRES_HOST else "No POSTGRES_HOST")
    print()

    print(f"Input project: {project_id}")
    print(f"Input team: {linear_team_input}")
    print(f"Project name override: {project_name or '(use AHS project name)'}")
    print("-" * 50)

    # Validate project_id format
    try:
        uuid.UUID(project_id)
    except ValueError:
        print(f"  ERROR: Invalid project_id format: {project_id}")
        return

    # Resolve team ID
    print("\nResolving team...")
    client = LinearClient()
    linear_team_id = resolve_team_id(linear_team_input, client)
    if not linear_team_id:
        print(f"  ERROR: Could not resolve team '{linear_team_input}'")
        print("  Use the import script's 'list' command to see available teams")
        return
    print(f"  Resolved: {linear_team_id}")

    # Run the export
    print("\nRunning export...")
    try:
        linear_project_id, result = await export_project_to_linear(
            project_id=project_id,
            linear_team_id=linear_team_id,
            linear_project_name=project_name,
        )

        print()
        print("SUCCESS!")
        print(f"  Linear Project ID: {linear_project_id}")
        print()
        print("Export Result:")
        print(f"  Created: {result.created}")
        print(f"  Updated: {result.updated}")
        print(f"  Skipped: {result.skipped}")
        print(f"  Errors: {result.errors}")

    except ValueError as e:
        print(f"NOT FOUND: {e}")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        raise


async def run_sync(project_id: str) -> None:
    """Run syncing task updates to Linear."""
    from ypl.agent_harness_service.tools.linear_sync.export_to_linear import sync_tasks_to_linear

    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"Syncing project: {project_id}")
    print("-" * 50)

    # Validate project_id format
    try:
        uuid.UUID(project_id)
    except ValueError:
        print(f"  ERROR: Invalid project_id format: {project_id}")
        return

    try:
        result = await sync_tasks_to_linear(project_id=project_id)

        print()
        print("SYNC SUCCESS!")
        print(f"  Created: {result.created}")
        print(f"  Updated: {result.updated}")
        print(f"  Skipped: {result.skipped}")
        print(f"  Errors: {result.errors}")

    except ValueError as e:
        print(f"NOT FOUND or NOT EXPORTED: {e}")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        raise


async def run_push_status(task_id: str) -> None:
    """Run pushing a single task's status to Linear."""
    from sqlmodel import col, select
    from ypl.agent_harness_service.tools.linear_sync.mapping import map_ahs_status_to_linear
    from ypl.backend.db import get_async_session
    from ypl.backend.utils.linear import LinearClient
    from ypl.db.agent_harness import AgentProject, AgentTask

    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"Pushing status for task: {task_id}")
    print("-" * 50)

    # Validate task_id format
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        print(f"  ERROR: Invalid task_id format: {task_id}")
        return

    # Fetch task and project
    async with get_async_session() as session:
        task_row = await session.execute(select(AgentTask).where(col(AgentTask.agent_task_id) == task_uuid))
        task = task_row.scalars().first()

        if not task:
            print(f"  ERROR: Task not found: {task_id}")
            return

        print(f"\nTask: {task.title}")
        print(f"  Status: {task.status.name if task.status else 'N/A'}")

        # Supports both nested format (canonical) and legacy flat format
        task_data = task.task_data or {}
        task_linear_ref = task_data.get("linear_ref") or {}
        linear_issue_id = task_linear_ref.get("linear_issue_id") or task_data.get("linear_issue_id")
        linear_identifier = task_linear_ref.get("linear_identifier") or task_data.get("linear_identifier", "")

        if not linear_issue_id:
            print("  ERROR: Task has no linked Linear issue. Export the project first.")
            return

        print(f"  Linear Issue: {linear_identifier} ({linear_issue_id})")

        # Get project to find team_id
        proj_row = await session.execute(
            select(AgentProject).where(col(AgentProject.agent_project_id) == task.agent_project_id)
        )
        project = proj_row.scalars().first()

        if not project:
            print(f"  ERROR: Parent project not found: {task.agent_project_id}")
            return

        # Supports both nested format (canonical) and legacy flat format
        project_data = project.project_data or {}
        proj_linear_ref = project_data.get("linear_ref") or {}
        linear_team_id = proj_linear_ref.get("linear_team_id") or project_data.get("linear_team_id")

        if not linear_team_id:
            print("  ERROR: Parent project has no linked Linear team. Export the project first.")
            return

        ahs_status = task.status.name if task.status else "PENDING"
        task_title = task.title  # Extract before session ends

    # Get workflow states and map status
    print("\nFetching Linear workflow states...")
    client = LinearClient()
    states_response = client.get_workflow_states(linear_team_id)

    gql_errors = states_response.get("errors")
    if gql_errors:
        error_msg = gql_errors[0].get("message", str(gql_errors)) if gql_errors else "Unknown error"
        print(f"  ERROR: Failed to fetch workflow states: {error_msg}")
        return

    team_states = (states_response.get("data") or {}).get("workflowStates", {}).get("nodes", [])
    print(f"  Found {len(team_states)} workflow states")

    linear_state_id = map_ahs_status_to_linear(ahs_status, team_states)

    if not linear_state_id:
        print(f"  ERROR: No matching Linear state for AHS status '{ahs_status}'")
        print("  Available states:")
        for state in team_states:
            print(f"    - {state.get('name')} ({state.get('type')}): {state.get('id')}")
        return

    # Find the state name for display
    state_name = next((s.get("name") for s in team_states if s.get("id") == linear_state_id), "Unknown")
    print(f"  Mapped '{ahs_status}' -> '{state_name}' ({linear_state_id})")

    # Push the update
    print("\nPushing status update to Linear...")
    try:
        response = client.update_issue(issue_id=linear_issue_id, state_id=linear_state_id)

        errors = response.get("errors")
        if errors:
            print(f"  ERROR: update_issue failed: {errors[0].get('message', str(errors))}")
            return

        update_result = (response.get("data") or {}).get("issueUpdate", {})
        if isinstance(update_result, dict) and not update_result.get("success"):
            print(f"  ERROR: update_issue returned success=False: {response}")
            return

        print()
        print("SUCCESS!")
        print(f"  Task: {task_title}")
        print(f"  Linear Issue: {linear_identifier}")
        print(f"  New State: {state_name}")

    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        raise


def main() -> None:
    _load_environment()
    parser = argparse.ArgumentParser(description="Test Linear project export")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List projects command
    list_parser = subparsers.add_parser("list-projects", help="List AHS projects available for export")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum number of projects to show")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export an AHS project to Linear")
    export_parser.add_argument(
        "project_id",
        help="AHS project UUID to export",
    )
    export_parser.add_argument(
        "linear_team",
        help="Linear team UUID, key, or name (e.g., 'YUP' or 'Yupp AI')",
    )
    export_parser.add_argument(
        "--name",
        help="Override the Linear project name (defaults to AHS project name)",
    )

    # Sync command
    sync_parser = subparsers.add_parser("sync", help="Sync task updates to Linear")
    sync_parser.add_argument("project_id", help="AHS project UUID (must be exported first)")

    # Push status command
    push_parser = subparsers.add_parser("push-status", help="Push a single task's status to Linear")
    push_parser.add_argument("task_id", help="AHS task UUID (must be linked to a Linear issue)")

    args = parser.parse_args()

    if args.command == "list-projects":
        asyncio.run(list_ahs_projects(args.limit))
    elif args.command == "export":
        asyncio.run(run_export(args.project_id, args.linear_team, args.name))
    elif args.command == "sync":
        asyncio.run(run_sync(args.project_id))
    elif args.command == "push-status":
        asyncio.run(run_push_status(args.task_id))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
