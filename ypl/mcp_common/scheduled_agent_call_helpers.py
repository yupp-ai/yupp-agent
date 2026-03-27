"""Shared helper functions for agent schedules.

Used by both yuppster MCP server, AHS local MCP server, and AHS REST endpoints.
"""

import json
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from croniter import croniter  # type: ignore[import-untyped,unused-ignore]
from sqlalchemy import func
from sqlmodel import col, select

from ypl.backend.db import get_async_session, retry_db
from ypl.backend.utils.slack_utils import get_user_email_from_slack
from ypl.db.agent_harness import (
    Agent,
    AgentSchedule,
    AgentScheduleStatus,
    AgentScheduleType,
)
from ypl.db.users import User, UserRole
from ypl.structured_logger import get_logger

logger = get_logger()


async def resolve_user_id_from_email(email: str) -> tuple[str | None, str | None]:
    """Resolve email to user_id.

    Args:
        email: User email to look up (case-insensitive)

    Returns:
        (user_id, error_message) tuple. If successful, error_message is None.
        If failed, user_id is None and error_message describes the issue.
    """
    async with get_async_session() as session:
        result = await session.execute(select(User).where(func.lower(User.email) == func.lower(email)))
        user = result.scalar_one_or_none()

        if not user:
            return None, f"User not found: {email}"

        if user.deleted_at is not None:
            return None, f"User account is deleted: {user.user_id}"

        return user.user_id, None


@retry_db
async def resolve_email_from_user_id(user_id: str) -> str | None:
    """Resolve user_id to email.

    Args:
        user_id: User ID to look up

    Returns:
        The user's email, or None if not found or user is deleted.
    """
    async with get_async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if not user or user.deleted_at is not None:
            return None
        if user.email:
            return str(user.email)
        return None


async def resolve_yuppster_user_id(
    email: str | None = None,
    user_id: str | None = None,
    slack_user_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve user identity to user_id and verify they are a Yuppster.

    These three identifiers are mutually exclusive - pass exactly ONE:
    - email: Direct email lookup (most common)
    - user_id: Direct database ID lookup
    - slack_user_id: Resolves via Slack API to get email, then looks up user

    When multiple are provided, priority is: slack_user_id -> email -> user_id.

    Args:
        email: User email to look up (case-insensitive)
        user_id: User ID to look up directly
        slack_user_id: Slack user ID to resolve via Slack API

    Returns:
        (user_id, error_message) tuple. If successful, error_message is None.
        If failed, user_id is None and error_message describes the issue.
    """
    # If slack_user_id provided, resolve to email first
    if slack_user_id and not email:
        try:
            resolved_email = await get_user_email_from_slack(slack_user_id)
            if resolved_email:
                email = resolved_email
            else:
                return None, f"Could not resolve Slack user ID to email: {slack_user_id}"
        except Exception as e:
            logger.warning(
                "Failed to resolve slack_user_id to email",
                slack_user_id=slack_user_id,
                error=str(e),
            )
            return None, f"Failed to resolve Slack user ID: {e}"

    if not email and not user_id:
        return None, "No user identifier provided (email, user_id, or slack_user_id required)"

    async with get_async_session() as session:
        if user_id:
            # Direct lookup by user_id
            user = await session.get(User, user_id)
        else:
            # Lookup by email (case-insensitive)
            result = await session.execute(select(User).where(func.lower(User.email) == func.lower(email)))
            user = result.scalar_one_or_none()

        if not user:
            identifier = user_id or email
            return None, f"User not found: {identifier}"

        if user.deleted_at is not None:
            return None, f"User account is deleted: {user.user_id}"

        # Check if user is a Yuppster
        if UserRole.YUPPSTER not in (user.role or []):
            return None, f"User is not a Yuppster: {user.user_id}"

        return user.user_id, None


async def resolve_yuppster_from_context(context: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Resolve user identity from session context and verify they are a Yuppster.

    Checks context keys in order: slack_user_email, user_email, user_id, slack_user_id.
    For slack_user_id, calls Slack API to resolve the email first.

    Args:
        context: Session context dict with user identity fields

    Returns:
        (user_id, error_message) tuple. If successful, error_message is None.
    """
    if not context:
        return None, "No session context provided"

    # Try email-based lookup first (most reliable)
    email = context.get("slack_user_email") or context.get("user_email")
    if email:
        return await resolve_yuppster_user_id(email=email)

    # Try direct user_id lookup
    uid = context.get("user_id")
    if uid:
        return await resolve_yuppster_user_id(user_id=uid)

    # Try slack_user_id (requires API call)
    slack_uid = context.get("slack_user_id")
    if slack_uid:
        return await resolve_yuppster_user_id(slack_user_id=slack_uid)

    return None, "No valid user identifier in session context (need email, user_id, or slack_user_id)"


def validate_timezone(tz: str) -> str | None:
    """Validate IANA timezone. Returns error message if invalid, None if valid."""
    try:
        ZoneInfo(tz)
        return None
    except ZoneInfoNotFoundError:
        return f"Invalid timezone: {tz}. Must be a valid IANA timezone (e.g., 'America/New_York', 'UTC')."


def compute_next_run_for_cron(cron_expression: str, tz: str) -> datetime:
    """Compute the next run time for a cron expression in the given timezone."""
    tz_obj = ZoneInfo(tz)
    now = datetime.now(tz_obj)
    cron = croniter(cron_expression, now)
    next_run_local: datetime = cron.get_next(datetime)
    return next_run_local.astimezone(ZoneInfo("UTC"))


def parse_schedule_context(context: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse context JSON string. Returns (context_dict, error_message)."""
    if not context:
        return None, None
    try:
        context_dict = json.loads(context)
        if not isinstance(context_dict, dict):
            return None, "context must be a JSON object"
        return context_dict, None
    except json.JSONDecodeError as e:
        return None, f"Invalid context JSON: {e}"


def validate_cron_expression(cron_expression: str, timezone: str) -> str | None:
    """Validate cron expression. Returns error message if invalid, None if valid."""
    try:
        tz_obj = ZoneInfo(timezone)
        now = datetime.now(tz_obj)
        croniter(cron_expression, now)
        return None
    except (ValueError, KeyError) as e:
        return f"Invalid cron expression: {e}"


def parse_execute_at(execute_at: str, timezone: str) -> tuple[datetime | None, str | None]:
    """Parse execute_at ISO-8601 string. Returns (datetime_utc, error_message)."""
    try:
        execute_at_dt = datetime.fromisoformat(execute_at)
    except ValueError as e:
        return None, f"Invalid execute_at format: {e}. Use ISO-8601 format."

    if execute_at_dt.tzinfo is None:
        execute_at_dt = execute_at_dt.replace(tzinfo=ZoneInfo(timezone))

    execute_at_utc = execute_at_dt.astimezone(ZoneInfo("UTC"))

    if execute_at_utc <= datetime.now(ZoneInfo("UTC")):
        return None, "execute_at must be in the future"

    return execute_at_utc, None


async def create_agent_schedule(
    agent_name: str,
    message: str,
    schedule_type: AgentScheduleType,
    context_dict: dict[str, Any] | None,
    created_by_user: str,  # User ID (UUID) of the creator
    # Agent name if schedule was created by an agent (for audit trail), None if created by human via MCP
    created_by_agent: str | None,
    name: str | None,
    description: str | None,
    # SCHEDULED-specific
    execute_at_utc: datetime | None = None,
    # RECURRING-specific (only used when schedule_type is RECURRING)
    cron_expression: str | None = None,
    cron_timezone: str = "UTC",  # IANA timezone for cron evaluation, defaults to UTC
    next_run_utc: datetime | None = None,
    max_runs: int | None = None,
) -> dict[str, Any]:
    """Create an AgentSchedule record. Returns result dict."""
    async with get_async_session() as session:
        # Validate agent exists and is not deleted
        result = await session.execute(
            select(Agent).where(Agent.name == agent_name).where(col(Agent.deleted_at).is_(None))
        )
        agent = result.scalar_one_or_none()
        if not agent:
            return {"success": False, "error": f"Agent not found: {agent_name}"}

        # Determine next_run_at based on schedule type
        final_next_run = execute_at_utc if schedule_type == AgentScheduleType.SCHEDULED else next_run_utc

        agent_schedule = AgentSchedule(
            agent_id=agent.agent_id,
            schedule_type=schedule_type,
            message=message,
            context=context_dict,
            status=AgentScheduleStatus.PENDING,
            execute_at=execute_at_utc,
            cron_expression=cron_expression,
            cron_timezone=cron_timezone,
            next_run_at=final_next_run,
            max_runs=max_runs,
            name=name,
            description=description,
            created_by_user=created_by_user,
            created_by_agent=created_by_agent,
        )

        session.add(agent_schedule)
        await session.commit()
        # Note: We don't call refresh() here to avoid issues with @retry_db.
        # If the connection drops between commit() and refresh(), retry would
        # re-execute and create a duplicate. The agent_schedule_id is already
        # set by uuid.uuid4() before commit, so we don't need to refresh.

        logger.info(
            f"Created {schedule_type.value.lower()} agent schedule",
            agent_schedule_id=str(agent_schedule.agent_schedule_id),
            agent_name=agent_name,
            next_run_at=final_next_run.isoformat() if final_next_run else None,
            created_by_user=created_by_user,
        )

        response: dict[str, Any] = {
            "success": True,
            "agent_schedule_id": str(agent_schedule.agent_schedule_id),
            "agent_name": agent_name,
            "schedule_type": schedule_type.value,
            "status": "PENDING",
            "name": name,
            "created_by_user": created_by_user,
        }

        if schedule_type == AgentScheduleType.SCHEDULED:
            response["execute_at"] = execute_at_utc.isoformat() if execute_at_utc else None
        else:
            response["cron_expression"] = cron_expression
            response["cron_timezone"] = cron_timezone
            response["next_run_at"] = next_run_utc.isoformat() if next_run_utc else None
            response["max_runs"] = max_runs

        return response


def _schedule_to_dict(schedule: AgentSchedule, agent_name: str, truncate_message: bool = False) -> dict[str, Any]:
    """Convert an AgentSchedule row + agent name to a serializable dict."""
    message = schedule.message
    if truncate_message and len(message) > 100:
        message = message[:100] + "..."

    return {
        "agent_schedule_id": str(schedule.agent_schedule_id),
        "agent_name": agent_name,
        "schedule_type": schedule.schedule_type.value,
        "status": schedule.status.value,
        "message": message,
        "name": schedule.name,
        "description": schedule.description,
        "execute_at": schedule.execute_at.isoformat() if schedule.execute_at else None,
        "cron_expression": schedule.cron_expression,
        "cron_timezone": schedule.cron_timezone,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "run_count": schedule.run_count,
        "max_runs": schedule.max_runs,
        "context": schedule.context,
        "created_by_user": schedule.created_by_user,
        "created_by_agent": schedule.created_by_agent,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
    }


async def get_agent_schedule_detail(agent_schedule_id: str) -> dict[str, Any]:
    """Get a single schedule by ID. Returns result dict with schedule info or error."""
    try:
        schedule_uuid = uuid.UUID(agent_schedule_id)
    except ValueError:
        return {
            "success": False,
            "error": f"Invalid agent_schedule_id format: {agent_schedule_id}",
            "error_code": "INVALID_ID",
        }

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentSchedule, col(Agent.name).label("agent_name"))
            .join(Agent, col(AgentSchedule.agent_id) == col(Agent.agent_id))
            .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
            .where(AgentSchedule.deleted_at.is_(None))  # type: ignore[union-attr]
        )
        row = result.one_or_none()
        if row is None:
            return {
                "success": False,
                "error": f"Agent schedule not found: {agent_schedule_id}",
                "error_code": "NOT_FOUND",
            }

        schedule_dict = _schedule_to_dict(row.AgentSchedule, row.agent_name)
        return {"success": True, "schedule": schedule_dict}


async def list_agent_schedules_by_filters(
    agent_name: str | None = None,
    status: str | None = None,
    schedule_type: str | None = None,
    created_by_user_id: str | None = None,
    limit: int = 50,
    truncate_message: bool = False,
) -> dict[str, Any]:
    """List schedules with optional filters. Returns result dict.

    Args:
        agent_name: Filter by agent name
        status: Filter by status string (e.g., "PENDING")
        schedule_type: Filter by type string (e.g., "RECURRING")
        created_by_user_id: Filter by creator user ID (not email)
        limit: Max results (1-200)
        truncate_message: If True, truncate message to 100 chars in output
    """
    limit = min(max(limit, 1), 200)

    status_enum: AgentScheduleStatus | None = None
    if status:
        try:
            status_enum = AgentScheduleStatus(status)
        except ValueError:
            valid = [s.value for s in AgentScheduleStatus]
            return {"success": False, "error": f"Invalid status: {status}. Must be one of: {valid}"}

    schedule_type_enum: AgentScheduleType | None = None
    if schedule_type:
        try:
            schedule_type_enum = AgentScheduleType(schedule_type)
        except ValueError:
            valid = [t.value for t in AgentScheduleType]
            return {"success": False, "error": f"Invalid schedule_type: {schedule_type}. Must be one of: {valid}"}

    async with get_async_session() as session:
        query: Any = (
            select(AgentSchedule, col(Agent.name).label("agent_name"))
            .join(Agent, col(AgentSchedule.agent_id) == col(Agent.agent_id))
            .where(AgentSchedule.deleted_at.is_(None))  # type: ignore[union-attr]
        )

        if agent_name:
            query = query.where(Agent.name == agent_name)
        if status_enum:
            query = query.where(AgentSchedule.status == status_enum)
        if schedule_type_enum:
            query = query.where(AgentSchedule.schedule_type == schedule_type_enum)
        if created_by_user_id:
            query = query.where(AgentSchedule.created_by_user == created_by_user_id)

        query = query.order_by(sa.nullslast(col(AgentSchedule.next_run_at))).limit(limit)

        result = await session.execute(query)
        rows = result.all()

        schedules = [_schedule_to_dict(row.AgentSchedule, row.agent_name, truncate_message) for row in rows]

        logger.info(
            "Listed agent schedules",
            count=len(schedules),
            filters={
                "agent_name": agent_name,
                "status": status,
                "schedule_type": schedule_type,
                "created_by_user_id": created_by_user_id,
            },
        )

        return {
            "success": True,
            "count": len(schedules),
            "agent_schedules": schedules,
        }


async def cancel_agent_schedule_by_id(
    agent_schedule_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Cancel a schedule. Only the creator can cancel PENDING/PAUSED schedules. Returns result dict."""
    try:
        schedule_uuid = uuid.UUID(agent_schedule_id)
    except ValueError:
        return {
            "success": False,
            "error": f"Invalid agent_schedule_id format: {agent_schedule_id}",
            "error_code": "INVALID_ID",
        }

    async with get_async_session() as session:
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
            check = await session.execute(
                select(AgentSchedule.status, AgentSchedule.created_by_user)
                .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
                .where(col(AgentSchedule.deleted_at).is_(None))
            )
            existing = check.one_or_none()
            if existing is None:
                return {
                    "success": False,
                    "error": f"Agent schedule not found: {agent_schedule_id}",
                    "error_code": "NOT_FOUND",
                }
            if existing.created_by_user != caller_user_id:
                return {
                    "success": False,
                    "error": "You can only cancel schedules you created",
                    "error_code": "FORBIDDEN",
                }
            return {
                "success": False,
                "error": f"Cannot cancel schedule with status {existing.status.value}. "
                f"Only PENDING or PAUSED schedules can be cancelled.",
                "error_code": "INVALID_STATUS",
            }

        logger.info(
            "Cancelled agent schedule",
            agent_schedule_id=agent_schedule_id,
            cancelled_by_user_id=caller_user_id,
        )

        return {
            "success": True,
            "agent_schedule_id": agent_schedule_id,
            "status": "CANCELLED",
        }


async def trigger_agent_schedule_now(
    agent_schedule_id: str,
    caller_user_id: str,
) -> dict[str, Any]:
    """Trigger a recurring schedule to run immediately without affecting its normal cadence.

    Creates a new AgentScheduleRun, executes the agent, and updates run tracking fields.
    After execution, next_run_at is recalculated from the cron expression so the recurring
    schedule continues on its normal cadence (as if the manual trigger never happened).

    Only the creator can trigger, and the schedule must be PENDING (i.e., not already running).

    Returns result dict with run details or error.
    """
    from ypl.agent_harness_service.common.types import SessionCreateRequest
    from ypl.agent_harness_service.scheduler import (
        get_agent_by_id,
        update_run_failure,
        update_run_success,
    )
    from ypl.agent_harness_service.service import create_session
    from ypl.db.agent_harness import AgentScheduleRun, AgentScheduleRunStatus

    try:
        schedule_uuid = uuid.UUID(agent_schedule_id)
    except ValueError:
        return {
            "success": False,
            "error": f"Invalid agent_schedule_id format: {agent_schedule_id}",
            "error_code": "INVALID_ID",
        }

    # Claim the schedule atomically. Use nowait=True (not skip_locked) since this is
    # user-initiated — if the row is locked, we want a clear error instead of "not found".
    async with get_async_session() as session:
        try:
            result = await session.execute(
                select(AgentSchedule)
                .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
                .where(AgentSchedule.deleted_at.is_(None))  # type: ignore[union-attr]
                .with_for_update(nowait=True)
            )
        except sa.exc.OperationalError as e:
            # Only catch PostgreSQL "lock_not_available" (55P03); re-raise other operational errors
            # so that @retry_db can handle transient infra issues properly.
            pg_code = getattr(e.orig, "pgcode", None) if e.orig else None
            if pg_code != "55P03":
                raise
            return {
                "success": False,
                "error": "Schedule is currently being processed. Please try again in a moment.",
                "error_code": "LOCKED",
            }
        schedule = result.scalar_one_or_none()

        if schedule is None:
            return {
                "success": False,
                "error": f"Agent schedule not found: {agent_schedule_id}",
                "error_code": "NOT_FOUND",
            }

        if schedule.created_by_user != caller_user_id:
            return {
                "success": False,
                "error": "You can only trigger schedules you created",
                "error_code": "FORBIDDEN",
            }

        if schedule.schedule_type != AgentScheduleType.RECURRING:
            return {
                "success": False,
                "error": "Only recurring schedules can be triggered. One-time schedules run automatically.",
                "error_code": "INVALID_TYPE",
            }

        if schedule.status != AgentScheduleStatus.PENDING:
            return {
                "success": False,
                "error": f"Cannot trigger schedule with status {schedule.status.value}. Must be PENDING.",
                "error_code": "INVALID_STATUS",
            }

        # Compute run number
        from sqlalchemy import func as sa_func

        max_run_result = await session.execute(
            select(sa_func.coalesce(sa_func.max(AgentScheduleRun.run_number), 0)).where(
                col(AgentScheduleRun.agent_schedule_id) == schedule.agent_schedule_id
            )
        )
        max_run_number = max_run_result.scalar_one()
        run_number = max_run_number + 1

        # Create run record
        from datetime import UTC, datetime

        run = AgentScheduleRun(
            agent_schedule_id=schedule.agent_schedule_id,
            run_number=run_number,
            status=AgentScheduleRunStatus.IN_PROGRESS,
            started_at=datetime.now(UTC),
        )
        session.add(run)

        # Mark schedule as IN_PROGRESS
        schedule.status = AgentScheduleStatus.IN_PROGRESS

        await session.commit()
        await session.refresh(schedule)
        await session.refresh(run)

    # Execute outside the DB transaction
    try:
        agent = await get_agent_by_id(schedule.agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {schedule.agent_id}")

        context = dict(schedule.context) if schedule.context else {}
        session_response = await create_session(
            SessionCreateRequest(
                agent_id=agent.name,
                trigger="manual",
                user_id=schedule.created_by_user,
                context=context,
                message=schedule.message,
                source="manual_trigger",
            )
        )

        await update_run_success(schedule, run, session_response.session_id)

        logger.info(
            "Manually triggered agent schedule",
            agent_schedule_id=agent_schedule_id,
            agent_schedule_run_id=str(run.agent_schedule_run_id),
            session_id=session_response.session_id,
            run_number=run_number,
        )

        return {
            "success": True,
            "agent_schedule_id": agent_schedule_id,
            "session_id": session_response.session_id,
            "run_number": run_number,
        }

    except Exception as e:
        error_msg = str(e)
        logger.error(
            "Manual trigger execution failed",
            agent_schedule_id=agent_schedule_id,
            error=error_msg,
            exc_info=True,
        )
        try:
            await update_run_failure(schedule, run, error_msg)
        except Exception:
            logger.error(
                "Failed to record trigger failure in DB",
                agent_schedule_id=agent_schedule_id,
                exc_info=True,
            )
        return {
            "success": False,
            "error": f"Execution failed: {error_msg}",
            "error_code": "EXECUTION_FAILED",
        }


async def edit_agent_schedule_fields(
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
    """Edit schedule fields. Only creator can edit PENDING/PAUSED schedules. Returns result dict."""
    try:
        schedule_uuid = uuid.UUID(agent_schedule_id)
    except ValueError:
        return {
            "success": False,
            "error": f"Invalid agent_schedule_id format: {agent_schedule_id}",
            "error_code": "INVALID_ID",
        }

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentSchedule)
            .where(col(AgentSchedule.agent_schedule_id) == schedule_uuid)
            .where(AgentSchedule.deleted_at.is_(None))  # type: ignore[union-attr]
            .with_for_update()
        )
        schedule = result.scalar_one_or_none()
        if schedule is None:
            return {
                "success": False,
                "error": f"Agent schedule not found: {agent_schedule_id}",
                "error_code": "NOT_FOUND",
            }

        if schedule.created_by_user != caller_user_id:
            return {
                "success": False,
                "error": "You can only edit schedules you created",
                "error_code": "FORBIDDEN",
            }

        if schedule.status not in (AgentScheduleStatus.PENDING, AgentScheduleStatus.PAUSED):
            return {
                "success": False,
                "error": f"Cannot edit schedule with status {schedule.status.value}. "
                "Only PENDING or PAUSED schedules can be edited.",
                "error_code": "INVALID_STATUS",
            }

        if message is not None:
            schedule.message = message
        if name is not None:
            schedule.name = name
        if description is not None:
            schedule.description = description
        if context is not None:
            schedule.context = context

        # Update agent if requested
        if agent_name is not None:
            agent_result = await session.execute(
                select(Agent).where(Agent.name == agent_name).where(Agent.deleted_at.is_(None))  # type: ignore[union-attr]
            )
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                return {"success": False, "error": f"Agent not found: {agent_name}"}
            schedule.agent_id = agent.agent_id

        if schedule.schedule_type == AgentScheduleType.RECURRING:
            if execute_at is not None:
                return {"success": False, "error": "Cannot set execute_at on a RECURRING schedule"}

            tz = timezone or schedule.cron_timezone
            cron = cron_expression or schedule.cron_expression

            if timezone is not None:
                tz_error = validate_timezone(timezone)
                if tz_error:
                    return {"success": False, "error": tz_error}
                schedule.cron_timezone = timezone

            if cron_expression is not None:
                cron_error = validate_cron_expression(cron_expression, tz)
                if cron_error:
                    return {"success": False, "error": cron_error}
                schedule.cron_expression = cron_expression

            if max_runs is not None:
                if max_runs <= 0:
                    return {"success": False, "error": "max_runs must be a positive integer"}
                schedule.max_runs = max_runs

            if cron_expression is not None or timezone is not None:
                if cron is None:
                    return {"success": False, "error": "Internal error: cron expression is unexpectedly None"}
                schedule.next_run_at = compute_next_run_for_cron(cron, tz)
        else:
            if cron_expression is not None:
                return {"success": False, "error": "Cannot set cron_expression on a SCHEDULED (one-time) schedule"}
            if max_runs is not None:
                return {"success": False, "error": "Cannot set max_runs on a SCHEDULED (one-time) schedule"}

            if timezone is not None:
                tz_error = validate_timezone(timezone)
                if tz_error:
                    return {"success": False, "error": tz_error}
                schedule.cron_timezone = timezone

            if execute_at is not None:
                tz_for_parse = timezone or schedule.cron_timezone or "UTC"
                execute_at_dt, dt_error = parse_execute_at(execute_at, tz_for_parse)
                if dt_error:
                    return {"success": False, "error": dt_error}
                schedule.execute_at = execute_at_dt
                schedule.next_run_at = execute_at_dt

        session.add(schedule)
        await session.commit()

        logger.info(
            "Edited agent schedule",
            agent_schedule_id=agent_schedule_id,
            edited_by=caller_user_id,
        )

        return {"success": True, "agent_schedule_id": agent_schedule_id, "status": "updated"}
