"""Unit tests for ypl/agent_harness_service/projects/project_service.py.

Tests project/task CRUD and status management with mocked async sessions.
"""

from __future__ import annotations
import types as _types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.projects.project_service import (
    _format_project,
    _format_task,
    _get_task_summaries_batch,
    _get_task_summary,
    _promote_dependents_to_ready,
    _resolve_agent_id_by_name,
    get_project_service,
    get_task_service,
    list_projects_service,
    list_tasks_service,
    resume_task_service,
    set_project_status_service,
    set_task_dependencies_service,
    set_task_status_service,
    update_project_service,
    update_task_service,
)
from ypl.agent_harness_service.projects.project_types import (
    ProjectDetailResponse,
    ProjectResponse,
    ProjectStatusRequest,
    ProjectUpdateRequest,
    TaskDependenciesRequest,
    TaskStatusRequest,
    TaskSummary,
    TaskUpdateRequest,
)
from ypl.db.agent_harness import (
    Agent,
    AgentProject,
    AgentProjectStatus,
    AgentTask,
    AgentTaskPriority,
    AgentTaskStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


def _make_project(
    name: str = "Test Project",
    status: AgentProjectStatus = AgentProjectStatus.ACTIVE,
    creator_user_id: str | None = "user-123",
) -> AgentProject:
    return AgentProject(
        agent_project_id=uuid.uuid4(),
        name=name,
        description="A test project",
        status=status,
        creator_user_id=creator_user_id,
        slack_channel=None,
        budget_usd=None,
        budget_spent_usd=Decimal(0),
        created_at=datetime.now(UTC),
        shared_state={},
    )


def _make_task(
    status: AgentTaskStatus = AgentTaskStatus.PENDING,
    depends_on: list[str] | None = None,
    agent_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    priority: AgentTaskPriority = AgentTaskPriority.NORMAL,
) -> AgentTask:
    return AgentTask(
        agent_task_id=uuid.uuid4(),
        agent_project_id=project_id or uuid.uuid4(),
        title="Test Task",
        description="A test task",
        status=status,
        priority=priority,
        depends_on=depends_on,
        agent_id=agent_id,
        assigned_session_ids=None,
        result=None,
        task_data=None,
        estimated_effort=None,
        actual_spending_usd=None,
        completed_at=None,
        created_at=datetime.now(UTC),
        parent_task_id=None,
    )


def _make_task_summary() -> TaskSummary:
    return TaskSummary(PENDING=2, READY=1, IN_PROGRESS=1, total=4)


# ---------------------------------------------------------------------------
# Tests: _format_project
# ---------------------------------------------------------------------------


class TestFormatProject:
    def test_returns_project_response_without_shared_state(self) -> None:
        project = _make_project()
        summary = _make_task_summary()
        result = _format_project(project, "Alice", summary)
        assert isinstance(result, ProjectResponse)
        assert result.name == "Test Project"
        assert result.creator_user_name == "Alice"
        assert result.task_summary.total == 4

    def test_returns_project_detail_response_with_shared_state(self) -> None:
        project = _make_project()
        summary = _make_task_summary()
        result = _format_project(project, "Alice", summary, include_shared_state=True)
        assert isinstance(result, ProjectDetailResponse)
        assert result.shared_state == {}

    def test_handles_none_creator(self) -> None:
        project = _make_project(creator_user_id=None)
        summary = TaskSummary()
        result = _format_project(project, None, summary)
        assert result.creator_user_name is None
        assert result.creator_user_id is None


# ---------------------------------------------------------------------------
# Tests: _format_task
# ---------------------------------------------------------------------------


class TestFormatTask:
    def test_basic_format(self) -> None:
        task = _make_task()
        result = _format_task(task, "reviewer", "user-1", "Bob")
        assert result.title == "Test Task"
        assert result.agent_name == "reviewer"
        assert result.creator_user_name == "Bob"
        assert result.status == AgentTaskStatus.PENDING.value

    def test_format_with_parent(self) -> None:
        parent_id = uuid.uuid4()
        task = _make_task()
        task.parent_task_id = parent_id
        result = _format_task(task, None, None, None)
        assert result.parent_task_id == str(parent_id)

    def test_format_without_agent(self) -> None:
        task = _make_task()
        result = _format_task(task, None, None, None)
        assert result.agent_name is None
        assert result.agent_id is None


# ---------------------------------------------------------------------------
# Tests: _get_task_summary
# ---------------------------------------------------------------------------


class TestGetTaskSummary:
    async def test_returns_summary_with_counts(self) -> None:
        mock_db = AsyncMock()
        result_mock = MagicMock()
        result_mock.all.return_value = [
            MagicMock(status=AgentTaskStatus.PENDING, __iter__=lambda self: iter([AgentTaskStatus.PENDING, 3])),
        ]
        # Simulate rows as tuples
        result_mock.all.return_value = [(AgentTaskStatus.PENDING, 3), (AgentTaskStatus.COMPLETED, 5)]
        mock_db.execute = AsyncMock(return_value=result_mock)

        proj_id = uuid.uuid4()
        summary = await _get_task_summary(mock_db, proj_id)
        assert summary.PENDING == 3
        assert summary.COMPLETED == 5
        assert summary.total == 8

    async def test_empty_project_returns_zeros(self) -> None:
        mock_db = AsyncMock()
        result_mock = MagicMock()
        result_mock.all.return_value = []
        mock_db.execute = AsyncMock(return_value=result_mock)

        proj_id = uuid.uuid4()
        summary = await _get_task_summary(mock_db, proj_id)
        assert summary.total == 0
        assert summary.PENDING == 0


# ---------------------------------------------------------------------------
# Tests: _get_task_summaries_batch
# ---------------------------------------------------------------------------


class TestGetTaskSummariesBatch:
    async def test_returns_empty_dict_for_empty_list(self) -> None:
        mock_db = AsyncMock()
        result = await _get_task_summaries_batch(mock_db, [])
        assert result == {}

    async def test_returns_summaries_for_multiple_projects(self) -> None:
        mock_db = AsyncMock()
        proj1 = uuid.uuid4()
        proj2 = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.all.return_value = [
            (proj1, AgentTaskStatus.PENDING, 2),
            (proj1, AgentTaskStatus.COMPLETED, 1),
            (proj2, AgentTaskStatus.READY, 3),
        ]
        mock_db.execute = AsyncMock(return_value=result_mock)

        summaries = await _get_task_summaries_batch(mock_db, [proj1, proj2])
        assert summaries[proj1].PENDING == 2
        assert summaries[proj1].COMPLETED == 1
        assert summaries[proj1].total == 3
        assert summaries[proj2].READY == 3
        assert summaries[proj2].total == 3


# ---------------------------------------------------------------------------
# Tests: _resolve_agent_id_by_name
# ---------------------------------------------------------------------------


class TestResolveAgentIdByName:
    async def test_returns_agent_id_when_found(self) -> None:
        mock_db = AsyncMock()
        agent_id = uuid.uuid4()
        agent = Agent(agent_id=agent_id, name="reviewer")
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = agent
        mock_db.execute = AsyncMock(return_value=result_mock)

        found = await _resolve_agent_id_by_name(mock_db, "reviewer")
        assert found == agent_id

    async def test_returns_none_when_not_found(self) -> None:
        mock_db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=result_mock)

        found = await _resolve_agent_id_by_name(mock_db, "nonexistent")
        assert found is None


# ---------------------------------------------------------------------------
# Tests: _promote_dependents_to_ready
# ---------------------------------------------------------------------------


class TestPromoteDependentsToReady:
    async def test_returns_empty_when_no_candidates(self) -> None:
        mock_db = AsyncMock()
        completed_task_id = uuid.uuid4()
        project_id = uuid.uuid4()

        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=candidates_result)

        result = await _promote_dependents_to_ready(mock_db, completed_task_id, project_id)
        assert result == []

    async def test_promotes_dependent_when_all_deps_completed(self) -> None:
        mock_db = AsyncMock()
        completed_task_id = uuid.uuid4()
        project_id = uuid.uuid4()

        dep_task_id = uuid.uuid4()
        # The blocked task depends on completed_task_id
        blocked_task = _make_task(status=AgentTaskStatus.BLOCKED, depends_on=[str(completed_task_id)])
        blocked_task.agent_task_id = dep_task_id

        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = [blocked_task]

        dep_status_result = MagicMock()
        dep_status_row = MagicMock()
        dep_status_row.agent_task_id = completed_task_id
        dep_status_row.status = AgentTaskStatus.COMPLETED
        dep_status_result.all.return_value = [dep_status_row]

        mock_db.execute = AsyncMock(side_effect=[candidates_result, dep_status_result])
        mock_db.add = MagicMock()

        result = await _promote_dependents_to_ready(mock_db, completed_task_id, project_id)
        assert len(result) == 1
        assert blocked_task.status == AgentTaskStatus.READY

    async def test_does_not_promote_when_dep_not_completed(self) -> None:
        mock_db = AsyncMock()
        completed_task_id = uuid.uuid4()
        other_dep_id = uuid.uuid4()
        project_id = uuid.uuid4()

        # blocked_task depends on both completed_task_id and other_dep_id (not completed)
        blocked_task = _make_task(
            status=AgentTaskStatus.BLOCKED,
            depends_on=[str(completed_task_id), str(other_dep_id)],
        )

        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = [blocked_task]

        dep_status_result = MagicMock()
        row1 = MagicMock()
        row1.agent_task_id = completed_task_id
        row1.status = AgentTaskStatus.COMPLETED
        row2 = MagicMock()
        row2.agent_task_id = other_dep_id
        row2.status = AgentTaskStatus.IN_PROGRESS
        dep_status_result.all.return_value = [row1, row2]

        mock_db.execute = AsyncMock(side_effect=[candidates_result, dep_status_result])
        mock_db.add = MagicMock()

        result = await _promote_dependents_to_ready(mock_db, completed_task_id, project_id)
        assert result == []
        assert blocked_task.status == AgentTaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# Tests: list_projects_service
# ---------------------------------------------------------------------------


class TestListProjectsService:
    async def test_returns_project_list(self) -> None:
        mock_db = AsyncMock()
        project = _make_project()
        project_id = project.agent_project_id

        # total count
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        # rows
        row = MagicMock()
        row.AgentProject = project
        row.user_name = "Alice"
        rows_result = MagicMock()
        rows_result.all.return_value = [row]

        # batch summaries
        summary_result = MagicMock()
        summary_result.all.return_value = [(project_id, AgentTaskStatus.PENDING, 2)]

        mock_db.execute = AsyncMock(side_effect=[count_result, rows_result, summary_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_projects_service()

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].name == "Test Project"

    async def test_raises_for_invalid_status(self) -> None:
        with pytest.raises(ValueError, match="Invalid status"):
            await list_projects_service(status="INVALID_STATUS")

    async def test_clamps_limit(self) -> None:
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.all.return_value = []
        summary_result = MagicMock()
        summary_result.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[count_result, rows_result, summary_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_projects_service(limit=200)  # Over max of 100
        assert result.limit == 100

    async def test_filters_by_creator_user_id(self) -> None:
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.all.return_value = []
        summary_result = MagicMock()
        summary_result.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[count_result, rows_result, summary_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_projects_service(creator_user_id="user-filter")
        assert result.total == 0


# ---------------------------------------------------------------------------
# Tests: get_project_service
# ---------------------------------------------------------------------------


class TestGetProjectService:
    async def test_returns_project_detail(self) -> None:
        mock_db = AsyncMock()
        project = _make_project()
        proj_id = str(project.agent_project_id)

        row = MagicMock()
        row.AgentProject = project
        row.user_name = "Bob"
        proj_result = MagicMock()
        proj_result.first.return_value = row

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[proj_result, summary_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await get_project_service(proj_id)

        assert isinstance(result, ProjectDetailResponse)
        assert result.name == "Test Project"

    async def test_raises_lookup_error_when_not_found(self) -> None:
        mock_db = AsyncMock()
        proj_result = MagicMock()
        proj_result.first.return_value = None
        mock_db.execute = AsyncMock(return_value=proj_result)

        fake_uuid = str(uuid.uuid4())
        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Project not found"),
        ):
            await get_project_service(fake_uuid)


# ---------------------------------------------------------------------------
# Tests: update_project_service
# ---------------------------------------------------------------------------


class TestUpdateProjectService:
    async def test_updates_name_and_description(self) -> None:
        mock_db = AsyncMock()
        project = _make_project()
        proj_id = str(project.agent_project_id)

        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = project

        user_result = MagicMock()
        user_result.first.return_value = _types.SimpleNamespace(name="Alice")

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[proj_result, user_result, summary_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = ProjectUpdateRequest(name="New Name", description="New Desc")
            result = await update_project_service(proj_id, req)

        assert result.name == "New Name"
        assert result.description == "New Desc"
        mock_db.commit.assert_called_once()

    async def test_raises_when_project_not_found(self) -> None:
        mock_db = AsyncMock()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=proj_result)

        fake_id = str(uuid.uuid4())
        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Project not found"),
        ):
            await update_project_service(fake_id, ProjectUpdateRequest())

    async def test_clears_slack_channel_on_empty_string(self) -> None:
        mock_db = AsyncMock()
        project = _make_project()
        project.slack_channel = "#general"
        proj_id = str(project.agent_project_id)

        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = project

        user_result = MagicMock()
        user_result.first.return_value = None

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[proj_result, user_result, summary_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = ProjectUpdateRequest(slack_channel="")  # empty → clear
            await update_project_service(proj_id, req)

        assert project.slack_channel is None


# ---------------------------------------------------------------------------
# Tests: set_project_status_service
# ---------------------------------------------------------------------------


class TestSetProjectStatusService:
    async def test_changes_status_to_archived(self) -> None:
        mock_db = AsyncMock()
        project = _make_project(status=AgentProjectStatus.ACTIVE)
        proj_id = str(project.agent_project_id)

        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = project

        user_result = MagicMock()
        user_result.first.return_value = _types.SimpleNamespace(name="Alice")

        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[proj_result, user_result, summary_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = ProjectStatusRequest(status="ARCHIVED")
            result = await set_project_status_service(proj_id, req)

        assert result.status == "ARCHIVED"
        assert project.status == AgentProjectStatus.ARCHIVED

    async def test_raises_for_invalid_status(self) -> None:
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="Invalid status"):
            await set_project_status_service(fake_id, ProjectStatusRequest(status="INVALID"))

    async def test_raises_lookup_when_project_not_found(self) -> None:
        mock_db = AsyncMock()
        proj_result = MagicMock()
        proj_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=proj_result)

        fake_id = str(uuid.uuid4())
        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Project not found"),
        ):
            await set_project_status_service(fake_id, ProjectStatusRequest(status="ACTIVE"))


# ---------------------------------------------------------------------------
# Tests: list_tasks_service
# ---------------------------------------------------------------------------


class TestListTasksService:
    async def test_returns_tasks_for_project(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(project_id=project_id)

        # project check
        proj_row = MagicMock()
        proj_row.creator_user_id = "user-1"
        proj_result = MagicMock()
        proj_result.first.return_value = proj_row

        # user name
        user_result = MagicMock()
        user_result.first.return_value = _types.SimpleNamespace(name="Alice")

        # count
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        # tasks
        task_row = MagicMock()
        task_row.AgentTask = task
        task_row.agent_name = "reviewer"
        tasks_result = MagicMock()
        tasks_result.all.return_value = [task_row]

        # summary
        summary_result = MagicMock()
        summary_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[proj_result, user_result, count_result, tasks_result, summary_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_tasks_service(str(project_id))

        assert result.total == 1
        assert len(result.items) == 1

    async def test_raises_for_invalid_task_status(self) -> None:
        with pytest.raises(ValueError, match="Invalid status"):
            await list_tasks_service(str(uuid.uuid4()), status="BOGUS")

    async def test_raises_lookup_when_project_not_found(self) -> None:
        mock_db = AsyncMock()
        proj_result = MagicMock()
        proj_result.first.return_value = None
        mock_db.execute = AsyncMock(return_value=proj_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Project not found"),
        ):
            await list_tasks_service(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Tests: get_task_service
# ---------------------------------------------------------------------------


class TestGetTaskService:
    async def test_returns_task(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(project_id=project_id)

        task_row = MagicMock()
        task_row.AgentTask = task
        task_row.agent_name = None
        tasks_result = MagicMock()
        tasks_result.first.return_value = task_row

        proj_result = MagicMock()
        proj_row = MagicMock()
        proj_row.creator_user_id = "user-1"
        proj_result.first.return_value = proj_row

        user_result = MagicMock()
        user_result.first.return_value = _types.SimpleNamespace(name="Alice")

        mock_db.execute = AsyncMock(side_effect=[tasks_result, proj_result, user_result])

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
            _make_mock_session_factory(mock_db),
        ):
            result = await get_task_service(str(project_id), str(task.agent_task_id))

        assert result.title == "Test Task"

    async def test_raises_when_task_not_found(self) -> None:
        mock_db = AsyncMock()
        tasks_result = MagicMock()
        tasks_result.first.return_value = None
        mock_db.execute = AsyncMock(return_value=tasks_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Task not found"),
        ):
            await get_task_service(str(uuid.uuid4()), str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Tests: update_task_service
# ---------------------------------------------------------------------------


class TestUpdateTaskService:
    async def test_updates_title(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(project_id=project_id)

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        agent_result = MagicMock()
        agent_result.first.return_value = None  # No agent assigned

        proj_result = MagicMock()
        proj_row = MagicMock()
        proj_row.creator_user_id = None
        proj_result.first.return_value = proj_row

        mock_db.execute = AsyncMock(side_effect=[task_result, agent_result, proj_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = TaskUpdateRequest(title="Updated Title")
            result = await update_task_service(str(project_id), str(task.agent_task_id), req)

        assert result.title == "Updated Title"

    async def test_raises_for_invalid_priority(self) -> None:
        mock_db = AsyncMock()
        task = _make_task()

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(ValueError, match="Invalid priority"),
        ):
            req = TaskUpdateRequest(priority="SUPER_HIGH")
            await update_task_service(str(uuid.uuid4()), str(task.agent_task_id), req)

    async def test_unassigns_agent_when_empty_string(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(project_id=project_id, agent_id=uuid.uuid4())

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # No agent resolved (agent_id is now None after unassign)
        agent_name_result = MagicMock()
        agent_name_result.first.return_value = None

        proj_result = MagicMock()
        proj_result.first.return_value = MagicMock(creator_user_id=None)

        mock_db.execute = AsyncMock(side_effect=[task_result, agent_name_result, proj_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = TaskUpdateRequest(agent_name="")  # empty = unassign
            await update_task_service(str(project_id), str(task.agent_task_id), req)

        assert task.agent_id is None

    async def test_merges_task_data(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(project_id=project_id)
        task.task_data = {"existing_key": "old_value"}

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        agent_result = MagicMock()
        agent_result.first.return_value = None

        proj_result = MagicMock()
        proj_result.first.return_value = MagicMock(creator_user_id=None)

        mock_db.execute = AsyncMock(side_effect=[task_result, agent_result, proj_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = TaskUpdateRequest(task_data={"new_key": "new_value"})
            await update_task_service(str(project_id), str(task.agent_task_id), req)

        assert task.task_data == {"existing_key": "old_value", "new_key": "new_value"}


# ---------------------------------------------------------------------------
# Tests: set_task_status_service
# ---------------------------------------------------------------------------


class TestSetTaskStatusService:
    async def test_raises_for_invalid_status(self) -> None:
        with pytest.raises(ValueError, match="Invalid status"):
            await set_task_status_service(str(uuid.uuid4()), str(uuid.uuid4()), TaskStatusRequest(status="INVALID"))

    async def test_raises_when_task_not_found(self) -> None:
        mock_db = AsyncMock()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Task not found"),
        ):
            await set_task_status_service(str(uuid.uuid4()), str(uuid.uuid4()), TaskStatusRequest(status="READY"))

    async def test_changes_task_status(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(status=AgentTaskStatus.PENDING, project_id=project_id)

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # promote dependents: no candidates
        candidates_result = MagicMock()
        candidates_result.scalars.return_value.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[task_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.projects.project_service.validate_task_status_change",
                AsyncMock(return_value=None),
            ),
        ):
            result = await set_task_status_service(
                str(project_id), str(task.agent_task_id), TaskStatusRequest(status="READY")
            )

        assert result.status == "READY"

    async def test_raises_when_validation_fails(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.PENDING)

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.projects.project_service.validate_task_status_change",
                AsyncMock(return_value="Cannot transition"),
            ),
            pytest.raises(ValueError, match="Cannot transition"),
        ):
            await set_task_status_service(str(uuid.uuid4()), str(task.agent_task_id), TaskStatusRequest(status="READY"))


# ---------------------------------------------------------------------------
# Tests: set_task_dependencies_service
# ---------------------------------------------------------------------------


class TestSetTaskDependenciesService:
    async def test_raises_for_invalid_uuid(self) -> None:
        with pytest.raises(ValueError, match="Invalid dependency UUID"):
            await set_task_dependencies_service(
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                TaskDependenciesRequest(depends_on=["not-a-uuid"]),
            )

    async def test_raises_for_self_dependency(self) -> None:
        task_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="depend on itself"):
            await set_task_dependencies_service(
                str(uuid.uuid4()),
                task_id,
                TaskDependenciesRequest(depends_on=[task_id]),
            )

    async def test_clears_dependencies_with_empty_list(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task = _make_task(
            project_id=project_id,
            status=AgentTaskStatus.BLOCKED,
            depends_on=["old-dep"],
        )

        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = task

        # After clearing, resolve agent and creator
        agent_result = MagicMock()
        agent_result.first.return_value = None

        proj_result = MagicMock()
        proj_result.first.return_value = MagicMock(creator_user_id=None)

        mock_db.execute = AsyncMock(side_effect=[task_result, agent_result, proj_result])
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "ypl.agent_harness_service.projects.project_service.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            await set_task_dependencies_service(
                str(project_id), str(task.agent_task_id), TaskDependenciesRequest(depends_on=[])
            )

        assert task.depends_on is None
        assert task.status == AgentTaskStatus.READY  # auto-adjusted to READY when no deps

    async def test_raises_when_task_not_found(self) -> None:
        mock_db = AsyncMock()
        task_result = MagicMock()
        task_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=task_result)

        dep_id = str(uuid.uuid4())
        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Task not found"),
        ):
            task_id = str(uuid.uuid4())
            await set_task_dependencies_service(
                str(uuid.uuid4()),
                task_id,
                TaskDependenciesRequest(depends_on=[dep_id]),
            )


# ---------------------------------------------------------------------------
# Tests: resume_task_service
# ---------------------------------------------------------------------------


class TestResumeTaskService:
    async def test_raises_when_task_not_found(self) -> None:
        mock_db = AsyncMock()
        task_result = MagicMock()
        task_result.first.return_value = None
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="Task not found"),
        ):
            await resume_task_service(str(uuid.uuid4()), str(uuid.uuid4()))

    async def test_raises_when_task_belongs_to_different_project(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        other_project_id = uuid.uuid4()
        task_id = uuid.uuid4()

        task_row = MagicMock()
        task_row.agent_project_id = other_project_id  # Different from requested project_id
        task_result = MagicMock()
        task_result.first.return_value = task_row
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(LookupError, match="does not belong to project"),
        ):
            await resume_task_service(str(project_id), str(task_id))

    async def test_raises_value_error_on_resume_failure(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task_id = uuid.uuid4()

        task_row = MagicMock()
        task_row.agent_project_id = project_id
        task_result = MagicMock()
        task_result.first.return_value = task_row
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.task_executor.resume_task",
                AsyncMock(return_value={"success": False, "error": "Task is not resumable"}),
            ),
            pytest.raises(ValueError, match="Task is not resumable"),
        ):
            await resume_task_service(str(project_id), str(task_id))

    async def test_returns_resume_response_on_success(self) -> None:
        mock_db = AsyncMock()
        project_id = uuid.uuid4()
        task_id = uuid.uuid4()

        task_row = MagicMock()
        task_row.agent_project_id = project_id
        task_result = MagicMock()
        task_result.first.return_value = task_row
        mock_db.execute = AsyncMock(return_value=task_result)

        with (
            patch(
                "ypl.agent_harness_service.projects.project_service.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.task_executor.resume_task",
                AsyncMock(return_value={"success": True, "status": "READY", "session_to_resume": "sess-123"}),
            ),
        ):
            result = await resume_task_service(str(project_id), str(task_id))

        assert result.status == "READY"
        assert result.session_to_resume == "sess-123"
