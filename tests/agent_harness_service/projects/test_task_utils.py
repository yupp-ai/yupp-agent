"""Unit tests for ypl/agent_harness_service/projects/task_utils.py.

Tests DB-dependent task utilities with mocked async sessions.
"""

from __future__ import annotations
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.projects.task_utils import (
    aggregate_task_spending,
    are_dependencies_completed,
    complete_task,
    restart_task,
    validate_task_status_change,
    validate_task_status_semantics,
)
from ypl.db.agent_harness import AgentTask, AgentTaskStatus


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


def _make_task(
    status: AgentTaskStatus = AgentTaskStatus.PENDING,
    depends_on: list[str] | None = None,
    assigned_session_ids: list[str] | None = None,
) -> AgentTask:
    return AgentTask(
        agent_task_id=uuid.uuid4(),
        agent_project_id=uuid.uuid4(),
        title="Test Task",
        status=status,
        depends_on=depends_on,
        assigned_session_ids=assigned_session_ids,
    )


# ===========================================================================
# Tests: are_dependencies_completed
# ===========================================================================


class TestAreDependenciesCompleted:
    """Tests for are_dependencies_completed."""

    async def test_returns_true_when_no_deps(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(depends_on=None)
        result = await are_dependencies_completed(mock_db, task)
        assert result is True

    async def test_returns_true_when_all_deps_completed(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        task = _make_task(depends_on=[dep_id])

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1  # 1 completed out of 1 expected
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await are_dependencies_completed(mock_db, task)
        assert result is True

    async def test_returns_false_when_dep_not_completed(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        task = _make_task(depends_on=[dep_id])

        count_result = MagicMock()
        count_result.scalar_one.return_value = 0  # 0 completed
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await are_dependencies_completed(mock_db, task)
        assert result is False

    async def test_returns_false_when_only_partial_deps_completed(self) -> None:
        mock_db = AsyncMock()
        dep_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        task = _make_task(depends_on=dep_ids)

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1  # only 1 of 2 completed
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await are_dependencies_completed(mock_db, task)
        assert result is False


# ===========================================================================
# Tests: validate_task_status_semantics
# ===========================================================================


class TestValidateTaskStatusSemantics:
    """Tests for validate_task_status_semantics."""

    async def test_ready_valid_when_all_deps_completed(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        task = _make_task(depends_on=[dep_id])

        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await validate_task_status_semantics(mock_db, task, AgentTaskStatus.READY)
        assert result is None

    async def test_ready_invalid_when_deps_not_completed(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        task = _make_task(depends_on=[dep_id])

        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await validate_task_status_semantics(mock_db, task, AgentTaskStatus.READY)
        assert result is not None
        assert "READY" in result

    async def test_blocked_valid_when_has_deps(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        task = _make_task(depends_on=[dep_id])

        result = await validate_task_status_semantics(mock_db, task, AgentTaskStatus.BLOCKED)
        assert result is None

    async def test_blocked_invalid_when_no_deps(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(depends_on=None)

        result = await validate_task_status_semantics(mock_db, task, AgentTaskStatus.BLOCKED)
        assert result is not None
        assert "BLOCKED" in result

    async def test_other_statuses_always_valid_semantically(self) -> None:
        mock_db = AsyncMock()
        task = _make_task()

        for status in [AgentTaskStatus.COMPLETED, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED]:
            result = await validate_task_status_semantics(mock_db, task, status)
            assert result is None


# ===========================================================================
# Tests: validate_task_status_change
# ===========================================================================


class TestValidateTaskStatusChange:
    """Tests for validate_task_status_change (transition + semantic)."""

    async def test_invalid_transition_caught_first(self) -> None:
        mock_db = AsyncMock()
        # PENDING → IN_PROGRESS is not valid
        task = _make_task(status=AgentTaskStatus.PENDING)

        result = await validate_task_status_change(mock_db, task, AgentTaskStatus.IN_PROGRESS)
        assert result is not None
        assert "Cannot transition" in result

    async def test_valid_transition_with_no_semantic_error(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.PENDING)

        result = await validate_task_status_change(mock_db, task, AgentTaskStatus.READY)
        assert result is None

    async def test_semantics_checked_after_valid_transition(self) -> None:
        mock_db = AsyncMock()
        dep_id = str(uuid.uuid4())
        # BLOCKED → READY is a valid transition, but if dep not complete, semantic error
        task = _make_task(status=AgentTaskStatus.BLOCKED, depends_on=[dep_id])

        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        mock_db.execute = AsyncMock(return_value=count_result)

        result = await validate_task_status_change(mock_db, task, AgentTaskStatus.READY)
        assert result is not None
        assert "dependencies" in result.lower() or "READY" in result


# ===========================================================================
# Tests: aggregate_task_spending
# ===========================================================================


class TestAggregateTaskSpending:
    """Tests for aggregate_task_spending."""

    async def test_returns_none_when_no_sessions(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(assigned_session_ids=None)
        result = await aggregate_task_spending(mock_db, task)
        assert result is None

    async def test_returns_total_spending(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(assigned_session_ids=[str(uuid.uuid4())])

        spend_result = MagicMock()
        spend_result.scalar_one_or_none.return_value = 1.25
        mock_db.execute = AsyncMock(return_value=spend_result)

        result = await aggregate_task_spending(mock_db, task)
        assert result == Decimal("1.25")

    async def test_returns_none_when_no_spending(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(assigned_session_ids=[str(uuid.uuid4())])

        spend_result = MagicMock()
        spend_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=spend_result)

        result = await aggregate_task_spending(mock_db, task)
        assert result is None


# ===========================================================================
# Tests: complete_task
# ===========================================================================


class TestCompleteTask:
    """Tests for complete_task."""

    async def test_raises_for_non_terminal_status(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS)

        with pytest.raises(ValueError, match="terminal status"):
            await complete_task(mock_db, task, AgentTaskStatus.PENDING)

    async def test_raises_for_invalid_transition(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.READY)

        # READY → COMPLETED is not a valid transition (must go via IN_PROGRESS)
        with pytest.raises(ValueError, match="Cannot transition"):
            await complete_task(mock_db, task, AgentTaskStatus.COMPLETED)

    async def test_completes_task_successfully(self) -> None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS)

        spend_result = MagicMock()
        spend_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=spend_result)

        await complete_task(mock_db, task, AgentTaskStatus.COMPLETED)

        assert task.status == AgentTaskStatus.COMPLETED
        assert task.completed_at is not None
        mock_db.add.assert_called_once_with(task)

    async def test_sets_result_when_provided(self) -> None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS)

        spend_result = MagicMock()
        spend_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=spend_result)

        result_data = {"summary": "done", "pr_url": "https://github.com/..."}
        await complete_task(mock_db, task, AgentTaskStatus.COMPLETED, result=result_data)

        assert task.result == result_data

    async def test_aggregates_spending_when_sessions_present(self) -> None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS, assigned_session_ids=[str(uuid.uuid4())])

        spend_result = MagicMock()
        spend_result.scalar_one_or_none.return_value = 3.50
        mock_db.execute = AsyncMock(return_value=spend_result)

        await complete_task(mock_db, task, AgentTaskStatus.COMPLETED)

        assert task.actual_spending_usd == Decimal("3.50")

    async def test_skips_spending_when_aggregate_false(self) -> None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS, assigned_session_ids=[str(uuid.uuid4())])

        await complete_task(mock_db, task, AgentTaskStatus.COMPLETED, aggregate_spending=False)

        # Should not query for spending
        mock_db.execute.assert_not_called()
        assert task.actual_spending_usd is None


# ===========================================================================
# Tests: restart_task
# ===========================================================================


class TestRestartTask:
    """Tests for restart_task."""

    async def test_returns_false_when_task_not_found(self) -> None:
        mock_db = AsyncMock()
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=task_result)

        with patch(
            "ypl.backend.db.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            success, message = await restart_task(uuid.uuid4())
            assert success is False
            assert "not found" in message.lower()

    async def test_returns_false_for_in_progress_task(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.IN_PROGRESS)
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)

        with patch(
            "ypl.backend.db.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            success, message = await restart_task(task.agent_task_id)
            assert success is False
            assert "Cannot restart" in message

    async def test_restarts_completed_task(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.COMPLETED)
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with patch(
            "ypl.backend.db.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            success, message = await restart_task(task.agent_task_id)
            assert success is True
            assert task.status == AgentTaskStatus.PENDING
            assert task.completed_at is None
            assert task.result is None
            assert task.assigned_session_ids is None
            mock_db.commit.assert_called_once()

    async def test_restarts_failed_task(self) -> None:
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.FAILED)
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with patch(
            "ypl.backend.db.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            success, message = await restart_task(task.agent_task_id)
            assert success is True
            assert "FAILED" in message

    async def test_returns_false_for_cancelled_blocked_task(self) -> None:
        # CANCELLED → PENDING is valid, but BLOCKED → PENDING is also valid
        # CANCELLED → PENDING should succeed
        mock_db = AsyncMock()
        task = _make_task(status=AgentTaskStatus.CANCELLED)
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = task
        mock_db.execute = AsyncMock(return_value=task_result)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with patch(
            "ypl.backend.db.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            success, message = await restart_task(task.agent_task_id)
            assert success is True
