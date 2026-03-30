"""Pydantic model validation tests for linear_sync types."""

from __future__ import annotations
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from ypl.agent_harness_service.tools.linear_sync.types import (
    LinearIssueRef,
    LinearProjectRef,
    SyncResult,
)

# ---------------------------------------------------------------------------
# LinearProjectRef
# ---------------------------------------------------------------------------


class TestLinearProjectRef:
    def test_required_fields(self) -> None:
        ref = LinearProjectRef(
            linear_project_id="proj-123",
            linear_team_id="team-456",
        )
        assert ref.linear_project_id == "proj-123"
        assert ref.linear_team_id == "team-456"
        assert ref.last_synced_at is None

    def test_with_last_synced_at(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        ref = LinearProjectRef(
            linear_project_id="proj-123",
            linear_team_id="team-456",
            last_synced_at=now,
        )
        assert ref.last_synced_at == now

    def test_missing_project_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            LinearProjectRef(linear_team_id="team-456")  # type: ignore[call-arg]

    def test_missing_team_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            LinearProjectRef(linear_project_id="proj-123")  # type: ignore[call-arg]

    def test_roundtrip_json(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        ref = LinearProjectRef(
            linear_project_id="proj-123",
            linear_team_id="team-456",
            last_synced_at=now,
        )
        data = ref.model_dump(mode="json")
        assert data["linear_project_id"] == "proj-123"
        assert data["linear_team_id"] == "team-456"
        # Validate the round-trip
        ref2 = LinearProjectRef.model_validate(data)
        assert ref2.linear_project_id == ref.linear_project_id
        assert ref2.linear_team_id == ref.linear_team_id
        assert ref2.last_synced_at == ref.last_synced_at

    def test_roundtrip_without_timestamp(self) -> None:
        ref = LinearProjectRef(
            linear_project_id="proj-abc",
            linear_team_id="team-xyz",
        )
        data = ref.model_dump(mode="json")
        ref2 = LinearProjectRef.model_validate(data)
        assert ref2.last_synced_at is None

    def test_extra_fields_ignored(self) -> None:
        """Pydantic should accept (and ignore) extra keys from stored JSONB data."""
        ref = LinearProjectRef.model_validate(
            {
                "linear_project_id": "proj-123",
                "linear_team_id": "team-456",
                "unknown_extra": "should_be_ignored",
            }
        )
        assert ref.linear_project_id == "proj-123"


# ---------------------------------------------------------------------------
# LinearIssueRef
# ---------------------------------------------------------------------------


class TestLinearIssueRef:
    def test_required_fields(self) -> None:
        ref = LinearIssueRef(
            linear_issue_id="issue-111",
            linear_identifier="ENG-42",
        )
        assert ref.linear_issue_id == "issue-111"
        assert ref.linear_identifier == "ENG-42"
        assert ref.last_synced_at is None

    def test_with_last_synced_at(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        ref = LinearIssueRef(
            linear_issue_id="issue-111",
            linear_identifier="ENG-42",
            last_synced_at=now,
        )
        assert ref.last_synced_at == now

    def test_missing_issue_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            LinearIssueRef(linear_identifier="ENG-42")  # type: ignore[call-arg]

    def test_missing_identifier_raises(self) -> None:
        with pytest.raises(ValidationError):
            LinearIssueRef(linear_issue_id="issue-111")  # type: ignore[call-arg]

    def test_roundtrip_json(self) -> None:
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        ref = LinearIssueRef(
            linear_issue_id="issue-abc",
            linear_identifier="PROJ-7",
            last_synced_at=now,
        )
        data = ref.model_dump(mode="json")
        ref2 = LinearIssueRef.model_validate(data)
        assert ref2.linear_issue_id == ref.linear_issue_id
        assert ref2.linear_identifier == ref.linear_identifier
        assert ref2.last_synced_at == ref.last_synced_at

    def test_identifier_human_readable_format(self) -> None:
        """Identifiers like 'ENG-123' or 'PROJ-1' should be accepted as-is."""
        for identifier in ["ENG-123", "PROJ-1", "TEAM-9999", "A-0"]:
            ref = LinearIssueRef(linear_issue_id="id", linear_identifier=identifier)
            assert ref.linear_identifier == identifier


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_all_default_to_zero(self) -> None:
        result = SyncResult()
        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 0
        assert result.errors == 0

    def test_explicit_values(self) -> None:
        result = SyncResult(created=5, updated=3, skipped=2, errors=1)
        assert result.created == 5
        assert result.updated == 3
        assert result.skipped == 2
        assert result.errors == 1

    def test_increment_created(self) -> None:
        result = SyncResult()
        result.created += 1
        result.created += 1
        assert result.created == 2

    def test_increment_errors(self) -> None:
        result = SyncResult()
        result.errors += 1
        assert result.errors == 1
        assert result.created == 0

    def test_total_is_sum_of_all_fields(self) -> None:
        result = SyncResult(created=2, updated=3, skipped=1, errors=0)
        total = result.created + result.updated + result.skipped + result.errors
        assert total == 6

    def test_roundtrip_json(self) -> None:
        result = SyncResult(created=10, updated=5, skipped=0, errors=2)
        data = result.model_dump()
        result2 = SyncResult.model_validate(data)
        assert result2 == result

    def test_negative_values_accepted(self) -> None:
        """Pydantic does not restrict to non-negative ints; decrements are valid in code."""
        result = SyncResult(created=-1)
        assert result.created == -1
