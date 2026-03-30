"""Extended edge-case tests for the Linear ↔ AHS mapping module.

The base mapping tests live in tests/agent_harness_service/tools/linear_sync/test_mapping.py.
This file adds edge-case coverage and integration-level assertions.
"""

from __future__ import annotations
from typing import Any

import pytest
from ypl.agent_harness_service.tools.linear_sync.mapping import (
    AHS_TO_LINEAR_PRIORITY,
    AHS_TO_LINEAR_STATUS,
    LINEAR_TO_AHS_PRIORITY,
    LINEAR_TO_AHS_STATUS,
    map_ahs_priority_to_linear,
    map_ahs_status_to_linear,
    map_linear_priority_to_ahs,
    map_linear_status_to_ahs,
)

from tests.agent_harness_service.linear_sync.conftest import TEAM_STATUSES

# ---------------------------------------------------------------------------
# Coverage of intentionally-omitted AHS statuses
# ---------------------------------------------------------------------------


class TestOmittedStatusMappings:
    """PENDING and IN_REVIEW are intentionally not in AHS_TO_LINEAR_STATUS."""

    def test_pending_not_in_mapping(self) -> None:
        assert "PENDING" not in AHS_TO_LINEAR_STATUS

    def test_in_review_not_in_mapping(self) -> None:
        assert "IN_REVIEW" not in AHS_TO_LINEAR_STATUS

    def test_map_ahs_status_pending_returns_none(self) -> None:
        assert map_ahs_status_to_linear("PENDING", TEAM_STATUSES) is None

    def test_map_ahs_status_in_review_returns_none(self) -> None:
        assert map_ahs_status_to_linear("IN_REVIEW", TEAM_STATUSES) is None


# ---------------------------------------------------------------------------
# NORMAL alias
# ---------------------------------------------------------------------------


class TestNormalAlias:
    """NORMAL is an alias for MEDIUM; both map to Linear priority 3."""

    def test_normal_and_medium_map_to_same_linear_priority(self) -> None:
        assert map_ahs_priority_to_linear("NORMAL") == map_ahs_priority_to_linear("MEDIUM")
        assert map_ahs_priority_to_linear("NORMAL") == 3

    def test_normal_in_priority_dict(self) -> None:
        assert "NORMAL" in AHS_TO_LINEAR_PRIORITY
        assert AHS_TO_LINEAR_PRIORITY["NORMAL"] == 3

    def test_linear_3_maps_back_to_medium_not_normal(self) -> None:
        """After round-trip, MEDIUM is the canonical name for priority 3."""
        assert map_linear_priority_to_ahs(3) == "MEDIUM"


# ---------------------------------------------------------------------------
# BLOCKED status maps to "started"
# ---------------------------------------------------------------------------


class TestBlockedStatusMapping:
    def test_blocked_maps_to_started_type(self) -> None:
        """BLOCKED has no native Linear equivalent; it maps to 'started'."""
        assert AHS_TO_LINEAR_STATUS["BLOCKED"] == "started"

    def test_blocked_and_in_progress_resolve_to_same_state_id(self) -> None:
        result_blocked = map_ahs_status_to_linear("BLOCKED", TEAM_STATUSES)
        result_in_progress = map_ahs_status_to_linear("IN_PROGRESS", TEAM_STATUSES)
        assert result_blocked == result_in_progress == "state-in-progress"


# ---------------------------------------------------------------------------
# Linear state types → AHS: edge cases beyond the base tests
# ---------------------------------------------------------------------------


class TestLinearToAhsStatusEdgeCases:
    def test_empty_string_type_defaults_to_ready(self) -> None:
        state = {"id": "x", "type": "", "name": "Empty"}
        assert map_linear_status_to_ahs(state) == "READY"

    def test_none_type_value_defaults_to_ready(self) -> None:
        state: dict[str, Any] = {"id": "x", "type": None, "name": "NoneType"}
        # None is not a key in LINEAR_TO_AHS_STATUS; .get() returns default
        assert map_linear_status_to_ahs(state) == "READY"

    def test_all_linear_types_covered(self) -> None:
        """Every type in LINEAR_TO_AHS_STATUS resolves to a non-empty AHS status."""
        for linear_type in LINEAR_TO_AHS_STATUS:
            state = {"id": "x", "type": linear_type, "name": linear_type}
            result = map_linear_status_to_ahs(state)
            assert result, f"map_linear_status_to_ahs returned empty for type {linear_type!r}"

    def test_triage_and_backlog_both_ready(self) -> None:
        for state_type in ("triage", "backlog"):
            assert map_linear_status_to_ahs({"type": state_type}) == "READY"


# ---------------------------------------------------------------------------
# Priority boundary / edge cases
# ---------------------------------------------------------------------------


class TestPriorityEdgeCases:
    def test_all_linear_priorities_0_to_4_map_to_known_ahs(self) -> None:
        known = {"NO_PRIORITY", "URGENT", "HIGH", "MEDIUM", "LOW"}
        for p in range(5):
            assert map_linear_priority_to_ahs(p) in known

    def test_large_out_of_range_linear_priority(self) -> None:
        assert map_linear_priority_to_ahs(100) == "NO_PRIORITY"

    def test_negative_linear_priority(self) -> None:
        assert map_linear_priority_to_ahs(-5) == "NO_PRIORITY"

    def test_empty_string_ahs_priority_defaults_to_zero(self) -> None:
        assert map_ahs_priority_to_linear("") == 0

    @pytest.mark.parametrize("variant", ["urgent", "Urgent", "URGENT"])
    def test_ahs_priority_case_insensitive(self, variant: str) -> None:
        assert map_ahs_priority_to_linear(variant) == 1


# ---------------------------------------------------------------------------
# map_ahs_status_to_linear: team with duplicate state types
# ---------------------------------------------------------------------------


class TestDuplicateStateTypes:
    def test_first_matching_state_returned_when_duplicates_exist(self) -> None:
        """If a team has two 'unstarted' states, the first one wins."""
        statuses = [
            {"id": "first-unstarted", "type": "unstarted", "name": "Todo"},
            {"id": "second-unstarted", "type": "unstarted", "name": "Backlog (custom)"},
        ]
        assert map_ahs_status_to_linear("READY", statuses) == "first-unstarted"

    def test_team_with_only_cancelled_states(self) -> None:
        statuses = [{"id": "c1", "type": "cancelled", "name": "Cancelled"}]
        assert map_ahs_status_to_linear("FAILED", statuses) == "c1"
        assert map_ahs_status_to_linear("CANCELLED", statuses) == "c1"
        assert map_ahs_status_to_linear("READY", statuses) is None


# ---------------------------------------------------------------------------
# Symmetry / consistency checks
# ---------------------------------------------------------------------------


class TestMappingConsistency:
    def test_linear_to_ahs_status_values_are_valid_ahs_statuses(self) -> None:
        valid = {"READY", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
        for ahs_status in LINEAR_TO_AHS_STATUS.values():
            assert ahs_status in valid, f"Unexpected AHS status in LINEAR_TO_AHS_STATUS: {ahs_status!r}"

    def test_ahs_to_linear_status_values_are_valid_linear_types(self) -> None:
        valid_types = {"triage", "backlog", "unstarted", "started", "completed", "cancelled"}
        for linear_type in AHS_TO_LINEAR_STATUS.values():
            assert linear_type in valid_types, f"Unexpected Linear type: {linear_type!r}"

    def test_linear_priority_values_are_canonical_ahs_names(self) -> None:
        canonical = {"NO_PRIORITY", "URGENT", "HIGH", "MEDIUM", "LOW"}
        for name in LINEAR_TO_AHS_PRIORITY.values():
            assert name in canonical
