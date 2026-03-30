"""Unit tests for the Linear ↔ AHS status/priority mapping module."""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Dict completeness / consistency checks
# ---------------------------------------------------------------------------


class TestMappingDicts:
    """Sanity-checks on the exported mapping dictionaries."""

    def test_ahs_to_linear_status_keys_are_upper(self) -> None:
        for key in AHS_TO_LINEAR_STATUS:
            assert key == key.upper(), f"Key {key!r} is not upper-case"

    def test_linear_to_ahs_status_values_are_upper(self) -> None:
        for value in LINEAR_TO_AHS_STATUS.values():
            assert value == value.upper(), f"Value {value!r} is not upper-case"

    def test_ahs_to_linear_priority_values_in_range(self) -> None:
        for key, val in AHS_TO_LINEAR_PRIORITY.items():
            assert 0 <= val <= 4, f"{key!r} maps to out-of-range priority {val}"

    def test_linear_to_ahs_priority_keys_cover_0_to_4(self) -> None:
        assert set(LINEAR_TO_AHS_PRIORITY.keys()) == {0, 1, 2, 3, 4}

    def test_priority_roundtrip_completeness(self) -> None:
        """Every AHS priority should survive a roundtrip through Linear and back.

        Note: NORMAL is an alias for MEDIUM (both map to Linear priority 3),
        so NORMAL→3→MEDIUM is an acceptable roundtrip.
        """
        # Known aliases: multiple AHS priorities that map to the same Linear value
        aliases = {"NORMAL": "MEDIUM"}  # NORMAL is an alias for MEDIUM

        for ahs_priority in AHS_TO_LINEAR_PRIORITY:
            linear_val = AHS_TO_LINEAR_PRIORITY[ahs_priority]
            recovered = LINEAR_TO_AHS_PRIORITY[linear_val]
            expected = aliases.get(ahs_priority, ahs_priority)
            assert recovered == expected, f"Roundtrip failed for {ahs_priority!r}: AHS→{linear_val}→{recovered!r}"


# ---------------------------------------------------------------------------
# map_ahs_status_to_linear
# ---------------------------------------------------------------------------


TEAM_STATUSES = [
    {"id": "state-triage", "type": "triage", "name": "Triage"},
    {"id": "state-backlog", "type": "backlog", "name": "Backlog"},
    {"id": "state-todo", "type": "unstarted", "name": "Todo"},
    {"id": "state-in-progress", "type": "started", "name": "In Progress"},
    {"id": "state-done", "type": "completed", "name": "Done"},
    {"id": "state-cancelled", "type": "cancelled", "name": "Cancelled"},
]


class TestMapAhsStatusToLinear:
    def test_ready_maps_to_unstarted(self) -> None:
        result = map_ahs_status_to_linear("READY", TEAM_STATUSES)
        assert result == "state-todo"

    def test_in_progress_maps_to_started(self) -> None:
        result = map_ahs_status_to_linear("IN_PROGRESS", TEAM_STATUSES)
        assert result == "state-in-progress"

    def test_completed_maps_to_completed(self) -> None:
        result = map_ahs_status_to_linear("COMPLETED", TEAM_STATUSES)
        assert result == "state-done"

    def test_failed_maps_to_cancelled(self) -> None:
        result = map_ahs_status_to_linear("FAILED", TEAM_STATUSES)
        assert result == "state-cancelled"

    def test_cancelled_maps_to_cancelled(self) -> None:
        result = map_ahs_status_to_linear("CANCELLED", TEAM_STATUSES)
        assert result == "state-cancelled"

    def test_blocked_maps_to_started(self) -> None:
        result = map_ahs_status_to_linear("BLOCKED", TEAM_STATUSES)
        assert result == "state-in-progress"

    def test_case_insensitive(self) -> None:
        assert map_ahs_status_to_linear("ready", TEAM_STATUSES) == map_ahs_status_to_linear("READY", TEAM_STATUSES)
        assert map_ahs_status_to_linear("in_progress", TEAM_STATUSES) == map_ahs_status_to_linear(
            "IN_PROGRESS", TEAM_STATUSES
        )

    def test_unknown_status_returns_none(self) -> None:
        result = map_ahs_status_to_linear("UNKNOWN_STATUS", TEAM_STATUSES)
        assert result is None

    def test_returns_none_when_no_matching_state_in_team(self) -> None:
        # Team has no "completed" states at all.
        limited_statuses = [s for s in TEAM_STATUSES if s["type"] != "completed"]
        result = map_ahs_status_to_linear("COMPLETED", limited_statuses)
        assert result is None

    def test_empty_team_statuses_returns_none(self) -> None:
        result = map_ahs_status_to_linear("READY", [])
        assert result is None

    def test_picks_first_match_when_multiple_states_of_same_type(self) -> None:
        statuses = [
            {"id": "first-started", "type": "started", "name": "In Progress"},
            {"id": "second-started", "type": "started", "name": "In Review"},
        ]
        result = map_ahs_status_to_linear("IN_PROGRESS", statuses)
        assert result == "first-started"


# ---------------------------------------------------------------------------
# map_linear_status_to_ahs
# ---------------------------------------------------------------------------


class TestMapLinearStatusToAhs:
    @pytest.mark.parametrize(
        ("state_type", "expected_ahs"),
        [
            ("triage", "READY"),
            ("backlog", "READY"),
            ("unstarted", "READY"),
            ("started", "IN_PROGRESS"),
            ("completed", "COMPLETED"),
            ("cancelled", "CANCELLED"),
        ],
    )
    def test_known_types(self, state_type: str, expected_ahs: str) -> None:
        state = {"id": "some-id", "type": state_type, "name": "Some State"}
        assert map_linear_status_to_ahs(state) == expected_ahs

    def test_unknown_type_defaults_to_ready(self) -> None:
        state = {"id": "x", "type": "something_new", "name": "Future State"}
        assert map_linear_status_to_ahs(state) == "READY"

    def test_missing_type_key_defaults_to_ready(self) -> None:
        state: dict[str, str] = {"id": "x", "name": "No Type"}
        assert map_linear_status_to_ahs(state) == "READY"


# ---------------------------------------------------------------------------
# map_ahs_priority_to_linear
# ---------------------------------------------------------------------------


class TestMapAhsPriorityToLinear:
    @pytest.mark.parametrize(
        ("ahs_priority", "linear_priority"),
        [
            ("URGENT", 1),
            ("HIGH", 2),
            ("MEDIUM", 3),
            ("LOW", 4),
            ("NO_PRIORITY", 0),
        ],
    )
    def test_known_priorities(self, ahs_priority: str, linear_priority: int) -> None:
        assert map_ahs_priority_to_linear(ahs_priority) == linear_priority

    def test_case_insensitive(self) -> None:
        assert map_ahs_priority_to_linear("high") == map_ahs_priority_to_linear("HIGH")
        assert map_ahs_priority_to_linear("urgent") == map_ahs_priority_to_linear("URGENT")

    def test_unknown_priority_defaults_to_zero(self) -> None:
        assert map_ahs_priority_to_linear("WHATEVER") == 0


# ---------------------------------------------------------------------------
# map_linear_priority_to_ahs
# ---------------------------------------------------------------------------


class TestMapLinearPriorityToAhs:
    @pytest.mark.parametrize(
        ("linear_priority", "ahs_priority"),
        [
            (0, "NO_PRIORITY"),
            (1, "URGENT"),
            (2, "HIGH"),
            (3, "MEDIUM"),
            (4, "LOW"),
        ],
    )
    def test_known_priorities(self, linear_priority: int, ahs_priority: str) -> None:
        assert map_linear_priority_to_ahs(linear_priority) == ahs_priority

    def test_out_of_range_defaults_to_no_priority(self) -> None:
        assert map_linear_priority_to_ahs(99) == "NO_PRIORITY"
        assert map_linear_priority_to_ahs(-1) == "NO_PRIORITY"

    def test_roundtrip_all_priorities(self) -> None:
        for linear_val in range(5):
            ahs_val = map_linear_priority_to_ahs(linear_val)
            recovered = map_ahs_priority_to_linear(ahs_val)
            assert recovered == linear_val, (
                f"Roundtrip failed for linear priority {linear_val}: →{ahs_val!r}→{recovered}"
            )
