"""Status and priority mapping between AHS and Linear."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Status mappings
# ---------------------------------------------------------------------------

# Maps AHS task statuses to the Linear workflow-state *type* used when
# looking up a matching state inside a team's workflow.  Linear state types:
#   "triage" | "backlog" | "unstarted" | "started" | "completed" | "cancelled"
AHS_TO_LINEAR_STATUS: dict[str, str] = {
    "READY": "unstarted",
    "IN_PROGRESS": "started",
    "COMPLETED": "completed",
    "FAILED": "cancelled",
    "CANCELLED": "cancelled",
    "BLOCKED": "started",  # still in-flight; no dedicated Linear type
    # "PENDING" is intentionally omitted: tasks awaiting execution have not
    # yet been claimed and do not yet need a Linear issue created for them.
    # "IN_REVIEW" is intentionally omitted: not yet part of the stable AHS
    # task status set; add here once the status is fully landed and a
    # consensus Linear mapping exists.
}

# Preferred Linear state names per type.  When multiple workflow states share
# a type (e.g. "To Do", "Needs UX", "Needs Product" are all "unstarted"),
# we prefer the one whose name matches Linear's default for that type.
_PREFERRED_STATE_NAMES: dict[str, str] = {
    "unstarted": "to do",
    "started": "in progress",
    "completed": "done",
    "cancelled": "canceled",
}

# Maps a Linear workflow-state type back to an AHS task status.
# "triage" and "backlog" are treated as not-yet-started work → "READY".
LINEAR_TO_AHS_STATUS: dict[str, str] = {
    "triage": "READY",
    "backlog": "READY",
    "unstarted": "READY",
    "started": "IN_PROGRESS",
    "completed": "COMPLETED",
    "cancelled": "CANCELLED",
}

# ---------------------------------------------------------------------------
# Priority mappings
# ---------------------------------------------------------------------------

# Maps AHS priority strings to Linear priority integers.
# Linear: 0 = No Priority, 1 = Urgent, 2 = High, 3 = Medium, 4 = Low
AHS_TO_LINEAR_PRIORITY: dict[str, int] = {
    "URGENT": 1,
    "HIGH": 2,
    "MEDIUM": 3,
    "NORMAL": 3,  # NORMAL is an alias for MEDIUM in AHS (AgentTaskPriority.NORMAL == 3)
    "LOW": 4,
    "NO_PRIORITY": 0,
}

# Maps Linear priority integers back to AHS priority strings.
LINEAR_TO_AHS_PRIORITY: dict[int, str] = {
    0: "NO_PRIORITY",
    1: "URGENT",
    2: "HIGH",
    3: "MEDIUM",
    4: "LOW",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def map_ahs_status_to_linear(status: str, team_statuses: list[dict[str, str]]) -> str | None:
    """Return the Linear workflow-state ID that best matches *status*.

    Args:
        status: An AHS task status string, e.g. ``"IN_PROGRESS"``.
        team_statuses: A list of Linear workflow-state dicts for a team.
            Each dict must contain at least ``"id"`` and ``"type"`` keys,
            where ``"type"`` is one of the Linear state types
            (``"triage"``, ``"backlog"``, ``"unstarted"``, ``"started"``,
            ``"completed"``, ``"cancelled"``).

    Returns:
        The ``id`` of the first matching Linear state, or ``None`` if no
        match is found (either because *status* is unknown or no state of the
        required type exists in *team_statuses*).
    """
    target_type = AHS_TO_LINEAR_STATUS.get(status.upper())
    if target_type is None:
        return None

    candidates = [state for state in team_statuses if state.get("type") == target_type]
    if not candidates:
        return None

    # When multiple states share a type (e.g. "To Do" and "Needs UX" are both
    # "unstarted"), prefer the one whose name matches Linear's default.
    preferred = _PREFERRED_STATE_NAMES.get(target_type)
    if preferred:
        for state in candidates:
            if state.get("name", "").lower() == preferred:
                return state["id"]

    return candidates[0]["id"]


def map_linear_status_to_ahs(state: dict[str, str]) -> str:
    """Return the AHS task status that corresponds to a Linear workflow state.

    Args:
        state: A Linear workflow-state dict containing at least a ``"type"``
            key (e.g. ``{"id": "...", "name": "In Progress", "type": "started"}``).

    Returns:
        An AHS status string.  Defaults to ``"READY"`` for unrecognised state
        types.
    """
    state_type = state.get("type", "")
    return LINEAR_TO_AHS_STATUS.get(state_type, "READY")


def map_ahs_priority_to_linear(priority: str) -> int:
    """Return the Linear priority integer for an AHS priority string.

    Args:
        priority: An AHS priority string, e.g. ``"HIGH"``.

    Returns:
        A Linear priority integer (0–4).  Defaults to ``0`` (No Priority) for
        unrecognised values.
    """
    return AHS_TO_LINEAR_PRIORITY.get(priority.upper(), 0)


def map_linear_priority_to_ahs(priority: int) -> str:
    """Return the AHS priority string for a Linear priority integer.

    Args:
        priority: A Linear priority integer (0–4).

    Returns:
        An AHS priority string.  Defaults to ``"NO_PRIORITY"`` for out-of-range
        values.
    """
    return LINEAR_TO_AHS_PRIORITY.get(priority, "NO_PRIORITY")
