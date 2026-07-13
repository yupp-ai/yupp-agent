"""Tests for scheduling.py — schedule_agent_call and schedule_recurring_agent_call."""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.agent_harness_service.tools.scheduling import (
    schedule_agent_call as _schedule_agent_call_tool,
)
from ypl.agent_harness_service.tools.scheduling import (
    schedule_recurring_agent_call as _schedule_recurring_agent_call_tool,
)

# Unwrap FunctionTool to get raw callables
schedule_agent_call = _schedule_agent_call_tool.fn
schedule_recurring_agent_call = _schedule_recurring_agent_call_tool.fn

VALID_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INVALID_SESSION = "not-a-uuid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_and_agent() -> tuple[MagicMock, MagicMock]:
    """Return (session, agent) mocks."""
    session = MagicMock()
    session.context = {"user_id": "user-abc", "is_platform": True}
    agent = MagicMock()
    agent.name = "sre"
    return session, agent


def _patch_get_session_and_agent(session: Any = None, agent: Any = None, error: str | None = None) -> Any:
    return patch(
        "ypl.agent_harness_service.tools.scheduling._get_session_and_agent",
        new=AsyncMock(return_value=(session, agent, error)),
    )


def _patch_resolve_creator(user_id: str = "user-abc", agent_name: str = "sre", error: str | None = None) -> Any:
    return patch(
        "ypl.agent_harness_service.tools.scheduling._resolve_creator_info",
        new=AsyncMock(return_value=(user_id, agent_name, error)),
    )


def _patch_create_schedule(result: dict[str, Any] | None = None) -> Any:
    if result is None:
        result = {"success": True, "schedule_id": "sched-123"}
    return patch(
        "ypl.agent_harness_service.tools.scheduling.create_agent_schedule",
        new=AsyncMock(return_value=result),
    )


# ---------------------------------------------------------------------------
# schedule_agent_call
# ---------------------------------------------------------------------------


class TestScheduleAgentCall:
    async def test_invalid_session_id_format(self) -> None:
        result = await schedule_agent_call(
            session_id=INVALID_SESSION,
            target_agent_name="worker",
            message="do stuff",
            execute_at="2030-01-01T10:00:00",
        )
        assert result["success"] is False
        assert "Invalid session_id" in result["error"]

    async def test_session_not_found(self) -> None:
        with _patch_get_session_and_agent(error="Session not found: " + VALID_SESSION):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
            )

        assert result["success"] is False
        assert "Session not found" in result["error"]

    async def test_invalid_timezone(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch(
                "ypl.agent_harness_service.tools.scheduling.validate_timezone",
                return_value="Unknown timezone: Atlantis/City",
            ),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
                timezone="Atlantis/City",
            )

        assert result["success"] is False
        assert "timezone" in result["error"].lower()

    async def test_invalid_execute_at(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_execute_at",
                return_value=(None, "Could not parse date: not-a-date"),
            ),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="not-a-date",
            )

        assert result["success"] is False
        assert "parse" in result["error"].lower()

    async def test_invalid_context_json(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_execute_at",
                return_value=("2030-01-01T10:00:00Z", None),
            ),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_schedule_context",
                return_value=(None, "Invalid JSON context"),
            ),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
                context="{bad json",
            )

        assert result["success"] is False
        assert "JSON" in result["error"]

    async def test_non_platform_rejected(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_execute_at",
                return_value=("2030-01-01T10:00:00Z", None),
            ),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_schedule_context",
                return_value=({}, None),
            ),
            _patch_resolve_creator(error="Only Platforms can schedule"),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
            )

        assert result["success"] is False
        assert "Platform" in result["error"]

    async def test_success(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_execute_at",
                return_value=("2030-01-01T10:00:00Z", None),
            ),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_schedule_context",
                return_value=({"key": "val"}, None),
            ),
            _patch_resolve_creator(),
            _patch_create_schedule(),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
                timezone="UTC",
                name="my-schedule",
                description="A scheduled call",
            )

        assert result["success"] is True

    async def test_exception_caught_as_failure(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.scheduling._get_session_and_agent",
            new=AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            result = await schedule_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="do stuff",
                execute_at="2030-01-01T10:00:00",
            )

        assert result["success"] is False
        assert "DB down" in result["error"]


# ---------------------------------------------------------------------------
# schedule_recurring_agent_call
# ---------------------------------------------------------------------------


class TestScheduleRecurringAgentCall:
    async def test_invalid_session_id_format(self) -> None:
        result = await schedule_recurring_agent_call(
            session_id=INVALID_SESSION,
            target_agent_name="worker",
            message="daily run",
            cron_expression="0 9 * * *",
        )
        assert result["success"] is False
        assert "Invalid session_id" in result["error"]

    async def test_invalid_cron_expression(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.validate_cron_expression",
                return_value="Invalid cron expression",
            ),
        ):
            result = await schedule_recurring_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="daily run",
                cron_expression="not-a-cron",
            )

        assert result["success"] is False
        assert "cron" in result["error"].lower()

    async def test_max_runs_zero_rejected(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch("ypl.agent_harness_service.tools.scheduling.validate_cron_expression", return_value=None),
        ):
            result = await schedule_recurring_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="daily run",
                cron_expression="0 9 * * *",
                max_runs=0,
            )

        assert result["success"] is False
        assert "max_runs" in result["error"]

    async def test_max_runs_negative_rejected(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch("ypl.agent_harness_service.tools.scheduling.validate_cron_expression", return_value=None),
        ):
            result = await schedule_recurring_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="daily run",
                cron_expression="0 9 * * *",
                max_runs=-5,
            )

        assert result["success"] is False
        assert "max_runs" in result["error"]

    async def test_success(self) -> None:
        session, agent = _mock_session_and_agent()
        with (
            _patch_get_session_and_agent(session=session, agent=agent),
            patch("ypl.agent_harness_service.tools.scheduling.validate_timezone", return_value=None),
            patch("ypl.agent_harness_service.tools.scheduling.validate_cron_expression", return_value=None),
            patch(
                "ypl.agent_harness_service.tools.scheduling.parse_schedule_context",
                return_value=({}, None),
            ),
            _patch_resolve_creator(),
            patch(
                "ypl.agent_harness_service.tools.scheduling.compute_next_run_for_cron",
                return_value="2030-01-01T09:00:00Z",
            ),
            _patch_create_schedule(),
        ):
            result = await schedule_recurring_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="daily run",
                cron_expression="0 9 * * *",
                max_runs=10,
            )

        assert result["success"] is True

    async def test_exception_caught_as_failure(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.scheduling._get_session_and_agent",
            new=AsyncMock(side_effect=RuntimeError("unexpected")),
        ):
            result = await schedule_recurring_agent_call(
                session_id=VALID_SESSION,
                target_agent_name="worker",
                message="daily run",
                cron_expression="0 9 * * *",
            )

        assert result["success"] is False
        assert "unexpected" in result["error"]
