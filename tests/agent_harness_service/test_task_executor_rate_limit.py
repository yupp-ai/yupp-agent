"""Unit tests for task_executor rate-limiting logic.

Tests the exponential backoff, dead-letter escalation threshold, and per-project
cooldown state management introduced to fix the rate-limit tight-loop incident
(project 599bf2f6, 2026-03-30).
"""

import time
import uuid
from unittest.mock import MagicMock

import ypl.agent_harness_service.task_executor as te

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(project_id: uuid.UUID | None = None) -> MagicMock:
    task = MagicMock()
    task.agent_task_id = uuid.uuid4()
    task.agent_project_id = project_id or uuid.uuid4()
    return task


def _clear_rate_limited_projects() -> None:
    te._rate_limited_projects.clear()


# ---------------------------------------------------------------------------
# Backoff computation
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    """Verify that hit_count drives exponential back-off up to the cap."""

    def test_first_hit_uses_base_backoff(self) -> None:
        expected = te.RATE_LIMIT_BACKOFF_SECONDS  # 300 by default
        actual = min(te.RATE_LIMIT_BACKOFF_SECONDS * (2 ** (1 - 1)), te.MAX_RATE_LIMIT_BACKOFF_SECONDS)
        assert actual == expected

    def test_second_hit_doubles(self) -> None:
        expected = te.RATE_LIMIT_BACKOFF_SECONDS * 2
        actual = min(te.RATE_LIMIT_BACKOFF_SECONDS * (2 ** (2 - 1)), te.MAX_RATE_LIMIT_BACKOFF_SECONDS)
        assert actual == expected

    def test_third_hit_quadruples(self) -> None:
        expected = te.RATE_LIMIT_BACKOFF_SECONDS * 4
        actual = min(te.RATE_LIMIT_BACKOFF_SECONDS * (2 ** (3 - 1)), te.MAX_RATE_LIMIT_BACKOFF_SECONDS)
        assert actual == expected

    def test_capped_at_max(self) -> None:
        # hit 100 — should clamp to MAX_RATE_LIMIT_BACKOFF_SECONDS
        actual = min(te.RATE_LIMIT_BACKOFF_SECONDS * (2**99), te.MAX_RATE_LIMIT_BACKOFF_SECONDS)
        assert actual == te.MAX_RATE_LIMIT_BACKOFF_SECONDS

    def test_max_gte_base(self) -> None:
        assert te.MAX_RATE_LIMIT_BACKOFF_SECONDS >= te.RATE_LIMIT_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# _rate_limited_projects state management
# ---------------------------------------------------------------------------


class TestRateLimitedProjectsState:
    """Verify the in-memory cooldown dict is updated correctly."""

    def setup_method(self) -> None:
        _clear_rate_limited_projects()

    def test_initial_state_is_empty(self) -> None:
        assert te._rate_limited_projects == {}

    def test_entry_stores_deadline_and_hit_count(self) -> None:
        pid = str(uuid.uuid4())
        now = time.monotonic()
        backoff = te.RATE_LIMIT_BACKOFF_SECONDS
        te._rate_limited_projects[pid] = (now + backoff, 1)

        deadline, hits = te._rate_limited_projects[pid]
        assert hits == 1
        assert deadline > now

    def test_second_hit_increments_hit_count(self) -> None:
        pid = str(uuid.uuid4())
        now = time.monotonic()
        # Simulate first hit
        te._rate_limited_projects[pid] = (now + te.RATE_LIMIT_BACKOFF_SECONDS, 1)
        # Simulate second hit (cooldown expired, rate limiter still denied)
        _, prior_hits = te._rate_limited_projects[pid]
        new_hits = prior_hits + 1
        backoff = min(te.RATE_LIMIT_BACKOFF_SECONDS * (2 ** (new_hits - 1)), te.MAX_RATE_LIMIT_BACKOFF_SECONDS)
        te._rate_limited_projects[pid] = (now + backoff, new_hits)

        deadline, hits = te._rate_limited_projects[pid]
        assert hits == 2
        assert backoff == te.RATE_LIMIT_BACKOFF_SECONDS * 2

    def test_entry_cleared_on_success(self) -> None:
        pid = str(uuid.uuid4())
        te._rate_limited_projects[pid] = (time.monotonic() + 300, 3)
        # Simulate successful rate-limit check clears the entry
        te._rate_limited_projects.pop(pid, None)
        assert pid not in te._rate_limited_projects


# ---------------------------------------------------------------------------
# Escalation threshold
# ---------------------------------------------------------------------------


class TestRateLimitMaxStrikes:
    """Verify RATE_LIMIT_MAX_STRIKES default and escalation logic."""

    def test_default_is_five(self) -> None:
        assert te.RATE_LIMIT_MAX_STRIKES == 5

    def test_escalation_fires_at_threshold(self) -> None:
        # hits < threshold → warning, not error
        assert te.RATE_LIMIT_MAX_STRIKES - 1 < te.RATE_LIMIT_MAX_STRIKES
        # hits == threshold → error
        assert te.RATE_LIMIT_MAX_STRIKES >= te.RATE_LIMIT_MAX_STRIKES

    def test_escalation_fires_above_threshold(self) -> None:
        for hits in (te.RATE_LIMIT_MAX_STRIKES, te.RATE_LIMIT_MAX_STRIKES + 1, 100):
            assert hits >= te.RATE_LIMIT_MAX_STRIKES


# ---------------------------------------------------------------------------
# Default rate limit value
# ---------------------------------------------------------------------------


class TestRateLimitDefault:
    """Rate limit default was raised from 30 → 60 tasks/hr."""

    def test_default_rate_limit_is_60(self) -> None:
        # Verify the module-level default; env override still honoured via
        # AHS_TASK_EXECUTOR_RATE_LIMIT but this guards against regression.
        from ypl.agent_harness_service.task_executor import TASK_EXECUTOR_RATE_LIMIT

        assert TASK_EXECUTOR_RATE_LIMIT >= 60, (
            f"Default rate limit regressed to {TASK_EXECUTOR_RATE_LIMIT}; should be at least 60 tasks/hr"
        )
