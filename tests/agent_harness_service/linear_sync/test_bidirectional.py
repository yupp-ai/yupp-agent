"""Bidirectional sync conflict-resolution tests.

Covers:
- ConflictResolution enum values
- _resolve_direction(): pure function for deciding which side wins
- sync_bidirectional(): full flow with matched pairs, AHS orphans, Linear orphans
- sync_tasks_from_linear(): incremental-sync guards (executor-owned / terminal /
  BLOCKED statuses, status capping for new issues, watermark behaviour)

All tests use mocked DB sessions and LinearClient — no real DB or network I/O.
"""

from __future__ import annotations
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.tools.linear_sync.bidirectional import (
    ConflictResolution,
    _resolve_direction,
    sync_bidirectional,
)
from ypl.agent_harness_service.tools.linear_sync.import_from_linear import sync_tasks_from_linear
from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskStatus

from tests.agent_harness_service.linear_sync.conftest import (
    LINEAR_PROJECT_ID,
    LINEAR_TEAM_ID,
    _call_sync,
    make_linear_issue,
    make_task,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
LATER = NOW + timedelta(hours=1)


def _make_project_with_linear_ref(project_id: uuid.UUID, since: datetime) -> AgentProject:
    p = AgentProject(
        name="Sync Project",
        status=AgentProjectStatus.ACTIVE,
        project_data={
            "linear_project_id": LINEAR_PROJECT_ID,
            "linear_team_id": LINEAR_TEAM_ID,
            "last_synced_at": since.isoformat(),
            "linear_ref": {
                "linear_project_id": LINEAR_PROJECT_ID,
                "linear_team_id": LINEAR_TEAM_ID,
                "last_synced_at": since.isoformat(),
            },
        },
    )
    p.agent_project_id = project_id
    return p


# ---------------------------------------------------------------------------
# ConflictResolution enum
# ---------------------------------------------------------------------------


class TestConflictResolutionEnum:
    def test_all_expected_values_exist(self) -> None:
        values = {cr.value for cr in ConflictResolution}
        assert "linear_wins" in values
        assert "ahs_wins" in values
        assert "latest_wins" in values
        assert "skip" in values

    def test_is_str_enum(self) -> None:
        """StrEnum — values should compare equal to plain strings."""
        assert ConflictResolution.LINEAR_WINS.value == "linear_wins"
        assert ConflictResolution.AHS_WINS.value == "ahs_wins"
        assert ConflictResolution.LATEST_WINS.value == "latest_wins"
        assert ConflictResolution.SKIP.value == "skip"


# ---------------------------------------------------------------------------
# _resolve_direction — pure function
# ---------------------------------------------------------------------------


class TestResolveDirection:
    # LINEAR_WINS always returns "linear_to_ahs"
    def test_linear_wins_always_linear(self) -> None:
        assert _resolve_direction(ConflictResolution.LINEAR_WINS, NOW, NOW) == "linear_to_ahs"
        assert _resolve_direction(ConflictResolution.LINEAR_WINS, LATER, EARLIER) == "linear_to_ahs"
        assert _resolve_direction(ConflictResolution.LINEAR_WINS, None, None) == "linear_to_ahs"

    # AHS_WINS always returns "ahs_to_linear"
    def test_ahs_wins_always_ahs(self) -> None:
        assert _resolve_direction(ConflictResolution.AHS_WINS, NOW, NOW) == "ahs_to_linear"
        assert _resolve_direction(ConflictResolution.AHS_WINS, EARLIER, LATER) == "ahs_to_linear"
        assert _resolve_direction(ConflictResolution.AHS_WINS, None, None) == "ahs_to_linear"

    # SKIP always returns "skip"
    def test_skip_always_skip(self) -> None:
        assert _resolve_direction(ConflictResolution.SKIP, NOW, NOW) == "skip"
        assert _resolve_direction(ConflictResolution.SKIP, LATER, EARLIER) == "skip"
        assert _resolve_direction(ConflictResolution.SKIP, None, None) == "skip"

    # LATEST_WINS: newer side wins
    def test_latest_wins_ahs_newer(self) -> None:
        assert _resolve_direction(ConflictResolution.LATEST_WINS, LATER, EARLIER) == "ahs_to_linear"

    def test_latest_wins_linear_newer(self) -> None:
        assert _resolve_direction(ConflictResolution.LATEST_WINS, EARLIER, LATER) == "linear_to_ahs"

    def test_latest_wins_identical_timestamps_skips(self) -> None:
        assert _resolve_direction(ConflictResolution.LATEST_WINS, NOW, NOW) == "skip"

    def test_latest_wins_both_none_skips(self) -> None:
        assert _resolve_direction(ConflictResolution.LATEST_WINS, None, None) == "skip"

    def test_latest_wins_only_ahs_none_linear_wins(self) -> None:
        """No AHS timestamp → treat Linear as newer."""
        assert _resolve_direction(ConflictResolution.LATEST_WINS, None, NOW) == "linear_to_ahs"

    def test_latest_wins_only_linear_none_ahs_wins(self) -> None:
        """No Linear timestamp → treat AHS as newer."""
        assert _resolve_direction(ConflictResolution.LATEST_WINS, NOW, None) == "ahs_to_linear"

    def test_latest_wins_naive_ahs_timestamp_normalised(self) -> None:
        """Naive AHS datetime must be treated as UTC (not fail)."""
        naive_now = datetime(2024, 6, 1, 12, 0, 0)  # noqa: DTZ001 — intentionally naive to test normalisation
        result = _resolve_direction(ConflictResolution.LATEST_WINS, naive_now, EARLIER)
        assert result == "ahs_to_linear"

    # ------------------------------------------------------------------
    # LATEST_WINS with last_synced (anti-feedback-loop logic)
    # ------------------------------------------------------------------

    def test_latest_wins_with_last_synced_only_ahs_changed(self) -> None:
        """AHS changed since last sync, Linear unchanged → ahs_to_linear."""
        last_synced = NOW - timedelta(hours=2)
        ahs_modified = NOW  # after last_synced
        linear_updated = last_synced - timedelta(minutes=30)  # before last_synced
        result = _resolve_direction(ConflictResolution.LATEST_WINS, ahs_modified, linear_updated, last_synced)
        assert result == "ahs_to_linear"

    def test_latest_wins_with_last_synced_only_linear_changed(self) -> None:
        """Linear changed since last sync, AHS unchanged → linear_to_ahs."""
        last_synced = NOW - timedelta(hours=2)
        ahs_modified = last_synced - timedelta(minutes=30)  # before last_synced
        linear_updated = NOW  # after last_synced
        result = _resolve_direction(ConflictResolution.LATEST_WINS, ahs_modified, linear_updated, last_synced)
        assert result == "linear_to_ahs"

    def test_latest_wins_with_last_synced_neither_changed(self) -> None:
        """Neither side changed since last sync → skip (no unnecessary writes)."""
        last_synced = NOW
        ahs_modified = last_synced - timedelta(hours=1)  # before last_synced
        linear_updated = last_synced - timedelta(minutes=30)  # before last_synced
        result = _resolve_direction(ConflictResolution.LATEST_WINS, ahs_modified, linear_updated, last_synced)
        assert result == "skip"

    def test_latest_wins_with_last_synced_both_changed_ahs_newer(self) -> None:
        """Both sides changed since last sync, AHS more recent → ahs_to_linear."""
        last_synced = NOW - timedelta(hours=2)
        ahs_modified = NOW  # after last_synced, more recent
        linear_updated = NOW - timedelta(hours=1)  # after last_synced, less recent
        result = _resolve_direction(ConflictResolution.LATEST_WINS, ahs_modified, linear_updated, last_synced)
        assert result == "ahs_to_linear"

    def test_latest_wins_with_last_synced_both_changed_linear_newer(self) -> None:
        """Both sides changed since last sync, Linear more recent → linear_to_ahs."""
        last_synced = NOW - timedelta(hours=2)
        ahs_modified = NOW - timedelta(hours=1)  # after last_synced, less recent
        linear_updated = NOW  # after last_synced, more recent
        result = _resolve_direction(ConflictResolution.LATEST_WINS, ahs_modified, linear_updated, last_synced)
        assert result == "linear_to_ahs"


# ---------------------------------------------------------------------------
# sync_bidirectional — mocked
# ---------------------------------------------------------------------------


class TestSyncBidirectional:
    """Integration-level tests for sync_bidirectional() with all I/O mocked."""

    @pytest.mark.asyncio
    async def test_raises_when_project_not_found(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ValueError, match="AHS project not found"),
        ):
            await sync_bidirectional(project_id=str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_raises_when_no_linear_ref(self) -> None:
        project = AgentProject(
            name="P",
            status=AgentProjectStatus.ACTIVE,
            project_data={},  # No linear_project_id / linear_ref
        )
        project.agent_project_id = uuid.uuid4()

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            pytest.raises(ValueError, match="no linked Linear project"),
        ):
            await sync_bidirectional(project_id=str(project.agent_project_id))

    @pytest.mark.asyncio
    async def test_skip_strategy_skips_all_matched_pairs(self) -> None:
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        issue_id = "li-123"
        task = make_task(task_data={"linear_ref": {"linear_issue_id": issue_id, "linear_identifier": "ENG-1"}})
        task.agent_project_id = project_id
        task.modified_at = NOW

        issue = make_linear_issue(issue_id=issue_id, title="Some Issue")
        issue["updatedAt"] = EARLIER.isoformat()

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(
                project_id=str(project_id),
                conflict_resolution=ConflictResolution.SKIP,
            )

        assert result.skipped == 1
        assert result.updated == 0
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_linear_wins_applies_linear_to_ahs(self) -> None:
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        issue_id = "li-456"
        task = make_task(task_data={"linear_ref": {"linear_issue_id": issue_id, "linear_identifier": "ENG-2"}})
        task.agent_project_id = project_id
        task.modified_at = LATER  # AHS is newer, but LINEAR_WINS overrides

        issue = make_linear_issue(issue_id=issue_id, title="Linear Version", state_type="unstarted")
        issue["updatedAt"] = EARLIER.isoformat()

        apply_l2a_called: list[bool] = []

        async def _fake_apply_l2a(t: AgentTask, i: dict, now: datetime) -> None:
            apply_l2a_called.append(True)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._apply_linear_to_ahs",
                side_effect=_fake_apply_l2a,
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(
                project_id=str(project_id),
                conflict_resolution=ConflictResolution.LINEAR_WINS,
            )

        assert result.updated == 1
        assert len(apply_l2a_called) == 1

    @pytest.mark.asyncio
    async def test_ahs_wins_applies_ahs_to_linear(self) -> None:
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        issue_id = "li-789"
        task = make_task(task_data={"linear_ref": {"linear_issue_id": issue_id, "linear_identifier": "ENG-3"}})
        task.agent_project_id = project_id
        task.modified_at = EARLIER  # Linear is newer, but AHS_WINS overrides

        issue = make_linear_issue(issue_id=issue_id, title="Linear Version")
        issue["updatedAt"] = LATER.isoformat()

        apply_a2l_called: list[bool] = []

        async def _fake_apply_a2l(t: AgentTask, statuses: list, now: datetime) -> None:
            apply_a2l_called.append(True)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._apply_ahs_to_linear",
                side_effect=_fake_apply_a2l,
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(
                project_id=str(project_id),
                conflict_resolution=ConflictResolution.AHS_WINS,
            )

        assert result.updated == 1
        assert len(apply_a2l_called) == 1

    @pytest.mark.asyncio
    async def test_latest_wins_ahs_newer_pushes_to_linear(self) -> None:
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        issue_id = "li-aaa"
        task = make_task(task_data={"linear_ref": {"linear_issue_id": issue_id, "linear_identifier": "ENG-10"}})
        task.agent_project_id = project_id
        task.modified_at = LATER  # AHS is newer

        issue = make_linear_issue(issue_id=issue_id, title="Old Linear")
        issue["updatedAt"] = EARLIER.isoformat()  # Linear is older

        apply_a2l_called: list[bool] = []

        async def _fake_apply_a2l(t: AgentTask, statuses: list, now: datetime) -> None:
            apply_a2l_called.append(True)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._apply_ahs_to_linear",
                side_effect=_fake_apply_a2l,
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(
                project_id=str(project_id),
                conflict_resolution=ConflictResolution.LATEST_WINS,
            )

        assert result.updated == 1
        assert len(apply_a2l_called) == 1

    @pytest.mark.asyncio
    async def test_ahs_orphan_pushed_to_linear(self) -> None:
        """AHS tasks with no Linear issue should be created in Linear."""
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        orphan_task = make_task(title="Orphan Task", task_data=None)
        orphan_task.agent_project_id = project_id
        new_issue_id = str(uuid.uuid4())

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[orphan_task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._upsert_linear_issue",
                new=AsyncMock(return_value=(new_issue_id, "ENG-99")),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._save_issue_ref",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(project_id=str(project_id))

        assert result.created == 1
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_linear_orphan_creates_ahs_task(self) -> None:
        """Linear issues with no AHS task should result in a new AHS task."""
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        orphan_issue = make_linear_issue(issue_id="li-orphan", title="Linear Orphan")

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[orphan_issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._create_ahs_task_from_linear",
                new=AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(project_id=str(project_id))

        assert result.created == 1
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_deleted_linear_issue_skipped(self) -> None:
        """A task whose Linear issue no longer exists should be skipped, not deleted."""
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        task = make_task(task_data={"linear_ref": {"linear_issue_id": "li-gone", "linear_identifier": "ENG-5"}})
        task.agent_project_id = project_id

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[]),  # issue is gone
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                new=AsyncMock(),
            ),
        ):
            result = await sync_bidirectional(project_id=str(project_id))

        assert result.skipped == 1
        assert result.updated == 0

    @pytest.mark.asyncio
    async def test_watermark_not_updated_when_errors(self) -> None:
        project_id = uuid.uuid4()
        project = _make_project_with_linear_ref(project_id, NOW)

        issue_id = "li-err"
        task = make_task(task_data={"linear_ref": {"linear_issue_id": issue_id, "linear_identifier": "ENG-7"}})
        task.agent_project_id = project_id
        task.modified_at = LATER

        issue = make_linear_issue(issue_id=issue_id, title="Error Issue")
        issue["updatedAt"] = EARLIER.isoformat()

        watermark_calls: list[bool] = []

        async def _fake_watermark(pid: str, now: datetime) -> None:
            watermark_calls.append(True)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.export_to_linear._fetch_project",
                new=AsyncMock(return_value=project),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_tasks",
                new=AsyncMock(return_value=[task]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_issues",
                new=AsyncMock(return_value=[issue]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._fetch_team_statuses",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._apply_ahs_to_linear",
                side_effect=RuntimeError("forced failure"),
            ),
            patch(
                "ypl.agent_harness_service.tools.linear_sync.bidirectional._update_project_last_synced",
                side_effect=_fake_watermark,
            ),
        ):
            result = await sync_bidirectional(
                project_id=str(project_id),
                conflict_resolution=ConflictResolution.AHS_WINS,
            )

        assert result.errors >= 1
        assert len(watermark_calls) == 0, "Watermark must NOT be updated when sync errors occurred"


# ---------------------------------------------------------------------------
# sync_tasks_from_linear: executor-owned / terminal / BLOCKED status guards
# ---------------------------------------------------------------------------


class TestExecutorOwnedStatusesPreserved:
    """IN_PROGRESS, IN_REVIEW tasks are owned by the executor; Linear must not override."""

    @pytest.mark.parametrize(
        "protected_status",
        [
            AgentTaskStatus.IN_PROGRESS,
            AgentTaskStatus.IN_REVIEW,
        ],
    )
    @pytest.mark.asyncio
    async def test_executor_status_not_overwritten(self, protected_status: AgentTaskStatus) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        linear_issue_id = "li-active"

        existing_task = make_task(
            title="Active Task",
            status=protected_status,
            task_data={"linear_ref": {"linear_issue_id": linear_issue_id, "linear_identifier": "TEST-1"}},
        )
        existing_task.agent_project_id = project_id

        linear_issue = make_linear_issue(issue_id=linear_issue_id, title="Active Task", state_type="unstarted")
        project = _make_project_with_linear_ref(project_id, since)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [linear_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([existing_task])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert existing_task.status == protected_status


class TestTerminalStatusesPreserved:
    """COMPLETED, FAILED, CANCELLED are terminal; Linear edits must not undo them."""

    @pytest.mark.parametrize(
        "terminal_status",
        [
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
        ],
    )
    @pytest.mark.asyncio
    async def test_terminal_status_not_overwritten(self, terminal_status: AgentTaskStatus) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        linear_issue_id = "li-terminal"

        existing_task = make_task(
            title="Done Task",
            status=terminal_status,
            task_data={"linear_ref": {"linear_issue_id": linear_issue_id, "linear_identifier": "TEST-1"}},
        )
        existing_task.agent_project_id = project_id

        linear_issue = make_linear_issue(issue_id=linear_issue_id, title="Done Task", state_type="started")
        project = _make_project_with_linear_ref(project_id, since)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [linear_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([existing_task])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert existing_task.status == terminal_status


class TestBlockedStatusPreserved:
    @pytest.mark.asyncio
    async def test_blocked_task_not_updated_from_linear(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        linear_issue_id = "li-blocked"

        existing_task = make_task(
            title="Waiting Task",
            status=AgentTaskStatus.BLOCKED,
            task_data={"linear_ref": {"linear_issue_id": linear_issue_id, "linear_identifier": "TEST-1"}},
        )
        existing_task.agent_project_id = project_id

        linear_issue = make_linear_issue(issue_id=linear_issue_id, title="Waiting Task", state_type="started")
        project = _make_project_with_linear_ref(project_id, since)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [linear_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([existing_task])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert existing_task.status == AgentTaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# New issues from incremental sync: status capping
# ---------------------------------------------------------------------------


class TestNewTaskStatusCapping:
    @pytest.mark.asyncio
    async def test_new_in_progress_issue_capped_to_ready(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        project = _make_project_with_linear_ref(project_id, since)
        captured_tasks: list[AgentTask] = []

        new_issue = make_linear_issue(
            issue_id="li-new-started",
            identifier="ENG-77",
            title="Started Elsewhere",
            state_type="started",
        )

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [new_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()

                def _add(obj: Any) -> None:
                    if isinstance(obj, AgentTask):
                        captured_tasks.append(obj)

                session.add = _add
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            result = await sync_tasks_from_linear(project_id=project_id, since=since)

        assert result.created == 1
        assert captured_tasks[0].status == AgentTaskStatus.READY, (
            f"Expected READY but got {captured_tasks[0].status!r} — IN_PROGRESS must be capped for unclaimed tasks"
        )

    @pytest.mark.asyncio
    async def test_new_completed_issue_keeps_completed(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        project = _make_project_with_linear_ref(project_id, since)
        captured_tasks: list[AgentTask] = []

        new_issue = make_linear_issue(
            issue_id="li-new-done",
            identifier="ENG-88",
            title="Already Done",
            state_type="completed",
        )

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [new_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()

                def _add(obj: Any) -> None:
                    if isinstance(obj, AgentTask):
                        captured_tasks.append(obj)

                session.add = _add
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert len(captured_tasks) == 1
        assert captured_tasks[0].status == AgentTaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Mutable fields (title, description) always propagated
# ---------------------------------------------------------------------------


class TestMutableFieldPropagation:
    @pytest.mark.asyncio
    async def test_title_and_description_updated(self) -> None:
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        linear_issue_id = "li-mutable"

        existing_task = make_task(
            title="Old Title",
            description="Old description",
            status=AgentTaskStatus.READY,
            task_data={"linear_ref": {"linear_issue_id": linear_issue_id, "linear_identifier": "TEST-1"}},
        )
        existing_task.agent_project_id = project_id

        updated_issue = make_linear_issue(
            issue_id=linear_issue_id,
            title="New Title",
            description="New description",
            state_type="unstarted",
        )
        project = _make_project_with_linear_ref(project_id, since)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [updated_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([existing_task])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert existing_task.title == "New Title"
        assert existing_task.description == "New description"

    @pytest.mark.asyncio
    async def test_description_cleared_when_linear_sets_none(self) -> None:
        """Deleting description in Linear should propagate None to AHS task."""
        project_id = uuid.uuid4()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        linear_issue_id = "li-no-desc"

        existing_task = make_task(
            title="Task",
            description="Was here before",
            status=AgentTaskStatus.READY,
            task_data={"linear_ref": {"linear_issue_id": linear_issue_id, "linear_identifier": "TEST-1"}},
        )
        existing_task.agent_project_id = project_id

        updated_issue = make_linear_issue(
            issue_id=linear_issue_id,
            title="Task",
            description=None,  # description removed in Linear
            state_type="unstarted",
        )
        project = _make_project_with_linear_ref(project_id, since)

        with (
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.asyncio.to_thread",
                side_effect=_call_sync,
            ),
            patch("ypl.agent_harness_service.tools.linear_sync.import_from_linear.LinearClient") as MockClient,
            patch(
                "ypl.agent_harness_service.tools.linear_sync.import_from_linear.get_async_session"
            ) as mock_get_session,
        ):
            MockClient.return_value.list_project_issues.return_value = [updated_issue]
            call_count = 0

            @asynccontextmanager
            async def _session_cm() -> AsyncGenerator[AsyncMock, None]:
                nonlocal call_count
                call_count += 1
                session = AsyncMock()
                session.commit = AsyncMock()
                session.add = MagicMock()
                if call_count == 1:
                    res = MagicMock()
                    res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(return_value=res)
                else:
                    task_res = MagicMock()
                    task_res.scalars.return_value = iter([existing_task])
                    proj_res = MagicMock()
                    proj_res.scalars.return_value.first.return_value = project
                    session.execute = AsyncMock(side_effect=[task_res, proj_res])
                yield session

            mock_get_session.side_effect = _session_cm
            await sync_tasks_from_linear(project_id=project_id, since=since)

        assert existing_task.description is None
