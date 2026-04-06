"""MCP tools for database queries.

Provides tools for executing read-only queries against the Yupp production
database (PostgreSQL) and BigQuery analytics warehouse.
"""

import asyncio
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from google.cloud import bigquery

from ypl.backend.config import DbName, settings
from ypl.backend.db import get_async_session_for, retry_db
from ypl.backend.utils.dynamic_app_settings import get_mcp_tools_settings
from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger
from ypl.utils import maybe_truncate

logger = get_logger()


# ============================================================================
# BigQuery Singleton
# ============================================================================

_BIGQUERY_CLIENT: bigquery.Client | None = None


def _get_bigquery_client() -> bigquery.Client:
    """Get or create BigQuery client singleton."""
    global _BIGQUERY_CLIENT
    if _BIGQUERY_CLIENT is None:
        _BIGQUERY_CLIENT = bigquery.Client(project=settings.GCP_PROJECT_ID)
    return _BIGQUERY_CLIENT


# ============================================================================
# SQL Helpers
# ============================================================================


def _strip_sql_comments(sql: str) -> str:
    """Strip SQL comments (-- line comments and /* block comments */) from a query.

    Note: this uses regex and does not handle comment-like syntax inside string literals
    (e.g. 'value with -- dashes'). This is acceptable because callers are AI agents that
    only place comments as leading annotation lines, not inside string values.
    """
    # Remove block comments (non-greedy, handles nested poorly but sufficient for validation)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # Remove line comments
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql.strip()


def _validate_bigquery_query(sql: str) -> dict[str, Any] | None:
    """Validate a BigQuery query for safety.

    Returns an error dict if validation fails, None if the query is valid.

    Note: This check requires queries to start with SELECT, which means CTE queries
    (WITH ... AS (...) SELECT ...) will be rejected. This is a known limitation.
    """
    sql_upper = _strip_sql_comments(sql).upper()
    if not sql_upper.startswith("SELECT"):
        return {
            "success": False,
            "error": "Only SELECT queries are allowed",
            "sql": sql,
        }

    # Block dangerous keywords using word boundaries to avoid false positives
    # (e.g., "create_date" should not match "CREATE")
    dangerous_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE"]
    for keyword in dangerous_keywords:
        if re.search(r"\b" + keyword + r"\b", sql_upper):
            return {
                "success": False,
                "error": f"Query contains forbidden keyword: {keyword}",
                "sql": sql,
            }

    return None


def _prepare_bigquery_sql(sql: str, max_rows: int) -> str:
    """Prepare SQL for execution by adding LIMIT if not present."""
    sql_upper = sql.strip().upper()
    sql_clean = sql.rstrip(";")
    if "LIMIT" not in sql_upper:
        return f"{sql_clean} LIMIT {max_rows}"
    return sql_clean


def _check_bigquery_cost(client: bigquery.Client, sql: str, max_bytes_gb: float) -> dict[str, Any] | None:
    """Check if a BigQuery query exceeds the cost limit using dry run.

    Returns an error dict if the query exceeds the limit, None otherwise.
    """
    if max_bytes_gb <= 0:
        return None  # Cost check disabled

    dry_run_config = bigquery.QueryJobConfig(dry_run=True, use_legacy_sql=False)
    dry_run_job = client.query(sql, job_config=dry_run_config)
    estimated_bytes = dry_run_job.total_bytes_processed or 0
    estimated_gb = estimated_bytes / (1024**3)

    if estimated_gb > max_bytes_gb:
        logger.warning(
            "BigQuery query exceeds cost limit",
            estimated_gb=round(estimated_gb, 2),
            max_gb=max_bytes_gb,
            sql=maybe_truncate(sql, 200),
        )
        return {
            "success": False,
            "error": f"Query would process {estimated_gb:.2f} GB, which exceeds the limit of {max_bytes_gb} GB. "
            "Consider adding filters or using a more specific query.",
            "sql": sql,
            "estimated_bytes_processed": estimated_bytes,
            "estimated_gb_processed": round(estimated_gb, 2),
            "limit_gb": max_bytes_gb,
        }

    return None


def _execute_bigquery_query(
    client: bigquery.Client, sql: str, max_rows: int, max_bytes: int | None = None
) -> dict[str, Any]:
    """Execute a BigQuery query and return formatted results.

    Args:
        client: BigQuery client instance
        sql: SQL query to execute
        max_rows: Maximum number of rows to return
        max_bytes: Maximum bytes to process (enforced server-side via maximum_bytes_billed)
    """
    job_config = bigquery.QueryJobConfig(use_legacy_sql=False, use_query_cache=True, maximum_bytes_billed=max_bytes)
    query_job = client.query(sql, job_config=job_config)
    results = query_job.result()

    formatted_results = []
    columns: list[str] = []
    for row in results:
        row_as_dict = dict(row.items())
        if not columns:
            columns = list(row_as_dict.keys())
        row_dict = {}
        for key, value in row_as_dict.items():
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = float(value)
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            row_dict[key] = value
        formatted_results.append(row_dict)
        if len(formatted_results) >= max_rows:
            break

    return {
        "success": True,
        "row_count": len(formatted_results),
        "columns": columns,
        "results": formatted_results,
        "bytes_processed": query_job.total_bytes_processed,
    }


async def _query_bigquery_impl(
    sql: str, max_rows: int, check_cost_limit: bool, use_expensive_limit: bool = False
) -> dict[str, Any]:
    """Common implementation for BigQuery query tools.

    Wraps blocking BigQuery client calls in asyncio.to_thread() to avoid
    blocking the event loop.

    Args:
        sql: SELECT query to execute (only SELECT allowed)
        max_rows: Maximum number of rows to return
        check_cost_limit: Whether to perform dry run cost check
        use_expensive_limit: If True, use the higher expensive query limit for server-side cap

    Returns:
        Dictionary containing query results
    """
    query_type = "BigQuery query" if check_cost_limit else "BigQuery expensive query"

    try:
        # Validate query syntax and safety (no I/O, runs synchronously)
        validation_error = _validate_bigquery_query(sql)
        if validation_error:
            return validation_error

        logger.info("Executing BigQuery query", query_type=query_type, sql=maybe_truncate(sql, 200))

        client = _get_bigquery_client()
        limited_sql = _prepare_bigquery_sql(sql, max_rows)

        mcp_settings = await get_mcp_tools_settings()

        # Check cost limit using dry run (if enabled)
        # Wrapped in to_thread() since client.query() is blocking
        if check_cost_limit:
            cost_error = await asyncio.to_thread(
                _check_bigquery_cost, client, limited_sql, mcp_settings.bigquery_max_bytes_processed_gb
            )
            if cost_error:
                return cost_error

        # Determine server-side byte limit
        if use_expensive_limit:
            max_bytes_gb = mcp_settings.bigquery_expensive_max_bytes_processed_gb
        else:
            max_bytes_gb = mcp_settings.bigquery_max_bytes_processed_gb
        max_bytes = int(max_bytes_gb * (1024**3)) if max_bytes_gb > 0 else None

        # Execute query - wrapped in to_thread() since BigQuery client is blocking
        result = await asyncio.to_thread(_execute_bigquery_query, client, limited_sql, max_rows, max_bytes)

        logger.info(
            "BigQuery query completed",
            query_type=query_type,
            row_count=result["row_count"],
            bytes_processed=result.get("bytes_processed"),
        )
        return result

    except Exception as e:
        logger.warning(
            "Error executing BigQuery query",
            query_type=query_type,
            error=str(e),
            sql=maybe_truncate(sql, 200),
            exc_info=True,
        )
        return {"success": False, "error": str(e), "sql": sql}


# ============================================================================
# MCP Tool Functions
# ============================================================================


async def _query_postgres_impl(
    sql: str,
    max_rows: int,
    database: DbName,
) -> dict[str, Any]:
    """Common implementation for Postgres query tools."""
    try:
        sql_upper = _strip_sql_comments(sql).upper()
        if not sql_upper.startswith("SELECT"):
            return {
                "success": False,
                "error": "Only SELECT queries are allowed",
                "sql": sql,
            }

        logger.info("Executing database query", database=database, sql=sql[:200])

        async with get_async_session_for(database, replica=True) as session:
            await session.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

            sql_clean = sql.rstrip(";")
            if "LIMIT" not in sql_upper:
                limited_sql = f"{sql_clean} LIMIT {max_rows}"
            else:
                limited_sql = sql_clean
            result = await session.execute(sa.text(limited_sql))

            rows = result.fetchall()
            columns = result.keys()

            formatted_results = []
            for row in rows:
                row_dict = {}
                for i, column in enumerate(columns):
                    value = row[i]
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    row_dict[column] = value
                formatted_results.append(row_dict)

            logger.info("Database query completed", database=database, row_count=len(formatted_results))

            return {
                "success": True,
                "database": database,
                "row_count": len(formatted_results),
                "columns": list(columns),
                "results": formatted_results[:max_rows] if len(formatted_results) > max_rows else formatted_results,
            }

    except Exception as e:
        logger.warning("Error executing database query", database=database, error=str(e), sql=sql[:200])
        return {"success": False, "error": str(e), "sql": sql}


@mcp_server.tool(
    name="query_yuppdb",
    description=(
        "Execute read-only SQL query on Yupp production database (yuppdb). Use SELECT queries to "
        "investigate data issues, check user states, or analyze patterns. "
        "IMPORTANT: Only SELECT queries are allowed - no writes/updates."
    ),
)
@retry_db
async def query_yuppdb(
    sql: str,
    max_rows: int = 1000,
) -> dict[str, Any]:
    """Execute read-only SQL query on Yupp database (yuppdb)."""
    return await _query_postgres_impl(sql, max_rows, "yuppdb")


@mcp_server.tool(
    name="query_agentdb",
    description=(
        "Execute read-only SQL query on the Agent database (agentdb / yadb). "
        "Contains agent harness data: sessions, tasks, projects, schedules, artifacts, memory indexes. "
        "IMPORTANT: Only SELECT queries are allowed - no writes/updates.\n\n"
        "SCHEMA REFERENCE (use exact column names — do NOT guess or infer):\n\n"
        "agents: agent_id (PK), name, display_name, description, executor_type, executor_model, "
        "config, creator_user_id, additional_system_prompt, agent_user_id, "
        "created_at, modified_at, deleted_at\n\n"
        "agent_sessions: agent_session_id (PK), agent_id (FK→agents), parent_session_id, "
        "creator_user_id, slack_session_id, trigger, context, workspace, llm_session_id, "
        "extra_dirs, model, status (ACTIVE|COMPLETED|STALE), title, "
        "created_at, modified_at, deleted_at\n"
        "  ⚠ No 'id', 'turn_count', or 'end_reason' columns — use agent_session_id as PK; "
        "count turns via agent_session_messages.\n\n"
        "agent_session_messages: agent_session_message_id (PK), agent_session_id (FK), "
        "turn_number, role (USER|AGENT|SYSTEM|FELLOW_AGENT), creator_user_id, content, "
        "raw_events, llm_name, llm_message_id, cost_usd, duration_ms, num_agent_turns, "
        "slack_ts, ttfct_ms, ttlct_ms, "
        "completion_status (IN_PROGRESS|SUCCESS|FAILED|ABORTED), "
        "error_type (NONE|ERROR_MAX_TURNS|ERROR_CONTEXT_OVERFLOW|ERROR_EXECUTOR|ERROR_INTERNAL), "
        "from_agent_id, agent_message_id_ref, created_at, modified_at, deleted_at\n\n"
        "agent_projects: agent_project_id (PK), name, description, "
        "status (ACTIVE|PAUSED|COMPLETED|ARCHIVED), creator_user_id, default_agent_id, "
        "project_data, shared_state, budget_usd, budget_spent_usd, slack_channel, "
        "created_at, modified_at, deleted_at\n\n"
        "agent_tasks: agent_task_id (PK), agent_project_id (FK), parent_task_id, title, "
        "description, status (PENDING|BLOCKED|READY|IN_PROGRESS|COMPLETED|FAILED|CANCELLED|IN_REVIEW), "
        "priority (1=URGENT|2=HIGH|3=NORMAL|4=LOW), agent_id, assigned_session_ids, result, "
        "task_data, estimated_effort, actual_spending_usd, completed_at, depends_on, "
        "created_at, modified_at, deleted_at\n\n"
        "agent_schedules: agent_schedule_id (PK), agent_id (FK), "
        "schedule_type (SCHEDULED|RECURRING), message, context, "
        "status (PENDING|IN_PROGRESS|COMPLETED|FAILED|CANCELLED|PAUSED), "
        "execute_at, cron_expression, cron_timezone, next_run_at, last_run_at, run_count, "
        "max_runs, last_session_id, last_error, name, description, "
        "created_by_user, created_by_agent, created_at, modified_at, deleted_at\n\n"
        "agent_schedule_runs: agent_schedule_run_id (PK), agent_schedule_id (FK), run_number, "
        "status (PENDING|IN_PROGRESS|COMPLETED|FAILED), started_at, completed_at, session_id, "
        "error, created_at, modified_at, deleted_at\n\n"
        "agent_artifacts: agent_artifact_id (PK), artifact_type (YUPPASTE|CODE_REVIEW|OTHER), "
        "title, description, url, creator_user_id, creator_agent_id, agent_session_id, "
        "agent_task_id, artifact_metadata, created_at, modified_at, deleted_at\n\n"
        "agent_messages: agent_message_id (PK), from_agent_id (FK), to_agent_id (FK), "
        "from_session_id, to_session_id, content, message_metadata, "
        "status (QUEUED|DELIVERING|DELIVERED|FAILED), queued_at, delivered_at, "
        "claimed_at, attempt_count, max_attempts, resolved_session_id, error, "
        "created_at, modified_at, deleted_at\n\n"
        "agent_feedbacks: agent_feedback_id (PK), agent_session_id (FK), "
        "agent_session_message_id (FK), user_id, rating (POSITIVE|NEUTRAL|NEGATIVE), "
        "structured, comment, slack_ts, created_at, modified_at, deleted_at\n\n"
        "agent_security_incidents: incident_id (PK), agent_session_id, agent_name, "
        "incident_type (SECRETS_PROBE|MEMORY_MANIPULATION|SCOPE_MANIPULATION|SOCIAL_ENGINEERING), "
        "severity (HIGH|MEDIUM|LOW), description, evidence, turn_number, offense_number, "
        "auto_detected, reported_at, reviewed_at, reviewed_by, "
        "resolution (FALSE_POSITIVE|CONFIRMED|MITIGATED), created_at, modified_at, deleted_at"
    ),
)
@retry_db
async def query_agentdb(
    sql: str,
    max_rows: int = 1000,
) -> dict[str, Any]:
    """Execute read-only SQL query on the Agent database (agentdb)."""
    return await _query_postgres_impl(sql, max_rows, "agentdb")


@mcp_server.tool(
    name="query_bigquery",
    description=(
        "Execute read-only SQL query on Yupp BigQuery analytics data. Use for analytics queries "
        "on aggregated/historical data: model battle summaries, user demographics, spending history, "
        "leaderboard data, etc. IMPORTANT: Only SELECT queries are allowed. "
        "Queries are subject to a configurable data processing limit (default: 200 GB)."
    ),
)
async def query_bigquery(sql: str, max_rows: int = 100) -> dict[str, Any]:
    """Execute read-only SQL query on Yupp BigQuery analytics data.

    This tool performs a dry run first to estimate the data to be processed.
    If the estimated bytes exceed the configured limit (from MCP tools settings),
    the query is rejected with an error.

    Args:
        sql: SELECT query to execute (only SELECT allowed)
        max_rows: Maximum number of rows to return

    Returns:
        Dictionary containing query results
    """
    return await _query_bigquery_impl(sql, max_rows, check_cost_limit=True)


@mcp_server.tool(
    name="query_bigquery_expensive",
    description=(
        "Execute read-only SQL query on Yupp BigQuery analytics data with higher limits. "
        "Use this ONLY when you need to run large analytical queries that exceed the normal "
        "200 GB data processing limit. Queries are still subject to a high server-side cap "
        "(default: 10 TB) for cost protection. IMPORTANT: Only SELECT queries are allowed. "
        "Prefer query_bigquery for most queries."
    ),
)
async def query_bigquery_expensive(sql: str, max_rows: int = 1000) -> dict[str, Any]:
    """Execute read-only SQL query on Yupp BigQuery analytics data with higher limits.

    This tool skips the dry run cost check, allowing large queries to run.
    A high but finite server-side cap (configurable, default 10 TB) is still enforced
    to prevent runaway costs.

    Args:
        sql: SELECT query to execute (only SELECT allowed)
        max_rows: Maximum number of rows to return (default: 1000)

    Returns:
        Dictionary containing query results
    """
    return await _query_bigquery_impl(sql, max_rows, check_cost_limit=False, use_expensive_limit=True)
