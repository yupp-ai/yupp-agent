"""Service layer for agent schedule REST endpoints.

Delegates to shared helpers in ypl.mcp_common.scheduled_agent_call_helpers.
"""

from typing import Any

from ypl.backend.db import retry_db
from ypl.db.agent_harness import AgentScheduleType
from ypl.mcp_common.scheduled_agent_call_helpers import (
    cancel_agent_schedule_by_id,
    compute_next_run_for_cron,
    create_agent_schedule,
    edit_agent_schedule_fields,
    get_agent_schedule_detail,
    list_agent_schedules_by_filters,
    parse_execute_at,
    trigger_agent_schedule_now,
    validate_cron_expression,
    validate_timezone,
)


@retry_db
async def create_one_time_schedule(
    agent_name: str,
    message: str,
    execute_at: str,
    timezone: str,
    created_by_user: str,
    context: dict[str, Any] | None = None,
    name: str | None = None,
    description: str | None = None,
    created_by_agent: str | None = None,
) -> dict[str, Any]:
    """Create a one-time agent schedule."""
    tz_error = validate_timezone(timezone)
    if tz_error:
        return {"success": False, "error": tz_error}

    execute_at_utc, exec_error = parse_execute_at(execute_at, timezone)
    if exec_error:
        return {"success": False, "error": exec_error}

    return await create_agent_schedule(
        agent_name=agent_name,
        message=message,
        schedule_type=AgentScheduleType.SCHEDULED,
        context_dict=context,
        created_by_user=created_by_user,
        created_by_agent=created_by_agent,
        name=name,
        description=description,
        execute_at_utc=execute_at_utc,
    )


@retry_db
async def create_recurring_schedule(
    agent_name: str,
    message: str,
    cron_expression: str,
    timezone: str,
    created_by_user: str,
    context: dict[str, Any] | None = None,
    name: str | None = None,
    description: str | None = None,
    max_runs: int | None = None,
    created_by_agent: str | None = None,
) -> dict[str, Any]:
    """Create a recurring agent schedule."""
    tz_error = validate_timezone(timezone)
    if tz_error:
        return {"success": False, "error": tz_error}

    cron_error = validate_cron_expression(cron_expression, timezone)
    if cron_error:
        return {"success": False, "error": cron_error}

    if max_runs is not None and max_runs <= 0:
        return {"success": False, "error": "max_runs must be a positive integer"}

    next_run_utc = compute_next_run_for_cron(cron_expression, timezone)

    return await create_agent_schedule(
        agent_name=agent_name,
        message=message,
        schedule_type=AgentScheduleType.RECURRING,
        context_dict=context,
        created_by_user=created_by_user,
        created_by_agent=created_by_agent,
        name=name,
        description=description,
        cron_expression=cron_expression,
        cron_timezone=timezone,
        next_run_utc=next_run_utc,
        max_runs=max_runs,
    )


@retry_db
async def get_schedule_detail(agent_schedule_id: str) -> dict[str, Any]:
    """Get a single schedule by ID."""
    return await get_agent_schedule_detail(agent_schedule_id)


@retry_db
async def list_schedules(
    agent_name: str | None = None,
    status: str | None = None,
    schedule_type: str | None = None,
    created_by_user: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List schedules with optional filters."""
    return await list_agent_schedules_by_filters(
        agent_name=agent_name,
        status=status,
        schedule_type=schedule_type,
        created_by_user_id=created_by_user,
        limit=limit,
    )


@retry_db
async def edit_schedule(
    agent_schedule_id: str,
    caller_user_id: str,
    message: str | None = None,
    cron_expression: str | None = None,
    timezone: str | None = None,
    name: str | None = None,
    description: str | None = None,
    context: dict[str, Any] | None = None,
    max_runs: int | None = None,
    execute_at: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Edit an existing schedule."""
    return await edit_agent_schedule_fields(
        agent_schedule_id=agent_schedule_id,
        caller_user_id=caller_user_id,
        message=message,
        cron_expression=cron_expression,
        timezone=timezone,
        name=name,
        description=description,
        context=context,
        max_runs=max_runs,
        execute_at=execute_at,
        agent_name=agent_name,
    )


@retry_db
async def trigger_schedule(agent_schedule_id: str, caller_user_id: str) -> dict[str, Any]:
    """Trigger a recurring schedule to run immediately."""
    return await trigger_agent_schedule_now(agent_schedule_id, caller_user_id)


@retry_db
async def cancel_schedule(agent_schedule_id: str, caller_user_id: str) -> dict[str, Any]:
    """Cancel an agent schedule."""
    return await cancel_agent_schedule_by_id(agent_schedule_id, caller_user_id)
