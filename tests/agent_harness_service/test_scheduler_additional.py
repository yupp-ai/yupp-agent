"""Additional unit tests for ypl/agent_harness_service/scheduler.py.

Extends the existing test_scheduler.py with tests for:
- _parse_env_int
- recover_stale_in_progress_schedules
- get_agent_by_id
- _extract_linear_ref
- _get_projects_needing_linear_sync
- wait_for_in_flight_tasks
"""

from __future__ import annotations
import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.agent_harness_service.scheduler import (
    _extract_linear_ref,
    _parse_env_int,
    get_agent_by_id,
    recover_stale_in_progress_schedules,
    wait_for_in_flight_tasks,
)
from ypl.db.agent_harness import (
    Agent,
    AgentSchedule,
    AgentScheduleRun,
    AgentScheduleRunStatus,
    AgentScheduleStatus,
    AgentScheduleType,
)


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


def _make_schedule(
    status: AgentScheduleStatus = AgentScheduleStatus.IN_PROGRESS,
    run_count: int = 0,
    max_runs: int | None = None,
    schedule_type: AgentScheduleType = AgentScheduleType.SCHEDULED,
) -> AgentSchedule:
    return AgentSchedule(
        agent_schedule_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        schedule_type=schedule_type,
        message="Test message",
        status=status,
        next_run_at=datetime.now(UTC) - timedelta(minutes=5),
        cron_timezone="UTC",
        created_by_user="test@example.com",
        run_count=run_count,
        max_runs=max_runs,
        modified_at=datetime.now(UTC) - timedelta(hours=2),
    )


# ===========================================================================
# Tests: _parse_env_int
# ===========================================================================


class TestParseEnvInt:
    """Tests for _parse_env_int."""

    def test_returns_default_when_env_not_set(self) -> None:
        key = "__SCHED_TEST_ABSENT__"
        os.environ.pop(key, None)
        assert _parse_env_int(key, 42) == 42

    def test_parses_valid_int(self) -> None:
        key = "__SCHED_TEST_INT__"
        os.environ[key] = "99"
        try:
            assert _parse_env_int(key, 1) == 99
        finally:
            del os.environ[key]

    def test_returns_default_for_invalid_value(self) -> None:
        key = "__SCHED_TEST_INVALID__"
        os.environ[key] = "not_a_number"
        try:
            assert _parse_env_int(key, 7) == 7
        finally:
            del os.environ[key]

    def test_returns_default_for_zero_value(self) -> None:
        key = "__SCHED_TEST_ZERO__"
        os.environ[key] = "0"
        try:
            assert _parse_env_int(key, 5) == 5
        finally:
            del os.environ[key]

    def test_returns_default_for_negative_value(self) -> None:
        key = "__SCHED_TEST_NEG__"
        os.environ[key] = "-3"
        try:
            assert _parse_env_int(key, 5) == 5
        finally:
            del os.environ[key]


# ===========================================================================
# Tests: _extract_linear_ref
# ===========================================================================


class TestExtractLinearRef:
    """Tests for _extract_linear_ref."""

    def test_returns_none_for_none_input(self) -> None:
        assert _extract_linear_ref(None) is None

    def test_returns_none_for_empty_dict(self) -> None:
        assert _extract_linear_ref({}) is None

    def test_returns_none_when_no_linear_ref_key(self) -> None:
        assert _extract_linear_ref({"other_key": "value"}) is None

    def test_returns_none_when_linear_ref_not_dict(self) -> None:
        assert _extract_linear_ref({"linear_ref": "not-a-dict"}) is None

    def test_returns_none_when_linear_project_id_missing(self) -> None:
        assert _extract_linear_ref({"linear_ref": {"some_key": "value"}}) is None

    def test_returns_none_when_linear_project_id_empty(self) -> None:
        assert _extract_linear_ref({"linear_ref": {"linear_project_id": ""}}) is None

    def test_returns_linear_ref_when_valid(self) -> None:
        linear_ref = {"linear_project_id": "proj-123", "last_synced_at": "2024-01-01T00:00:00Z"}
        project_data = {"linear_ref": linear_ref}
        result = _extract_linear_ref(project_data)
        assert result == linear_ref

    def test_returns_linear_ref_without_last_synced_at(self) -> None:
        linear_ref = {"linear_project_id": "proj-abc"}
        result = _extract_linear_ref({"linear_ref": linear_ref})
        assert result == linear_ref


# ===========================================================================
# Tests: get_agent_by_id
# ===========================================================================


class TestGetAgentById:
    """Tests for get_agent_by_id."""

    async def test_returns_agent_when_found(self) -> None:
        agent = Agent(agent_id=uuid.uuid4(), name="test-agent", display_name="Test Agent")
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=agent)

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await get_agent_by_id(agent.agent_id)
            assert result is agent
            mock_db.get.assert_called_once_with(Agent, agent.agent_id)

    async def test_returns_none_when_not_found(self) -> None:
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=None)

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await get_agent_by_id(uuid.uuid4())
            assert result is None


# ===========================================================================
# Tests: recover_stale_in_progress_schedules
# ===========================================================================


class TestRecoverStaleInProgressSchedules:
    """Tests for recover_stale_in_progress_schedules."""

    async def test_returns_zero_when_no_stale_schedules(self) -> None:
        mock_db = AsyncMock()
        scalars = MagicMock()
        scalars.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars
        mock_db.execute = AsyncMock(return_value=result_mock)

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            count = await recover_stale_in_progress_schedules()
            assert count == 0

    async def test_recovers_stale_schedule_to_pending(self) -> None:
        schedule = _make_schedule()
        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()

        schedule_scalars = MagicMock()
        schedule_scalars.all.return_value = [schedule]
        schedule_result = MagicMock()
        schedule_result.scalars.return_value = schedule_scalars

        # orphaned runs query returns empty
        runs_scalars = MagicMock()
        runs_scalars.all.return_value = []
        runs_result = MagicMock()
        runs_result.scalars.return_value = runs_scalars

        mock_db.execute = AsyncMock(side_effect=[schedule_result, runs_result])

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            count = await recover_stale_in_progress_schedules()
            assert count == 1
            assert schedule.status == AgentScheduleStatus.PENDING
            assert "Recovered" in (schedule.last_error or "")

    async def test_marks_schedule_failed_when_max_runs_reached(self) -> None:
        schedule = _make_schedule(run_count=3, max_runs=3)
        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()

        schedule_scalars = MagicMock()
        schedule_scalars.all.return_value = [schedule]
        schedule_result = MagicMock()
        schedule_result.scalars.return_value = schedule_scalars

        # orphaned runs query returns an IN_PROGRESS run (which increments run_count)
        mock_run = AgentScheduleRun(
            agent_schedule_run_id=uuid.uuid4(),
            agent_schedule_id=schedule.agent_schedule_id,
            run_number=3,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        runs_scalars = MagicMock()
        runs_scalars.all.return_value = [mock_run]
        runs_result = MagicMock()
        runs_result.scalars.return_value = runs_scalars

        mock_db.execute = AsyncMock(side_effect=[schedule_result, runs_result])

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            count = await recover_stale_in_progress_schedules()
            assert count == 1
            # run_count should have been incremented to 4, which is >= max_runs(3)
            assert schedule.status == AgentScheduleStatus.FAILED

    async def test_marks_orphaned_runs_as_failed(self) -> None:
        schedule = _make_schedule()
        mock_run = AgentScheduleRun(
            agent_schedule_run_id=uuid.uuid4(),
            agent_schedule_id=schedule.agent_schedule_id,
            run_number=1,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()

        schedule_scalars = MagicMock()
        schedule_scalars.all.return_value = [schedule]
        schedule_result = MagicMock()
        schedule_result.scalars.return_value = schedule_scalars

        runs_scalars = MagicMock()
        runs_scalars.all.return_value = [mock_run]
        runs_result = MagicMock()
        runs_result.scalars.return_value = runs_scalars

        mock_db.execute = AsyncMock(side_effect=[schedule_result, runs_result])

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            await recover_stale_in_progress_schedules()
            assert mock_run.status == AgentScheduleRunStatus.FAILED
            assert mock_run.completed_at is not None
            assert mock_run.error is not None


# ===========================================================================
# Tests: wait_for_in_flight_tasks
# ===========================================================================


class TestWaitForInFlightTasks:
    """Tests for wait_for_in_flight_tasks."""

    async def test_returns_zero_when_no_tasks(self) -> None:
        import ypl.agent_harness_service.scheduler as sched_module

        original = sched_module._execution_tasks.copy()
        sched_module._execution_tasks.clear()
        try:
            result = await wait_for_in_flight_tasks(timeout_seconds=1.0)
            assert result == 0
        finally:
            sched_module._execution_tasks.update(original)

    async def test_waits_for_in_flight_tasks(self) -> None:
        import ypl.agent_harness_service.scheduler as sched_module

        original = sched_module._execution_tasks.copy()
        sched_module._execution_tasks.clear()

        async def quick_task() -> None:
            await asyncio.sleep(0)

        task = asyncio.create_task(quick_task())
        sched_module._execution_tasks.add(task)

        try:
            result = await wait_for_in_flight_tasks(timeout_seconds=5.0)
            assert result == 0  # all tasks completed within timeout
        finally:
            sched_module._execution_tasks.clear()
            sched_module._execution_tasks.update(original)


# ===========================================================================
# Tests: poll_and_sync_linear_projects
# ===========================================================================


class TestPollAndSyncLinearProjects:
    """Tests for poll_and_sync_linear_projects."""

    async def test_does_nothing_when_no_projects_need_sync(self) -> None:
        from ypl.agent_harness_service.scheduler import poll_and_sync_linear_projects

        with patch(
            "ypl.agent_harness_service.scheduler._get_projects_needing_linear_sync",
            AsyncMock(return_value=[]),
        ):
            # Should not raise
            await poll_and_sync_linear_projects()

    async def test_handles_query_error_gracefully(self) -> None:
        from ypl.agent_harness_service.scheduler import poll_and_sync_linear_projects

        with patch(
            "ypl.agent_harness_service.scheduler._get_projects_needing_linear_sync",
            AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            # Should not raise — logs and continues
            await poll_and_sync_linear_projects()

    async def test_syncs_projects_with_linear(self) -> None:
        from ypl.agent_harness_service.scheduler import poll_and_sync_linear_projects

        sync_result = MagicMock()
        sync_result.created = 1
        sync_result.updated = 2
        sync_result.skipped = 0
        sync_result.errors = 0

        with (
            patch(
                "ypl.agent_harness_service.scheduler._get_projects_needing_linear_sync",
                AsyncMock(return_value=[("proj-id-1", "linear-proj-1")]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional.sync_bidirectional",
                AsyncMock(return_value=sync_result),
            ),
        ):
            # Should not raise
            await poll_and_sync_linear_projects()

    async def test_continues_after_per_project_sync_error(self) -> None:
        from ypl.agent_harness_service.scheduler import poll_and_sync_linear_projects

        with (
            patch(
                "ypl.agent_harness_service.scheduler._get_projects_needing_linear_sync",
                AsyncMock(return_value=[("proj-id-1", "linear-proj-1"), ("proj-id-2", "linear-proj-2")]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional.sync_bidirectional",
                AsyncMock(side_effect=RuntimeError("sync failed")),
            ),
        ):
            # Should not raise — logs errors and continues
            await poll_and_sync_linear_projects()
