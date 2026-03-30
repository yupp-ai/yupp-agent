"""Unit tests for task status transitions and validation logic.

Tests the state machine defined in task_utils.py — valid/invalid transitions,
terminal status detection, and non-restartable statuses.
"""

import pytest
from ypl.agent_harness_service.projects.task_utils import (
    NON_RESTARTABLE_STATUSES,
    TERMINAL_TASK_STATUSES,
    VALID_TASK_TRANSITIONS,
    validate_task_status_transition,
)
from ypl.db.agent_harness import AgentTaskStatus


class TestValidateTaskStatusTransition:
    """Test validate_task_status_transition for all status pairs."""

    # ---- Valid transitions ----

    @pytest.mark.parametrize(
        "current,target",
        [
            (AgentTaskStatus.PENDING, AgentTaskStatus.BLOCKED),
            (AgentTaskStatus.PENDING, AgentTaskStatus.READY),
            (AgentTaskStatus.PENDING, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.BLOCKED, AgentTaskStatus.READY),
            (AgentTaskStatus.BLOCKED, AgentTaskStatus.PENDING),
            (AgentTaskStatus.BLOCKED, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.READY, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.READY, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.READY, AgentTaskStatus.BLOCKED),
            (AgentTaskStatus.READY, AgentTaskStatus.PENDING),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.FAILED),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.READY),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.PENDING),
            (AgentTaskStatus.IN_PROGRESS, AgentTaskStatus.IN_REVIEW),
            (AgentTaskStatus.IN_REVIEW, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.IN_REVIEW, AgentTaskStatus.FAILED),
            (AgentTaskStatus.IN_REVIEW, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.IN_REVIEW, AgentTaskStatus.PENDING),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.PENDING),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.FAILED),
            (AgentTaskStatus.FAILED, AgentTaskStatus.PENDING),
            (AgentTaskStatus.FAILED, AgentTaskStatus.READY),
            (AgentTaskStatus.CANCELLED, AgentTaskStatus.PENDING),
        ],
    )
    def test_valid_transitions(self, current: AgentTaskStatus, target: AgentTaskStatus) -> None:
        assert validate_task_status_transition(current, target) is None

    # ---- Invalid transitions ----

    @pytest.mark.parametrize(
        "current,target",
        [
            (AgentTaskStatus.PENDING, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.PENDING, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.PENDING, AgentTaskStatus.FAILED),
            (AgentTaskStatus.PENDING, AgentTaskStatus.IN_REVIEW),
            (AgentTaskStatus.BLOCKED, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.BLOCKED, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.READY, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.READY, AgentTaskStatus.FAILED),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.READY),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.BLOCKED),
            (AgentTaskStatus.COMPLETED, AgentTaskStatus.CANCELLED),
            (AgentTaskStatus.FAILED, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.FAILED, AgentTaskStatus.COMPLETED),
            (AgentTaskStatus.CANCELLED, AgentTaskStatus.READY),
            (AgentTaskStatus.CANCELLED, AgentTaskStatus.IN_PROGRESS),
            (AgentTaskStatus.CANCELLED, AgentTaskStatus.COMPLETED),
        ],
    )
    def test_invalid_transitions(self, current: AgentTaskStatus, target: AgentTaskStatus) -> None:
        result = validate_task_status_transition(current, target)
        assert result is not None
        assert "Cannot transition" in result
        assert current.value in result
        assert target.value in result

    def test_self_transition_is_invalid(self) -> None:
        """No status should be able to transition to itself."""
        for status in AgentTaskStatus:
            result = validate_task_status_transition(status, status)
            assert result is not None, f"{status.value} -> {status.value} should be invalid"


class TestTerminalStatuses:
    """Test TERMINAL_TASK_STATUSES constant."""

    def test_terminal_statuses_are_complete_set(self) -> None:
        assert TERMINAL_TASK_STATUSES == {
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
        }

    def test_non_terminal_statuses_are_not_in_set(self) -> None:
        non_terminal = {
            AgentTaskStatus.PENDING,
            AgentTaskStatus.BLOCKED,
            AgentTaskStatus.READY,
            AgentTaskStatus.IN_PROGRESS,
            AgentTaskStatus.IN_REVIEW,
        }
        for status in non_terminal:
            assert status not in TERMINAL_TASK_STATUSES


class TestNonRestartableStatuses:
    """Test NON_RESTARTABLE_STATUSES constant."""

    def test_in_progress_is_non_restartable(self) -> None:
        assert AgentTaskStatus.IN_PROGRESS in NON_RESTARTABLE_STATUSES

    def test_completed_is_restartable(self) -> None:
        assert AgentTaskStatus.COMPLETED not in NON_RESTARTABLE_STATUSES

    def test_failed_is_restartable(self) -> None:
        assert AgentTaskStatus.FAILED not in NON_RESTARTABLE_STATUSES


class TestTransitionMapCompleteness:
    """Verify the transition map covers all statuses."""

    def test_all_statuses_have_transitions(self) -> None:
        for status in AgentTaskStatus:
            assert status in VALID_TASK_TRANSITIONS, f"{status.value} missing from VALID_TASK_TRANSITIONS"

    def test_all_terminal_statuses_allow_pending(self) -> None:
        """Terminal statuses should all allow transition back to PENDING for restart."""
        for status in TERMINAL_TASK_STATUSES:
            allowed = VALID_TASK_TRANSITIONS[status]
            assert AgentTaskStatus.PENDING in allowed, f"{status.value} should allow transition to PENDING"
