"""Shared pytest fixtures for Linear sync tests."""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEAM_ID = "team-abc-123"
# Use a fixed UUID for deterministic test output and easier debugging
PROJECT_ID = "00000000-0000-0000-0000-000000000001"
LINEAR_PROJECT_ID = "lin-proj-abc-123"
LINEAR_TEAM_ID = "lin-team-abc-123"
CREATOR_USER_ID = "user-creator-001"

TEAM_STATUSES = [
    {"id": "state-triage", "type": "triage", "name": "Triage"},
    {"id": "state-backlog", "type": "backlog", "name": "Backlog"},
    {"id": "state-todo", "type": "unstarted", "name": "Todo"},
    {"id": "state-in-progress", "type": "started", "name": "In Progress"},
    {"id": "state-done", "type": "completed", "name": "Done"},
    {"id": "state-cancelled", "type": "cancelled", "name": "Cancelled"},
]


# ---------------------------------------------------------------------------
# AHS object factories
# ---------------------------------------------------------------------------


def make_project(
    *,
    project_id: str | None = None,
    name: str = "Test Project",
    project_data: dict[str, Any] | None = None,
) -> AgentProject:
    """Return an unsaved AgentProject instance for testing."""
    proj = AgentProject(
        name=name,
        description="A test project",
        status=AgentProjectStatus.PAUSED,
        creator_user_id=CREATOR_USER_ID,
        project_data=project_data,
    )
    proj.agent_project_id = uuid.UUID(project_id) if project_id else uuid.uuid4()
    return proj


def make_task(
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    title: str = "Test Task",
    description: str | None = "A test task description",
    status: AgentTaskStatus = AgentTaskStatus.READY,
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL,
    parent_task_id: uuid.UUID | None = None,
    depends_on: list[str] | None = None,
    task_data: dict[str, Any] | None = None,
) -> AgentTask:
    """Return an unsaved AgentTask instance for testing."""
    task = AgentTask(
        agent_project_id=uuid.UUID(project_id) if project_id else uuid.uuid4(),
        title=title,
        description=description,
        status=status,
        priority=priority,
        parent_task_id=parent_task_id,
        depends_on=depends_on,
        task_data=task_data,
    )
    task.agent_task_id = uuid.UUID(task_id) if task_id else uuid.uuid4()
    return task


# ---------------------------------------------------------------------------
# Linear API response factories
# ---------------------------------------------------------------------------


def make_linear_project_response(
    *,
    project_id: str = LINEAR_PROJECT_ID,
    name: str = "Linear Project",
    description: str | None = "Linear project description",
) -> dict[str, Any]:
    """Return a dict mimicking a Linear `get_project` response."""
    return {
        "data": {
            "project": {
                "id": project_id,
                "name": name,
                "description": description,
                "state": "started",
            }
        }
    }


def make_linear_issue(
    *,
    issue_id: str | None = None,
    identifier: str = "ENG-1",
    title: str = "Linear Issue",
    description: str | None = "Issue description",
    state_type: str = "unstarted",
    priority: int = 3,
    parent_id: str | None = None,
    blocked_by_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return a dict mimicking a single Linear issue from list_project_issues."""
    issue: dict[str, Any] = {
        "id": issue_id or str(uuid.uuid4()),
        "identifier": identifier,
        "title": title,
        "description": description,
        "priority": priority,
        "state": {"id": f"state-{state_type}", "type": state_type, "name": state_type.title()},
        "parent": {"id": parent_id} if parent_id else None,
        "relations": {"nodes": []},
    }
    for blocker_id in blocked_by_ids or []:
        issue["relations"]["nodes"].append({"type": "blocked_by", "relatedIssue": {"id": blocker_id}})
    return issue


def make_create_issue_response(
    *,
    issue_id: str | None = None,
    identifier: str = "ENG-1",
) -> dict[str, Any]:
    """Return a dict mimicking a successful `create_issue` API response."""
    return {
        "data": {
            "issueCreate": {
                "success": True,
                "issue": {
                    "id": issue_id or str(uuid.uuid4()),
                    "identifier": identifier,
                },
            }
        }
    }


def make_update_issue_response(
    *,
    issue_id: str = "issue-123",
    identifier: str = "ENG-1",
) -> dict[str, Any]:
    """Return a dict mimicking a successful `update_issue` API response."""
    return {
        "data": {
            "issueUpdate": {
                "success": True,
                "issue": {
                    "id": issue_id,
                    "identifier": identifier,
                },
            }
        }
    }


def make_create_project_response(
    *,
    project_id: str = LINEAR_PROJECT_ID,
) -> dict[str, Any]:
    """Return a dict mimicking a successful `create_project` API response."""
    return {
        "data": {
            "projectCreate": {
                "success": True,
                "project": {"id": project_id},
            }
        }
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def linear_project_id() -> str:
    return LINEAR_PROJECT_ID


@pytest.fixture
def linear_team_id() -> str:
    return LINEAR_TEAM_ID


@pytest.fixture
def sync_now() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def mock_linear_client() -> MagicMock:
    """Return a MagicMock that resembles LinearClient."""
    client = MagicMock()
    client.create_project.return_value = make_create_project_response()
    client.create_issue.return_value = make_create_issue_response()
    client.update_issue.return_value = make_update_issue_response()
    client.set_issue_blocked_by.return_value = None
    client.get_project.return_value = make_linear_project_response()
    client.list_project_issues.return_value = []
    return client


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _call_sync(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a sync function directly; used as side_effect for asyncio.to_thread patches."""
    return fn(*args, **kwargs)
