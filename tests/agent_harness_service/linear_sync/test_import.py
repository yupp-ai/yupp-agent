"""Tests for the import_from_linear module.

Covers pure helper functions (no I/O) and higher-level import flows with
mocked LinearClient and async DB sessions.
"""

from __future__ import annotations
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.tools.linear_sync.import_from_linear import (
    _ahs_priority,
    _ahs_status,
    _extract_blocked_by,
    _topological_sort,
    import_project_from_linear,
    sync_tasks_from_linear,
)
from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

from tests.agent_harness_service.linear_sync.conftest import (
    CREATOR_USER_ID,
    LINEAR_PROJECT_ID,
    LINEAR_TEAM_ID,
    _call_sync,
    make_linear_issue,
)

# ---------------------------------------------------------------------------
# _extract_blocked_by
# ---------------------------------------------------------------------------


class TestExtractBlockedBy:
    def test_no_relations(self) -> None:
        issue: dict[str, Any] = {"id": "i1", "title": "Task", "relations": {"nodes": []}}
        assert _extract_blocked_by(issue) == []

    def test_single_blocked_by_relation(self) -> None:
        issue = make_linear_issue(issue_id="i1", blocked_by_ids=["blocker-id"])
        result = _extract_blocked_by(issue)
        assert result == ["blocker-id"]

    def test_multiple_blocked_by(self) -> None:
        issue = make_linear_issue(issue_id="i1", blocked_by_ids=["b1", "b2", "b3"])
        result = _extract_blocked_by(issue)
        assert result == ["b1", "b2", "b3"]

    def test_ignores_non_blocked_by_relation_types(self) -> None:
        issue: dict[str, Any] = {
            "id": "i1",
            "relations": {
                "nodes": [
                    {"type": "blocks", "relatedIssue": {"id": "other-id"}},
                    {"type": "duplicate", "relatedIssue": {"id": "dup-id"}},
                    {"type": "blocked_by", "relatedIssue": {"id": "real-blocker"}},
                ]
            },
        }
        result = _extract_blocked_by(issue)
        assert result == ["real-blocker"]

    def test_missing_relations_key(self) -> None:
        issue: dict[str, Any] = {"id": "i1", "title": "No relations"}
        assert _extract_blocked_by(issue) == []

    def test_relation_without_related_issue(self) -> None:
        issue: dict[str, Any] = {
            "id": "i1",
            "relations": {"nodes": [{"type": "blocked_by", "relatedIssue": None}]},
        }
        # relatedIssue is None → should be skipped gracefully
        assert _extract_blocked_by(issue) == []


# ---------------------------------------------------------------------------
# _topological_sort (Kahn's algorithm — import variant)
# ---------------------------------------------------------------------------


class TestImportTopologicalSort:
    def test_empty_list(self) -> None:
        assert _topological_sort([], {}, {}) == []

    def test_single_node_no_edges(self) -> None:
        parent_map: dict[str, str | None] = {"a": None}
        result = _topological_sort(["a"], parent_map, {"a": []})
        assert result == ["a"]

    def test_linear_chain_parent(self) -> None:
        """a → b → c (parent order): a must come before b, b before c."""
        issue_ids = ["a", "b", "c"]
        parent_map: dict[str, str | None] = {"a": None, "b": "a", "c": "b"}
        blocked_by_map: dict[str, list[str]] = {"a": [], "b": [], "c": []}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert result.index("a") < result.index("b") < result.index("c")

    def test_linear_chain_blocker(self) -> None:
        """a blocks b blocks c: a must come first."""
        issue_ids = ["a", "b", "c"]
        parent_map: dict[str, str | None] = {"a": None, "b": None, "c": None}
        blocked_by_map: dict[str, list[str]] = {"a": [], "b": ["a"], "c": ["b"]}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert result.index("a") < result.index("b")
        assert result.index("b") < result.index("c")

    def test_independent_nodes(self) -> None:
        """Nodes with no dependencies can appear in any order."""
        issue_ids = ["x", "y", "z"]
        parent_map: dict[str, str | None] = {"x": None, "y": None, "z": None}
        blocked_by_map: dict[str, list[str]] = {"x": [], "y": [], "z": []}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert set(result) == {"x", "y", "z"}

    def test_cycle_all_nodes_returned(self) -> None:
        """Cyclic graphs must still return all nodes (no infinite loop)."""
        issue_ids = ["a", "b"]
        parent_map: dict[str, str | None] = {"a": "b", "b": "a"}
        blocked_by_map: dict[str, list[str]] = {"a": [], "b": []}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert set(result) == {"a", "b"}

    def test_edges_to_external_issues_ignored(self) -> None:
        """Blockers/parents that are not in issue_ids are ignored."""
        issue_ids = ["a"]
        parent_map: dict[str, str | None] = {"a": "external-parent"}
        blocked_by_map: dict[str, list[str]] = {"a": ["external-blocker"]}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert result == ["a"]

    def test_diamond_dependency(self) -> None:
        """a → b, a → c, b & c → d: a must be first, d must be last."""
        issue_ids = ["a", "b", "c", "d"]
        parent_map: dict[str, str | None] = dict.fromkeys(issue_ids)
        blocked_by_map: dict[str, list[str]] = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
        result = _topological_sort(issue_ids, parent_map, blocked_by_map)
        assert result.index("a") < result.index("b")
        assert result.index("a") < result.index("c")
        assert result.index("b") < result.index("d")
        assert result.index("c") < result.index("d")


# ---------------------------------------------------------------------------
# _ahs_priority
# ---------------------------------------------------------------------------


class TestAhsPriority:
    @pytest.mark.parametrize(
        ("linear_priority", "expected"),
        [
            (0, AgentTaskPriority.NORMAL),  # NO_PRIORITY → NORMAL fallback
            (1, AgentTaskPriority.URGENT),
            (2, AgentTaskPriority.HIGH),
            (3, AgentTaskPriority.NORMAL),
            (4, AgentTaskPriority.LOW),
        ],
    )
    def test_known_priorities(self, linear_priority: int, expected: AgentTaskPriority) -> None:
        assert _ahs_priority(linear_priority) == expected

    def test_none_input_defaults_to_normal(self) -> None:
        """None linear priority (field absent from API) should default gracefully."""
        assert _ahs_priority(None) == AgentTaskPriority.NORMAL

    def test_out_of_range_falls_back_to_normal(self) -> None:
        # map_linear_priority_to_ahs(99) → "NO_PRIORITY" → AgentTaskPriority["NO_PRIORITY"]
        # AgentTaskPriority has no NO_PRIORITY member → KeyError → NORMAL fallback
        result = _ahs_priority(99)
        assert result == AgentTaskPriority.NORMAL


# ---------------------------------------------------------------------------
# _ahs_status
# ---------------------------------------------------------------------------


class TestAhsStatus:
    def test_has_unmet_deps_always_blocked(self) -> None:
        issue = make_linear_issue(state_type="unstarted")
        assert _ahs_status(issue, has_unmet_deps=True) == AgentTaskStatus.BLOCKED

    def test_no_deps_unstarted_is_ready(self) -> None:
        issue = make_linear_issue(state_type="unstarted")
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.READY

    def test_no_deps_started_is_in_progress(self) -> None:
        issue = make_linear_issue(state_type="started")
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.IN_PROGRESS

    def test_no_deps_completed_is_completed(self) -> None:
        issue = make_linear_issue(state_type="completed")
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.COMPLETED

    def test_no_deps_cancelled_is_cancelled(self) -> None:
        issue = make_linear_issue(state_type="cancelled")
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.CANCELLED

    def test_no_deps_triage_is_ready(self) -> None:
        issue = make_linear_issue(state_type="triage")
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.READY

    def test_missing_state_key_defaults_to_ready(self) -> None:
        issue: dict[str, Any] = {"id": "x", "title": "No state"}
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.READY

    def test_unknown_state_type_defaults_to_ready(self) -> None:
        issue: dict[str, Any] = {
            "id": "x",
            "title": "Unknown state",
            "state": {"type": "future_state", "id": "y"},
        }
        assert _ahs_status(issue, has_unmet_deps=False) == AgentTaskStatus.READY


# ---------------------------------------------------------------------------
# import_project_from_linear — mocked
# ---------------------------------------------------------------------------


class TestImportProjectFromLinear:
    """Tests for import_project_from_linear with mocked LinearClient and DB."""

    def _mock_db_session(self) -> AsyncMock:
        session = AsyncMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        # Make session.add a no-op that captures the object for inspection
        self._added_objects: list[Any] = []

        def _add(obj: Any) -> None:
            self._added_objects.append(obj)
            # Assign a UUID if the object doesn't have one yet (simulates DB flush)
            if isinstance(obj, AgentProject) and not getattr(obj, "agent_project_id", None):
                obj.agent_project_id = uuid.uuid4()
            if isinstance(obj, AgentTask) and not getattr(obj, "agent_task_id", None):
                obj.agent_task_id = uuid.uuid4()

        session.add = _add
        return session

    @pytest.mark.asyncio
    async def test_creates_project_and_tasks(self) -> None:
        issues = [
            make_linear_issue(issue_id="li-1", identifier="ENG-1", title="Task One"),
            make_linear_issue(issue_id="li-2", identifier="ENG-2", title="Task Two"),
        ]

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.get_project.return_value = {
                "data": {"project": {"id": LINEAR_PROJECT_ID, "name": "My Project", "description": "desc"}}
            }
            client.list_project_issues.return_value = issues

            session = self._mock_db_session()

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                yield session

            mock_get_session.return_value = _session_cm()

            project, result = await import_project_from_linear(
                linear_project_id=LINEAR_PROJECT_ID,
                linear_team_id=LINEAR_TEAM_ID,
                creator_user_id=CREATOR_USER_ID,
            )

        assert result.created == 2
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_empty_project_no_tasks(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.get_project.return_value = {
                "data": {"project": {"id": LINEAR_PROJECT_ID, "name": "Empty", "description": None}}
            }
            client.list_project_issues.return_value = []

            session = self._mock_db_session()

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                yield session

            mock_get_session.return_value = _session_cm()

            project, result = await import_project_from_linear(
                linear_project_id=LINEAR_PROJECT_ID,
                linear_team_id=LINEAR_TEAM_ID,
                creator_user_id=CREATOR_USER_ID,
            )

        assert result.created == 0

    @pytest.mark.asyncio
    async def test_project_with_parent_child_tasks(self) -> None:
        parent_issue_id = "parent-li-1"
        child_issue_id = "child-li-2"
        issues = [
            make_linear_issue(issue_id=parent_issue_id, identifier="ENG-1", title="Parent"),
            make_linear_issue(
                issue_id=child_issue_id,
                identifier="ENG-2",
                title="Child",
                parent_id=parent_issue_id,
            ),
        ]

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.get_project.return_value = {
                "data": {"project": {"id": LINEAR_PROJECT_ID, "name": "P", "description": None}}
            }
            client.list_project_issues.return_value = issues

            captured_tasks: list[AgentTask] = []
            session = AsyncMock()
            session.flush = AsyncMock()
            session.commit = AsyncMock()
            session.refresh = AsyncMock()

            def _add(obj: Any) -> None:
                if isinstance(obj, AgentProject):
                    obj.agent_project_id = uuid.uuid4()
                if isinstance(obj, AgentTask):
                    obj.agent_task_id = uuid.uuid4()
                    captured_tasks.append(obj)

            session.add = _add

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                yield session

            mock_get_session.return_value = _session_cm()

            project, result = await import_project_from_linear(
                linear_project_id=LINEAR_PROJECT_ID,
                linear_team_id=LINEAR_TEAM_ID,
                creator_user_id=CREATOR_USER_ID,
            )

        assert result.created == 2
        # Child task should have parent_task_id set
        child_task = next((t for t in captured_tasks if t.title == "Child"), None)
        assert child_task is not None
        assert child_task.parent_task_id is not None

    @pytest.mark.asyncio
    async def test_task_with_dependency_is_blocked(self) -> None:
        """A task whose blocker exists in the import set should start as BLOCKED."""
        blocker_id = "blocker-li"
        blocked_id = "blocked-li"
        issues = [
            make_linear_issue(issue_id=blocker_id, identifier="ENG-1", title="Blocker"),
            make_linear_issue(
                issue_id=blocked_id,
                identifier="ENG-2",
                title="Blocked",
                blocked_by_ids=[blocker_id],
            ),
        ]

        captured_tasks: list[AgentTask] = []

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.get_project.return_value = {
                "data": {"project": {"id": LINEAR_PROJECT_ID, "name": "P", "description": None}}
            }
            client.list_project_issues.return_value = issues

            session = AsyncMock()
            session.flush = AsyncMock()
            session.commit = AsyncMock()
            session.refresh = AsyncMock()

            def _add(obj: Any) -> None:
                if isinstance(obj, AgentProject):
                    obj.agent_project_id = uuid.uuid4()
                if isinstance(obj, AgentTask):
                    obj.agent_task_id = uuid.uuid4()
                    captured_tasks.append(obj)

            session.add = _add

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                yield session

            mock_get_session.return_value = _session_cm()

            _, result = await import_project_from_linear(
                linear_project_id=LINEAR_PROJECT_ID,
                linear_team_id=LINEAR_TEAM_ID,
                creator_user_id=CREATOR_USER_ID,
            )

        blocked_task = next((t for t in captured_tasks if t.title == "Blocked"), None)
        assert blocked_task is not None
        assert blocked_task.status == AgentTaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_linear_api_error_raises_runtime_error(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
        ):
            client = MockClient.return_value
            client.get_project.return_value = {"errors": [{"message": "Not found"}]}
            with pytest.raises(RuntimeError, match="Not found"):
                await import_project_from_linear(
                    linear_project_id="bad-id",
                    linear_team_id=LINEAR_TEAM_ID,
                    creator_user_id=CREATOR_USER_ID,
                )


# ---------------------------------------------------------------------------
# sync_tasks_from_linear — mocked
# ---------------------------------------------------------------------------


class TestSyncTasksFromLinear:
    """Tests for sync_tasks_from_linear with mocked DB and LinearClient."""

    @pytest.mark.asyncio
    async def test_updates_existing_task_title_and_description(self) -> None:
        project_id = uuid.uuid4()
        linear_issue_id = "li-existing"
        since = datetime(2024, 1, 1, tzinfo=UTC)

        existing_task = AgentTask(
            agent_project_id=project_id,
            title="Old Title",
            description="Old desc",
            status=AgentTaskStatus.READY,
            priority=AgentTaskPriority.NORMAL,
            # Use nested format to match production code (sync_tasks_from_linear queries
            # task_data['linear_ref']['linear_issue_id'])
            task_data={
                "linear_ref": {
                    "linear_issue_id": linear_issue_id,
                    "linear_identifier": "TEST-1",
                    "last_synced_at": "2024-01-01T00:00:00+00:00",
                }
            },
        )
        existing_task.agent_task_id = uuid.uuid4()

        updated_issue = make_linear_issue(
            issue_id=linear_issue_id,
            title="New Title",
            description="New desc",
            state_type="started",
            priority=2,
        )

        project = AgentProject(
            name="Test",
            status=AgentProjectStatus.ACTIVE,
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": since.isoformat(),
                }
            },
        )
        project.agent_project_id = project_id

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.list_project_issues.return_value = [updated_issue]

            # First session: fetch project
            # Second session: fetch existing tasks + update
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()

                if call_count == 1:
                    # Project fetch
                    proj_result = MagicMock()
                    proj_result.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=proj_result)
                else:
                    # Task fetch + project update
                    task_result = MagicMock()
                    task_result.scalars.return_value = iter([existing_task])

                    proj_result2 = MagicMock()
                    proj_result2.scalars.return_value.first.return_value = project

                    session.execute = AsyncMock(side_effect=[task_result, proj_result2])

                yield session

            mock_get_session.side_effect = _session_cm

            result = await sync_tasks_from_linear(project_id=project_id, since=since)

        assert result.updated == 1
        assert result.created == 0
        assert result.errors == 0
        assert existing_task.title == "New Title"
        assert existing_task.description == "New desc"

    @pytest.mark.asyncio
    async def test_creates_new_task_for_unknown_issue(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        new_linear_id = "li-new-456"

        new_issue = make_linear_issue(
            issue_id=new_linear_id,
            identifier="ENG-99",
            title="Brand New Issue",
            state_type="unstarted",
        )

        project = AgentProject(
            name="P",
            status=AgentProjectStatus.ACTIVE,
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": since.isoformat(),
                }
            },
        )
        project.agent_project_id = project_id

        captured_new_tasks: list[AgentTask] = []

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.list_project_issues.return_value = [new_issue]

            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()

                def _add(obj: Any) -> None:
                    if isinstance(obj, AgentTask):
                        captured_new_tasks.append(obj)

                session.add = _add

                if call_count == 1:
                    proj_result = MagicMock()
                    proj_result.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=proj_result)
                else:
                    task_result = MagicMock()
                    task_result.scalars.return_value = iter([])  # No existing tasks

                    proj_result2 = MagicMock()
                    proj_result2.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_result, proj_result2])

                yield session

            mock_get_session.side_effect = _session_cm

            result = await sync_tasks_from_linear(project_id=project_id, since=since)

        assert result.created == 1
        assert result.updated == 0

    @pytest.mark.asyncio
    async def test_no_issues_updated_returns_empty_result(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)

        project = AgentProject(
            name="P",
            status=AgentProjectStatus.ACTIVE,
            project_data={
                "linear_ref": {
                    "linear_project_id": LINEAR_PROJECT_ID,
                    "linear_team_id": LINEAR_TEAM_ID,
                    "last_synced_at": since.isoformat(),
                }
            },
        )
        project.agent_project_id = project_id

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=lambda fn, *args, **kwargs: _call_sync(fn, *args, **kwargs),
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            client = MockClient.return_value
            client.list_project_issues.return_value = []

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                session = AsyncMock()
                proj_result = MagicMock()
                proj_result.scalars.return_value.first.return_value = project
                session.execute = AsyncMock(return_value=proj_result)
                yield session

            mock_get_session.side_effect = _session_cm

            result = await sync_tasks_from_linear(project_id=project_id, since=since)

        assert result.created == 0
        assert result.updated == 0
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_project_not_found_raises_value_error(self) -> None:
        project_id = uuid.uuid4()

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                session = AsyncMock()
                proj_result = MagicMock()
                proj_result.scalars.return_value.first.return_value = None  # Not found
                session.execute = AsyncMock(return_value=proj_result)
                yield session

            mock_get_session.side_effect = _session_cm

            with pytest.raises(ValueError, match="AHS project not found"):
                await sync_tasks_from_linear(project_id=project_id)

    @pytest.mark.asyncio
    async def test_project_without_linear_ref_raises_value_error(self) -> None:
        project_id = uuid.uuid4()

        project = AgentProject(
            name="P",
            status=AgentProjectStatus.ACTIVE,
            project_data={},  # No linear_ref
        )
        project.agent_project_id = project_id

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                session = AsyncMock()
                proj_result = MagicMock()
                proj_result.scalars.return_value.first.return_value = project
                session.execute = AsyncMock(return_value=proj_result)
                yield session

            mock_get_session.side_effect = _session_cm

            with pytest.raises(ValueError, match="no Linear reference"):
                await sync_tasks_from_linear(project_id=project_id)
