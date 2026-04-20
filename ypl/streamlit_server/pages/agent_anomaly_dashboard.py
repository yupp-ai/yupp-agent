"""Agent Anomaly Detection Dashboard.

Surfaces agents with high cost variance across configurable
time windows, helping catch regressions and runaway agents early.
"""

from __future__ import annotations
import uuid
import warnings
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text
from sqlmodel import col, select
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import Agent
from ypl.streamlit_server.auth import require_auth
from ypl.structured_logger import get_logger

logger = get_logger()

warnings.filterwarnings("ignore", message=".*You probably want to use.*session.exec.*", category=DeprecationWarning)

# ── Page config & auth ───────────────────────────────────────────────────────

st.set_page_config(page_title="Agent Anomaly Detection", page_icon="🔍", layout="wide")
require_auth()


# ── Helpers ──────────────────────────────────────────────────────────────────


_VALID_SQL_ALIASES = frozenset({"s", "a", "m"})


def _agent_filter(agent_id: uuid.UUID | None, alias: str = "s") -> tuple[str, dict[str, Any]]:
    """Return a SQL WHERE fragment and params dict for optional agent-id filtering."""
    if alias not in _VALID_SQL_ALIASES:
        raise ValueError(f"Invalid SQL alias: {alias!r}")
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


_MAX_CACHE_ENTRIES = 50


def _check_cache(cache_key: str | None) -> tuple[bool, Any]:
    """Check if the cache has a hit for the given key. Returns (hit, value)."""
    if cache_key is None:
        return False, None
    cache: dict[str, Any] = st.session_state.get("aad_query_cache", {})
    if cache_key in cache:
        return True, cache[cache_key]
    return False, None


def _safe_load(
    label: str,
    coro: Any,
    default: Any = None,
    *,
    timeout: float = 60,
    cache_key: str | None = None,
) -> Any:
    """Run an async query with spinner and error handling."""
    cache: dict[str, Any] = st.session_state.setdefault("aad_query_cache", {})
    if cache_key is not None and cache_key in cache:
        if hasattr(coro, "close"):
            coro.close()
        return cache[cache_key]

    with st.spinner(f"Loading {label}…"):
        try:
            result = run_coroutine_in_lit_worker(coro, timeout=timeout)
            if cache_key is not None:
                if len(cache) >= _MAX_CACHE_ENTRIES:
                    oldest_key = next(iter(cache))
                    del cache[oldest_key]
                cache[cache_key] = result
            return result
        except Exception as e:
            st.error(f"Error loading {label}: {e}")
            logger.exception("Dashboard query error", query_label=label)
            return default if default is not None else pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════════
# DB QUERY FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════


def _cost_base_query(agent_filter_fragment: str) -> str:
    """Shared FROM/JOIN/WHERE clause used by all cost queries."""
    return f"""
        FROM agent_session_messages m
        JOIN agent_sessions s ON s.agent_session_id = m.agent_session_id
        JOIN agents a ON a.agent_id = s.agent_id
        WHERE s.parent_session_id IS NULL
          AND m.cost_usd IS NOT NULL
          AND m.created_at >= :date_from AND m.created_at < :date_to
          {agent_filter_fragment}
    """


@retry_db
async def _fetch_agents() -> list[Agent]:
    async with get_async_session_read_replica() as session:
        result = await session.exec(select(Agent).order_by(col(Agent.name)))
        return list(result.all())


@retry_db
async def _fetch_cost_variance_by_agent(
    agent_id: uuid.UUID | None, date_from: date, date_to: date, granularity: str
) -> pd.DataFrame:
    """Fetch per-agent, per-bucket cost statistics: mean, stddev, cv, min, max, count."""
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap, "gran": granularity}
    base = _cost_base_query(af)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name,
                           date_trunc(:gran, m.created_at) AS bucket,
                           COUNT(*) AS n_messages,
                           AVG(m.cost_usd)::float AS mean_cost,
                           STDDEV(m.cost_usd)::float AS std_cost,
                           MIN(m.cost_usd)::float AS min_cost,
                           MAX(m.cost_usd)::float AS max_cost
                    {base}
                    GROUP BY a.display_name, bucket
                    HAVING COUNT(*) >= 2
                    ORDER BY bucket
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_cost_summary_by_agent(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    """Fetch overall cost statistics per agent across the full date range."""
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    base = _cost_base_query(af)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name,
                           COUNT(*) AS n_messages,
                           AVG(m.cost_usd)::float AS mean_cost,
                           STDDEV(m.cost_usd)::float AS std_cost,
                           MIN(m.cost_usd)::float AS min_cost,
                           MAX(m.cost_usd)::float AS max_cost,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY m.cost_usd)::float AS median_cost,
                           percentile_cont(0.9) WITHIN GROUP (ORDER BY m.cost_usd)::float AS p90_cost,
                           SUM(m.cost_usd)::float AS total_cost
                    {base}
                    GROUP BY a.display_name
                    HAVING COUNT(*) >= 2
                    ORDER BY STDDEV(m.cost_usd) DESC NULLS LAST
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


@retry_db
async def _fetch_cost_per_session_by_agent(agent_id: uuid.UUID | None, date_from: date, date_to: date) -> pd.DataFrame:
    """Fetch per-session cost for box-plot distribution, grouped by agent."""
    af, ap = _agent_filter(agent_id)
    params: dict[str, Any] = {**_dt_range(date_from, date_to), **ap}
    base = _cost_base_query(af)
    async with get_async_session_read_replica() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT a.display_name AS agent_name,
                           s.agent_session_id,
                           SUM(m.cost_usd)::float AS session_cost
                    {base}
                    GROUP BY a.display_name, s.agent_session_id
                """),
                params=params,
            )
        ).all()
    return _rows_to_df(rows)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN PAGE
# ═════════════════════════════════════════════════════════════════════════════

st.title("Agent Anomaly Detection")
st.caption("Identify agents with high cost variance across configurable time windows.")

# ── Load agents ──

if "aad_agents" not in st.session_state:
    st.session_state.aad_agents = _safe_load("agents", _fetch_agents(), default=[], timeout=30)

agents: list[Agent] = st.session_state.aad_agents
if not agents:
    st.warning("Could not load agents. Please refresh the page.")
    st.stop()
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
    filter_from: date | tuple[date, ...] = st.date_input("From", value=default_from, key="aad_from")
with fcols[2]:
    filter_to: date | tuple[date, ...] = st.date_input("To", value=_today, key="aad_to")
with fcols[3]:
    granularity: str | None = st.selectbox("Granularity", ["day", "hour"], index=0)

if not isinstance(filter_from, date) or not isinstance(filter_to, date):
    st.warning("Please select valid single dates (not a range).")
    st.stop()
if not isinstance(granularity, str):
    st.stop()

if filter_from > filter_to:
    st.warning("'From' date is after 'To' date. Please adjust the date range.")
    st.stop()

st.divider()

_query_scope = f"{selected_agent_id or 'all'}|{filter_from.isoformat()}|{filter_to.isoformat()}|{granularity}"


def _safe_load_cached(label: str, coro: Any, default: Any = None, *, timeout: float = 60) -> Any:
    key = f"{_query_scope}:{label}"
    hit, value = _check_cache(key)
    if hit:
        if hasattr(coro, "close"):
            coro.close()
        return value
    return _safe_load(
        label,
        coro,
        default=default,
        timeout=timeout,
        cache_key=key,
    )


# ── Cost Variance ────────────────────────────────────────────────────────────

# Summary table sorted by CV
df_cost_summary = _safe_load_cached(
    "cost summary by agent",
    _fetch_cost_summary_by_agent(selected_agent_id, filter_from, filter_to),
)

if not df_cost_summary.empty:
    # Compute coefficient of variation
    df_cost_summary["cv"] = df_cost_summary.apply(
        lambda r: r["std_cost"] / r["mean_cost"] if r["mean_cost"] and r["mean_cost"] > 0 else 0,
        axis=1,
    )
    df_cost_summary = df_cost_summary.sort_values("cv", ascending=False)

    # KPI row: highest-variance agent
    top = df_cost_summary.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Highest CV Agent", top["agent_name"])
    c2.metric("CV (Coeff. of Variation)", f"{top['cv']:.2f}")
    c3.metric("Mean Cost", f"${top['mean_cost']:.4f}")
    c4.metric("Std Dev", f"${top['std_cost']:.4f}")

    # Summary table
    st.subheader("Cost Variance by Agent")
    display_df = df_cost_summary[
        [
            "agent_name",
            "n_messages",
            "mean_cost",
            "std_cost",
            "cv",
            "min_cost",
            "max_cost",
            "median_cost",
            "p90_cost",
            "total_cost",
        ]
    ].copy()
    display_df.columns = [
        "Agent",
        "Messages",
        "Mean ($)",
        "Std Dev ($)",
        "CV",
        "Min ($)",
        "Max ($)",
        "Median ($)",
        "P90 ($)",
        "Total ($)",
    ]
    # Format numeric columns
    for col_name in [
        "Mean ($)",
        "Std Dev ($)",
        "Min ($)",
        "Max ($)",
        "Median ($)",
        "P90 ($)",
        "Total ($)",
    ]:
        display_df[col_name] = display_df[col_name].apply(lambda v: f"{v:.4f}" if v is not None else "0")
    display_df["CV"] = display_df["CV"].apply(lambda v: f"{v:.2f}" if v is not None else "0")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Cost over time with variance bands per agent
    st.subheader("Cost Variance Over Time")
    df_cost_time = _safe_load_cached(
        "cost variance over time",
        _fetch_cost_variance_by_agent(selected_agent_id, filter_from, filter_to, granularity),
    )
    if not df_cost_time.empty:
        # Compute CV per bucket
        df_cost_time["cv"] = df_cost_time.apply(
            lambda r: r["std_cost"] / r["mean_cost"] if r["mean_cost"] and r["mean_cost"] > 0 else 0,
            axis=1,
        )

        # Mean cost with +/- 1 std band per agent
        agent_names = df_cost_time["agent_name"].unique()
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        for i, agent in enumerate(agent_names):
            agent_df = df_cost_time[df_cost_time["agent_name"] == agent].sort_values("bucket")
            color = colors[i % len(colors)]

            # Upper bound (mean + std)
            upper = agent_df["mean_cost"] + agent_df["std_cost"].fillna(0)
            lower = (agent_df["mean_cost"] - agent_df["std_cost"].fillna(0)).clip(lower=0)

            fig.add_trace(
                go.Scatter(
                    x=agent_df["bucket"],
                    y=upper,
                    mode="lines",
                    line={"width": 0},
                    showlegend=False,
                    hoverinfo="skip",
                )
            )
            r, g, b = px.colors.hex_to_rgb(color)
            band_color = f"rgba({r},{g},{b},0.15)"
            fig.add_trace(
                go.Scatter(
                    x=agent_df["bucket"],
                    y=lower,
                    mode="lines",
                    line={"width": 0},
                    fill="tonexty",
                    fillcolor=band_color,
                    showlegend=False,
                    hoverinfo="skip",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=agent_df["bucket"],
                    y=agent_df["mean_cost"],
                    mode="lines+markers",
                    name=agent,
                    line={"color": color},
                    hovertemplate=(
                        f"{agent}<br>Mean: $%{{y:.4f}}<br>"
                        f"Std: $%{{customdata[0]:.4f}}<br>"
                        f"CV: %{{customdata[1]:.2f}}"
                        f"<extra></extra>"
                    ),
                    customdata=agent_df[["std_cost", "cv"]].values,
                )
            )
        fig.update_layout(
            title="Mean Cost per Turn with ±1σ Band",
            xaxis_title="",
            yaxis_title="Cost (USD)",
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)

        # CV over time
        fig_cv = px.line(
            df_cost_time,
            x="bucket",
            y="cv",
            color="agent_name",
            title="Coefficient of Variation Over Time",
            labels={
                "bucket": "",
                "cv": "CV (σ/μ)",
                "agent_name": "Agent",
            },
            markers=True,
        )
        fig_cv.add_hline(
            y=1.0,
            line_dash="dash",
            line_color="red",
            annotation_text="CV=1 (high variance)",
        )
        st.plotly_chart(fig_cv, use_container_width=True)
    else:
        st.info("Not enough data points per bucket for variance analysis.")

    # Box plot: cost distribution per session by agent
    st.subheader("Session Cost Distribution")
    df_session_cost = _safe_load_cached(
        "session cost distribution",
        _fetch_cost_per_session_by_agent(selected_agent_id, filter_from, filter_to),
    )
    if not df_session_cost.empty:
        fig = px.box(
            df_session_cost,
            x="agent_name",
            y="session_cost",
            title="Cost Distribution per Session",
            labels={
                "agent_name": "",
                "session_cost": "Session Cost (USD)",
            },
            points="outliers",
        )
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No cost data for selected range.")
