"""Unit tests for ypl/mcp_server/tools/project_tasks.py.

Covers all MCP tool handlers and helpers with mocked DB/external deps.
No live database is required.
"""

from __future__ import annotations
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ypl.mcp_server.tools.project_tasks as _pt
from ypl.db.agent_harness import (
    AgentProject,
    AgentProjectStatus,
    AgentTask,
    AgentTaskPriority,
    AgentTaskStatus,
)
from ypl.mcp_common.auth_context import RequestContext, request_context
from ypl.mcp_server.tools.project_tasks import (
    _batch_resolve_agent_names,
    _format_project,
    _format_task_row,
    _get_task_status_summary,
    _parse_priority,
    _promote_dependents_to_ready,
    _resolve_agent_id_by_name,
    _resolve_agent_name_by_id,
)

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
add_project = _pt.add_project.fn
add_task_sequence = _pt.add_task_sequence.fn
add_tasks = _pt.add_tasks.fn
claim_task = _pt.claim_task.fn
get_project = _pt.get_project.fn
get_project_state = _pt.get_project_state.fn
get_project_tasks = _pt.get_project_tasks.fn
get_ready_tasks = _pt.get_ready_tasks.fn
get_task = _pt.get_task.fn
list_projects = _pt.list_projects.fn
resume_failed_task = _pt.resume_failed_task.fn
set_project_state = _pt.set_project_state.fn
set_project_status = _pt.set_project_status.fn
set_task_dependencies = _pt.set_task_dependencies.fn
set_task_status = _pt.set_task_status.fn
update_project = _pt.update_project.fn
update_task = _pt.update_task.fn

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PROJECT_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
AGENT_ID = uuid.uuid4()
USER_ID = "user-abc-123"
NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _grant_project_admin_by_default() -> Any:
    """By default, treat the test caller as MANAGE_AGENT_PROJECTS-holder so the
    ownership check in mutating tools is a no-op. Individual tests that care
    about the check should override this patch within their own scope.
    """
    with patch(
        "ypl.mcp_server.tools.project_tasks.has_permission_by_user_id_cached",
        new=AsyncMock(return_value=True),
    ):
        yield


def _set_auth_context(user_id: str = USER_ID, email: str = "test@example.com") -> None:
    """Set up a valid authenticated request context."""
    request_context.set(RequestContext(auth_kind="oauth_user", requesting_user_id=user_id, audit_email=email))


def _set_no_auth_context() -> None:
    """Clear the auth context to simulate unauthenticated request."""
    request_context.set(None)


def _make_project(
    project_id: str | None = None,
    name: str = "Test Project",
    status: AgentProjectStatus = AgentProjectStatus.PAUSED,
    default_agent_id: uuid.UUID | None = None,
    shared_state: dict | None = None,
    creator_user_id: str | None = None,
) -> MagicMock:
    proj = MagicMock(spec=AgentProject)
    proj.agent_project_id = uuid.UUID(project_id) if project_id else uuid.uuid4()
    proj.name = name
    proj.description = "A test project"
    proj.status = status
    proj.slack_channel = None
    proj.default_agent_id = default_agent_id
    proj.project_data = None
    proj.shared_state = shared_state
    proj.creator_user_id = creator_user_id or USER_ID  # owned by the default caller in _set_auth_context
    proj.created_at = NOW
    proj.deleted_at = None
    return proj


def _make_task(
    task_id: str | None = None,
    project_id: str | None = None,
    title: str = "Test Task",
    status: AgentTaskStatus = AgentTaskStatus.READY,
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL,
    depends_on: list[str] | None = None,
    agent_id: uuid.UUID | None = None,
) -> MagicMock:
    task = MagicMock(spec=AgentTask)
    task.agent_task_id = uuid.UUID(task_id) if task_id else uuid.uuid4()
    task.agent_project_id = uuid.UUID(project_id) if project_id else uuid.uuid4()
    task.title = title
    task.description = "A test task"
    task.status = status
    task.priority = priority
    task.parent_task_id = None
    task.depends_on = depends_on
    task.agent_id = agent_id
    task.result = None
    task.task_data = None
    task.estimated_effort = None
    task.completed_at = None
    task.created_at = NOW
    task.deleted_at = None
    return task


def _make_mock_session() -> MagicMock:
    """Create a mock async session."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_async_session_ctx(session: MagicMock) -> Any:
    """Return a context manager that yields the given mock session."""

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return _ctx()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestParsePriority:
    def test_valid_priority_normal(self) -> None:
        priority, err = _parse_priority("NORMAL", "test")
        assert priority == AgentTaskPriority.NORMAL
        assert err is None

    def test_valid_priority_urgent(self) -> None:
        priority, err = _parse_priority("URGENT", "test")
        assert priority == AgentTaskPriority.URGENT
        assert err is None

    def test_invalid_priority_returns_error(self) -> None:
        priority, err = _parse_priority("BOGUS", "test context")
        assert priority is None
        assert err is not None
        assert "Invalid priority" in err["error"]
        assert "BOGUS" in err["error"]


class TestFormatTaskRow:
    def test_basic_task_row(self) -> None:
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID)
        result = _format_task_row(task, "my-agent")
        assert result["agent_task_id"] == TASK_ID
        assert result["agent_project_id"] == PROJECT_ID
        assert result["title"] == "Test Task"
        assert result["status"] == AgentTaskStatus.READY.value
        assert result["agent_name"] == "my-agent"
        assert result["completed_at"] is None

    def test_task_row_with_completed_at(self) -> None:
        task = _make_task()
        task.completed_at = NOW
        result = _format_task_row(task, None)
        assert result["completed_at"] == NOW.isoformat()

    def test_task_row_no_agent(self) -> None:
        task = _make_task()
        result = _format_task_row(task, None)
        assert result["agent_name"] is None


class TestFormatProject:
    def test_basic_project(self) -> None:
        proj = _make_project(project_id=PROJECT_ID)
        result = _format_project(proj)
        assert result["agent_project_id"] == PROJECT_ID
        assert result["name"] == "Test Project"
        assert result["status"] == AgentProjectStatus.PAUSED.value
        assert result["default_agent_name"] is None

    def test_project_with_agent_name(self) -> None:
        proj = _make_project(project_id=PROJECT_ID, default_agent_id=AGENT_ID)
        result = _format_project(proj, "my-agent")
        assert result["default_agent_name"] == "my-agent"
        assert result["default_agent_id"] == str(AGENT_ID)

    def test_project_created_at_isoformat(self) -> None:
        proj = _make_project()
        result = _format_project(proj)
        assert result["created_at"] == NOW.isoformat()


class TestResolveAgentIdByName:
    async def test_agent_found(self) -> None:
        session = _make_mock_session()
        mock_agent = MagicMock()
        mock_agent.agent_id = AGENT_ID
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = mock_agent
        session.execute.return_value = result_mock

        result = await _resolve_agent_id_by_name(session, "my-agent")
        assert result == AGENT_ID

    async def test_agent_not_found(self) -> None:
        session = _make_mock_session()
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        session.execute.return_value = result_mock

        result = await _resolve_agent_id_by_name(session, "unknown-agent")
        assert result is None


class TestResolveAgentNameById:
    async def test_name_found(self) -> None:
        session = _make_mock_session()
        mock_row = MagicMock()
        mock_row.name = "test-agent"
        exec_result = MagicMock()
        exec_result.first.return_value = mock_row
        session.execute = AsyncMock(return_value=exec_result)

        result = await _resolve_agent_name_by_id(session, AGENT_ID)
        assert result == "test-agent"

    async def test_name_not_found(self) -> None:
        session = _make_mock_session()
        exec_result = MagicMock()
        exec_result.first.return_value = None
        session.execute = AsyncMock(return_value=exec_result)

        result = await _resolve_agent_name_by_id(session, AGENT_ID)
        assert result is None


class TestBatchResolveAgentNames:
    async def test_empty_set_returns_empty_dict(self) -> None:
        session = _make_mock_session()
        result = await _batch_resolve_agent_names(session, set())
        assert result == {}

    async def test_resolves_multiple_agents(self) -> None:
        session = _make_mock_session()
        id1, id2 = uuid.uuid4(), uuid.uuid4()
        row1 = MagicMock()
        row1.agent_id = id1
        row1.name = "agent-one"
        row2 = MagicMock()
        row2.agent_id = id2
        row2.name = "agent-two"
        exec_result = MagicMock()
        exec_result.all.return_value = [row1, row2]
        session.execute = AsyncMock(return_value=exec_result)

        result = await _batch_resolve_agent_names(session, {id1, id2})
        assert result[id1] == "agent-one"
        assert result[id2] == "agent-two"


class TestGetTaskStatusSummary:
    async def test_returns_summary_dict(self) -> None:
        session = _make_mock_session()
        # Simulate rows: 2 READY, 1 COMPLETED
        row1 = (AgentTaskStatus.READY, 2)
        row2 = (AgentTaskStatus.COMPLETED, 1)
        exec_result = MagicMock()
        exec_result.all.return_value = [row1, row2]
        session.execute = AsyncMock(return_value=exec_result)

        proj_id = uuid.UUID(PROJECT_ID)
        result = await _get_task_status_summary(session, proj_id)
        assert result["READY"] == 2
        assert result["COMPLETED"] == 1
        assert result["PENDING"] == 0
        assert result["total"] == 3


class TestPromoteDependentsToReady:
    async def test_no_candidates_returns_empty(self) -> None:
        session = _make_mock_session()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=exec_result)

        proj_id = uuid.UUID(PROJECT_ID)
        task_id = uuid.uuid4()
        result = await _promote_dependents_to_ready(session, task_id, proj_id)
        assert result == []

    async def test_promotes_task_when_all_deps_completed(self) -> None:
        session = _make_mock_session()
        completed_id = uuid.uuid4()
        dep_task_id = uuid.uuid4()

        # Candidate task that depends on the completed task
        candidate = _make_task(
            task_id=str(dep_task_id),
            status=AgentTaskStatus.BLOCKED,
            depends_on=[str(completed_id)],
        )

        # First execute: get candidates
        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = [candidate]

        # Second execute: get dep statuses
        dep_row = MagicMock()
        dep_row.status = AgentTaskStatus.COMPLETED
        dep_result = MagicMock()
        dep_result.all.return_value = [dep_row]

        session.execute.side_effect = [candidates_result, dep_result]

        proj_id = uuid.UUID(PROJECT_ID)
        result = await _promote_dependents_to_ready(session, completed_id, proj_id)
        assert str(dep_task_id) in result
        assert candidate.status == AgentTaskStatus.READY

    async def test_skips_task_not_in_completed_deps(self) -> None:
        session = _make_mock_session()
        completed_id = uuid.uuid4()
        other_id = uuid.uuid4()

        # A candidate that depends on a different task
        candidate = _make_task(
            status=AgentTaskStatus.BLOCKED,
            depends_on=[str(other_id)],
        )

        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = [candidate]

        session.execute.return_value = candidates_result

        proj_id = uuid.UUID(PROJECT_ID)
        result = await _promote_dependents_to_ready(session, completed_id, proj_id)
        assert result == []


# ---------------------------------------------------------------------------
# add_project
# ---------------------------------------------------------------------------


class TestAddProject:
    async def test_success(self) -> None:
        _set_auth_context()

        mock_session = _make_mock_session()
        mock_session.refresh.side_effect = lambda _: None

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_project("Test Project", description="desc", slack_channel="C123")

        assert result["success"] is True
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await add_project("Test Project")
        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_no_requesting_user_id_returns_error(self) -> None:
        """An auth context with ``requesting_user_id=None`` is no longer a
        valid identity at the tool layer — the OAuth/DevToken middleware
        resolves the email to a user_id at verify time. Tools that need an
        attributable caller raise ``PermissionError`` via
        ``require_caller_user_id``."""
        request_context.set(
            RequestContext(auth_kind="oauth_user", requesting_user_id=None, audit_email="engineer@example.com")
        )
        result = await add_project("Test")
        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB is down"),
        ):
            result = await add_project("Test")

        assert result["success"] is False
        assert "DB is down" in result["error"]


# ---------------------------------------------------------------------------
# add_task_sequence
# ---------------------------------------------------------------------------


class TestAddTaskSequence:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await add_task_sequence(PROJECT_ID, '[{"title": "T1"}]')
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await add_task_sequence("not-a-uuid", '[{"title": "T1"}]')
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_tasks_json(self) -> None:
        _set_auth_context()
        result = await add_task_sequence(PROJECT_ID, "not-json")
        assert result["success"] is False
        assert "Invalid tasks JSON" in result["error"]

    async def test_empty_tasks_array(self) -> None:
        _set_auth_context()
        result = await add_task_sequence(PROJECT_ID, "[]")
        assert result["success"] is False
        assert "non-empty JSON array" in result["error"]

    async def test_tasks_not_array(self) -> None:
        _set_auth_context()
        result = await add_task_sequence(PROJECT_ID, '{"title": "T1"}')
        assert result["success"] is False
        assert "non-empty JSON array" in result["error"]

    async def test_project_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        # Project not found
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_task_sequence(PROJECT_ID, '[{"title": "T1"}]')

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_invalid_parent_task_id(self) -> None:
        _set_auth_context()
        result = await add_task_sequence(PROJECT_ID, '[{"title": "T1"}]', parent_task_id="bad-uuid")
        assert result["success"] is False
        assert "Invalid parent_task_id" in result["error"]

    async def test_parent_task_not_found(self) -> None:
        _set_auth_context()
        parent_id = str(uuid.uuid4())
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        parent_result = MagicMock()
        parent_result.scalars.return_value.first.return_value = None
        mock_session.execute.side_effect = [proj_result, parent_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_task_sequence(PROJECT_ID, '[{"title": "T1"}]', parent_task_id=parent_id)

        assert result["success"] is False
        assert "Parent task not found" in result["error"]

    async def test_task_missing_title(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_task_sequence(PROJECT_ID, '[{"description": "no title"}]')

        assert result["success"] is False
        assert "'title'" in result["error"]

    async def test_success_single_task(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        created_task = _make_task(project_id=PROJECT_ID, title="T1", status=AgentTaskStatus.READY)
        mock_session.flush = AsyncMock()

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.AgentTask",
                return_value=created_task,
            ),
        ):
            result = await add_task_sequence(PROJECT_ID, '[{"title": "T1"}]')

        assert result["success"] is True
        assert result["task_count"] == 1

    async def test_invalid_priority_in_task(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_task_sequence(PROJECT_ID, '[{"title": "T1", "priority": "INVALID"}]')

        assert result["success"] is False
        assert "Invalid priority" in result["error"]


# ---------------------------------------------------------------------------
# add_tasks
# ---------------------------------------------------------------------------


class TestAddTasks:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await add_tasks(PROJECT_ID, '[{"name": "t1", "title": "T1"}]')
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await add_tasks("bad-uuid", '[{"name": "t1", "title": "T1"}]')
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_tasks_json(self) -> None:
        _set_auth_context()
        result = await add_tasks(PROJECT_ID, "not-json")
        assert result["success"] is False
        assert "Invalid tasks JSON" in result["error"]

    async def test_empty_tasks_array(self) -> None:
        _set_auth_context()
        result = await add_tasks(PROJECT_ID, "[]")
        assert result["success"] is False

    async def test_task_missing_title(self) -> None:
        _set_auth_context()
        result = await add_tasks(PROJECT_ID, '[{"name": "t1"}]')
        assert result["success"] is False
        assert "'title'" in result["error"]

    async def test_task_missing_name(self) -> None:
        _set_auth_context()
        result = await add_tasks(PROJECT_ID, '[{"title": "T1"}]')
        assert result["success"] is False
        assert "'name'" in result["error"]

    async def test_duplicate_task_name(self) -> None:
        _set_auth_context()
        tasks = json.dumps(
            [
                {"name": "t1", "title": "Task 1"},
                {"name": "t1", "title": "Task 1 again"},
            ]
        )
        result = await add_tasks(PROJECT_ID, tasks)
        assert result["success"] is False
        assert "Duplicate task name" in result["error"]

    async def test_cycle_detection(self) -> None:
        _set_auth_context()
        tasks = json.dumps(
            [
                {"name": "t1", "title": "Task 1", "depends_on": ["t2"]},
                {"name": "t2", "title": "Task 2", "depends_on": ["t1"]},
            ]
        )
        result = await add_tasks(PROJECT_ID, tasks)
        assert result["success"] is False
        assert "cycle" in result["error"].lower()

    async def test_project_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await add_tasks(PROJECT_ID, '[{"name": "t1", "title": "T1"}]')

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_success_simple_tasks(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        task1 = _make_task(project_id=PROJECT_ID, title="T1", status=AgentTaskStatus.PENDING)
        task2 = _make_task(project_id=PROJECT_ID, title="T2", status=AgentTaskStatus.PENDING)

        task_calls = [task1, task2]
        call_idx = [0]

        def make_task(**kwargs: Any) -> MagicMock:
            t = task_calls[call_idx[0]]
            call_idx[0] += 1
            return t

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.AgentTask",
                side_effect=make_task,
            ),
        ):
            tasks_json = json.dumps(
                [
                    {"name": "t1", "title": "T1"},
                    {"name": "t2", "title": "T2", "depends_on": ["t1"]},
                ]
            )
            result = await add_tasks(PROJECT_ID, tasks_json)

        assert result["success"] is True
        assert result["task_count"] == 2

    async def test_invalid_priority(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        task_obj = _make_task(project_id=PROJECT_ID)

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.AgentTask",
                return_value=task_obj,
            ),
        ):
            tasks_json = json.dumps([{"name": "t1", "title": "T1", "priority": "BOGUS"}])
            result = await add_tasks(PROJECT_ID, tasks_json)

        assert result["success"] is False
        assert "Invalid priority" in result["error"]


# ---------------------------------------------------------------------------
# get_ready_tasks
# ---------------------------------------------------------------------------


class TestGetReadyTasks:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await get_ready_tasks(PROJECT_ID)
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await get_ready_tasks("bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_success_no_candidates(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()

        # candidates (BLOCKED/PENDING)
        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = []

        # ready tasks
        ready_result = MagicMock()
        ready_result.all.return_value = []

        mock_session.execute.side_effect = [candidates_result, ready_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_ready_tasks(PROJECT_ID)

        assert result["success"] is True
        assert result["task_count"] == 0
        assert result["promoted_count"] == 0

    async def test_promotes_task_without_deps(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()

        pending_task = _make_task(
            project_id=PROJECT_ID,
            status=AgentTaskStatus.PENDING,
            depends_on=None,
        )

        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = [pending_task]

        ready_result = MagicMock()
        ready_result.all.return_value = []

        mock_session.execute.side_effect = [candidates_result, ready_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_ready_tasks(PROJECT_ID)

        assert result["success"] is True
        assert result["promoted_count"] == 1
        assert pending_task.status == AgentTaskStatus.READY

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await get_ready_tasks(PROJECT_ID)

        assert result["success"] is False
        assert "DB error" in result["error"]


# ---------------------------------------------------------------------------
# set_task_status
# ---------------------------------------------------------------------------


class TestSetTaskStatus:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await set_task_status(TASK_ID, "COMPLETED")
        assert result["success"] is False

    async def test_invalid_task_uuid(self) -> None:
        _set_auth_context()
        result = await set_task_status("bad-uuid", "COMPLETED")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_invalid_status(self) -> None:
        _set_auth_context()
        result = await set_task_status(TASK_ID, "INVALID_STATUS")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_invalid_result_json(self) -> None:
        _set_auth_context()
        result = await set_task_status(TASK_ID, "COMPLETED", result="not-json")
        assert result["success"] is False
        assert "Invalid result JSON" in result["error"]

    async def test_task_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_task_status(TASK_ID, "COMPLETED")

        assert result["success"] is False
        assert "Task not found" in result["error"]

    async def test_validation_error(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, status=AgentTaskStatus.READY)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.validate_task_status_change",
                new=AsyncMock(return_value="Cannot transition from READY to PENDING"),
            ),
        ):
            result = await set_task_status(TASK_ID, "PENDING")

        assert result["success"] is False
        assert "Cannot transition" in result["error"]

    async def test_success_non_terminal(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, status=AgentTaskStatus.READY)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.validate_task_status_change",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await set_task_status(TASK_ID, "IN_PROGRESS")

        assert result["success"] is True
        assert result["status"] == "IN_PROGRESS"

    async def test_success_terminal_calls_complete_task(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, status=AgentTaskStatus.IN_PROGRESS)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        # For _promote_dependents_to_ready: second execute returns empty
        promote_result = MagicMock()
        promote_result.scalars.return_value.all.return_value = []
        mock_session.execute.side_effect = [task_result, promote_result]

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.validate_task_status_change",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "ypl.mcp_server.tools.project_tasks.complete_task",
                new=AsyncMock(),
            ) as mock_complete,
        ):
            result = await set_task_status(TASK_ID, "COMPLETED")

        assert result["success"] is True
        mock_complete.assert_awaited_once()

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("boom"),
        ):
            result = await set_task_status(TASK_ID, "COMPLETED")

        assert result["success"] is False
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# set_project_status
# ---------------------------------------------------------------------------


class TestSetProjectStatus:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await set_project_status(PROJECT_ID, "ACTIVE")
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await set_project_status("bad-uuid", "ACTIVE")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_status(self) -> None:
        _set_auth_context()
        result = await set_project_status(PROJECT_ID, "BOGUS")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_project_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        # First execute: project lookup. Second: task summary
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_project_status(PROJECT_ID, "ACTIVE")

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_success(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)

        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        summary_row = (AgentTaskStatus.READY, 1)
        summary_result = MagicMock()
        summary_result.all.return_value = [summary_row]

        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_project_status(PROJECT_ID, "ACTIVE")

        assert result["success"] is True
        assert result["status"] == "ACTIVE"
        assert "task_summary" in result


# ---------------------------------------------------------------------------
# get_project
# ---------------------------------------------------------------------------


class TestGetProject:
    async def test_no_params_returns_error(self) -> None:
        result = await get_project()
        assert result["success"] is False
        assert "project_id or name" in result["error"]

    async def test_invalid_project_uuid(self) -> None:
        result = await get_project(project_id="bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_project_not_found_by_id(self) -> None:
        mock_session = _make_mock_session()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project(project_id=PROJECT_ID)

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_success_by_id(self) -> None:
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)

        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project(project_id=PROJECT_ID)

        assert result["success"] is True
        assert "project" in result
        assert "task_summary" in result

    async def test_not_found_by_name(self) -> None:
        mock_session = _make_mock_session()
        name_result = MagicMock()
        name_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = name_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project(name="NonExistent")

        assert result["success"] is False
        assert "No projects found" in result["error"]

    async def test_success_by_name(self) -> None:
        mock_session = _make_mock_session()
        proj1 = _make_project(project_id=PROJECT_ID, name="MyProject")

        name_result = MagicMock()
        name_result.scalars.return_value.all.return_value = [proj1]

        # Batch agent resolution
        batch_result = MagicMock()
        batch_result.all.return_value = []

        # Task summary
        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [name_result, batch_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project(name="MyProject")

        assert result["success"] is True
        assert result["count"] == 1

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            side_effect=RuntimeError("DB error"),
        ):
            result = await get_project(project_id=PROJECT_ID)

        assert result["success"] is False
        assert "DB error" in result["error"]


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------


class TestGetTask:
    async def test_no_params_returns_error(self) -> None:
        result = await get_task()
        assert result["success"] is False
        assert "task_id or title" in result["error"]

    async def test_invalid_task_uuid(self) -> None:
        result = await get_task(task_id="bad-uuid")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_task_not_found_by_id(self) -> None:
        mock_session = _make_mock_session()
        exec_result = MagicMock()
        exec_result.first.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_task(task_id=TASK_ID)

        assert result["success"] is False
        assert "Task not found" in result["error"]

    async def test_success_by_id(self) -> None:
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID)
        row = MagicMock()
        row.AgentTask = task
        row.agent_name = "my-agent"
        exec_result = MagicMock()
        exec_result.first.return_value = row
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_task(task_id=TASK_ID)

        assert result["success"] is True
        assert "task" in result

    async def test_no_tasks_found_by_title(self) -> None:
        mock_session = _make_mock_session()
        exec_result = MagicMock()
        exec_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_task(title="NonExistent")

        assert result["success"] is False
        assert "No tasks found" in result["error"]

    async def test_success_by_title(self) -> None:
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, title="My Task")
        row = MagicMock()
        row.AgentTask = task
        row.agent_name = None
        exec_result = MagicMock()
        exec_result.all.return_value = [row]
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_task(title="My Task")

        assert result["success"] is True
        assert result["count"] == 1

    async def test_title_search_with_project_filter_invalid_uuid(self) -> None:
        result = await get_task(title="My Task", project_id="bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            side_effect=RuntimeError("DB error"),
        ):
            result = await get_task(task_id=TASK_ID)

        assert result["success"] is False
        assert "DB error" in result["error"]


# ---------------------------------------------------------------------------
# get_project_tasks
# ---------------------------------------------------------------------------


class TestGetProjectTasks:
    async def test_invalid_project_uuid(self) -> None:
        result = await get_project_tasks("bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_status_filter(self) -> None:
        result = await get_project_tasks(PROJECT_ID, status="INVALID")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_success_no_filter(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.return_value.all.return_value = []
        # Task summary second call
        summary_result = MagicMock()
        summary_result.all.return_value = []
        mock_session.execute.side_effect = [
            MagicMock(**{"all.return_value": []}),
            summary_result,
        ]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_tasks(PROJECT_ID)

        assert result["success"] is True
        assert result["task_count"] == 0

    async def test_success_with_status_filter(self) -> None:
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, status=AgentTaskStatus.READY)
        row = MagicMock()
        row.AgentTask = task
        row.agent_name = None

        tasks_result = MagicMock()
        tasks_result.all.return_value = [row]

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [tasks_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_tasks(PROJECT_ID, status="READY")

        assert result["success"] is True
        assert result["task_count"] == 1

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            side_effect=RuntimeError("boom"),
        ):
            result = await get_project_tasks(PROJECT_ID)

        assert result["success"] is False


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    async def test_invalid_status_filter(self) -> None:
        result = await list_projects(status="INVALID")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_success_no_projects(self) -> None:
        mock_session = _make_mock_session()
        projects_result = MagicMock()
        projects_result.scalars.return_value.all.return_value = []
        # Batch agent names
        batch_result = MagicMock()
        batch_result.all.return_value = []
        mock_session.execute.side_effect = [projects_result, batch_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await list_projects()

        assert result["success"] is True
        assert result["count"] == 0

    async def test_success_with_projects(self) -> None:
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)

        projects_result = MagicMock()
        projects_result.scalars.return_value.all.return_value = [proj]

        batch_result = MagicMock()
        batch_result.all.return_value = []

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [projects_result, batch_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await list_projects(status="PAUSED")

        assert result["success"] is True
        assert result["count"] == 1

    async def test_limit_capped_at_100(self) -> None:
        mock_session = _make_mock_session()
        projects_result = MagicMock()
        projects_result.scalars.return_value.all.return_value = []
        batch_result = MagicMock()
        batch_result.all.return_value = []
        mock_session.execute.side_effect = [projects_result, batch_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            # Should not raise even with extreme limit
            result = await list_projects(limit=9999)

        assert result["success"] is True

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            side_effect=RuntimeError("DB error"),
        ):
            result = await list_projects()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


class TestUpdateTask:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await update_task(TASK_ID)
        assert result["success"] is False

    async def test_invalid_task_uuid(self) -> None:
        _set_auth_context()
        result = await update_task("bad-uuid")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_task_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID)

        assert result["success"] is False
        assert "Task not found" in result["error"]

    async def test_invalid_priority(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, priority="BOGUS")

        assert result["success"] is False
        assert "Invalid priority" in result["error"]

    async def test_invalid_task_data_json(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, task_data="not-json")

        assert result["success"] is False
        assert "Invalid task_data JSON" in result["error"]

    async def test_agent_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # Agent lookup returns None
        agent_result = MagicMock()
        agent_result.scalars.return_value.first.return_value = None

        mock_session.execute.side_effect = [task_result, agent_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, agent_name="nonexistent-agent")

        assert result["success"] is False
        assert "Agent not found" in result["error"]

    async def test_success_update_title(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # Second execute for agent name resolution (agent_id is None so won't be called)
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, title="New Title")

        assert result["success"] is True
        assert task.title == "New Title"

    async def test_clear_agent_with_empty_string(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, agent_id=AGENT_ID)
        task.agent_id = AGENT_ID  # task has an agent
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, agent_name="")

        assert result["success"] is True
        assert task.agent_id is None

    async def test_merge_task_data(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID)
        task.task_data = {"existing_key": "existing_value"}
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_task(TASK_ID, task_data='{"new_key": "new_value"}')

        assert result["success"] is True
        assert task.task_data == {"existing_key": "existing_value", "new_key": "new_value"}

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await update_task(TASK_ID)

        assert result["success"] is False


# ---------------------------------------------------------------------------
# set_task_dependencies
# ---------------------------------------------------------------------------


class TestSetTaskDependencies:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await set_task_dependencies(TASK_ID, "[]")
        assert result["success"] is False

    async def test_invalid_task_uuid(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies("bad-uuid", "[]")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_invalid_depends_on_json(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies(TASK_ID, "not-json")
        assert result["success"] is False
        assert "Invalid depends_on JSON" in result["error"]

    async def test_depends_on_not_array(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies(TASK_ID, '"a string"')
        assert result["success"] is False
        assert "JSON array" in result["error"]

    async def test_non_string_dep_returns_error(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies(TASK_ID, "[123]")
        assert result["success"] is False
        assert "string UUID" in result["error"]

    async def test_invalid_dep_uuid_returns_error(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies(TASK_ID, '["not-a-uuid"]')
        assert result["success"] is False
        assert "Invalid dependency UUID" in result["error"]

    async def test_self_dependency_returns_error(self) -> None:
        _set_auth_context()
        result = await set_task_dependencies(TASK_ID, f'["{TASK_ID}"]')
        assert result["success"] is False
        assert "cannot depend on itself" in result["error"]

    async def test_task_not_found(self) -> None:
        _set_auth_context()
        dep_id = str(uuid.uuid4())
        mock_session = _make_mock_session()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_task_dependencies(TASK_ID, f'["{dep_id}"]')

        assert result["success"] is False
        assert "Task not found" in result["error"]

    async def test_success_clear_dependencies(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            status=AgentTaskStatus.BLOCKED,
            depends_on=[str(uuid.uuid4())],
        )
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_task_dependencies(TASK_ID, "[]")

        assert result["success"] is True
        assert task.depends_on is None
        assert task.status == AgentTaskStatus.READY

    async def test_dep_missing_from_project(self) -> None:
        _set_auth_context()
        dep_id = str(uuid.uuid4())
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # Dep lookup returns empty (dep not in project)
        existing_result = MagicMock()
        existing_result.all.return_value = []

        mock_session.execute.side_effect = [task_result, existing_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_task_dependencies(TASK_ID, f'["{dep_id}"]')

        assert result["success"] is False
        assert "not found in project" in result["error"]

    async def test_cycle_detection_returns_error(self) -> None:
        _set_auth_context()
        dep_id = uuid.uuid4()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # Dep exists in project
        dep_row = MagicMock()
        dep_row.agent_task_id = dep_id
        dep_row.status = AgentTaskStatus.READY
        existing_result = MagicMock()
        existing_result.all.return_value = [dep_row]

        # All project tasks: dep depends on this task → creating a cycle
        all_tasks_row = MagicMock()
        all_tasks_row.agent_task_id = dep_id
        all_tasks_row.depends_on = [TASK_ID]  # dep -> TASK_ID (our task)
        all_tasks_result = MagicMock()
        all_tasks_result.all.return_value = [all_tasks_row]

        mock_session.execute.side_effect = [task_result, existing_result, all_tasks_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_task_dependencies(TASK_ID, f'["{dep_id}"]')

        assert result["success"] is False
        assert "cycle" in result["error"]

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await set_task_dependencies(TASK_ID, "[]")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# update_project
# ---------------------------------------------------------------------------


class TestUpdateProject:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await update_project(PROJECT_ID)
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await update_project("bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_project_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID)

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_negative_budget_returns_error(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, budget_usd=-10.0)

        assert result["success"] is False
        assert "non-negative" in result["error"]

    async def test_invalid_project_data_json(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, project_data="not-json")

        assert result["success"] is False
        assert "Invalid project_data JSON" in result["error"]

    async def test_project_data_not_dict(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, project_data='["list", "not", "dict"]')

        assert result["success"] is False
        assert "JSON object" in result["error"]

    async def test_agent_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        # Agent lookup returns None
        agent_result = MagicMock()
        agent_result.scalars.return_value.first.return_value = None

        mock_session.execute.side_effect = [proj_result, agent_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, default_agent_name="nonexistent")

        assert result["success"] is False
        assert "Agent not found" in result["error"]

    async def test_success_update_name(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, name="Updated Name")

        assert result["success"] is True
        assert proj.name == "Updated Name"

    async def test_clear_agent_with_empty_string(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID, default_agent_id=AGENT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, default_agent_name="")

        assert result["success"] is True
        assert proj.default_agent_id is None

    async def test_success_set_budget(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, budget_usd=100.0)

        assert result["success"] is True
        assert proj.budget_usd == Decimal("100.0")

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await update_project(PROJECT_ID)

        assert result["success"] is False

    async def test_non_owner_without_admin_permission_is_forbidden(self) -> None:
        """A caller who is neither the project's creator nor holds
        MANAGE_AGENT_PROJECTS cannot update the project."""
        _set_auth_context(user_id="not-owner-xyz")
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID, creator_user_id=USER_ID)  # owned by someone else
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with (
            patch(
                "ypl.mcp_server.tools.project_tasks.get_async_session",
                return_value=_make_async_session_ctx(mock_session),
            ),
            # Override the fixture-level admin-grant: this caller is NOT admin.
            patch(
                "ypl.mcp_server.tools.project_tasks.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await update_project(PROJECT_ID, name="Hijacked")

        assert result["success"] is False
        assert "MANAGE_AGENT_PROJECTS" in result["error"]

    async def test_admin_can_update_any_project(self) -> None:
        """A caller holding MANAGE_AGENT_PROJECTS can update a project they
        don't own."""
        _set_auth_context(user_id="admin-user")
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID, creator_user_id=USER_ID)  # owned by someone else
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        summary_result = MagicMock()
        summary_result.all.return_value = []
        mock_session.execute.side_effect = [proj_result, summary_result]

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await update_project(PROJECT_ID, name="Admin rename")

        assert result["success"] is True
        assert proj.name == "Admin rename"


# ---------------------------------------------------------------------------
# claim_task
# ---------------------------------------------------------------------------


class TestClaimTask:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await claim_task(PROJECT_ID)
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await claim_task("bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_task_uuid(self) -> None:
        _set_auth_context()
        result = await claim_task(PROJECT_ID, task_id="bad-uuid")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_no_ready_tasks_available(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await claim_task(PROJECT_ID)

        assert result["success"] is False
        assert "No READY tasks" in result["error"]

    async def test_specific_task_not_ready(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await claim_task(PROJECT_ID, task_id=TASK_ID)

        assert result["success"] is False
        assert "not READY" in result["error"]

    async def test_success_claim_any_task(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, status=AgentTaskStatus.READY)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await claim_task(PROJECT_ID)

        assert result["success"] is True
        assert task.status == AgentTaskStatus.IN_PROGRESS

    async def test_success_claim_specific_task(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        task = _make_task(task_id=TASK_ID, project_id=PROJECT_ID, status=AgentTaskStatus.READY)
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_session.execute.return_value = task_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await claim_task(PROJECT_ID, task_id=TASK_ID)

        assert result["success"] is True
        assert task.status == AgentTaskStatus.IN_PROGRESS

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await claim_task(PROJECT_ID)

        assert result["success"] is False


# ---------------------------------------------------------------------------
# get_project_state
# ---------------------------------------------------------------------------


class TestGetProjectState:
    async def test_invalid_project_uuid(self) -> None:
        result = await get_project_state("bad-uuid")
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_project_not_found(self) -> None:
        mock_session = _make_mock_session()
        exec_result = MagicMock()
        exec_result.first.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_state(PROJECT_ID)

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_success_full_state(self) -> None:
        mock_session = _make_mock_session()
        row = MagicMock()
        row.shared_state = {"key1": "val1", "key2": 42}
        exec_result = MagicMock()
        exec_result.first.return_value = row
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_state(PROJECT_ID)

        assert result["success"] is True
        assert result["state"] == {"key1": "val1", "key2": 42}

    async def test_success_single_key_exists(self) -> None:
        mock_session = _make_mock_session()
        row = MagicMock()
        row.shared_state = {"key1": "val1"}
        exec_result = MagicMock()
        exec_result.first.return_value = row
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_state(PROJECT_ID, key="key1")

        assert result["success"] is True
        assert result["value"] == "val1"
        assert result["exists"] is True

    async def test_success_single_key_missing(self) -> None:
        mock_session = _make_mock_session()
        row = MagicMock()
        row.shared_state = {"key1": "val1"}
        exec_result = MagicMock()
        exec_result.first.return_value = row
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_state(PROJECT_ID, key="missing_key")

        assert result["success"] is True
        assert result["value"] is None
        assert result["exists"] is False

    async def test_null_state_returns_empty_dict(self) -> None:
        mock_session = _make_mock_session()
        row = MagicMock()
        row.shared_state = None
        exec_result = MagicMock()
        exec_result.first.return_value = row
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await get_project_state(PROJECT_ID)

        assert result["success"] is True
        assert result["state"] == {}

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session_read_replica",
            side_effect=RuntimeError("DB error"),
        ):
            result = await get_project_state(PROJECT_ID)

        assert result["success"] is False


# ---------------------------------------------------------------------------
# set_project_state
# ---------------------------------------------------------------------------


class TestSetProjectState:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await set_project_state(PROJECT_ID, "key", '"value"')
        assert result["success"] is False

    async def test_invalid_project_uuid(self) -> None:
        _set_auth_context()
        result = await set_project_state("bad-uuid", "key", '"value"')
        assert result["success"] is False
        assert "Invalid project_id" in result["error"]

    async def test_invalid_value_json(self) -> None:
        _set_auth_context()
        result = await set_project_state(PROJECT_ID, "key", "not-json")
        assert result["success"] is False
        assert "Invalid value JSON" in result["error"]

    async def test_project_not_found(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_project_state(PROJECT_ID, "key", '"value"')

        assert result["success"] is False
        assert "Project not found" in result["error"]

    async def test_success_sets_new_key(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID, shared_state=None)
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_project_state(PROJECT_ID, "my_key", '"my_value"')

        assert result["success"] is True
        assert result["key"] == "my_key"
        assert result["state"] == {"my_key": "my_value"}

    async def test_success_merges_with_existing_state(self) -> None:
        _set_auth_context()
        mock_session = _make_mock_session()
        proj = _make_project(project_id=PROJECT_ID, shared_state={"existing": "data"})
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = proj
        mock_session.execute.return_value = proj_result

        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            return_value=_make_async_session_ctx(mock_session),
        ):
            result = await set_project_state(PROJECT_ID, "new_key", "42")

        assert result["success"] is True
        assert result["state"] == {"existing": "data", "new_key": 42}

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()
        with patch(
            "ypl.mcp_server.tools.project_tasks.get_async_session",
            side_effect=RuntimeError("DB error"),
        ):
            result = await set_project_state(PROJECT_ID, "key", '"value"')

        assert result["success"] is False


# ---------------------------------------------------------------------------
# resume_failed_task
# ---------------------------------------------------------------------------


class TestResumeFailedTask:
    async def test_no_auth_returns_error(self) -> None:
        _set_no_auth_context()
        result = await resume_failed_task(TASK_ID)
        assert result["success"] is False

    async def test_invalid_task_uuid(self) -> None:
        _set_auth_context()
        result = await resume_failed_task("bad-uuid")
        assert result["success"] is False
        assert "Invalid task_id" in result["error"]

    async def test_success(self) -> None:
        _set_auth_context()

        # resume_task is imported locally inside the function, patch at that module
        with patch(
            "ypl.agent_harness_service.task_executor.resume_task",
            new=AsyncMock(return_value={"success": True, "session_to_resume": "sess-123"}),
        ):
            result = await resume_failed_task(TASK_ID)

        assert result["success"] is True
        assert result["session_to_resume"] == "sess-123"

    async def test_failure_propagated(self) -> None:
        _set_auth_context()

        with patch(
            "ypl.agent_harness_service.task_executor.resume_task",
            new=AsyncMock(return_value={"success": False, "error": "Task not resumable"}),
        ):
            result = await resume_failed_task(TASK_ID)

        assert result["success"] is False
        assert "not resumable" in result["error"]

    async def test_exception_returns_error(self) -> None:
        _set_auth_context()

        with patch(
            "ypl.agent_harness_service.task_executor.resume_task",
            new=AsyncMock(side_effect=RuntimeError("unexpected error")),
        ):
            result = await resume_failed_task(TASK_ID)

        assert result["success"] is False
        assert "unexpected error" in result["error"]
