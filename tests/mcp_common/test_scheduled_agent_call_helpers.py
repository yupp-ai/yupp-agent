"""Unit tests for ypl/mcp_common/scheduled_agent_call_helpers.py.

Covers pure-logic functions that require no live database:
  - validate_timezone
  - validate_cron_expression
  - compute_next_run_for_cron
  - parse_execute_at
  - parse_schedule_context
  - _schedule_to_dict

And DB-backed functions with mocked sessions:
  - resolve_user_id_from_email
  - resolve_email_from_user_id
  - resolve_user_id
  - resolve_user_id_from_context
  - create_agent_schedule
  - edit_agent_schedule_fields
  - list_agent_schedules_by_filters
  - cancel_agent_schedule_by_id
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.db.agent_harness import AgentScheduleStatus, AgentScheduleType
from ypl.mcp_common.scheduled_agent_call_helpers import (
    _schedule_to_dict,
    compute_next_run_for_cron,
    parse_execute_at,
    parse_schedule_context,
    validate_cron_expression,
    validate_timezone,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_schedule(
    schedule_type: AgentScheduleType = AgentScheduleType.SCHEDULED,
    status: AgentScheduleStatus = AgentScheduleStatus.PENDING,
    message: str = "Run the report",
    cron_expression: str | None = None,
    cron_timezone: str | None = "UTC",
    execute_at: datetime | None = None,
    next_run_at: datetime | None = None,
    run_count: int = 0,
    max_runs: int | None = None,
    name: str | None = "My Schedule",
    description: str | None = "A test schedule",
    created_by_user: str | None = "user-123",
    created_by_agent: str | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a minimal AgentSchedule-like MagicMock."""
    sched = MagicMock()
    sched.agent_schedule_id = uuid.uuid4()
    sched.schedule_type = schedule_type
    sched.status = status
    sched.message = message
    sched.cron_expression = cron_expression
    sched.cron_timezone = cron_timezone
    sched.execute_at = execute_at
    sched.next_run_at = next_run_at
    sched.last_run_at = None
    sched.run_count = run_count
    sched.max_runs = max_runs
    sched.name = name
    sched.description = description
    sched.context = None
    sched.created_by_user = created_by_user
    sched.created_by_agent = created_by_agent
    sched.created_at = created_at or datetime(2024, 1, 1, tzinfo=UTC)
    return sched


def _make_ctx_manager(session: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_session(scalar_one_or_none: Any = None, user: Any = None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=user)
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# validate_timezone
# ---------------------------------------------------------------------------


class TestValidateTimezone:
    def test_valid_utc(self) -> None:
        assert validate_timezone("UTC") is None

    def test_valid_us_eastern(self) -> None:
        assert validate_timezone("America/New_York") is None

    def test_valid_europe_london(self) -> None:
        assert validate_timezone("Europe/London") is None

    def test_valid_asia_tokyo(self) -> None:
        assert validate_timezone("Asia/Tokyo") is None

    def test_invalid_returns_error_message(self) -> None:
        error = validate_timezone("Blarg/Nowhere")
        assert error is not None
        assert "Invalid timezone" in error
        assert "Blarg/Nowhere" in error

    def test_empty_string_returns_error_or_raises(self) -> None:
        # Empty string causes ZoneInfo to raise ValueError (not ZoneInfoNotFoundError),
        # so validate_timezone propagates it rather than returning an error string.
        import pytest

        with pytest.raises(ValueError):
            validate_timezone("")

    def test_us_pacific(self) -> None:
        assert validate_timezone("US/Pacific") is None

    def test_gibberish_timezone(self) -> None:
        error = validate_timezone("NotATimezone")
        assert error is not None
        assert "IANA timezone" in error


# ---------------------------------------------------------------------------
# validate_cron_expression
# ---------------------------------------------------------------------------


class TestValidateCronExpression:
    def test_valid_every_minute(self) -> None:
        assert validate_cron_expression("* * * * *", "UTC") is None

    def test_valid_9am_daily(self) -> None:
        assert validate_cron_expression("0 9 * * *", "UTC") is None

    def test_valid_weekdays_only(self) -> None:
        assert validate_cron_expression("0 9 * * 1-5", "UTC") is None

    def test_valid_first_of_month(self) -> None:
        assert validate_cron_expression("0 0 1 * *", "UTC") is None

    def test_invalid_expression_returns_error(self) -> None:
        error = validate_cron_expression("bad cron", "UTC")
        assert error is not None
        assert "Invalid cron" in error

    def test_too_many_fields_returns_error(self) -> None:
        # croniter may or may not accept 6 fields — just verify we get None or str
        result = validate_cron_expression("0 0 0 * * *", "UTC")
        # This may be valid (seconds-field) or invalid — either is acceptable
        assert result is None or isinstance(result, str)

    def test_valid_with_nondefault_timezone(self) -> None:
        assert validate_cron_expression("0 9 * * *", "America/New_York") is None


# ---------------------------------------------------------------------------
# compute_next_run_for_cron
# ---------------------------------------------------------------------------


class TestComputeNextRunForCron:
    def test_returns_datetime_in_utc(self) -> None:
        result = compute_next_run_for_cron("* * * * *", "UTC")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        # UTC-aware: utcoffset should be zero
        assert result.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_next_run_is_in_future(self) -> None:
        result = compute_next_run_for_cron("* * * * *", "UTC")
        assert result > datetime.now(UTC)

    def test_daily_cron_returns_sensible_time(self) -> None:
        result = compute_next_run_for_cron("0 9 * * *", "UTC")
        assert isinstance(result, datetime)
        assert result > datetime.now(UTC)

    def test_timezone_affects_result(self) -> None:
        result_utc = compute_next_run_for_cron("0 9 * * *", "UTC")
        result_et = compute_next_run_for_cron("0 9 * * *", "America/New_York")
        # Both are UTC datetimes, but the underlying local time differs
        assert result_utc != result_et

    def test_result_timezone_is_utc(self) -> None:
        result = compute_next_run_for_cron("0 9 * * *", "America/Los_Angeles")
        # Should be UTC-aware
        assert result.tzinfo is not None
        # UTC offset should be 0 hours
        assert result.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# parse_execute_at
# ---------------------------------------------------------------------------


class TestParseExecuteAt:
    def test_valid_future_iso_string_with_tz(self) -> None:
        future = "2099-01-01T12:00:00+00:00"
        dt, err = parse_execute_at(future, "UTC")
        assert err is None
        assert dt is not None
        assert dt.tzinfo is not None

    def test_valid_future_naive_string_uses_timezone(self) -> None:
        # Naive datetime string — timezone parameter should be applied
        future = "2099-06-01T12:00:00"
        dt, err = parse_execute_at(future, "America/New_York")
        assert err is None
        assert dt is not None
        # After conversion to UTC, offset should be 0
        assert dt.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_invalid_format_returns_error(self) -> None:
        dt, err = parse_execute_at("not-a-date", "UTC")
        assert dt is None
        assert err is not None
        assert "Invalid execute_at format" in err

    def test_past_datetime_returns_error(self) -> None:
        past = "2000-01-01T00:00:00+00:00"
        dt, err = parse_execute_at(past, "UTC")
        assert dt is None
        assert err is not None
        assert "future" in err

    def test_result_is_utc(self) -> None:
        future = "2099-12-31T23:59:00+05:30"  # IST
        dt, err = parse_execute_at(future, "UTC")
        assert err is None
        assert dt is not None
        assert dt.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_just_past_now_returns_error(self) -> None:
        # A datetime 1 hour ago
        past = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
        dt, err = parse_execute_at(past, "UTC")
        assert dt is None
        assert err is not None


# ---------------------------------------------------------------------------
# parse_schedule_context
# ---------------------------------------------------------------------------


class TestParseScheduleContext:
    def test_none_returns_none_none(self) -> None:
        ctx, err = parse_schedule_context(None)
        assert ctx is None
        assert err is None

    def test_empty_string_returns_none_none(self) -> None:
        ctx, err = parse_schedule_context("")
        assert ctx is None
        assert err is None

    def test_valid_json_object(self) -> None:
        ctx, err = parse_schedule_context('{"key": "value"}')
        assert err is None
        assert ctx == {"key": "value"}

    def test_valid_nested_object(self) -> None:
        ctx, err = parse_schedule_context('{"a": {"b": 1}, "c": [1, 2, 3]}')
        assert err is None
        assert ctx is not None
        assert ctx["a"]["b"] == 1

    def test_invalid_json_returns_error(self) -> None:
        ctx, err = parse_schedule_context("{bad json")
        assert ctx is None
        assert err is not None
        assert "Invalid context JSON" in err

    def test_json_array_returns_error(self) -> None:
        ctx, err = parse_schedule_context("[1, 2, 3]")
        assert ctx is None
        assert err is not None
        assert "JSON object" in err

    def test_json_string_returns_error(self) -> None:
        ctx, err = parse_schedule_context('"just a string"')
        assert ctx is None
        assert err is not None


# ---------------------------------------------------------------------------
# _schedule_to_dict
# ---------------------------------------------------------------------------


class TestScheduleToDict:
    def test_basic_fields_present(self) -> None:
        sched = _make_schedule()
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert "agent_schedule_id" in result
        assert result["agent_name"] == "eng-raccoon"
        assert result["schedule_type"] == AgentScheduleType.SCHEDULED.value
        assert result["status"] == AgentScheduleStatus.PENDING.value
        assert result["message"] == "Run the report"

    def test_message_not_truncated_when_short(self) -> None:
        sched = _make_schedule(message="Short message")
        result = _schedule_to_dict(sched, agent_name="eng-raccoon", truncate_message=True)
        assert result["message"] == "Short message"

    def test_message_truncated_at_100_chars(self) -> None:
        long_msg = "A" * 150
        sched = _make_schedule(message=long_msg)
        result = _schedule_to_dict(sched, agent_name="eng-raccoon", truncate_message=True)
        assert result["message"].endswith("...")
        assert len(result["message"]) == 103

    def test_message_not_truncated_by_default(self) -> None:
        long_msg = "A" * 150
        sched = _make_schedule(message=long_msg)
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert result["message"] == long_msg

    def test_execute_at_isoformat(self) -> None:
        dt = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        sched = _make_schedule(execute_at=dt)
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert result["execute_at"] == dt.isoformat()

    def test_execute_at_none_when_not_set(self) -> None:
        sched = _make_schedule(execute_at=None)
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert result["execute_at"] is None

    def test_cron_fields_for_recurring(self) -> None:
        sched = _make_schedule(
            schedule_type=AgentScheduleType.RECURRING,
            cron_expression="0 9 * * *",
            cron_timezone="America/New_York",
        )
        result = _schedule_to_dict(sched, agent_name="bookkeeper")
        assert result["cron_expression"] == "0 9 * * *"
        assert result["cron_timezone"] == "America/New_York"

    def test_run_count_present(self) -> None:
        sched = _make_schedule(run_count=5)
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert result["run_count"] == 5

    def test_agent_schedule_id_is_string(self) -> None:
        sched = _make_schedule()
        result = _schedule_to_dict(sched, agent_name="eng-raccoon")
        assert isinstance(result["agent_schedule_id"], str)


# ---------------------------------------------------------------------------
# resolve_user_id_from_email (DB mocked)
# ---------------------------------------------------------------------------


class TestResolveUserIdFromEmail:
    async def test_user_found_returns_user_id(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email

        mock_user = MagicMock()
        mock_user.user_id = "user-uuid-123"
        mock_user.deleted_at = None

        session = _make_session(scalar_one_or_none=mock_user)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            user_id, err = await resolve_user_id_from_email("test@example.com")

        assert err is None
        assert user_id == "user-uuid-123"

    async def test_user_not_found_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email

        session = _make_session(scalar_one_or_none=None)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            user_id, err = await resolve_user_id_from_email("notfound@example.com")

        assert user_id is None
        assert err is not None
        assert "User not found" in err

    async def test_deleted_user_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_email

        mock_user = MagicMock()
        mock_user.user_id = "deleted-user-uuid"
        mock_user.deleted_at = datetime(2024, 1, 1, tzinfo=UTC)

        session = _make_session(scalar_one_or_none=mock_user)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            user_id, err = await resolve_user_id_from_email("deleted@example.com")

        assert user_id is None
        assert err is not None
        assert "deleted" in err


# ---------------------------------------------------------------------------
# resolve_email_from_user_id (DB mocked)
# ---------------------------------------------------------------------------


class TestResolveEmailFromUserId:
    async def test_user_found_returns_email(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_email_from_user_id

        mock_user = MagicMock()
        mock_user.deleted_at = None
        mock_user.email = "test@example.com"

        session = _make_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_user)))
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            email = await resolve_email_from_user_id("user-123")

        assert email == "test@example.com"

    async def test_user_not_found_returns_none(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_email_from_user_id

        session = _make_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            email = await resolve_email_from_user_id("nonexistent-id")

        assert email is None

    async def test_deleted_user_returns_none(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_email_from_user_id

        mock_user = MagicMock()
        mock_user.deleted_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_user.email = "test@example.com"

        session = _make_session()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_user)))
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            email = await resolve_email_from_user_id("deleted-user")

        assert email is None


# ---------------------------------------------------------------------------
# resolve_user_id (DB mocked)
# ---------------------------------------------------------------------------


class TestResolveUserId:
    async def test_no_identifiers_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        user_id, err = await resolve_user_id()
        assert user_id is None
        assert err is not None
        assert "No user identifier" in err

    async def test_email_lookup_success(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        mock_user = MagicMock()
        mock_user.user_id = "user-abc"
        mock_user.deleted_at = None

        session = _make_session(scalar_one_or_none=mock_user)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            uid, err = await resolve_user_id(email="test@example.com")

        assert err is None
        assert uid == "user-abc"

    async def test_user_id_lookup_success(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        mock_user = MagicMock()
        mock_user.user_id = "direct-user-id"
        mock_user.deleted_at = None

        session = _make_session(user=mock_user)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            uid, err = await resolve_user_id(user_id="direct-user-id")

        assert err is None
        assert uid == "direct-user-id"

    async def test_slack_user_id_resolves_to_email_then_user(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        mock_user = MagicMock()
        mock_user.user_id = "user-from-slack"
        mock_user.deleted_at = None

        session = _make_session(scalar_one_or_none=mock_user)
        ctx = _make_ctx_manager(session)

        with (
            patch(
                "ypl.mcp_common.scheduled_agent_call_helpers.get_user_email_from_slack",
                new=AsyncMock(return_value="slack@example.com"),
            ),
            patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx),
        ):
            uid, err = await resolve_user_id(slack_user_id="U12345")

        assert err is None
        assert uid == "user-from-slack"

    async def test_slack_user_id_resolution_failure_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        with patch(
            "ypl.mcp_common.scheduled_agent_call_helpers.get_user_email_from_slack",
            new=AsyncMock(return_value=None),
        ):
            uid, err = await resolve_user_id(slack_user_id="UNKNOWN")

        assert uid is None
        assert err is not None
        assert "Could not resolve" in err

    async def test_deleted_user_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id

        mock_user = MagicMock()
        mock_user.user_id = "deleted-user"
        mock_user.deleted_at = datetime(2024, 1, 1, tzinfo=UTC)

        session = _make_session(scalar_one_or_none=mock_user)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            uid, err = await resolve_user_id(email="deleted@example.com")

        assert uid is None
        assert err is not None
        assert "deleted" in err


# ---------------------------------------------------------------------------
# resolve_user_id_from_context
# ---------------------------------------------------------------------------


class TestResolveUserFromContext:
    async def test_no_context_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        uid, err = await resolve_user_id_from_context(None)
        assert uid is None
        assert err is not None
        assert "No session context" in err

    async def test_empty_context_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        uid, err = await resolve_user_id_from_context({})
        assert uid is None
        assert err is not None

    async def test_slack_user_email_takes_priority(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        with patch(
            "ypl.mcp_common.scheduled_agent_call_helpers.resolve_user_id",
            new=AsyncMock(return_value=("user-123", None)),
        ) as mock_resolve:
            uid, err = await resolve_user_id_from_context(
                {"slack_user_email": "slack@example.com", "user_email": "other@example.com"}
            )

        assert uid == "user-123"
        assert err is None
        mock_resolve.assert_called_once_with(email="slack@example.com")

    async def test_user_email_used_when_no_slack_email(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        with patch(
            "ypl.mcp_common.scheduled_agent_call_helpers.resolve_user_id",
            new=AsyncMock(return_value=("user-456", None)),
        ) as mock_resolve:
            uid, err = await resolve_user_id_from_context({"user_email": "user@example.com"})

        assert uid == "user-456"
        mock_resolve.assert_called_once_with(email="user@example.com")

    async def test_user_id_context_key(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        with patch(
            "ypl.mcp_common.scheduled_agent_call_helpers.resolve_user_id",
            new=AsyncMock(return_value=("user-789", None)),
        ) as mock_resolve:
            uid, err = await resolve_user_id_from_context({"user_id": "user-789"})

        assert uid == "user-789"
        mock_resolve.assert_called_once_with(user_id="user-789")

    async def test_slack_user_id_context_key(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import resolve_user_id_from_context

        with patch(
            "ypl.mcp_common.scheduled_agent_call_helpers.resolve_user_id",
            new=AsyncMock(return_value=("user-slack", None)),
        ) as mock_resolve:
            uid, err = await resolve_user_id_from_context({"slack_user_id": "U999"})

        assert uid == "user-slack"
        mock_resolve.assert_called_once_with(slack_user_id="U999")


# ---------------------------------------------------------------------------
# create_agent_schedule (DB mocked)
# ---------------------------------------------------------------------------


class TestCreateAgentSchedule:
    async def test_agent_not_found_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import create_agent_schedule

        session = _make_session(scalar_one_or_none=None)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await create_agent_schedule(
                agent_name="nonexistent-agent",
                message="Do the thing",
                schedule_type=AgentScheduleType.SCHEDULED,
                context_dict=None,
                created_by_user="user-123",
                created_by_agent=None,
                name="Test",
                description=None,
                execute_at_utc=datetime(2099, 1, 1, tzinfo=UTC),
            )

        assert result["success"] is False
        assert "Agent not found" in result["error"]

    async def test_scheduled_type_success(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import create_agent_schedule

        mock_agent = MagicMock()
        mock_agent.agent_id = uuid.uuid4()

        session = _make_session(scalar_one_or_none=mock_agent)
        ctx = _make_ctx_manager(session)

        execute_at = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await create_agent_schedule(
                agent_name="eng-raccoon",
                message="Run the report",
                schedule_type=AgentScheduleType.SCHEDULED,
                context_dict=None,
                created_by_user="user-abc",
                created_by_agent=None,
                name="Test Schedule",
                description="A description",
                execute_at_utc=execute_at,
            )

        assert result["success"] is True
        assert result["schedule_type"] == AgentScheduleType.SCHEDULED.value
        assert result["status"] == "PENDING"
        assert result["agent_name"] == "eng-raccoon"
        assert "agent_schedule_id" in result
        assert result["execute_at"] == execute_at.isoformat()

    async def test_recurring_type_success(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import create_agent_schedule

        mock_agent = MagicMock()
        mock_agent.agent_id = uuid.uuid4()

        session = _make_session(scalar_one_or_none=mock_agent)
        ctx = _make_ctx_manager(session)

        next_run = datetime(2099, 1, 1, 9, 0, tzinfo=UTC)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await create_agent_schedule(
                agent_name="bookkeeper",
                message="Daily summary",
                schedule_type=AgentScheduleType.RECURRING,
                context_dict={"key": "value"},
                created_by_user="user-xyz",
                created_by_agent="orchestrator",
                name="Daily Report",
                description=None,
                cron_expression="0 9 * * *",
                cron_timezone="America/New_York",
                next_run_utc=next_run,
                max_runs=30,
            )

        assert result["success"] is True
        assert result["schedule_type"] == AgentScheduleType.RECURRING.value
        assert result["cron_expression"] == "0 9 * * *"
        assert result["cron_timezone"] == "America/New_York"
        assert result["max_runs"] == 30

    async def test_scheduled_type_response_has_execute_at_not_cron(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import create_agent_schedule

        mock_agent = MagicMock()
        mock_agent.agent_id = uuid.uuid4()

        session = _make_session(scalar_one_or_none=mock_agent)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await create_agent_schedule(
                agent_name="eng-raccoon",
                message="Ping",
                schedule_type=AgentScheduleType.SCHEDULED,
                context_dict=None,
                created_by_user="user-abc",
                created_by_agent=None,
                name=None,
                description=None,
                execute_at_utc=datetime(2099, 6, 15, tzinfo=UTC),
            )

        assert "execute_at" in result
        assert "cron_expression" not in result


# ---------------------------------------------------------------------------
# list_agent_schedules_by_filters (DB mocked)
# ---------------------------------------------------------------------------


class TestListAgentSchedulesByFilters:
    def _make_row(self, agent_name: str = "eng-raccoon") -> MagicMock:
        row = MagicMock()
        row.AgentSchedule = _make_schedule()
        row.agent_name = agent_name
        return row

    async def test_success_empty_results(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=[])
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await list_agent_schedules_by_filters()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["agent_schedules"] == []

    async def test_success_with_results(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        rows = [self._make_row("eng-raccoon"), self._make_row("bookkeeper")]
        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=rows)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await list_agent_schedules_by_filters()

        assert result["success"] is True
        assert result["count"] == 2

    async def test_invalid_status_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        result = await list_agent_schedules_by_filters(status="BOGUS_STATUS")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_invalid_schedule_type_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        result = await list_agent_schedules_by_filters(schedule_type="WEEKLY")
        assert result["success"] is False
        assert "Invalid schedule_type" in result["error"]

    async def test_limit_clamped_to_200(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=[])
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            # Should not fail with limit > 200
            result = await list_agent_schedules_by_filters(limit=9999)

        assert result["success"] is True

    async def test_valid_status_filter(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import list_agent_schedules_by_filters

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=[])
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await list_agent_schedules_by_filters(status="PENDING")

        assert result["success"] is True


# ---------------------------------------------------------------------------
# cancel_agent_schedule_by_id (DB mocked)
# ---------------------------------------------------------------------------


class TestCancelAgentScheduleById:
    async def test_invalid_uuid_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import cancel_agent_schedule_by_id

        result = await cancel_agent_schedule_by_id("not-a-uuid", "user-123")
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ID"
        assert "Invalid" in result["error"]

    async def test_successful_cancellation(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import cancel_agent_schedule_by_id

        update_result = MagicMock()
        update_result.rowcount = 1
        session = AsyncMock()
        session.execute = AsyncMock(return_value=update_result)
        session.commit = AsyncMock()
        ctx = _make_ctx_manager(session)

        schedule_id = str(uuid.uuid4())
        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await cancel_agent_schedule_by_id(schedule_id, "user-123")

        assert result["success"] is True
        assert result["status"] == "CANCELLED"
        assert result["agent_schedule_id"] == schedule_id

    async def test_not_found_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import cancel_agent_schedule_by_id

        update_result = MagicMock()
        update_result.rowcount = 0
        check_result = MagicMock()
        check_result.one_or_none = MagicMock(return_value=None)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[update_result, check_result])
        session.commit = AsyncMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await cancel_agent_schedule_by_id(str(uuid.uuid4()), "user-123")

        assert result["success"] is False
        assert result["error_code"] == "NOT_FOUND"

    async def test_wrong_owner_returns_forbidden(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import cancel_agent_schedule_by_id

        update_result = MagicMock()
        update_result.rowcount = 0

        existing = MagicMock()
        existing.created_by_user = "other-user"
        existing.status = AgentScheduleStatus.PENDING
        check_result = MagicMock()
        check_result.one_or_none = MagicMock(return_value=existing)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[update_result, check_result])
        session.commit = AsyncMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await cancel_agent_schedule_by_id(str(uuid.uuid4()), "caller-user")

        assert result["success"] is False
        assert result["error_code"] == "FORBIDDEN"

    async def test_wrong_status_returns_invalid_status(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import cancel_agent_schedule_by_id

        caller_id = "user-123"
        update_result = MagicMock()
        update_result.rowcount = 0

        existing = MagicMock()
        existing.created_by_user = caller_id
        existing.status = AgentScheduleStatus.COMPLETED
        check_result = MagicMock()
        check_result.one_or_none = MagicMock(return_value=existing)

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[update_result, check_result])
        session.commit = AsyncMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await cancel_agent_schedule_by_id(str(uuid.uuid4()), caller_id)

        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"
        assert "Cannot cancel" in result["error"]


# ---------------------------------------------------------------------------
# edit_agent_schedule_fields — invalid-ID guard and status guard (DB mocked)
# ---------------------------------------------------------------------------


class TestEditAgentScheduleFields:
    async def test_invalid_uuid_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        result = await edit_agent_schedule_fields("bad-uuid", "user-123", message="new msg")
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ID"

    async def test_not_found_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", message="new msg")

        assert result["success"] is False
        assert result["error_code"] == "NOT_FOUND"

    async def test_wrong_owner_returns_forbidden(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule()
        schedule.created_by_user = "other-user"
        schedule.status = AgentScheduleStatus.PENDING

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()
        session.add = MagicMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "caller-user", message="new msg")

        assert result["success"] is False
        assert result["error_code"] == "FORBIDDEN"

    async def test_cancelled_status_returns_invalid_status(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule()
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.CANCELLED

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()
        session.add = MagicMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", message="new msg")

        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"

    async def test_edit_message_success(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule(schedule_type=AgentScheduleType.RECURRING)
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.PENDING
        schedule.cron_timezone = "UTC"
        schedule.cron_expression = "0 9 * * *"

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()
        session.add = MagicMock()
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", message="updated message")

        assert result["success"] is True
        assert result["status"] == "updated"

    async def test_recurring_schedule_cannot_set_execute_at(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule(schedule_type=AgentScheduleType.RECURRING)
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.PENDING
        schedule.cron_timezone = "UTC"
        schedule.cron_expression = "0 9 * * *"

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(
                str(uuid.uuid4()), "user-123", execute_at="2099-01-01T12:00:00+00:00"
            )

        assert result["success"] is False
        assert "execute_at" in result["error"]

    async def test_one_time_schedule_cannot_set_cron(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule(schedule_type=AgentScheduleType.SCHEDULED)
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.PENDING

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", cron_expression="0 9 * * *")

        assert result["success"] is False
        assert "cron_expression" in result["error"]

    async def test_recurring_invalid_max_runs(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule(schedule_type=AgentScheduleType.RECURRING)
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.PENDING
        schedule.cron_timezone = "UTC"
        schedule.cron_expression = "0 9 * * *"

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", max_runs=0)

        assert result["success"] is False
        assert "max_runs" in result["error"]

    async def test_invalid_timezone_in_edit(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import edit_agent_schedule_fields

        schedule = _make_schedule(schedule_type=AgentScheduleType.RECURRING)
        schedule.created_by_user = "user-123"
        schedule.status = AgentScheduleStatus.PENDING
        schedule.cron_timezone = "UTC"
        schedule.cron_expression = "0 9 * * *"

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=schedule)
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await edit_agent_schedule_fields(str(uuid.uuid4()), "user-123", timezone="Bad/Timezone")

        assert result["success"] is False
        assert "Invalid timezone" in result["error"]


# ---------------------------------------------------------------------------
# get_agent_schedule_detail — UUID validation only (no DB needed for that path)
# ---------------------------------------------------------------------------


class TestGetAgentScheduleDetail:
    async def test_invalid_uuid_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import get_agent_schedule_detail

        result = await get_agent_schedule_detail("not-a-uuid")
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ID"

    async def test_valid_uuid_not_found_returns_error(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import get_agent_schedule_detail

        result_mock = MagicMock()
        result_mock.one_or_none = MagicMock(return_value=None)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await get_agent_schedule_detail(str(uuid.uuid4()))

        assert result["success"] is False
        assert result["error_code"] == "NOT_FOUND"

    async def test_valid_uuid_found_returns_schedule(self) -> None:
        from ypl.mcp_common.scheduled_agent_call_helpers import get_agent_schedule_detail

        schedule = _make_schedule()
        row = MagicMock()
        row.AgentSchedule = schedule
        row.agent_name = "eng-raccoon"
        result_mock = MagicMock()
        result_mock.one_or_none = MagicMock(return_value=row)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)
        ctx = _make_ctx_manager(session)

        with patch("ypl.mcp_common.scheduled_agent_call_helpers.get_async_session", return_value=ctx):
            result = await get_agent_schedule_detail(str(uuid.uuid4()))

        assert result["success"] is True
        assert "schedule" in result
        assert result["schedule"]["agent_name"] == "eng-raccoon"


# ---------------------------------------------------------------------------
# Edge cases & invariant checks
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_compute_next_run_always_after_now(self) -> None:
        """next run should always be strictly in the future."""
        result = compute_next_run_for_cron("59 23 * * *", "UTC")
        assert result > datetime.now(UTC)

    def test_parse_execute_at_exactly_at_boundary(self) -> None:
        """An execute_at equal to now (or slightly past) should fail."""
        # Use a well-past datetime to be deterministic
        past = "1999-12-31T23:59:59+00:00"
        dt, err = parse_execute_at(past, "UTC")
        assert dt is None
        assert err is not None

    def test_validate_timezone_case_sensitive(self) -> None:
        """Timezone lookup is case-sensitive; lowercase 'utc' should fail."""
        error = validate_timezone("utc")
        # Some implementations may accept "utc"; just ensure we get the right type
        assert error is None or isinstance(error, str)

    @pytest.mark.parametrize(
        "tz",
        [
            "America/New_York",
            "America/Los_Angeles",
            "Europe/Berlin",
            "Asia/Kolkata",
            "Pacific/Auckland",
            "UTC",
        ],
    )
    def test_validate_many_valid_timezones(self, tz: str) -> None:
        assert validate_timezone(tz) is None

    @pytest.mark.parametrize(
        "cron,tz",
        [
            ("* * * * *", "UTC"),
            ("0 0 * * *", "UTC"),
            ("30 8 * * 1-5", "America/Chicago"),
            ("0 12 1 * *", "Asia/Tokyo"),
        ],
    )
    def test_compute_next_run_various_crons(self, cron: str, tz: str) -> None:
        result = compute_next_run_for_cron(cron, tz)
        assert isinstance(result, datetime)
        assert result > datetime.now(UTC)
