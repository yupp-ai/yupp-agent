#!/usr/bin/env python3
"""Quick e2e test script for Linear project import.

Usage:
    # Make sure .env has LINEAR_API_KEY set
    conda activate ys-dev
    poetry run python tests/agent_harness_service/e2e/test_linear_import.py import <project> <team>

Examples:
    # Using UUIDs
    poetry run python tests/agent_harness_service/e2e/test_linear_import.py import \\
        0d69dd47-90f8-49c9-ac47-466edd520b70 75eb453f-2fed-478c-989e-e96e2cf1024d

    # Using names/keys (case-insensitive)
    poetry run python tests/agent_harness_service/e2e/test_linear_import.py import Litweezer YUP
    poetry run python tests/agent_harness_service/e2e/test_linear_import.py import litweezer "Yupp AI"

    # List available teams and projects
    poetry run python tests/agent_harness_service/e2e/test_linear_import.py list
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


def list_linear_resources() -> None:
    """List all available Linear teams and projects."""
    from ypl.agent_harness_service.tools.linear_sync.linear_utils import list_projects, list_teams

    # List teams
    print("\n=== Teams ===")
    teams = list_teams()
    for team in teams:
        print(f"  {team['id']} - {team['name']} (key: {team.get('key', 'N/A')})")

    # List projects
    print("\n=== Projects (first 30) ===")
    projects = list_projects(limit=30)
    for proj in projects:
        print(f"  {proj['id']} - {proj['name']} (slug: {proj.get('slug_id', 'N/A')})")


async def resolve_user_id(user_input: str | None) -> str | None:
    """Resolve user ID from input or look up from team directory.

    Args:
        user_input: User ID, email, or Linear name. If None, tries to find any valid user.

    Returns:
        Valid user_id from the database, or None if not found.
    """
    from sqlmodel import col, select
    from ypl.backend.db import get_async_session
    from ypl.backend.llm.constants import LINEAR_TO_EMAIL, TEAM_DIRECTORY
    from ypl.db.users import User

    # If user provided a specific ID, try it directly
    if user_input:
        # Check if it's a Linear name -> convert to email
        if user_input in LINEAR_TO_EMAIL:
            email = LINEAR_TO_EMAIL[user_input]
            print(f"  Mapped Linear name '{user_input}' to email '{email}'")
            user_input = email

        # Try to find user by email or user_id
        async with get_async_session() as db:
            result = await db.execute(
                select(User)
                .where((col(User.email) == user_input) | (col(User.user_id) == user_input))
                .where(col(User.deleted_at).is_(None))
            )
            user = result.scalars().first()
            if user:
                print(f"  Found user: {user.user_id} ({user.email})")
                user_id: str = user.user_id
                return user_id

        print(f"  WARNING: User '{user_input}' not found in database")

    # Try to find any user from the team directory
    print("  Looking for any team member in database...")
    async with get_async_session() as db:
        team_emails = [member.email for member in TEAM_DIRECTORY if member.email]
        if team_emails:
            result = await db.execute(
                select(User).where(col(User.email).in_(team_emails)).where(col(User.deleted_at).is_(None)).limit(1)
            )
            user = result.scalars().first()
            if user:
                print(f"  Found team member: {user.user_id} ({user.email})")
                found_user_id: str = user.user_id
                return found_user_id

    return None


async def run_import(
    linear_project_input: str, linear_team_input: str, include_completed: bool = False, user_id: str | None = None
) -> None:
    """Run the import and print results."""
    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    # This prevents "failed to locate a name" errors from circular relationships
    from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear
    from ypl.agent_harness_service.tools.linear_sync.linear_utils import resolve_project_id, resolve_team_id
    from ypl.backend.config import settings
    from ypl.backend.utils.linear import LinearClient
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"Linear API Key configured: {'Yes' if settings.LINEAR_API_KEY else 'No'}")
    print(f"Database URL: {settings.POSTGRES_HOST[:50]}..." if settings.POSTGRES_HOST else "No POSTGRES_HOST")
    print()

    print(f"Input project: {linear_project_input}")
    print(f"Input team: {linear_team_input}")
    print(f"Include completed: {include_completed}")
    print(f"Input user: {user_id or '(auto-detect)'}")
    print("-" * 50)

    client = LinearClient()

    # Resolve team ID
    print("\nResolving team...")
    linear_team_id = resolve_team_id(linear_team_input, client)
    if not linear_team_id:
        print(f"  ERROR: Could not resolve team '{linear_team_input}'")
        print("  Use 'list' command to see available teams")
        return
    print(f"  Resolved: {linear_team_id}")

    # Resolve project ID
    print("\nResolving project...")
    linear_project_id = resolve_project_id(linear_project_input, client)
    if not linear_project_id:
        print(f"  ERROR: Could not resolve project '{linear_project_input}'")
        print("  Use 'list' command to see available projects")
        return
    print(f"  Resolved: {linear_project_id}")

    print("\nResolved IDs:")
    print(f"  Project: {linear_project_id}")
    print(f"  Team: {linear_team_id}")
    print("-" * 50)

    # Verify project exists
    print("\nVerifying project access...")
    try:
        project_resp = client.get_project(linear_project_id)
        project_data = (project_resp.get("data") or {}).get("project")
        if project_data:
            print(f"  Found project: {project_data.get('name')}")
        else:
            print(f"  Project not found! Response: {project_resp}")
            return
    except Exception as e:
        print(f"  get_project failed: {e}")
        return

    # Test fetching issues
    print("\nFetching issues...")
    try:
        issues = client.list_project_issues(linear_project_id, include_completed=include_completed)
        print(f"  Found {len(issues)} issues")
    except Exception as e:
        print(f"  list_project_issues failed: {e}")
        raise

    # Resolve user ID
    print("\nResolving user...")
    creator_user_id = await resolve_user_id(user_id)
    if not creator_user_id:
        print("  ERROR: Could not find a valid user in the database")
        print("  Use --user-id to specify a user ID or email")
        return

    # Run the import
    print("\nRunning import...")
    try:
        project, result = await import_project_from_linear(
            linear_project_id=linear_project_id,
            linear_team_id=linear_team_id,
            creator_user_id=creator_user_id,
            include_completed=include_completed,
        )

        print()
        print("SUCCESS!")
        print(f"  Project ID: {project.agent_project_id}")
        print(f"  Project Name: {project.name}")
        print(f"  Description: {project.description[:100] if project.description else '(none)'}...")
        print(f"  Status: {project.status}")
        print()
        print("Sync Result:")
        print(f"  Created: {result.created}")
        print(f"  Updated: {result.updated}")
        print(f"  Skipped: {result.skipped}")
        print(f"  Errors: {result.errors}")

    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        raise


async def run_sync(project_id: str) -> None:
    """Run incremental sync on an existing project."""
    from ypl.agent_harness_service.tools.linear_sync.import_from_linear import sync_tasks_from_linear

    # Import all DB models to ensure SQLAlchemy mappers are fully initialized
    from ypl.db.all_models import all_models  # noqa: F401

    print(f"Syncing project: {project_id}")
    print("-" * 50)

    try:
        result = await sync_tasks_from_linear(project_id=uuid.UUID(project_id))

        print()
        print("SYNC SUCCESS!")
        print(f"  Created: {result.created}")
        print(f"  Updated: {result.updated}")
        print(f"  Skipped: {result.skipped}")
        print(f"  Errors: {result.errors}")

    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        raise


def main() -> None:
    # Load environment first (guarded to avoid side effects at import time)
    _load_environment()

    parser = argparse.ArgumentParser(description="Test Linear project import")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List command
    subparsers.add_parser("list", help="List available Linear teams and projects")

    # Import command
    import_parser = subparsers.add_parser("import", help="Import a Linear project")
    import_parser.add_argument(
        "linear_project",
        help="Linear project UUID, name, or slug (e.g., 'Litweezer' or '0d69dd47-...')",
    )
    import_parser.add_argument(
        "linear_team",
        help="Linear team UUID, key, or name (e.g., 'YUP' or 'Yupp AI')",
    )
    import_parser.add_argument("--include-completed", action="store_true", help="Include completed issues")
    import_parser.add_argument(
        "--user-id",
        help="User ID for creator_user_id (defaults to first user found in DB)",
    )

    # Sync command
    sync_parser = subparsers.add_parser("sync", help="Sync an existing AHS project")
    sync_parser.add_argument("project_id", help="AHS project UUID")

    args = parser.parse_args()

    if args.command == "list":
        list_linear_resources()
    elif args.command == "import":
        asyncio.run(run_import(args.linear_project, args.linear_team, args.include_completed, args.user_id))
    elif args.command == "sync":
        asyncio.run(run_sync(args.project_id))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
