"""Tests for the agent schedule scheduler."""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.types import SessionCreateResponse
from ypl.agent_harness_service.scheduler import (
    claim_agent_schedule,
    execute_agent_schedule,
    get_due_agent_schedules,
    poll_and_execute_due_schedules,
    update_run_failure,
    update_run_success,
)
from ypl.db.agent_harness import (
    Agent,
    AgentSchedule,
    AgentScheduleRun,
    AgentScheduleRunStatus,
    AgentScheduleStatus,
    AgentScheduleType,
)


def create_mock_session_factory(mock_db: AsyncMock) -> Any:
    """Create a mock async context manager factory for get_async_session."""

    @asynccontextmanager
    async def mock_get_async_session() -> AsyncGenerator[Any, None]:
        yield mock_db

    return mock_get_async_session


@pytest.fixture
def mock_agent() -> Agent:
    """Create a mock agent."""
    return Agent(
        agent_id=uuid.uuid4(),
        name="test-agent",
        display_name="Test Agent",
    )


@pytest.fixture
def mock_scheduled_schedule(mock_agent: Agent) -> AgentSchedule:
    """Create a mock SCHEDULED agent schedule."""
    return AgentSchedule(
        agent_schedule_id=uuid.uuid4(),
        agent_id=mock_agent.agent_id,
        schedule_type=AgentScheduleType.SCHEDULED,
        message="Test message",
        status=AgentScheduleStatus.PENDING,
        execute_at=datetime.now(UTC) - timedelta(minutes=1),
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        cron_timezone="UTC",
        created_by_user="test@example.com",
    )


@pytest.fixture
def mock_recurring_schedule(mock_agent: Agent) -> AgentSchedule:
    """Create a mock RECURRING agent schedule."""
    return AgentSchedule(
        agent_schedule_id=uuid.uuid4(),
        agent_id=mock_agent.agent_id,
        schedule_type=AgentScheduleType.RECURRING,
        message="Test recurring message",
        status=AgentScheduleStatus.PENDING,
        cron_expression="*/5 * * * *",
        next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        cron_timezone="UTC",
        created_by_user="test@example.com",
        run_count=0,
        max_runs=3,
    )


@pytest.fixture
def mock_run(mock_scheduled_schedule: AgentSchedule) -> AgentScheduleRun:
    """Create a mock run record."""
    return AgentScheduleRun(
        agent_schedule_run_id=uuid.uuid4(),
        agent_schedule_id=mock_scheduled_schedule.agent_schedule_id,
        run_number=1,
        status=AgentScheduleRunStatus.IN_PROGRESS,
        started_at=datetime.now(UTC),
    )


class TestGetDueAgentSchedules:
    """Tests for get_due_agent_schedules."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_due_schedules(self) -> None:
        """Should return empty list when no schedules are due."""
        mock_db = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            result = await get_due_agent_schedules()
            assert result == []

    @pytest.mark.asyncio
    async def test_returns_due_schedules(self, mock_scheduled_schedule: AgentSchedule) -> None:
        """Should return schedules that are due."""
        mock_db = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_scheduled_schedule]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            result = await get_due_agent_schedules()
            assert len(result) == 1
            assert result[0] == mock_scheduled_schedule


class TestClaimAgentSchedule:
    """Tests for claim_agent_schedule."""

    @pytest.mark.asyncio
    async def test_claim_pending_schedule(self, mock_scheduled_schedule: AgentSchedule) -> None:
        """Should claim a pending schedule, create a run, and set statuses."""
        mock_db = AsyncMock()

        # First execute: SELECT ... FOR UPDATE returns the schedule
        mock_schedule_result = MagicMock()
        mock_schedule_result.scalar_one_or_none.return_value = mock_scheduled_schedule

        # Second execute: SELECT MAX(run_number) returns 0 (no previous runs)
        mock_max_result = MagicMock()
        mock_max_result.scalar_one.return_value = 0

        mock_db.execute.side_effect = [mock_schedule_result, mock_max_result]

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            schedule, run = await claim_agent_schedule(mock_scheduled_schedule.agent_schedule_id)

            assert schedule == mock_scheduled_schedule
            assert schedule.status == AgentScheduleStatus.IN_PROGRESS
            assert run is not None
            assert run.run_number == 1
            assert run.status == AgentScheduleRunStatus.IN_PROGRESS
            mock_db.add.assert_called_once()  # Run was added
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_already_claimed(self) -> None:
        """Should return (None, None) when schedule is already claimed."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            schedule, run = await claim_agent_schedule(uuid.uuid4())
            assert schedule is None
            assert run is None


class TestUpdateRunSuccess:
    """Tests for update_run_success."""

    @pytest.mark.asyncio
    async def test_scheduled_completed(
        self, mock_scheduled_schedule: AgentSchedule, mock_run: AgentScheduleRun
    ) -> None:
        """SCHEDULED should be marked as COMPLETED on success."""
        session_id = str(uuid.uuid4())
        mock_db = AsyncMock()
        mock_db.get.side_effect = [mock_run, mock_scheduled_schedule]

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            await update_run_success(mock_scheduled_schedule, mock_run, session_id)

            # Check run was updated
            assert mock_run.status == AgentScheduleRunStatus.COMPLETED
            assert mock_run.session_id == uuid.UUID(session_id)
            assert mock_run.completed_at is not None

            # Check schedule was updated
            assert mock_scheduled_schedule.status == AgentScheduleStatus.COMPLETED
            assert mock_scheduled_schedule.run_count == 1
            assert mock_scheduled_schedule.last_error is None
            assert mock_scheduled_schedule.last_session_id == uuid.UUID(session_id)
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_recurring_reschedules(self, mock_recurring_schedule: AgentSchedule, mock_agent: Agent) -> None:
        """RECURRING should be rescheduled on success."""
        session_id = str(uuid.uuid4())
        next_run = datetime.now(UTC) + timedelta(minutes=5)
        mock_run = AgentScheduleRun(
            agent_schedule_run_id=uuid.uuid4(),
            agent_schedule_id=mock_recurring_schedule.agent_schedule_id,
            run_number=1,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        mock_db = AsyncMock()
        mock_db.get.side_effect = [mock_run, mock_recurring_schedule]

        with (
            patch(
                "ypl.agent_harness_service.scheduler.get_async_session",
                create_mock_session_factory(mock_db),
            ),
            patch("ypl.agent_harness_service.scheduler.compute_next_run_for_cron") as mock_compute,
        ):
            mock_compute.return_value = next_run

            await update_run_success(mock_recurring_schedule, mock_run, session_id)

            assert mock_recurring_schedule.status == AgentScheduleStatus.PENDING
            assert mock_recurring_schedule.next_run_at == next_run
            assert mock_recurring_schedule.run_count == 1
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_recurring_completes_at_max_runs(
        self, mock_recurring_schedule: AgentSchedule, mock_agent: Agent
    ) -> None:
        """RECURRING should be COMPLETED when max_runs is reached."""
        mock_recurring_schedule.run_count = 2  # Will become 3 after increment
        mock_recurring_schedule.max_runs = 3
        session_id = str(uuid.uuid4())
        mock_run = AgentScheduleRun(
            agent_schedule_run_id=uuid.uuid4(),
            agent_schedule_id=mock_recurring_schedule.agent_schedule_id,
            run_number=3,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        mock_db = AsyncMock()
        mock_db.get.side_effect = [mock_run, mock_recurring_schedule]

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            await update_run_success(mock_recurring_schedule, mock_run, session_id)

            assert mock_recurring_schedule.status == AgentScheduleStatus.COMPLETED
            assert mock_recurring_schedule.run_count == 3
            mock_db.commit.assert_called_once()


class TestUpdateRunFailure:
    """Tests for update_run_failure."""

    @pytest.mark.asyncio
    async def test_scheduled_fails(self, mock_scheduled_schedule: AgentSchedule, mock_run: AgentScheduleRun) -> None:
        """SCHEDULED should be marked as FAILED on failure."""
        mock_db = AsyncMock()
        mock_db.get.side_effect = [mock_run, mock_scheduled_schedule]

        with patch(
            "ypl.agent_harness_service.scheduler.get_async_session",
            create_mock_session_factory(mock_db),
        ):
            await update_run_failure(mock_scheduled_schedule, mock_run, "Test error")

            # Check run was updated
            assert mock_run.status == AgentScheduleRunStatus.FAILED
            assert mock_run.error == "Test error"
            assert mock_run.completed_at is not None

            # Check schedule was updated
            assert mock_scheduled_schedule.status == AgentScheduleStatus.FAILED
            assert mock_scheduled_schedule.last_error == "Test error"
            assert mock_scheduled_schedule.run_count == 1
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_recurring_reschedules_on_failure(self, mock_recurring_schedule: AgentSchedule) -> None:
        """RECURRING should be rescheduled on failure (retry on next schedule)."""
        next_run = datetime.now(UTC) + timedelta(minutes=5)
        mock_run = AgentScheduleRun(
            agent_schedule_run_id=uuid.uuid4(),
            agent_schedule_id=mock_recurring_schedule.agent_schedule_id,
            run_number=1,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        mock_db = AsyncMock()
        mock_db.get.side_effect = [mock_run, mock_recurring_schedule]

        with (
            patch(
                "ypl.agent_harness_service.scheduler.get_async_session",
                create_mock_session_factory(mock_db),
            ),
            patch("ypl.agent_harness_service.scheduler.compute_next_run_for_cron") as mock_compute,
        ):
            mock_compute.return_value = next_run

            await update_run_failure(mock_recurring_schedule, mock_run, "Test error")

            assert mock_recurring_schedule.status == AgentScheduleStatus.PENDING
            assert mock_recurring_schedule.next_run_at == next_run
            assert mock_recurring_schedule.last_error == "Test error"
            assert mock_recurring_schedule.run_count == 1
            mock_db.commit.assert_called_once()


class TestExecuteAgentSchedule:
    """Tests for execute_agent_schedule."""

    @pytest.mark.asyncio
    async def test_execute_scheduled(
        self, mock_scheduled_schedule: AgentSchedule, mock_agent: Agent, mock_run: AgentScheduleRun
    ) -> None:
        """Should execute a SCHEDULED successfully."""
        session_id = str(uuid.uuid4())
        with (
            patch("ypl.agent_harness_service.scheduler.claim_agent_schedule") as mock_claim,
            patch("ypl.agent_harness_service.scheduler.get_agent_by_id") as mock_get_agent,
            patch("ypl.agent_harness_service.scheduler.create_session") as mock_create_session,
            patch("ypl.agent_harness_service.scheduler.update_run_success") as mock_update_success,
        ):
            mock_claim.return_value = (mock_scheduled_schedule, mock_run)
            mock_get_agent.return_value = mock_agent
            mock_create_session.return_value = SessionCreateResponse(session_id=session_id, status="ACTIVE")

            await execute_agent_schedule(mock_scheduled_schedule.agent_schedule_id)

            mock_claim.assert_called_once_with(mock_scheduled_schedule.agent_schedule_id)
            mock_get_agent.assert_called_once_with(mock_scheduled_schedule.agent_id)
            # create_session now takes the message directly (no separate send_message call)
            mock_create_session.assert_called_once()
            create_session_request = mock_create_session.call_args[0][0]
            assert create_session_request.message == mock_scheduled_schedule.message
            mock_update_success.assert_called_once_with(mock_scheduled_schedule, mock_run, session_id)

    @pytest.mark.asyncio
    async def test_skip_already_claimed(self) -> None:
        """Should skip if schedule is already claimed."""
        schedule_id = uuid.uuid4()
        with (
            patch("ypl.agent_harness_service.scheduler.claim_agent_schedule") as mock_claim,
            patch("ypl.agent_harness_service.scheduler.get_agent_by_id") as mock_get_agent,
        ):
            mock_claim.return_value = (None, None)

            await execute_agent_schedule(schedule_id)

            mock_claim.assert_called_once_with(schedule_id)
            mock_get_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_failure(
        self, mock_scheduled_schedule: AgentSchedule, mock_agent: Agent, mock_run: AgentScheduleRun
    ) -> None:
        """Should call update_run_failure on error."""
        with (
            patch("ypl.agent_harness_service.scheduler.claim_agent_schedule") as mock_claim,
            patch("ypl.agent_harness_service.scheduler.get_agent_by_id") as mock_get_agent,
            patch("ypl.agent_harness_service.scheduler.update_run_failure") as mock_update_failure,
        ):
            mock_claim.return_value = (mock_scheduled_schedule, mock_run)
            mock_get_agent.return_value = None  # Agent not found

            await execute_agent_schedule(mock_scheduled_schedule.agent_schedule_id)

            mock_update_failure.assert_called_once()


class TestPollAndExecuteDueSchedules:
    """Tests for poll_and_execute_due_schedules."""

    @pytest.mark.asyncio
    async def test_spawns_tasks_for_due_schedules(self, mock_scheduled_schedule: AgentSchedule) -> None:
        """Should spawn background tasks for each due schedule."""
        with (
            patch("ypl.agent_harness_service.scheduler.get_due_agent_schedules") as mock_get_due,
            patch("ypl.agent_harness_service.scheduler.create_background_task") as mock_create_task,
        ):
            mock_get_due.return_value = [mock_scheduled_schedule]

            await poll_and_execute_due_schedules()

            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_nothing_when_no_due_schedules(self) -> None:
        """Should do nothing when no schedules are due."""
        with (
            patch("ypl.agent_harness_service.scheduler.get_due_agent_schedules") as mock_get_due,
            patch("ypl.agent_harness_service.scheduler.create_background_task") as mock_create_task,
        ):
            mock_get_due.return_value = []

            await poll_and_execute_due_schedules()

            mock_create_task.assert_not_called()
