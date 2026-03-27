"""TaskIQ tasks for MCP token management."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import case, func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col, select

from ypl.backend.config import settings
from ypl.backend.db import get_async_session
from ypl.backend.email.email_types import EmailConfig
from ypl.backend.email.send_email import send_email_async
from ypl.backend.jobs.taskiq import taskiq_app
from ypl.db.mcp import MCPAuditLog, MCPAuditLogStatus, MCPDevToken, MCPTokenStatus
from ypl.mcp_server.auth_dev_token import create_token, revoke_token
from ypl.structured_logger import get_logger

logger = get_logger()


@taskiq_app.task(retry_on_error=True, max_retries=3, delay=10)
async def create_mcp_token_task(
    email: str,
    expires_days: int,
    description: str | None = None,
    github_actor: str | None = None,
    github_run_url: str | None = None,
) -> dict[str, Any]:
    """Create a new MCP developer token and email it to the user.

    Args:
        email: Engineer email address
        expires_days: Number of days until token expires (must be between 1 and 365)
        description: Description of what this token is for
        github_actor: GitHub Actions actor (caller)
        github_run_url: GitHub Actions run URL

    Returns:
        Dictionary with token information including token_id, description, and expires_at

    Raises:
        ValueError: If expires_days is not between 1 and 365
    """
    # Validate expires_days
    if expires_days < 1 or expires_days > 365:
        raise ValueError(f"expires_days must be between 1 and 365, got {expires_days}")

    # Calculate expiration date
    expires_at = datetime.now(UTC) + timedelta(days=expires_days)

    # Build final description: append GitHub Actions info if provided
    parts = []
    if description:
        parts.append(description)
    if github_run_url:
        parts.append(f"GitHub Actions: {github_run_url}")
    if github_actor:
        parts.append(f"Caller: {github_actor}")
    final_description = "\n".join(parts)

    # Validate email domain
    email_domain = email.split("@")[-1]
    allowed_domains = settings.ALLOWED_MCP_EMAIL_DOMAINS
    if email_domain not in allowed_domains:
        raise ValueError(f"Email domain {email_domain} not in allowed domains: {allowed_domains}")

    # Extract name from email (part before @) for email template
    email_name = email.split("@")[0] if "@" in email else "User"

    # Create token (manages its own DB session internally)
    try:
        plaintext_token, db_token = await create_token(
            email=email,
            description=final_description,
            expires_at=expires_at,
        )
    except PermissionError as e:
        denial_reason = str(e)
        logger.warning("MCP token creation denied due to missing permission", email=email, error=denial_reason)
        # Send denial email and return error (don't re-raise to avoid task retries)
        try:
            await send_email_async(
                EmailConfig(
                    campaign="mcp_token_denied",
                    to_address=email,
                    template_params={
                        "email_recipient_name": email_name,
                        "denial_reason": denial_reason,
                    },
                )
            )
        except Exception as email_error:
            # Don't let email failures cause task retries for permission denials
            logger.warning(
                "Failed to send MCP token denial email",
                email=email,
                error=str(email_error),
            )
        return {
            "error": "permission_denied",
            "email": email,
            "denial_reason": denial_reason,
        }

    # Send email with token (do not print it)
    await send_email_async(
        EmailConfig(
            campaign="mcp_token",
            to_address=email,
            template_params={
                "email_recipient_name": email_name,
                "token": plaintext_token,
                "mcp_server_url": settings.MCP_SERVER_BASE_URL,
            },
        )
    )

    return {
        "token_id": db_token.mcp_dev_token_id,
        "email": email,
        "description": final_description or "N/A",
        "expires_at": expires_at.isoformat() if expires_at else "Never",
    }


@taskiq_app.task(retry_on_error=True, max_retries=3, delay=10)
async def list_mcp_tokens_task(
    email: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    """List all MCP tokens.

    Args:
        email: Filter by engineer email
        active_only: Show only active tokens

    Returns:
        List of dictionaries containing token information
    """
    async with get_async_session() as session:
        # Build query
        statement = select(MCPDevToken)

        if email:
            statement = statement.where(MCPDevToken.email == email)

        if active_only:
            statement = statement.where(MCPDevToken.status == MCPTokenStatus.ACTIVE)

        created_at_col = cast(ColumnElement[Any], MCPDevToken.created_at)
        statement = statement.order_by(sa.desc(created_at_col))

        result = await session.exec(statement)
        tokens = result.all()

        return [
            {
                "token_id": token.mcp_dev_token_id,
                "email": token.email,
                "status": token.status.value,
                "description": token.description or "N/A",
                "created_at": token.created_at.isoformat() if token.created_at else "N/A",
                "last_used_at": token.last_used_at.isoformat() if token.last_used_at else "Never",
                "expires_at": token.expires_at.isoformat() if token.expires_at else "Never",
                "revoked_by": token.revoked_by,
                "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
                "revoked_reason": token.revoked_reason,
            }
            for token in tokens
        ]


@taskiq_app.task(retry_on_error=True, max_retries=3, delay=10)
async def revoke_mcp_token_task(
    token_id: str,
    reason: str,
    revoked_by: str,
) -> dict[str, Any] | None:
    """Revoke an MCP token.

    Args:
        token_id: ID of the token to revoke
        reason: Reason for revoking the token
        revoked_by: Email of person revoking the token

    Returns:
        Dictionary with revoked token information, or None if token not found
    """
    # Revoke token (manages its own DB session internally)
    result = await revoke_token(
        token_id=token_id,
        revoked_by=revoked_by,
        reason=reason,
    )

    if not result:
        return None

    return {
        "token_id": result.mcp_dev_token_id,
        "email": result.email,
        "revoked_by": revoked_by,
        "reason": reason,
    }


@taskiq_app.task(retry_on_error=True, max_retries=3, delay=10)
async def get_mcp_audit_log_task(
    email: str | None = None,
    tool: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """View MCP audit logs.

    Args:
        email: Filter by token owner email
        tool: Filter by tool name
        hours: Show logs from last N hours (default: 24)
        limit: Maximum number of logs to show (default: 50)

    Returns:
        List of dictionaries containing audit log information
    """
    async with get_async_session() as session:
        # Build query
        statement = select(MCPAuditLog, MCPDevToken).join(
            MCPDevToken, col(MCPAuditLog.mcp_dev_token_id) == col(MCPDevToken.mcp_dev_token_id)
        )

        # Apply filters
        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)
        created_at_col = cast(ColumnElement[Any], MCPAuditLog.created_at)
        statement = statement.where(created_at_col >= cutoff_time)

        if email:
            statement = statement.where(MCPDevToken.email == email)

        if tool:
            statement = statement.where(MCPAuditLog.tool_name == tool)

        statement = statement.order_by(sa.desc(created_at_col)).limit(limit)

        result = await session.exec(statement)
        rows = result.all()

        return [
            {
                "status": "OK" if log.status == MCPAuditLogStatus.SUCCESS else "FAIL",
                "created_at": log.created_at.isoformat() if log.created_at else "N/A",
                "email": token.email,
                "tool_name": log.tool_name,
                "tool_parameters": log.tool_parameters,
                "result_summary": log.result_summary or "",
                "execution_time_ms": log.execution_time_ms,
                "error_message": log.error_message,
            }
            for log, token in rows
        ]


@taskiq_app.task(retry_on_error=True, max_retries=3, delay=10)
async def get_mcp_stats_task(
    days: int = 7,
) -> dict[str, Any]:
    """Show MCP usage statistics.

    Args:
        days: Show stats for last N days (default: 7)

    Returns:
        Dictionary containing statistics including total calls, success/failure counts,
        average execution time, top engineers, and top tools
    """
    async with get_async_session() as session:
        cutoff_time = datetime.now(UTC) - timedelta(days=days)

        # Overall statistics query
        overall_stats = (
            select(
                func.count().label("total_calls"),
                func.sum(case((col(MCPAuditLog.status) == MCPAuditLogStatus.SUCCESS, 1), else_=0)).label(
                    "successful_calls"
                ),
                func.avg(MCPAuditLog.execution_time_ms).label("avg_execution_time_ms"),
            )
            .select_from(MCPAuditLog)
            .join(MCPDevToken, col(MCPAuditLog.mcp_dev_token_id) == col(MCPDevToken.mcp_dev_token_id))
            .where(col(MCPAuditLog.created_at) >= cutoff_time)
        )
        overall_result = await session.execute(overall_stats)
        overall_row = overall_result.one()

        total_calls = int(overall_row[0]) if overall_row[0] else 0
        successful_calls = int(overall_row[1]) if overall_row[1] else 0
        failed_calls = total_calls - successful_calls
        avg_exec_time = float(overall_row[2]) if overall_row[2] else 0.0

        # Engineer counts query
        engineer_stats = (
            select(
                MCPDevToken.email,
                func.count().label("call_count"),
            )
            .select_from(MCPAuditLog)
            .join(MCPDevToken, col(MCPAuditLog.mcp_dev_token_id) == col(MCPDevToken.mcp_dev_token_id))
            .where(col(MCPAuditLog.created_at) >= cutoff_time)
            .group_by(MCPDevToken.email)
        )
        engineer_result = await session.exec(engineer_stats)
        engineer_counts: dict[str, int] = {row[0]: int(row[1]) for row in engineer_result}

        # Tool counts query
        tool_stats = (
            select(
                MCPAuditLog.tool_name,
                func.count().label("call_count"),
            )
            .where(col(MCPAuditLog.created_at) >= cutoff_time)
            .group_by(MCPAuditLog.tool_name)
        )
        tool_result = await session.exec(tool_stats)
        tool_counts: dict[str, int] = {row[0]: int(row[1]) for row in tool_result}

        return {
            "days": days,
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "avg_execution_time_ms": avg_exec_time,
            "engineer_counts": engineer_counts,
            "tool_counts": tool_counts,
        }
