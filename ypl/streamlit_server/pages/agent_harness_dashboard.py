"""Agent Harness Analytics Dashboard.

Operational analytics for agent sessions, costs, latency, errors,
feedback, and schedule health.
"""

from __future__ import annotations
import uuid
import warnings
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import text
from sqlmodel import col, select
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.llm.constants import LINEAR_TO_SLACK_ID
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import Agent
from ypl.streamlit_server.auth import require_auth
from ypl.structured_logger import get_logger

logger = get_logger()

# SQLModel emits a DeprecationWarning for session.execute() even when used
# correctly with raw text() SQL (exec() is only for SQLModel select() stmts).
warnings.filterwarnings("ignore", message=".*You probably want to use.*session.exec.*", category=DeprecationWarning)

# ── Slack user-name resolution ───────────────────────────────────────────────

_SLACK_ID_TO_NAME: dict[str, str] = {}
for _name, _sid in LINEAR_TO_SLACK_ID.items():
    if _sid not in _SLACK_ID_TO_NAME or len(_name) > len(_SLACK_ID_TO_NAME[_sid]):
        _SLACK_ID_TO_NAME[_sid] = _name


def _resolve_slack_names(df: pd.DataFrame, column: str = "slack_user_id") -> pd.DataFrame:
    """Replace Slack user IDs with human-readable names where possible."""
    if column in df.columns:
        df[column] = df[column].map(lambda uid: _SLACK_ID_TO_NAME.get(uid, uid) if uid else "unknown")
    return df


# ── Page config & auth ───────────────────────────────────────────────────────

st.set_page_config(page_title="Agent Harness Dashboard", page_icon="📊", layout="wide")
require_auth()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _agent_filter(agent_id: uuid.UUID | None, alias: str = "s") -> tuple[str, dict[str, Any]]:
    """Return a SQL WHERE fragment and params dict for optional agent-id filtering."""
    if agent_id:
        return f"AND {alias}.agent_id = :agent_id", {"agent_id": agent_id}
    return "", {}


def _dt_range(date_from: date, date_to: date) -> dict[str, datetime]:
    """Convert date range to datetime params (inclusive start, exclusive end)."""
    return {
        "date_from": datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC),
        "date_to": datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1),
    }


def _rows_to_df(rows: Any) -> pd.DataFrame:
    """Convert SQLAlchemy result rows to a DataFrame."""
    return pd.DataFrame([dict(r._mapping) for r in rows]) if rows else pd.DataFrame()


def _safe_load(
    label: str,
    coro: Any,
    default: Any = None,
    *,
    timeout: float = 60,
    cache_key: str | None = None,
) -> Any:
    """Run an async query with spinner and error handling."""
    cache = st.session_state.setdefault("ahd_query_cache", {})
    if cache_key is not None and cache_key in cache:
        if hasattr(coro, "close"):
            coro.close()
        return cache[cache_key]

    with st.spinner(f"Loading {label}…"):
        try:
            result = run_coroutine_in_lit_worker(coro, timeout=timeout)
            if cache_key is not None:
                cache[cache_key] = result
            return result
        except Exception as e:
            st.error(f"Error loading {label}: {e}")
            logger.exception("Dashboard query error for %s", label)
            return default if default is not None else pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════════════════
# DB QUERY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════


@retry_db
async def _fetch_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).order_by(col(Agent.name)))
        return list(result.all())


# ── Tab 1: Usage & Sessions ──────────────────────────────────────────────────


@retry_db
async def _fetch_session_kpis(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> dict[str, Any]:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text(f"""
                    SELECT
                        COUNT(DISTINCT s.agent_session_id) AS total_sessions,
                        COUNT(DISTINCT s.agent_session_id)
                            FILTER (WHERE s.status = 'ACTIVE') AS active_sessions,
                        COALESCE(
                            ROUND(
                                COUNT(m.agent_session_message_id)::numeric
                                / NULLIF(COUNT(DISTINCT s.agent_session_id), 0),
                                1
                            ),
                            0
                        ) AS avg_messages,
                        COUNT(DISTINCT s.agent_id) AS unique_agents
                    FROM agent_sessions s
                    LEFT JOIN agent_session_messages m
                        ON s.agent_session_id = m.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      {af}
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_sessions_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, s.created_at) AS bucket,
                           a.display_name AS agent_name,
                           COUNT(*) AS session_count
                    FROM agent_sessions s
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      {af}
                    GROUP BY bucket, a.display_name ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_session_status_dist(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT s.status, COUNT(*) AS count
                    FROM agent_sessions s
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      {af}
                    GROUP BY s.status
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_session_trigger_dist(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT s.trigger, COUNT(*) AS count
                    FROM agent_sessions s
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      {af}
                    GROUP BY s.trigger ORDER BY count DESC
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 1 (cont): User Activity ─────────────────────────────────────────────


@retry_db
async def _fetch_sessions_by_user(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT s.context->>'slack_user_id' AS slack_user_id,
                           COUNT(DISTINCT s.agent_session_id) AS session_count,
                           COUNT(m.agent_session_message_id) AS total_messages,
                           COALESCE(SUM(m.cost_usd), 0) AS total_cost
                    FROM agent_sessions s
                    LEFT JOIN agent_session_messages m
                        ON s.agent_session_id = m.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      AND s.context->>'slack_user_id' IS NOT NULL
                      {af}
                    GROUP BY s.context->>'slack_user_id'
                    ORDER BY session_count DESC
                    LIMIT 20
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_unique_users_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, s.created_at) AS bucket,
                           COUNT(DISTINCT s.context->>'slack_user_id') AS unique_users
                    FROM agent_sessions s
                    WHERE s.parent_session_id IS NULL
                      AND s.created_at >= :date_from AND s.created_at < :date_to
                      AND s.context->>'slack_user_id' IS NOT NULL
                      {af}
                    GROUP BY bucket ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 2: Cost Analytics ────────────────────────────────────────────────────


@retry_db
async def _fetch_cost_kpis(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> dict[str, Any]:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text(f"""
                    SELECT
                        COALESCE(SUM(m.cost_usd), 0) AS total_cost,
                        COALESCE(
                            SUM(m.cost_usd) / NULLIF(COUNT(DISTINCT s.agent_session_id), 0),
                            0
                        ) AS avg_cost_per_session,
                        COALESCE(AVG(m.cost_usd), 0) AS avg_cost_per_turn,
                        COUNT(*) FILTER (WHERE m.cost_usd IS NOT NULL) AS priced_messages
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_cost_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, m.created_at) AS bucket,
                           a.display_name AS agent_name,
                           COALESCE(SUM(m.cost_usd), 0) AS cost
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL
                      AND m.cost_usd IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY bucket, a.display_name ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_cost_per_session(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name,
                           SUM(m.cost_usd) AS session_cost
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL
                      AND m.cost_usd IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY s.agent_session_id, a.display_name
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 3: Latency & Performance ─────────────────────────────────────────────


@retry_db
async def _fetch_latency_kpis(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> dict[str, Any]:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text(f"""
                    SELECT
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY m.duration_ms) AS p50,
                        percentile_cont(0.9) WITHIN GROUP (ORDER BY m.duration_ms) AS p90,
                        percentile_cont(0.99) WITHIN GROUP (ORDER BY m.duration_ms) AS p99
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND m.duration_ms IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_latency_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, m.created_at) AS bucket,
                           percentile_cont(0.5)  WITHIN GROUP (ORDER BY m.duration_ms) AS p50,
                           percentile_cont(0.9)  WITHIN GROUP (ORDER BY m.duration_ms) AS p90,
                           percentile_cont(0.99) WITHIN GROUP (ORDER BY m.duration_ms) AS p99
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND m.duration_ms IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY bucket ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_latency_distribution(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name, m.duration_ms
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL
                      AND m.duration_ms IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    ORDER BY m.created_at DESC LIMIT 5000
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_tool_usage(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT event->>'name' AS tool_name, COUNT(*) AS usage_count
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    , LATERAL jsonb_array_elements(m.raw_events) AS event
                    WHERE s.parent_session_id IS NULL
                      AND m.raw_events IS NOT NULL
                      AND jsonb_typeof(m.raw_events) = 'array'
                      AND (event->>'type') = 'tool_use'
                      AND event->>'name' IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY tool_name ORDER BY usage_count DESC LIMIT 30
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 4: Errors & Feedback ─────────────────────────────────────────────────


@retry_db
async def _fetch_error_feedback_kpis(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> dict[str, Any]:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text(f"""
                    WITH err AS (
                        SELECT COUNT(*) AS total_errors
                        FROM agent_session_messages m
                        JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                        WHERE s.parent_session_id IS NULL AND m.role = 'SYSTEM'
                          AND m.created_at >= :date_from AND m.created_at < :date_to {af}
                    ),
                    sess AS (
                        SELECT COUNT(*) AS total
                        FROM agent_sessions s
                        WHERE s.parent_session_id IS NULL
                          AND s.created_at >= :date_from AND s.created_at < :date_to {af}
                    ),
                    fb AS (
                        SELECT
                            COUNT(*) FILTER (WHERE f.rating = 'POSITIVE') AS positive,
                            COUNT(*) FILTER (WHERE f.rating = 'NEGATIVE') AS negative
                        FROM agent_feedbacks f
                        JOIN agent_sessions s ON f.agent_session_id = s.agent_session_id
                        WHERE s.parent_session_id IS NULL
                          AND f.created_at >= :date_from AND f.created_at < :date_to {af}
                    )
                    SELECT err.total_errors,
                           ROUND(err.total_errors::numeric / NULLIF(sess.total, 0), 3) AS error_rate,
                           fb.positive AS positive_feedback,
                           fb.negative AS negative_feedback
                    FROM err, sess, fb
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_errors_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, m.created_at) AS bucket,
                           a.display_name AS agent_name,
                           COUNT(*) AS error_count
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL AND m.role = 'SYSTEM'
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY bucket, a.display_name ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_error_patterns(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT LEFT(m.content, 100) AS error_pattern,
                           COUNT(*)             AS occurrences,
                           MAX(m.created_at)    AS last_seen
                    FROM agent_session_messages m
                    JOIN agent_sessions s ON m.agent_session_id = s.agent_session_id
                    WHERE s.parent_session_id IS NULL AND m.role = 'SYSTEM'
                      AND m.content IS NOT NULL
                      AND m.created_at >= :date_from AND m.created_at < :date_to
                      {af}
                    GROUP BY LEFT(m.content, 100)
                    ORDER BY occurrences DESC LIMIT 20
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_feedback_by_agent(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name, f.rating, COUNT(*) AS count
                    FROM agent_feedbacks f
                    JOIN agent_sessions s ON f.agent_session_id = s.agent_session_id
                    JOIN agents a ON s.agent_id = a.agent_id
                    WHERE s.parent_session_id IS NULL
                      AND f.created_at >= :date_from AND f.created_at < :date_to
                      {af}
                    GROUP BY a.display_name, f.rating
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_feedback_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, f.created_at) AS bucket,
                           f.rating,
                           COUNT(*) AS count
                    FROM agent_feedbacks f
                    JOIN agent_sessions s ON f.agent_session_id = s.agent_session_id
                    WHERE s.parent_session_id IS NULL
                      AND f.created_at >= :date_from AND f.created_at < :date_to
                      {af}
                    GROUP BY bucket, f.rating ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 5: Schedules ─────────────────────────────────────────────────────────


@retry_db
async def _fetch_schedule_kpis(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> dict[str, Any]:
    af, ap = _agent_filter(agent_id, alias="sch")
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text(f"""
                    WITH runs AS (
                        SELECT r.status
                        FROM agent_schedule_runs r
                        JOIN agent_schedules sch ON r.agent_schedule_id = sch.agent_schedule_id
                        WHERE sch.deleted_at IS NULL
                          AND r.created_at >= :date_from AND r.created_at < :date_to
                          {af}
                    )
                    SELECT
                        (SELECT COUNT(*) FROM runs) AS total_runs,
                        (SELECT ROUND(
                            COUNT(*) FILTER (WHERE status = 'COMPLETED')::numeric
                            / NULLIF(COUNT(*), 0), 3
                        ) FROM runs) AS success_rate,
                        (SELECT COUNT(*) FROM agent_schedules sch
                         WHERE sch.status IN ('PENDING', 'IN_PROGRESS')
                           AND sch.deleted_at IS NULL {af}) AS active_schedules
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_schedule_run_outcomes(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id, alias="sch")
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT r.status, COUNT(*) AS count
                    FROM agent_schedule_runs r
                    JOIN agent_schedules sch ON r.agent_schedule_id = sch.agent_schedule_id
                    WHERE sch.deleted_at IS NULL
                      AND r.created_at >= :date_from AND r.created_at < :date_to
                      {af}
                    GROUP BY r.status
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_schedule_runs_over_time(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id, alias="sch")
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT date_trunc(:gran, r.created_at) AS bucket,
                           r.status, COUNT(*) AS count
                    FROM agent_schedule_runs r
                    JOIN agent_schedules sch ON r.agent_schedule_id = sch.agent_schedule_id
                    WHERE sch.deleted_at IS NULL
                      AND r.created_at >= :date_from AND r.created_at < :date_to
                      {af}
                    GROUP BY bucket, r.status ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_schedule_status_dist(agent_id: uuid.UUID | None = None) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id, alias="sch")
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT sch.status, COUNT(*) AS count
                    FROM agent_schedules sch
                    WHERE sch.deleted_at IS NULL {af}
                    GROUP BY sch.status
                """),
                params=ap,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_top_failing_schedules(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    af, ap = _agent_filter(agent_id, alias="sch")
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT COALESCE(sch.name, sch.agent_schedule_id::text) AS schedule_name,
                           a.display_name AS agent_name,
                           COUNT(*) FILTER (WHERE r.status = 'FAILED') AS failure_count,
                           COUNT(*) AS total_runs,
                           (
                               ARRAY_AGG(r.error ORDER BY r.created_at DESC)
                               FILTER (WHERE r.error IS NOT NULL)
                           )[1] AS last_error
                    FROM agent_schedule_runs r
                    JOIN agent_schedules sch ON r.agent_schedule_id = sch.agent_schedule_id
                    JOIN agents a ON sch.agent_id = a.agent_id
                    WHERE sch.deleted_at IS NULL
                      AND r.created_at >= :date_from AND r.created_at < :date_to
                      {af}
                    GROUP BY sch.agent_schedule_id, sch.name, a.display_name
                    HAVING COUNT(*) FILTER (WHERE r.status = 'FAILED') > 0
                    ORDER BY failure_count DESC LIMIT 20
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ── Tab 6: MCP Tools ─────────────────────────────────────────────────────────


@retry_db
async def _fetch_mcp_kpis(date_from: date, date_to: date) -> dict[str, Any]:
    params: dict[str, Any] = _dt_range(date_from, date_to)
    async with get_async_session_read_replica() as session:
        row = (
            await session.execute(
                text("""
                    SELECT
                        COUNT(*) AS total_calls,
                        ROUND(
                            COUNT(*) FILTER (WHERE status = 'SUCCESS')::numeric
                            / NULLIF(COUNT(*), 0), 3
                        ) AS success_rate,
                        ROUND(AVG(execution_time_ms)::numeric, 1) AS avg_exec_time_ms
                    FROM mcp_audit_logs
                    WHERE deleted_at IS NULL
                      AND created_at >= :date_from AND created_at < :date_to
                """),
                params=params,
            )
        ).one()
        return dict(row._mapping)


@retry_db
async def _fetch_mcp_calls_over_time(date_from: date, date_to: date, granularity: str) -> pd.DataFrame:
    params: dict[str, Any] = {**_dt_range(date_from, date_to), "gran": granularity}
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT date_trunc(:gran, created_at) AS bucket,
                           status::text AS status,
                           COUNT(*) AS count
                    FROM mcp_audit_logs
                    WHERE deleted_at IS NULL
                      AND created_at >= :date_from AND created_at < :date_to
                    GROUP BY bucket, status ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_mcp_tools_by_usage(date_from: date, date_to: date) -> pd.DataFrame:
    params: dict[str, Any] = _dt_range(date_from, date_to)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT tool_name,
                           COUNT(*) AS usage_count,
                           COUNT(*) FILTER (WHERE status = 'SUCCESS') AS success_count,
                           COUNT(*) FILTER (WHERE status = 'FAILED') AS failure_count
                    FROM mcp_audit_logs
                    WHERE deleted_at IS NULL
                      AND created_at >= :date_from AND created_at < :date_to
                    GROUP BY tool_name ORDER BY usage_count DESC
                    LIMIT 30
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_mcp_failing_tools(date_from: date, date_to: date) -> pd.DataFrame:
    params: dict[str, Any] = _dt_range(date_from, date_to)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT tool_name,
                           COUNT(*) AS failure_count,
                           (ARRAY_AGG(error_message ORDER BY created_at DESC)
                            FILTER (WHERE error_message IS NOT NULL))[1] AS last_error,
                           MAX(created_at) AS last_failure
                    FROM mcp_audit_logs
                    WHERE deleted_at IS NULL
                      AND status = 'FAILED'
                      AND created_at >= :date_from AND created_at < :date_to
                    GROUP BY tool_name
                    ORDER BY failure_count DESC
                    LIMIT 20
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_mcp_top_users(date_from: date, date_to: date) -> pd.DataFrame:
    params: dict[str, Any] = _dt_range(date_from, date_to)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT email,
                           COUNT(*) AS call_count,
                           COUNT(DISTINCT tool_name) AS tools_used,
                           ROUND(AVG(execution_time_ms)::numeric, 1) AS avg_exec_ms
                    FROM mcp_audit_logs
                    WHERE deleted_at IS NULL
                      AND email IS NOT NULL
                      AND created_at >= :date_from AND created_at < :date_to
                    GROUP BY email
                    ORDER BY call_count DESC
                    LIMIT 20
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PAGE
# ═══════════════════════════════════════════════════════════════════════════════

st.title("Agent Harness Dashboard")

# ── Load agents ──

if "ahd_agents" not in st.session_state:
    with st.spinner("Loading agents…"):
        st.session_state.ahd_agents = run_coroutine_in_lit_worker(_fetch_agents(), timeout=30)

agents: list[Agent] = st.session_state.ahd_agents
agent_options: dict[str, uuid.UUID | None] = {"(all)": None}
agent_options.update({f"{a.display_name} ({a.name})": a.agent_id for a in agents})

# ── Global filters ──

_today = datetime.now(UTC).date()

fcols = st.columns([2, 1, 1, 1])
with fcols[0]:
    selected_agent_name = st.selectbox("Agent", list(agent_options.keys()), index=0)
    selected_agent_id = agent_options.get(selected_agent_name)
with fcols[1]:
    default_from = _today - timedelta(days=7)
    filter_from = st.date_input("From", value=default_from, key="ahd_from")
with fcols[2]:
    filter_to = st.date_input("To", value=_today, key="ahd_to")
with fcols[3]:
    granularity = st.selectbox("Granularity", ["day", "hour"], index=0)

assert isinstance(filter_from, date)
assert isinstance(filter_to, date)
assert isinstance(granularity, str)

if filter_from > filter_to:
    st.warning("'From' date is after 'To' date. Please adjust the date range.")
    st.stop()

st.divider()

_query_scope = f"{selected_agent_id or 'all'}|{filter_from.isoformat()}|{filter_to.isoformat()}|{granularity}"


def _safe_load_cached(label: str, coro: Any, default: Any = None, *, timeout: float = 60) -> Any:
    return _safe_load(
        label,
        coro,
        default=default,
        timeout=timeout,
        cache_key=f"{_query_scope}:{label}",
    )


# ── Tabs ──

tab_labels = ["📊 Usage", "💰 Cost", "⚡ Latency", "❌ Errors & Feedback", "📅 Schedules", "🔧 MCP Tools"]
tabs = st.tabs(tab_labels)

# ── Tab 1: Usage & Sessions ──────────────────────────────────────────────────

with tabs[0]:
    kpis = _safe_load_cached("session KPIs", _fetch_session_kpis(selected_agent_id, filter_from, filter_to), default={})
    if kpis:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sessions", f"{kpis.get('total_sessions', 0):,}")
        c2.metric("Active Sessions", f"{kpis.get('active_sessions', 0):,}")
        c3.metric("Avg Msgs / Session", kpis.get("avg_messages", 0))
        c4.metric("Unique Agents", kpis.get("unique_agents", 0))

    df_sessions = _safe_load_cached(
        "sessions over time",
        _fetch_sessions_over_time(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_sessions.empty:
        fig = px.bar(
            df_sessions,
            x="bucket",
            y="session_count",
            color="agent_name",
            title="Sessions Over Time",
            labels={"bucket": "", "session_count": "Sessions"},
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No session data for selected range.")

    col_a, col_b = st.columns(2)
    with col_a:
        df_status = _safe_load_cached(
            "session status", _fetch_session_status_dist(selected_agent_id, filter_from, filter_to)
        )
        if not df_status.empty:
            fig = px.pie(df_status, names="status", values="count", title="Session Status Distribution")
            st.plotly_chart(fig, width="stretch")
    with col_b:
        df_trigger = _safe_load_cached(
            "session triggers", _fetch_session_trigger_dist(selected_agent_id, filter_from, filter_to)
        )
        if not df_trigger.empty:
            fig = px.bar(
                df_trigger,
                x="trigger",
                y="count",
                title="Sessions by Trigger",
                labels={"trigger": "", "count": "Sessions"},
            )
            st.plotly_chart(fig, width="stretch")

    # ── User Activity ──
    st.subheader("User Activity")

    col_users, col_unique = st.columns(2)
    with col_users:
        df_by_user = _safe_load_cached(
            "sessions by user", _fetch_sessions_by_user(selected_agent_id, filter_from, filter_to)
        )
        if not df_by_user.empty:
            df_by_user = _resolve_slack_names(df_by_user)
            fig = px.bar(
                df_by_user,
                x="session_count",
                y="slack_user_id",
                orientation="h",
                title="Top Users by Session Count",
                labels={"session_count": "Sessions", "slack_user_id": ""},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No user activity data for selected range.")
    with col_unique:
        df_unique = _safe_load_cached(
            "unique users over time",
            _fetch_unique_users_over_time(selected_agent_id, filter_from, filter_to, granularity),
        )
        if not df_unique.empty:
            fig = px.line(
                df_unique,
                x="bucket",
                y="unique_users",
                title="Unique Users Over Time",
                labels={"bucket": "", "unique_users": "Unique Users"},
                markers=True,
            )
            st.plotly_chart(fig, width="stretch")

# ── Tab 2: Cost Analytics ────────────────────────────────────────────────────

with tabs[1]:
    cost_kpis = _safe_load_cached("cost KPIs", _fetch_cost_kpis(selected_agent_id, filter_from, filter_to), default={})
    if cost_kpis:
        c1, c2, c3 = st.columns(3)
        priced_messages = int(cost_kpis.get("priced_messages") or 0)
        if priced_messages == 0:
            c1.metric("Total Cost", "N/A")
            c2.metric("Avg Cost / Session", "N/A")
            c3.metric("Avg Cost / Turn", "N/A")
            st.info("No cost data captured for the selected range (all matching `cost_usd` values are null).")
        else:
            c1.metric("Total Cost", f"${float(cost_kpis.get('total_cost', 0)):.2f}")
            c2.metric("Avg Cost / Session", f"${float(cost_kpis.get('avg_cost_per_session', 0)):.4f}")
            c3.metric("Avg Cost / Turn", f"${float(cost_kpis.get('avg_cost_per_turn', 0)):.4f}")

    df_cost = _safe_load_cached(
        "cost over time",
        _fetch_cost_over_time(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_cost.empty:
        fig = px.bar(
            df_cost,
            x="bucket",
            y="cost",
            color="agent_name",
            title="Cost Over Time",
            labels={"bucket": "", "cost": "Cost (USD)"},
        )
        st.plotly_chart(fig, width="stretch")

        # Cumulative cost
        df_cum = df_cost.groupby("bucket", as_index=False)["cost"].sum().sort_values("bucket")
        df_cum["cost"] = df_cum["cost"].astype(float)
        df_cum["cumulative_cost"] = df_cum["cost"].cumsum()
        fig = px.area(
            df_cum,
            x="bucket",
            y="cumulative_cost",
            title="Cumulative Cost",
            labels={"bucket": "", "cumulative_cost": "Cumulative Cost (USD)"},
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No cost data for selected range.")

    df_cost_dist = _safe_load_cached(
        "cost distribution", _fetch_cost_per_session(selected_agent_id, filter_from, filter_to)
    )
    if not df_cost_dist.empty:
        fig = px.box(
            df_cost_dist,
            x="agent_name",
            y="session_cost",
            title="Cost Distribution per Session",
            labels={"agent_name": "", "session_cost": "Session Cost (USD)"},
        )
        st.plotly_chart(fig, width="stretch")

# ── Tab 3: Latency & Performance ─────────────────────────────────────────────

with tabs[2]:
    lat_kpis = _safe_load_cached(
        "latency KPIs", _fetch_latency_kpis(selected_agent_id, filter_from, filter_to), default={}
    )
    if lat_kpis and lat_kpis.get("p50") is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("Median Latency", f"{float(lat_kpis['p50']):,.0f} ms")
        c2.metric("p90 Latency", f"{float(lat_kpis['p90']):,.0f} ms")
        c3.metric("p99 Latency", f"{float(lat_kpis['p99']):,.0f} ms")

    df_lat = _safe_load_cached(
        "latency over time",
        _fetch_latency_over_time(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_lat.empty:
        df_melted = df_lat.melt(
            id_vars=["bucket"],
            value_vars=["p50", "p90", "p99"],
            var_name="percentile",
            value_name="latency_ms",
        )
        fig = px.line(
            df_melted,
            x="bucket",
            y="latency_ms",
            color="percentile",
            title="Latency Percentiles Over Time",
            labels={"bucket": "", "latency_ms": "Latency (ms)"},
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No latency data for selected range.")

    df_lat_dist = _safe_load_cached(
        "latency distribution", _fetch_latency_distribution(selected_agent_id, filter_from, filter_to)
    )
    if not df_lat_dist.empty:
        col_a, col_b = st.columns(2)
        with col_a:
            fig = px.histogram(
                df_lat_dist,
                x="duration_ms",
                color="agent_name",
                nbins=50,
                title="Latency Distribution",
                labels={"duration_ms": "Duration (ms)"},
            )
            st.plotly_chart(fig, width="stretch")
        with col_b:
            fig = px.box(
                df_lat_dist,
                x="agent_name",
                y="duration_ms",
                title="Latency by Agent",
                labels={"agent_name": "", "duration_ms": "Duration (ms)"},
            )
            st.plotly_chart(fig, width="stretch")

    df_tools = _safe_load_cached("tool usage", _fetch_tool_usage(selected_agent_id, filter_from, filter_to))
    if not df_tools.empty:
        fig = px.bar(
            df_tools,
            x="usage_count",
            y="tool_name",
            orientation="h",
            title="Tool Usage Frequency",
            labels={"usage_count": "Usage Count", "tool_name": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")

# ── Tab 4: Errors & Feedback ─────────────────────────────────────────────────

with tabs[3]:
    ef_kpis = _safe_load_cached(
        "error & feedback KPIs",
        _fetch_error_feedback_kpis(selected_agent_id, filter_from, filter_to),
        default={},
    )
    if ef_kpis:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Errors", f"{ef_kpis.get('total_errors', 0):,}")
        rate = ef_kpis.get("error_rate", 0) or 0
        c2.metric("Error Rate", f"{float(rate):.1%}")
        c3.metric("Positive Feedback", f"{ef_kpis.get('positive_feedback', 0):,}")
        c4.metric("Negative Feedback", f"{ef_kpis.get('negative_feedback', 0):,}")

    df_err = _safe_load_cached(
        "errors over time",
        _fetch_errors_over_time(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_err.empty:
        fig = px.line(
            df_err,
            x="bucket",
            y="error_count",
            color="agent_name",
            title="Errors Over Time",
            labels={"bucket": "", "error_count": "Errors"},
        )
        st.plotly_chart(fig, width="stretch")

    df_patterns = _safe_load_cached("error patterns", _fetch_error_patterns(selected_agent_id, filter_from, filter_to))
    if not df_patterns.empty:
        st.subheader("Top Error Patterns")
        st.dataframe(df_patterns, width="stretch", hide_index=True)

    col_a, col_b = st.columns(2)
    with col_a:
        df_fb_agent = _safe_load_cached(
            "feedback by agent", _fetch_feedback_by_agent(selected_agent_id, filter_from, filter_to)
        )
        if not df_fb_agent.empty:
            fig = px.bar(
                df_fb_agent,
                x="agent_name",
                y="count",
                color="rating",
                barmode="stack",
                title="Feedback by Agent",
                labels={"agent_name": "", "count": "Count"},
                color_discrete_map={"POSITIVE": "#2ecc71", "NEGATIVE": "#e74c3c"},
            )
            st.plotly_chart(fig, width="stretch")
    with col_b:
        df_fb_time = _safe_load_cached(
            "feedback over time",
            _fetch_feedback_over_time(selected_agent_id, filter_from, filter_to, granularity),
        )
        if not df_fb_time.empty:
            fig = px.line(
                df_fb_time,
                x="bucket",
                y="count",
                color="rating",
                title="Feedback Over Time",
                labels={"bucket": "", "count": "Count"},
                color_discrete_map={"POSITIVE": "#2ecc71", "NEGATIVE": "#e74c3c"},
            )
            st.plotly_chart(fig, width="stretch")

# ── Tab 5: Schedules ─────────────────────────────────────────────────────────

with tabs[4]:
    sch_kpis = _safe_load_cached(
        "schedule KPIs", _fetch_schedule_kpis(selected_agent_id, filter_from, filter_to), default={}
    )
    if sch_kpis:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Runs", f"{sch_kpis.get('total_runs', 0):,}")
        sr = sch_kpis.get("success_rate", 0) or 0
        c2.metric("Success Rate", f"{float(sr):.1%}")
        c3.metric("Active Schedules", f"{sch_kpis.get('active_schedules', 0):,}")

    col_a, col_b = st.columns(2)
    with col_a:
        df_outcomes = _safe_load_cached(
            "run outcomes", _fetch_schedule_run_outcomes(selected_agent_id, filter_from, filter_to)
        )
        if not df_outcomes.empty:
            fig = px.pie(df_outcomes, names="status", values="count", title="Schedule Run Outcomes")
            st.plotly_chart(fig, width="stretch")
    with col_b:
        df_sch_status = _safe_load_cached("schedule status", _fetch_schedule_status_dist(selected_agent_id))
        if not df_sch_status.empty:
            fig = px.bar(
                df_sch_status,
                x="status",
                y="count",
                title="Schedule Status Distribution",
                labels={"status": "", "count": "Count"},
            )
            st.plotly_chart(fig, width="stretch")

    df_runs_time = _safe_load_cached(
        "runs over time",
        _fetch_schedule_runs_over_time(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_runs_time.empty:
        fig = px.bar(
            df_runs_time,
            x="bucket",
            y="count",
            color="status",
            title="Schedule Runs Over Time",
            labels={"bucket": "", "count": "Runs"},
        )
        st.plotly_chart(fig, width="stretch")

    df_failing = _safe_load_cached(
        "top failing schedules",
        _fetch_top_failing_schedules(selected_agent_id, filter_from, filter_to),
    )
    if not df_failing.empty:
        st.subheader("Top Failing Schedules")
        st.dataframe(df_failing, width="stretch", hide_index=True)

# ── Tab 6: MCP Tools ─────────────────────────────────────────────────────────

_mcp_scope = f"mcp|{filter_from.isoformat()}|{filter_to.isoformat()}|{granularity}"


def _safe_load_mcp(label: str, coro: Any, default: Any = None, *, timeout: float = 60) -> Any:
    return _safe_load(label, coro, default=default, timeout=timeout, cache_key=f"{_mcp_scope}:{label}")


with tabs[5]:
    st.caption(
        "MCP tool calls are user-level (not agent-level). "
        "The agent filter does not apply to this tab; date range and granularity still apply."
    )

    mcp_kpis = _safe_load_mcp("MCP KPIs", _fetch_mcp_kpis(filter_from, filter_to), default={})
    if mcp_kpis:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Calls", f"{mcp_kpis.get('total_calls', 0):,}")
        sr = mcp_kpis.get("success_rate", 0) or 0
        c2.metric("Success Rate", f"{float(sr):.1%}")
        avg_ms = mcp_kpis.get("avg_exec_time_ms", 0) or 0
        c3.metric("Avg Execution Time", f"{float(avg_ms):,.1f} ms")

    df_mcp_time = _safe_load_mcp("MCP calls over time", _fetch_mcp_calls_over_time(filter_from, filter_to, granularity))
    if not df_mcp_time.empty:
        fig = px.bar(
            df_mcp_time,
            x="bucket",
            y="count",
            color="status",
            title="MCP Calls Over Time",
            labels={"bucket": "", "count": "Calls"},
            color_discrete_map={"SUCCESS": "#2ecc71", "FAILED": "#e74c3c"},
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No MCP tool call data for selected range.")

    df_mcp_tools = _safe_load_mcp("MCP tools by usage", _fetch_mcp_tools_by_usage(filter_from, filter_to))
    if not df_mcp_tools.empty:
        fig = px.bar(
            df_mcp_tools,
            x="usage_count",
            y="tool_name",
            orientation="h",
            title="Top Tools by Usage",
            labels={"usage_count": "Calls", "tool_name": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")

    df_mcp_fail = _safe_load_mcp("MCP failing tools", _fetch_mcp_failing_tools(filter_from, filter_to))
    if not df_mcp_fail.empty:
        st.subheader("Top Failing Tools")
        st.dataframe(df_mcp_fail, width="stretch", hide_index=True)

    df_mcp_users = _safe_load_mcp("MCP top users", _fetch_mcp_top_users(filter_from, filter_to))
    if not df_mcp_users.empty:
        fig = px.bar(
            df_mcp_users,
            x="call_count",
            y="email",
            orientation="h",
            title="Top MCP Users by Call Count",
            labels={"call_count": "Calls", "email": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
