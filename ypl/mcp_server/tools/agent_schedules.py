"""MCP tools for agent scheduling and AHS agent management.

Provides tools for listing AHS agents, creating one-time and recurring
agent schedules, cancelling schedules, and listing schedules with filters.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlmodel import col, select

from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.utils.soul_utils import has_permission_cached
from ypl.db.agent_harness import (
    Agent,
    AgentSchedule,
    AgentScheduleStatus,
    AgentScheduleType,
)
from ypl.db.rbac import Permission
from ypl.mcp_common.scheduled_agent_call_helpers import (
    compute_next_run_for_cron,
    create_agent_schedule,
    edit_agent_schedule_fields,
    parse_execute_at,
    parse_schedule_context,
    resolve_user_id_from_email,
    resolve_yuppster_user_id,
    validate_cron_expression,
    validate_timezone,
)
from ypl.mcp_server.core import get_authenticated_user_email, get_requesting_user_id, mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()


# ============================================================================
# AHS Agent Tools
# ============================================================================


@mcp_server.tool(
    name="list_ahs_agents",
    description=(
        "List all configured agents in the Agent Harness Service (AHS). "
        "Returns agent names, display names, and descriptions from the database. "
        "Use this to discover which agents are available for task delegation."
    ),
)
@retry_db
async def list_ahs_agents() -> dict[str, Any]:
    """List all agents registered in AHS.

    Returns:
        Dict with list of agents (name, display_name, description).
    """
    from sqlmodel import select

    from ypl.db.agent_harness import Agent

    try:
        async with get_async_session_read_replica() as session:
            result = await session.exec(
                select(Agent).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
            )
            agents = [
                {
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "description": agent.description,
                }
                for agent in result.all()
            ]

        return {
            "success": True,
            "agent_count": len(agents),
            "agents": agents,
        }
    except Exception as e:
        logger.warning("Error listing AHS agents", error=str(e))
        return {"success": False, "error": str(e)}


# ============================================================================
# Agent Schedule Tools
# ============================================================================


@mcp_server.tool(
    name="create_agent_schedule",
    description=(
        "Create a one-time agent schedule to execute at a specific time. "
        "Use this to delay an agent invocation to a future time. "
        "The agent will receive the message as a prompt when the scheduled time arrives."
    ),
)
@retry_db
async def create_agent_schedule_tool(
    agent_name: str,
    message: str,
    execute_at: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
    created_by_agent: str | None = None,
) -> dict[str, Any]:
    """Schedule a one-time agent call."""
    try:
        # Validate timezone
        tz_error = validate_timezone(timezone)
        if tz_error:
            return {"success": False, "error": tz_error}

        # Parse execute_at
        execute_at_utc, exec_error = parse_execute_at(execute_at, timezone)
        if exec_error:
            return {"success": False, "error": exec_error}

        # Parse context
        context_dict, ctx_error = parse_schedule_context(context)
        if ctx_error:
            return {"success": False, "error": ctx_error}

        created_by_user = get_requesting_user_id()
        if not created_by_user:
            auth_email = get_authenticated_user_email()
            if auth_email == "unknown":
                return {"success": False, "error": "Authentication required to create agent schedules"}
            created_by_user, user_error = await resolve_yuppster_user_id(email=auth_email)
            if user_error:
                return {"success": False, "error": user_error}

        return await create_agent_schedule(
            agent_name=agent_name,
            message=message,
            schedule_type=AgentScheduleType.SCHEDULED,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            execute_at_utc=execute_at_utc,
        )

    except Exception as e:
        logger.error("Error creating agent schedule", error=str(e), agent_name=agent_name, exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="create_recurring_agent_schedule",
    description=(
        "Create a recurring agent schedule using a cron expression. "
        "Use this to set up periodic agent invocations (e.g., daily reports, weekly checks). "
        "The agent will receive the message as a prompt at each scheduled time."
    ),
)
@retry_db
async def create_recurring_agent_schedule_tool(
    agent_name: str,
    message: str,
    cron_expression: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
    max_runs: int | None = None,
    created_by_agent: str | None = None,
) -> dict[str, Any]:
    """Create a recurring agent schedule."""
    try:
        # Validate timezone
        tz_error = validate_timezone(timezone)
        if tz_error:
            return {"success": False, "error": tz_error}

        # Validate cron expression
        cron_error = validate_cron_expression(cron_expression, timezone)
        if cron_error:
            return {"success": False, "error": cron_error}

        if max_runs is not None and max_runs <= 0:
            return {"success": False, "error": "max_runs must be a positive integer"}

        # Parse context
        context_dict, ctx_error = parse_schedule_context(context)
        if ctx_error:
            return {"success": False, "error": ctx_error}

        created_by_user = get_requesting_user_id()
        if not created_by_user:
            auth_email = get_authenticated_user_email()
            if auth_email == "unknown":
                return {"success": False, "error": "Authentication required to create agent schedules"}
            created_by_user, user_error = await resolve_yuppster_user_id(email=auth_email)
            if user_error:
                return {"success": False, "error": user_error}

        next_run_utc = compute_next_run_for_cron(cron_expression, timezone)

        return await create_agent_schedule(
            agent_name=agent_name,
            message=message,
            schedule_type=AgentScheduleType.RECURRING,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            cron_expression=cron_expression,
            cron_timezone=timezone,
            next_run_utc=next_run_utc,
            max_runs=max_runs,
        )

    except Exception as e:
        logger.error("Error creating recurring agent schedule", error=str(e), agent_name=agent_name, exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="cancel_agent_schedule",
    description=(
        "Cancel an agent schedule. "
        "Can only cancel schedules that are in PENDING or PAUSED status. "
        "Cannot cancel schedules that are already IN_PROGRESS, COMPLETED, FAILED, or CANCELLED."
    ),
)
@retry_db
async def cancel_agent_schedule(
    agent_schedule_id: str,
) -> dict[str, Any]:
    """Cancel an agent schedule.

    Args:
        agent_schedule_id: UUID of the agent schedule to cancel

    Returns:
        Dictionary with the cancellation result
    """
    try:
        # Verify caller has USE_MCP permission
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required to cancel agent schedules"}

        if not await has_permission_cached(auth_email, Permission.USE_MCP):
            return {"success": False, "error": "You do not have permission to use MCP tools"}

        # Resolve email to user_id for ownership check
        caller_user_id, user_error = await resolve_user_id_from_email(auth_email)
        if user_error:
            return {"success": False, "error": user_error}

        # Parse UUID
        try:
            schedule_uuid = uuid.UUID(agent_schedule_id)
        except ValueError:
            return {"success": False, "error": f"Invalid agent_schedule_id format: {agent_schedule_id}"}

        async with get_async_session() as session:
            # Atomic conditional UPDATE with ownership check
            # Only cancels if status is PENDING or PAUSED AND caller owns the schedule
            result = await session.execute(
                sa.update(AgentSchedule)
                .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
                .where(col(AgentSchedule.deleted_at).is_(None))
                .where(col(AgentSchedule.status).in_([AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED]))
                .where(col(AgentSchedule.created_by_user) == caller_user_id)
                .values(status=AgentScheduleStatus.CANCELLED, modified_at=sa.func.now())
            )
            await session.commit()

            if result.rowcount == 0:  # type: ignore[attr-defined]
                # Check why it failed: not found, wrong status, or not owner
                check_result = await session.execute(
                    select(AgentSchedule.status, AgentSchedule.created_by_user)
                    .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
                    .where(col(AgentSchedule.deleted_at).is_(None))
                )
                existing = check_result.one_or_none()
                if existing is None:
                    return {"success": False, "error": f"Agent schedule not found: {agent_schedule_id}"}
                if existing.created_by_user != caller_user_id:
                    return {"success": False, "error": "You can only cancel schedules you created"}
                return {
                    "success": False,
                    "error": f"Cannot cancel schedule with status {existing.status.value}. "
                    f"Only PENDING or PAUSED schedules can be cancelled.",
                }

            logger.info(
                "Cancelled agent schedule",
                agent_schedule_id=agent_schedule_id,
                cancelled_by=auth_email,
                cancelled_by_user_id=caller_user_id,
            )

            return {
                "success": True,
                "agent_schedule_id": agent_schedule_id,
                "status": "CANCELLED",
            }

    except Exception as e:
        logger.error(
            "Error cancelling agent schedule", error=str(e), agent_schedule_id=agent_schedule_id, exc_info=True
        )
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="edit_agent_schedule",
    description=(
        "Edit an existing agent schedule. "
        "Can update the message/prompt, name, description, context, agent_name, "
        "and for recurring schedules: cron expression, timezone, and max_runs. "
        "For one-time schedules: execute_at and timezone. "
        "Only the creator can edit, and only PENDING or PAUSED schedules can be edited. "
        "Pass only the fields you want to change; omitted fields are left unchanged."
    ),
)
@retry_db
async def edit_agent_schedule(
    agent_schedule_id: str,
    message: str | None = None,
    name: str | None = None,
    description: str | None = None,
    context: str | None = None,
    cron_expression: str | None = None,
    timezone: str | None = None,
    max_runs: int | None = None,
    execute_at: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Edit an existing agent schedule.

    Args:
        agent_schedule_id: UUID of the agent schedule to edit
        message: New prompt message for the agent
        name: New schedule name
        description: New schedule description
        context: New context as JSON string (e.g. '{"key": "value"}')
        cron_expression: New cron expression (recurring schedules only)
        timezone: New IANA timezone for cron evaluation or execute_at interpretation
        max_runs: New max runs limit (recurring schedules only)
        execute_at: New execution time as ISO 8601 string (one-time schedules only).
            Interpreted in the schedule's timezone (or the timezone parameter if provided).
        agent_name: Change which agent runs on this schedule

    Returns:
        Dictionary with the edit result
    """
    try:
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required to edit agent schedules"}

        if not await has_permission_cached(auth_email, Permission.USE_MCP):
            return {"success": False, "error": "You do not have permission to use MCP tools"}

        caller_user_id, user_error = await resolve_user_id_from_email(auth_email)
        if user_error:
            return {"success": False, "error": user_error}

        # Parse context JSON string if provided
        context_dict: dict[str, Any] | None = None
        if context is not None:
            context_dict, ctx_error = parse_schedule_context(context)
            if ctx_error:
                return {"success": False, "error": ctx_error}

        result = await edit_agent_schedule_fields(
            agent_schedule_id=agent_schedule_id,
            caller_user_id=caller_user_id,  # type: ignore[arg-type]
            message=message,
            cron_expression=cron_expression,
            timezone=timezone,
            name=name,
            description=description,
            context=context_dict,
            max_runs=max_runs,
            execute_at=execute_at,
            agent_name=agent_name,
        )

        if not result.get("success"):
            return result

        logger.info(
            "Edited agent schedule via MCP",
            agent_schedule_id=agent_schedule_id,
            edited_by=auth_email,
        )

        return result

    except Exception as e:
        logger.error("Error editing agent schedule", error=str(e), agent_schedule_id=agent_schedule_id, exc_info=True)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="list_agent_schedules",
    description=(
        "List agent schedules with optional filters. "
        "Can filter by agent name, status, schedule type, or creator. "
        "Returns a list of schedules ordered by next_run_at."
    ),
)
@retry_db
async def list_agent_schedules(
    agent_name: str | None = None,
    status: str | None = None,
    schedule_type: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List agent schedules.

    Args:
        agent_name: Optional filter by agent name
        status: Optional filter by status (PENDING, IN_PROGRESS, COMPLETED, FAILED, CANCELLED, PAUSED)
        schedule_type: Optional filter by schedule type (SCHEDULED, RECURRING)
        created_by: Optional filter by creator email address
        limit: Maximum number of results (default: 50, max: 200)

    Returns:
        Dictionary with list of agent schedules
    """
    try:
        # Validate limit bounds
        if limit <= 0:
            return {"success": False, "error": "limit must be between 1 and 200"}
        limit = min(limit, 200)

        # Verify caller has USE_MCP permission
        auth_email = get_authenticated_user_email()
        if auth_email == "unknown":
            return {"success": False, "error": "Authentication required to list agent schedules"}

        if not await has_permission_cached(auth_email, Permission.USE_MCP):
            return {"success": False, "error": "You do not have permission to use MCP tools"}

        # Default to caller's schedules if no created_by filter provided
        if not created_by:
            created_by = auth_email

        # Validate status if provided
        if status:
            try:
                status_enum = AgentScheduleStatus(status)
            except ValueError:
                valid_statuses = [s.value for s in AgentScheduleStatus]
                return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid_statuses}"}
        else:
            status_enum = None

        # Validate schedule_type if provided
        if schedule_type:
            try:
                schedule_type_enum = AgentScheduleType(schedule_type)
            except ValueError:
                valid_types = [t.value for t in AgentScheduleType]
                return {
                    "success": False,
                    "error": f"Invalid schedule_type: {schedule_type}. Must be one of: {valid_types}",
                }
        else:
            schedule_type_enum = None

        # Resolve created_by email to user_id
        created_by_user_id: str | None = None
        if created_by:
            created_by_user_id, resolve_error = await resolve_user_id_from_email(created_by)
            if resolve_error:
                return {"success": False, "error": f"Invalid created_by filter: {resolve_error}"}

        async with get_async_session_read_replica() as session:
            # Build query
            query: Any = (
                select(AgentSchedule, col(Agent.name).label("agent_name"))
                .join(Agent, col(AgentSchedule.agent_id) == col(Agent.agent_id))
                .where(AgentSchedule.deleted_at.is_(None))  # type: ignore[union-attr]
            )

            # Apply filters
            if agent_name:
                query = query.where(Agent.name == agent_name)
            if status_enum:
                query = query.where(AgentSchedule.status == status_enum)
            if schedule_type_enum:
                query = query.where(AgentSchedule.schedule_type == schedule_type_enum)
            if created_by_user_id:
                query = query.where(AgentSchedule.created_by_user == created_by_user_id)

            # Order by next_run_at (nulls last)
            query = query.order_by(sa.nullslast(col(AgentSchedule.next_run_at))).limit(limit)

            result = await session.execute(query)
            rows = result.all()

            agent_schedules = []
            for row in rows:
                schedule: AgentSchedule = row.AgentSchedule
                ag_name: str = row.agent_name
                agent_schedules.append(
                    {
                        "agent_schedule_id": str(schedule.agent_schedule_id),
                        "agent_name": ag_name,
                        "schedule_type": schedule.schedule_type.value,
                        "status": schedule.status.value,
                        "message": schedule.message[:100] + "..." if len(schedule.message) > 100 else schedule.message,
                        "name": schedule.name,
                        "execute_at": schedule.execute_at.isoformat() if schedule.execute_at else None,
                        "cron_expression": schedule.cron_expression,
                        "cron_timezone": schedule.cron_timezone,
                        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
                        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
                        "run_count": schedule.run_count,
                        "max_runs": schedule.max_runs,
                        "created_by_user": schedule.created_by_user,
                        "created_by_agent": schedule.created_by_agent,
                        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
                    }
                )

            logger.info(
                "Listed agent schedules",
                count=len(agent_schedules),
                filters={
                    "agent_name": agent_name,
                    "status": status,
                    "schedule_type": schedule_type,
                    "created_by": created_by,
                },
            )

            return {
                "success": True,
                "count": len(agent_schedules),
                "agent_schedules": agent_schedules,
            }

    except Exception as e:
        logger.error("Error listing agent schedules", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}
