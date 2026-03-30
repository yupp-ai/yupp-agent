"""Tests for the export_to_linear module.

Covers pure helper functions (no I/O) and higher-level export flows with
mocked LinearClient and async DB sessions.
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from ypl.agent_harness_service.tools.linear_sync.export_to_linear import (
    _extract_issue_ref,
    _extract_project_ref,
    _parse_dt,
    _topological_sort,
    export_project_to_linear,
    sync_tasks_to_linear,
)
from ypl.db.agent_harness import AgentTask

from tests.agent_harness_service.linear_sync.conftest import (
    LINEAR_PROJECT_ID,
    LINEAR_TEAM_ID,
    _call_sync,
    make_create_issue_response,
    make_create_project_response,
    make_project,
    make_task,
    make_update_issue_response,
)

# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------


class TestParseDt:
    def test_none_returns_none(self) -> None:
        assert _parse_dt(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_dt("") is None

    def test_invalid_iso_string_returns_none(self) -> None:
        assert _parse_dt("not-a-date") is None

    def test_valid_utc_iso_string(self) -> None:
        dt = _parse_dt("2024-06-01T12:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2024 and dt.month == 6 and dt.day == 1

    def test_naive_datetime_string_gets_utc_tzinfo(self) -> None:
        dt = _parse_dt("2024-06-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo == UTC

    def test_iso_with_z_suffix(self) -> None:
        """Python 3.11+ fromisoformat supports Z; 3.10 does not — verify it at least parses or returns None."""
        result = _parse_dt("2024-06-01T12:00:00Z")
        # Either parsed successfully (3.11+) or returned None (3.10 fallback)
        if result is not None:
            assert result.tzinfo is not None

    def test_roundtrip_from_isoformat(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        iso = now.isoformat()
        dt = _parse_dt(iso)
        assert dt is not None
        assert dt == now


# ---------------------------------------------------------------------------
# _extract_project_ref
# ---------------------------------------------------------------------------


class TestExtractProjectRef:
    def test_returns_none_when_no_linear_data(self) -> None:
        project = make_project(project_data={})
        assert _extract_project_ref(project) is None

    def test_returns_none_when_project_data_is_none(self) -> None:
        project = make_project(project_data=None)
        assert _extract_project_ref(project) is None

    def test_returns_ref_when_all_fields_present(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": "proj-abc",
                    "linear_team_id": "team-xyz",
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        ref = _extract_project_ref(project)
        assert ref is not None
        assert ref.linear_project_id == "proj-abc"
        assert ref.linear_team_id == "team-xyz"
        assert ref.last_synced_at == now

    def test_raises_when_team_id_missing(self) -> None:
        """Data corruption: linear_project_id present but linear_team_id absent → ValueError."""
        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": "proj-abc",
                    # linear_team_id intentionally omitted
                }
            }
        )
        with pytest.raises(ValueError, match="missing linear_team_id"):
            _extract_project_ref(project)

    def test_last_synced_at_defaults_to_none(self) -> None:
        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": "proj-abc",
                    "linear_team_id": "team-xyz",
                }
            }
        )
        ref = _extract_project_ref(project)
        assert ref is not None
        assert ref.last_synced_at is None


# ---------------------------------------------------------------------------
# _extract_issue_ref
# ---------------------------------------------------------------------------


class TestExtractIssueRef:
    def test_returns_none_when_no_linear_data(self) -> None:
        task = make_task(task_data={})
        assert _extract_issue_ref(task) is None

    def test_returns_none_when_task_data_is_none(self) -> None:
        task = make_task(task_data=None)
        assert _extract_issue_ref(task) is None

    def test_returns_ref_when_all_fields_present(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        task = make_task(
            task_data={
                "linear_ref": {
                    "linear_issue_id": "issue-999",
                    "linear_identifier": "ENG-99",
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        ref = _extract_issue_ref(task)
        assert ref is not None
        assert ref.linear_issue_id == "issue-999"
        assert ref.linear_identifier == "ENG-99"
        assert ref.last_synced_at == now

    def test_raises_when_identifier_missing(self) -> None:
        """Data corruption: linear_issue_id present but linear_identifier absent → ValueError."""
        task = make_task(
            task_data={
                "linear_ref": {
                    "linear_issue_id": "issue-999",
                    # linear_identifier intentionally omitted
                }
            }
        )
        with pytest.raises(ValueError, match="missing linear_identifier"):
            _extract_issue_ref(task)

    def test_extra_task_data_keys_preserved_in_roundtrip(self) -> None:
        """Other keys in task_data (e.g. inputs) must not be clobbered."""
        task = make_task(
            task_data={
                "linear_ref": {
                    "linear_issue_id": "issue-1",
                    "linear_identifier": "ENG-1",
                },
                "custom_key": "custom_value",
            }
        )
        ref = _extract_issue_ref(task)
        assert ref is not None
        # The custom key is still in the raw task_data
        assert task.task_data is not None
        assert task.task_data["custom_key"] == "custom_value"


# ---------------------------------------------------------------------------
# _topological_sort (DFS — export variant)
# ---------------------------------------------------------------------------


class TestExportTopologicalSort:
    def test_empty_list(self) -> None:
        assert _topological_sort([]) == []

    def test_single_task_no_edges(self) -> None:
        t = make_task()
        result = _topological_sort([t])
        assert result == [t]

    def test_parent_before_child(self) -> None:
        parent = make_task(title="Parent")
        child = make_task(
            title="Child",
            project_id=str(parent.agent_project_id),
            parent_task_id=parent.agent_task_id,
        )
        result = _topological_sort([child, parent])  # intentionally reversed input
        assert result.index(parent) < result.index(child)

    def test_dependency_before_dependent(self) -> None:
        dep = make_task(title="Dep")
        task = make_task(title="Blocked", depends_on=[str(dep.agent_task_id)])
        result = _topological_sort([task, dep])
        assert result.index(dep) < result.index(task)

    def test_chain_a_b_c(self) -> None:
        a = make_task(title="A")
        b = make_task(title="B", depends_on=[str(a.agent_task_id)])
        c = make_task(title="C", depends_on=[str(b.agent_task_id)])
        result = _topological_sort([c, a, b])
        assert result.index(a) < result.index(b) < result.index(c)

    def test_cycle_does_not_infinite_loop(self) -> None:
        """DFS must detect cycles and not loop infinitely."""
        a = make_task(title="A")
        b = make_task(title="B")
        # Manually wire a cycle: a depends on b, b depends on a
        a.depends_on = [str(b.agent_task_id)]
        b.depends_on = [str(a.agent_task_id)]
        result = _topological_sort([a, b])
        # Both tasks must still appear in result.
        # Note: Ordering within the cycle is implementation-defined (DFS traversal order),
        # so we only check presence, not order.
        assert len(result) == 2
        assert {t.title for t in result} == {"A", "B"}

    def test_independent_tasks_all_present(self) -> None:
        tasks = [make_task(title=f"T{i}") for i in range(5)]
        result = _topological_sort(tasks)
        assert len(result) == 5
        assert {t.title for t in result} == {t.title for t in tasks}

    def test_diamond_dependency(self) -> None:
        a = make_task(title="A")
        b = make_task(title="B", depends_on=[str(a.agent_task_id)])
        c = make_task(title="C", depends_on=[str(a.agent_task_id)])
        d = make_task(title="D", depends_on=[str(b.agent_task_id), str(c.agent_task_id)])
        result = _topological_sort([d, c, b, a])
        assert result.index(a) < result.index(b)
        assert result.index(a) < result.index(c)
        assert result.index(b) < result.index(d)
        assert result.index(c) < result.index(d)


# ---------------------------------------------------------------------------
# export_project_to_linear — mocked
# ---------------------------------------------------------------------------


class TestExportProjectToLinear:
    @pytest.mark.asyncio
    async def test_creates_new_linear_project_when_none_exists(self) -> None:
        project = make_project(project_data=None)
        tasks = [make_task(title="Task A", project_id=str(project.agent_project_id))]

        issue_id = str(uuid.uuid4())

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_project_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value
            client.create_project.return_value = make_create_project_response(project_id=LINEAR_PROJECT_ID)
            client.create_issue.return_value = make_create_issue_response(issue_id=issue_id, identifier="ENG-1")

            lin_proj_id, result = await export_project_to_linear(
                project_id=str(project.agent_project_id),
                linear_team_id=LINEAR_TEAM_ID,
            )

        assert lin_proj_id == LINEAR_PROJECT_ID
        assert result.created == 1
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_reuses_existing_linear_project(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        tasks: list[AgentTask] = []

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=tasks),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_project_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value

            lin_proj_id, result = await export_project_to_linear(
                project_id=str(project.agent_project_id),
                linear_team_id=LINEAR_TEAM_ID,
            )

            # create_project must NOT have been called
            client.create_project.assert_not_called()

        assert lin_proj_id == LINEAR_PROJECT_ID
        assert result.created == 0
        assert result.updated == 0

    @pytest.mark.asyncio
    async def test_updates_existing_issue(self) -> None:
        existing_issue_id = "existing-issue-abc"
        existing_identifier = "ENG-5"
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        task = make_task(
            title="Existing Task",
            project_id=str(project.agent_project_id),
            task_data={
                "linear_ref": {
                    "linear_issue_id": existing_issue_id,
                    "linear_identifier": existing_identifier,
                    "last_synced_at": now.isoformat(),
                }
            },
        )

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_project_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value
            client.update_issue.return_value = make_update_issue_response(
                issue_id=existing_issue_id, identifier=existing_identifier
            )

            _, result = await export_project_to_linear(
                project_id=str(project.agent_project_id),
                linear_team_id=LINEAR_TEAM_ID,
            )

            client.update_issue.assert_called_once()

        assert result.updated == 1
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_project_not_found_raises_value_error(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ValueError, match="AHS project not found"),
        ):
            await export_project_to_linear(
                project_id=str(uuid.uuid4()),
                linear_team_id=LINEAR_TEAM_ID,
            )

    @pytest.mark.asyncio
    async def test_linear_create_issue_error_increments_errors(self) -> None:
        project = make_project(project_data=None)
        task = make_task(title="Bad Task", project_id=str(project.agent_project_id))

        save_issue_ref_mock = AsyncMock()
        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_project_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=save_issue_ref_mock,
            ),
        ):
            client = MockClient.return_value
            client.create_project.return_value = make_create_project_response()
            # Simulate API error for create_issue
            client.create_issue.return_value = {"errors": [{"message": "Permission denied"}]}

            _, result = await export_project_to_linear(
                project_id=str(project.agent_project_id),
                linear_team_id=LINEAR_TEAM_ID,
            )

        assert result.errors == 1
        assert result.created == 0
        # Verify error path short-circuits: issue ref is NOT saved when create fails
        save_issue_ref_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_project_name_override(self) -> None:
        project = make_project(name="AHS Name", project_data=None)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_project_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value
            client.create_project.return_value = make_create_project_response()

            await export_project_to_linear(
                project_id=str(project.agent_project_id),
                linear_team_id=LINEAR_TEAM_ID,
                linear_project_name="Custom Linear Name",
            )

            # create_project should be called with the overridden name
            call_kwargs = client.create_project.call_args
            assert call_kwargs.kwargs.get("name") == "Custom Linear Name" or call_kwargs.args[0] == "Custom Linear Name"


# ---------------------------------------------------------------------------
# sync_tasks_to_linear — mocked
# ---------------------------------------------------------------------------


class TestSyncTasksToLinear:
    @pytest.mark.asyncio
    async def test_raises_when_no_linear_project_ref(self) -> None:
        project = make_project(project_data=None)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            pytest.raises(ValueError, match="Call export_project_to_linear first"),
        ):
            await sync_tasks_to_linear(project_id=str(project.agent_project_id))

    @pytest.mark.asyncio
    async def test_updates_existing_issues(self) -> None:
        now = datetime(2024, 6, 1, tzinfo=UTC)
        existing_issue_id = "existing-456"

        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        task = make_task(
            title="My Task",
            project_id=str(project.agent_project_id),
            task_data={
                "linear_ref": {
                    "linear_issue_id": existing_issue_id,
                    "linear_identifier": "ENG-10",
                    "last_synced_at": now.isoformat(),
                }
            },
        )

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value
            client.update_issue.return_value = make_update_issue_response(issue_id=existing_issue_id)

            result = await sync_tasks_to_linear(project_id=str(project.agent_project_id))

        assert result.updated == 1
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_creates_new_issues_for_unlinked_tasks(self) -> None:
        now = datetime(2024, 6, 1, tzinfo=UTC)

        project = make_project(
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": now.isoformat(),
                }
            }
        )
        task = make_task(
            title="New Task",
            project_id=str(project.agent_project_id),
            task_data=None,  # No linear_issue_id yet
        )
        new_issue_id = str(uuid.uuid4())

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._save_issue_ref",
                new=AsyncMock(),
            ),
        ):
            client = MockClient.return_value
            client.create_issue.return_value = make_create_issue_response(issue_id=new_issue_id, identifier="ENG-20")

            result = await sync_tasks_to_linear(project_id=str(project.agent_project_id))

        assert result.created == 1
        assert result.updated == 0
