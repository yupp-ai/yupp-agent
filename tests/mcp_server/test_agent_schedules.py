"""Unit tests for ypl/mcp_server/tools/agent_schedules.py.

Covers:
- list_ahs_agents: success (returns agents), database error
- create_agent_schedule_tool: timezone validation error, execute_at parse error,
  context parse error, no auth (unknown email), resolved user_id, success
- create_recurring_agent_schedule_tool: timezone error, cron error, max_runs ≤ 0,
  context parse error, no auth, success
- cancel_agent_schedule: unauthenticated, no USE_MCP permission, invalid UUID,
  successful cancel, not found, wrong owner, wrong status
- list_agent_schedules: limit ≤ 0, unauthenticated, no USE_MCP permission,
  invalid status, invalid schedule_type, success (returns schedules), DB error

All external dependencies (DB sessions, permission checks, helper functions)
are mocked — no live database required.
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
import ypl.mcp_server.tools.agent_schedules as _sched_mod

list_ahs_agents = _sched_mod.list_ahs_agents.fn
create_agent_schedule_tool = _sched_mod.create_agent_schedule_tool.fn
create_recurring_agent_schedule_tool = _sched_mod.create_recurring_agent_schedule_tool.fn
cancel_agent_schedule = _sched_mod.cancel_agent_schedule.fn
edit_agent_schedule = _sched_mod.edit_agent_schedule.fn
list_agent_schedules = _sched_mod.list_agent_schedules.fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_row(
    name: str = "eng-raccoon",
    display_name: str = "Engineering Raccoon",
    description: str = "A test agent",
) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    agent.display_name = display_name
    agent.description = description
    return agent


def _make_schedule_row(
    schedule_id: str | None = None,
    agent_name: str = "eng-raccoon",
    schedule_type: str = "SCHEDULED",
    status: str = "PENDING",
    message: str = "Do the thing",
) -> MagicMock:
    from ypl.db.agent_harness import AgentScheduleStatus, AgentScheduleType

    row = MagicMock()
    schedule = MagicMock()
    schedule.agent_schedule_id = uuid.UUID(schedule_id or str(uuid.uuid4()))
    schedule.schedule_type = AgentScheduleType(schedule_type)
    schedule.status = AgentScheduleStatus(status)
    schedule.message = message
    schedule.name = "Test Schedule"
    schedule.execute_at = datetime(2024, 1, 1, tzinfo=UTC)
    schedule.cron_expression = None
    schedule.cron_timezone = None
    schedule.next_run_at = datetime(2024, 1, 1, tzinfo=UTC)
    schedule.last_run_at = None
    schedule.run_count = 0
    schedule.max_runs = None
    schedule.created_by_user = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    schedule.created_by_agent = None
    schedule.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    row.AgentSchedule = schedule
    row.agent_name = agent_name
    return row


def _ctx_manager_session(exec_result: Any = None, all_result: Any = None) -> MagicMock:
    """Create an async context manager mock for get_async_session / get_async_session_read_replica."""
    session = AsyncMock()
    if exec_result is not None:
        session.execute = AsyncMock(return_value=exec_result)
    if all_result is not None:
        result = MagicMock()
        result.all = MagicMock(return_value=all_result)
        session.exec = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# list_ahs_agents
# ---------------------------------------------------------------------------


class TestListAhsAgents:
    async def test_success_returns_agents(self) -> None:
        agents = [_make_agent_row("eng-raccoon"), _make_agent_row("bookkeeper")]
        ctx = _ctx_manager_session(all_result=agents)

        with patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx):
            result = await list_ahs_agents()

        assert result["success"] is True
        assert result["agent_count"] == 2
        names = [a["name"] for a in result["agents"]]
        assert "eng-raccoon" in names
        assert "bookkeeper" in names

    async def test_db_error_returns_failure(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("DB connection failed"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx):
            result = await list_ahs_agents()

        assert result["success"] is False
        assert "DB connection failed" in result["error"]


# ---------------------------------------------------------------------------
# create_agent_schedule_tool
# ---------------------------------------------------------------------------


class TestCreateAgentScheduleTool:
    async def test_invalid_timezone_returns_error(self) -> None:
        with patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value="Invalid timezone: Blarg"):
            result = await create_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Do thing",
                execute_at="2024-01-01T12:00:00",
                timezone="Blarg",
            )

        assert result["success"] is False
        assert "Invalid timezone" in result["error"]

    async def test_invalid_execute_at_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_execute_at", return_value=(None, "Bad date format")),
        ):
            result = await create_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Do thing",
                execute_at="not-a-date",
            )

        assert result["success"] is False
        assert "Bad date format" in result["error"]

    async def test_invalid_context_returns_error(self) -> None:
        execute_at_utc = datetime(2024, 6, 1, tzinfo=UTC)
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_execute_at", return_value=(execute_at_utc, None)),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=(None, "Invalid JSON")),
        ):
            result = await create_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Do thing",
                execute_at="2024-06-01T12:00:00",
                context="{bad json",
            )

        assert result["success"] is False
        assert "Invalid JSON" in result["error"]

    async def test_unauthenticated_returns_error(self) -> None:
        execute_at_utc = datetime(2024, 6, 1, tzinfo=UTC)
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_execute_at", return_value=(execute_at_utc, None)),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=({}, None)),
            patch(
                "ypl.mcp_server.tools.agent_schedules.require_caller_user_id",
                side_effect=PermissionError("Authentication required"),
            ),
        ):
            result = await create_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Do thing",
                execute_at="2024-06-01T12:00:00",
            )

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_success_with_requesting_user_id(self) -> None:
        execute_at_utc = datetime(2024, 6, 1, tzinfo=UTC)
        user_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        expected_result = {"success": True, "agent_schedule_id": "sched-123"}

        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_execute_at", return_value=(execute_at_utc, None)),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=({}, None)),
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=user_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=user_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.create_agent_schedule",
                new=AsyncMock(return_value=expected_result),
            ),
        ):
            result = await create_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Do thing",
                execute_at="2024-06-01T12:00:00",
            )

        assert result["success"] is True
        assert result["agent_schedule_id"] == "sched-123"


# ---------------------------------------------------------------------------
# create_recurring_agent_schedule_tool
# ---------------------------------------------------------------------------


class TestCreateRecurringAgentScheduleTool:
    async def test_invalid_timezone_returns_error(self) -> None:
        with patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value="Bad timezone"):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="0 9 * * *",
                timezone="BadZone",
            )

        assert result["success"] is False

    async def test_invalid_cron_expression(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.validate_cron_expression", return_value="Invalid cron: bad"),
        ):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="bad cron",
            )

        assert result["success"] is False
        assert "Invalid cron" in result["error"]

    async def test_max_runs_zero_or_negative_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.validate_cron_expression", return_value=None),
        ):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="0 9 * * *",
                max_runs=0,
            )

        assert result["success"] is False
        assert "max_runs" in result["error"]

    async def test_invalid_context_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.validate_cron_expression", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=(None, "Bad context")),
        ):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="0 9 * * *",
                context="{bad",
            )

        assert result["success"] is False

    async def test_unauthenticated_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.validate_cron_expression", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=({}, None)),
            patch(
                "ypl.mcp_server.tools.agent_schedules.require_caller_user_id",
                side_effect=PermissionError("Authentication required"),
            ),
        ):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="0 9 * * *",
            )

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_success(self) -> None:
        user_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        next_run = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
        expected = {"success": True, "agent_schedule_id": "recurring-123"}

        with (
            patch("ypl.mcp_server.tools.agent_schedules.validate_timezone", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.validate_cron_expression", return_value=None),
            patch("ypl.mcp_server.tools.agent_schedules.parse_schedule_context", return_value=({}, None)),
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=user_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=user_id),
            patch("ypl.mcp_server.tools.agent_schedules.compute_next_run_for_cron", return_value=next_run),
            patch("ypl.mcp_server.tools.agent_schedules.create_agent_schedule", new=AsyncMock(return_value=expected)),
        ):
            result = await create_recurring_agent_schedule_tool(
                agent_name="eng-raccoon",
                message="Daily report",
                cron_expression="0 9 * * *",
                max_runs=5,
            )

        assert result["success"] is True


# ---------------------------------------------------------------------------
# cancel_agent_schedule
# ---------------------------------------------------------------------------


class TestCancelAgentSchedule:
    """The MCP tool delegates DB work to cancel_agent_schedule_by_id; these
    tests cover the tool's contract (auth, permission, admin bypass, caller
    resolution). The helper itself has its own tests in
    test_scheduled_agent_call_helpers.py."""

    async def test_unauthenticated_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.agent_schedules.require_caller_user_id",
            side_effect=PermissionError("Authentication required"),
        ):
            result = await cancel_agent_schedule(str(uuid.uuid4()))

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_no_permission_returns_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await cancel_agent_schedule(str(uuid.uuid4()))

        assert result["success"] is False
        assert "permission" in result["error"].lower()

    async def test_delegates_to_helper_as_non_admin(self) -> None:
        """Non-admin caller: MCP tool calls helper with allow_any_owner=False."""
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        schedule_id = str(uuid.uuid4())
        helper = AsyncMock(return_value={"success": True, "agent_schedule_id": schedule_id, "status": "CANCELLED"})

        # has_permission_by_user_id_cached is called twice: once for USE_MCP
        # (True) and once for MANAGE_AGENT_SCHEDULES (False).
        permission_mock = AsyncMock(side_effect=[True, False])
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=permission_mock,
            ),
            patch("ypl.mcp_server.tools.agent_schedules.cancel_agent_schedule_by_id", new=helper),
        ):
            result = await cancel_agent_schedule(schedule_id)

        assert result["success"] is True
        helper.assert_awaited_once()
        assert helper.await_args is not None
        kwargs = helper.await_args.kwargs
        assert kwargs["caller_user_id"] == caller_id
        assert kwargs["allow_any_owner"] is False

    async def test_admin_bypass_passes_allow_any_owner(self) -> None:
        """Caller holding MANAGE_AGENT_SCHEDULES passes allow_any_owner=True
        so the helper can cancel a schedule owned by another user."""
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        schedule_id = str(uuid.uuid4())
        helper = AsyncMock(return_value={"success": True, "agent_schedule_id": schedule_id, "status": "CANCELLED"})

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),  # USE_MCP and MANAGE_AGENT_SCHEDULES both granted
            ),
            patch("ypl.mcp_server.tools.agent_schedules.cancel_agent_schedule_by_id", new=helper),
        ):
            result = await cancel_agent_schedule(schedule_id)

        assert result["success"] is True
        assert helper.await_args is not None
        kwargs = helper.await_args.kwargs
        assert kwargs["allow_any_owner"] is True


# ---------------------------------------------------------------------------
# edit_agent_schedule
# ---------------------------------------------------------------------------


class TestEditAgentSchedule:
    async def test_unauthenticated_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.agent_schedules.require_caller_user_id",
            side_effect=PermissionError("Authentication required"),
        ):
            result = await edit_agent_schedule(str(uuid.uuid4()), message="new msg")

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_no_permission_returns_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await edit_agent_schedule(str(uuid.uuid4()), message="new msg")

        assert result["success"] is False
        assert "permission" in result["error"].lower()

    async def test_caller_user_id_passed_to_editor(self) -> None:
        """The MCP tool threads the caller's user_id to the edit helper."""
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        schedule_id = str(uuid.uuid4())
        editor = AsyncMock(return_value={"success": True, "agent_schedule_id": schedule_id})

        # USE_MCP True, MANAGE_AGENT_SCHEDULES False
        permission_mock = AsyncMock(side_effect=[True, False])
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=permission_mock,
            ),
            patch("ypl.mcp_server.tools.agent_schedules.edit_agent_schedule_fields", new=editor),
        ):
            result = await edit_agent_schedule(schedule_id, message="updated message")

        assert result["success"] is True
        editor.assert_awaited_once()
        assert editor.await_args is not None
        call_kwargs = editor.await_args.kwargs
        assert call_kwargs["caller_user_id"] == caller_id
        assert call_kwargs["message"] == "updated message"
        assert call_kwargs["allow_any_owner"] is False

    async def test_admin_passes_allow_any_owner(self) -> None:
        """Admin holding MANAGE_AGENT_SCHEDULES can edit any user's schedule."""
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        schedule_id = str(uuid.uuid4())
        editor = AsyncMock(return_value={"success": True, "agent_schedule_id": schedule_id})

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),  # USE_MCP and MANAGE_AGENT_SCHEDULES both granted
            ),
            patch("ypl.mcp_server.tools.agent_schedules.edit_agent_schedule_fields", new=editor),
        ):
            result = await edit_agent_schedule(schedule_id, name="new name")

        assert result["success"] is True
        assert editor.await_args is not None
        call_kwargs = editor.await_args.kwargs
        assert call_kwargs["allow_any_owner"] is True

    async def test_invalid_context_json_returns_error(self) -> None:
        """Context parse error is returned without calling the edit helper."""
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        editor = AsyncMock()

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
            patch("ypl.mcp_server.tools.agent_schedules.edit_agent_schedule_fields", new=editor),
        ):
            result = await edit_agent_schedule(str(uuid.uuid4()), context="{not: valid json")

        assert result["success"] is False
        editor.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_agent_schedules
# ---------------------------------------------------------------------------


class TestListAgentSchedules:
    async def test_limit_zero_returns_error(self) -> None:
        result = await list_agent_schedules(limit=0)

        assert result["success"] is False
        assert "limit" in result["error"].lower()

    async def test_unauthenticated_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.agent_schedules.require_caller_user_id",
            side_effect=PermissionError("Authentication required"),
        ):
            result = await list_agent_schedules()

        assert result["success"] is False
        assert "Authentication required" in result["error"]

    async def test_no_permission_returns_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=False),
            ),
        ):
            result = await list_agent_schedules()

        assert result["success"] is False
        assert "permission" in result["error"].lower()

    async def test_invalid_status_returns_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await list_agent_schedules(status="BOGUS")

        assert result["success"] is False
        assert "Invalid status" in result["error"]

    async def test_invalid_schedule_type_returns_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await list_agent_schedules(schedule_type="WEEKLY")

        assert result["success"] is False
        assert "Invalid schedule_type" in result["error"]

    async def test_success_returns_schedules(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        rows = [_make_schedule_row(), _make_schedule_row(agent_name="bookkeeper")]

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=rows)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
            patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_agent_schedules()

        assert result["success"] is True
        assert result["count"] == 2

    async def test_message_truncated_at_100_chars(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        long_message = "A" * 150
        rows = [_make_schedule_row(message=long_message)]

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=rows)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
            patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_agent_schedules()

        assert result["success"] is True
        msg = result["agent_schedules"][0]["message"]
        assert msg.endswith("...")
        assert len(msg) == 103  # 100 + "..."

    async def test_created_by_filter_resolve_error(self) -> None:
        caller_id = str(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "ypl.mcp_server.tools.agent_schedules.resolve_user_id_from_email",
                new=AsyncMock(return_value=(None, "Not found")),
            ),
        ):
            result = await list_agent_schedules(created_by="other@example.com")

        assert result["success"] is False
        assert "Invalid created_by filter" in result["error"]

    async def test_default_filters_by_caller_user_id(self) -> None:
        """When no ``created_by`` is passed, the list query filters by the
        caller's own user_id — the typed RequestContext provides it directly."""
        caller_id = str(uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))
        rows = [_make_schedule_row(), _make_schedule_row(agent_name="bookkeeper")]

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=rows)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),
            ),
            patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_agent_schedules()

        assert result["success"] is True
        assert result["count"] == 2

    async def test_explicit_created_by_requires_admin_permission(self) -> None:
        """Non-admin caller passing created_by=<someone_else@email> is rejected.

        Under the new policy, listing another user's schedules requires
        MANAGE_AGENT_SCHEDULES — a regular user with USE_MCP can only list
        their own.
        """
        caller_id = str(uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"))
        other_user_id = str(uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))

        # USE_MCP True, MANAGE_AGENT_SCHEDULES False
        permission_mock = AsyncMock(side_effect=[True, False])
        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=permission_mock,
            ),
            patch(
                "ypl.mcp_server.tools.agent_schedules.resolve_user_id_from_email",
                new=AsyncMock(return_value=(other_user_id, None)),
            ),
        ):
            result = await list_agent_schedules(created_by="other@example.com")

        assert result["success"] is False
        assert "MANAGE_AGENT_SCHEDULES" in result["error"]

    async def test_admin_can_list_another_users_schedules(self) -> None:
        """Admin holding MANAGE_AGENT_SCHEDULES can list any user's schedules
        when they pass created_by=<email>."""
        caller_id = str(uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"))
        other_user_id = str(uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))
        rows = [_make_schedule_row()]

        exec_result = MagicMock()
        exec_result.all = MagicMock(return_value=rows)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=exec_result)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_schedules.require_caller_user_id", return_value=caller_id),
            patch("ypl.mcp_server.tools.agent_schedules.require_principal_user_id", return_value=caller_id),
            patch(
                "ypl.mcp_server.tools.agent_schedules.has_permission_by_user_id_cached",
                new=AsyncMock(return_value=True),  # USE_MCP and MANAGE_AGENT_SCHEDULES both granted
            ),
            patch(
                "ypl.mcp_server.tools.agent_schedules.resolve_user_id_from_email",
                new=AsyncMock(return_value=(other_user_id, None)),
            ),
            patch("ypl.mcp_server.tools.agent_schedules.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_agent_schedules(created_by="other@example.com")

        assert result["success"] is True
        assert result["count"] == 1
