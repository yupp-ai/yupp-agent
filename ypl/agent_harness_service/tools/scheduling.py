"""Agent scheduling tools for the harness MCP server.

Provides tools for agents to schedule one-time and recurring agent calls.
"""

from __future__ import annotations
import uuid as _uuid
from typing import Any

from sqlmodel import select

from ypl.agent_harness_service.tools.mcp_instance import mcp
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import Agent, AgentScheduleType, AgentSession
from ypl.mcp_common.scheduled_agent_call_helpers import (
    compute_next_run_for_cron,
    create_agent_schedule,
    parse_execute_at,
    parse_schedule_context,
    resolve_user_id_from_context,
    validate_cron_expression,
    validate_timezone,
)
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _get_session_and_agent(session_id: str) -> tuple[AgentSession | None, Agent | None, str | None]:
    """Look up session and its agent. Returns (session, agent, error_message)."""
    try:
        session_uuid = _uuid.UUID(session_id)
    except ValueError:
        return None, None, f"Invalid session_id format: {session_id}"

    async with get_async_session() as db_session:
        result = await db_session.execute(select(AgentSession).where(AgentSession.agent_session_id == session_uuid))
        agent_session = result.scalar_one_or_none()
        if not agent_session:
            return None, None, f"Session not found: {session_id}"

        agent = await db_session.get(Agent, agent_session.agent_id)
        if not agent:
            return agent_session, None, f"Agent not found for session: {session_id}"

        return agent_session, agent, None


async def _resolve_creator_info(
    caller_session: AgentSession | None, caller_agent: Agent | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve creator user_id from session context and verify they are an authorized user.

    Returns (user_id, created_by_agent, error_message).
    If successful, error_message is None.
    """
    session_context = caller_session.context or {} if caller_session else {}
    created_by_agent = caller_agent.name if caller_agent else None

    # Resolve user identity to user_id and verify authorization
    user_id, error = await resolve_user_id_from_context(session_context)
    if error:
        return None, created_by_agent, error

    return user_id, created_by_agent, None


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="schedule_agent_call",
    description=(
        "Schedule a one-time agent call to execute at a specific future time. "
        "Use this to delay an agent invocation. The target agent will receive "
        "the message as a prompt when the scheduled time arrives. "
        "Requires your session_id (provided in the system prompt)."
    ),
)
async def schedule_agent_call(
    session_id: str,
    target_agent_name: str,
    message: str,
    execute_at: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Schedule a one-time agent call."""
    try:
        logger.info(
            "MCP tool: schedule_agent_call",
            session_id=session_id,
            target_agent_name=target_agent_name,
            execute_at=execute_at,
        )

        # Validate session and get caller agent info
        caller_session, caller_agent, error = await _get_session_and_agent(session_id)
        if error:
            return {"success": False, "error": error}

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

        # Resolve user identity and verify authorization
        created_by_user, created_by_agent, user_error = await _resolve_creator_info(caller_session, caller_agent)
        if user_error:
            return {"success": False, "error": user_error}

        return await create_agent_schedule(
            agent_name=target_agent_name,
            message=message,
            schedule_type=AgentScheduleType.SCHEDULED,
            cron_timezone=timezone,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            execute_at_utc=execute_at_utc,
        )
    except Exception as e:
        logger.error("schedule_agent_call failed", error=str(e), session_id=session_id, exc_info=True)
        return {"success": False, "error": f"Failed to create schedule: {e}"}


@mcp.tool(
    name="schedule_recurring_agent_call",
    description=(
        "Schedule a recurring agent call using a cron expression. "
        "Use this to set up periodic agent invocations (e.g., daily reports, weekly checks). "
        "The target agent will receive the message as a prompt at each scheduled time. "
        "Requires your session_id (provided in the system prompt)."
    ),
)
async def schedule_recurring_agent_call(
    session_id: str,
    target_agent_name: str,
    message: str,
    cron_expression: str,
    timezone: str = "UTC",
    context: str | None = None,
    name: str | None = None,
    description: str | None = None,
    max_runs: int | None = None,
) -> dict[str, Any]:
    """Schedule a recurring agent call."""
    try:
        logger.info(
            "MCP tool: schedule_recurring_agent_call",
            session_id=session_id,
            target_agent_name=target_agent_name,
            cron_expression=cron_expression,
        )

        # Validate session and get caller agent info
        caller_session, caller_agent, error = await _get_session_and_agent(session_id)
        if error:
            return {"success": False, "error": error}

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

        # Resolve user identity and verify authorization
        created_by_user, created_by_agent, user_error = await _resolve_creator_info(caller_session, caller_agent)
        if user_error:
            return {"success": False, "error": user_error}

        next_run_utc = compute_next_run_for_cron(cron_expression, timezone)

        return await create_agent_schedule(
            agent_name=target_agent_name,
            message=message,
            schedule_type=AgentScheduleType.RECURRING,
            cron_timezone=timezone,
            context_dict=context_dict,
            created_by_user=created_by_user,  # type: ignore[arg-type]
            created_by_agent=created_by_agent,
            name=name,
            description=description,
            cron_expression=cron_expression,
            next_run_utc=next_run_utc,
            max_runs=max_runs,
        )
    except Exception as e:
        logger.error("schedule_recurring_agent_call failed", error=str(e), session_id=session_id, exc_info=True)
        return {"success": False, "error": f"Failed to create schedule: {e}"}
