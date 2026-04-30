"""Feedbacks — browse agent feedback signals across all sessions."""

from __future__ import annotations
import html
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from ypl.backend.db import get_async_session_read_replica, retry_db
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import (
    Agent,
    AgentFeedback,
    AgentFeedbackRating,
    AgentSession,
    AgentSessionTrigger,
)
from ypl.streamlit_server.auth import require_auth

st.set_page_config(page_title="Feedbacks", page_icon="⭐", layout="wide")
require_auth()

st.title("⭐ Feedbacks")

# ── Timezone helper ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _to_local(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_PACIFIC)
    return local.strftime(f"%Y-%m-%d %H:%M:%S {local.strftime('%Z')}")


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_feedbacks(
    limit: int = 100,
    rating: AgentFeedbackRating | None = None,
    agent_name: str | None = None,
    trigger: AgentSessionTrigger | None = None,
) -> list[dict[str, Any]]:
    async with get_async_session_read_replica() as session:
        query = (
            select(AgentFeedback)
            .options(
                selectinload(AgentFeedback.message),  # type: ignore[arg-type]
                selectinload(AgentFeedback.session).selectinload(AgentSession.agent),  # type: ignore[arg-type]
            )
            .order_by(col(AgentFeedback.created_at).desc())
        )
        if rating is not None:
            query = query.where(col(AgentFeedback.rating) == rating)
        if trigger is not None or agent_name:
            # Need to filter by columns on AgentSession
            query = query.join(AgentSession, col(AgentFeedback.agent_session_id) == col(AgentSession.agent_session_id))
            if trigger is not None:
                query = query.where(col(AgentSession.trigger) == trigger)
            if agent_name:
                query = query.join(Agent, col(AgentSession.agent_id) == col(Agent.agent_id)).where(
                    col(Agent.name) == agent_name
                )
        query = query.limit(limit)
        rows = list((await session.exec(query)).all())

    out: list[dict[str, Any]] = []
    for fb in rows:
        msg_content = fb.message.content[:200] if fb.message and fb.message.content else None
        sess = fb.session
        agent = sess.agent if sess else None
        out.append(
            {
                "feedback_id": str(fb.agent_feedback_id),
                "rating": fb.rating.value if fb.rating else None,
                "comment": fb.comment,
                "structured": fb.structured,
                "user_id": fb.user_id,
                "created_at": fb.created_at,
                "agent_session_id": str(fb.agent_session_id),
                "agent_session_message_id": (str(fb.agent_session_message_id) if fb.agent_session_message_id else None),
                "message_preview": msg_content,
                "agent_name": agent.name if agent else None,
                "agent_display_name": agent.display_name if agent else None,
                "session_title": sess.title if sess else None,
                "session_trigger": sess.trigger.value if sess and sess.trigger else None,
                "session_status": sess.status.value if sess and sess.status else None,
                "session_created_at": sess.created_at if sess else None,
            }
        )
    return out


@retry_db
async def fetch_agent_names_with_feedback() -> list[str]:
    async with get_async_session_read_replica() as session:
        stmt = (
            select(Agent.name)
            .join(AgentSession, col(Agent.agent_id) == col(AgentSession.agent_id))
            .join(AgentFeedback, col(AgentSession.agent_session_id) == col(AgentFeedback.agent_session_id))
            .group_by(Agent.name)
            .order_by(Agent.name)
        )
        return [row for row in (await session.exec(stmt)).all() if row]


# ── Cached wrappers ──────────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _cached_agent_names() -> list[str]:
    return run_coroutine_in_lit_worker(fetch_agent_names_with_feedback(), timeout=30)


def _refresh_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _rating_badge(rating_str: str | None) -> str:
    label = rating_str or "—"
    color = (
        "#388e3c"
        if rating_str == "POSITIVE"
        else "#d32f2f"
        if rating_str == "NEGATIVE"
        else "#f57c00"
        if rating_str == "NEUTRAL"
        else "#999"
    )
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:0.85em;">{html.escape(label)}</span>'
    )


def _trigger_badge(trigger_str: str | None) -> str:
    if not trigger_str:
        return '<span style="color:#999;">—</span>'
    return f'<code style="background:#eef;padding:1px 6px;border-radius:4px;">{html.escape(trigger_str)}</code>'


# ── Main ─────────────────────────────────────────────────────────────────────

# Filters
filter_cols = st.columns([1.5, 1.5, 1.5, 1, 1])

with filter_cols[0]:
    rating_options = ["(all)"] + [r.value for r in AgentFeedbackRating]
    selected_rating = st.selectbox("Rating", rating_options, key="fb_rating_filter")

with filter_cols[1]:
    trigger_options = ["(all)"] + [t.value for t in AgentSessionTrigger]
    selected_trigger = st.selectbox("Session trigger", trigger_options, key="fb_trigger_filter")

with filter_cols[2]:
    agent_names = _cached_agent_names()
    agent_options = ["(all)"] + agent_names
    selected_agent = st.selectbox("Agent", agent_options, key="fb_agent_filter")

with filter_cols[3]:
    limit_options = [50, 100, 200, 500, 1000]
    fb_limit = st.selectbox("Limit", limit_options, index=1, key="fb_limit")

with filter_cols[4]:
    st.markdown("&nbsp;")
    if st.button("Refresh", key="fb_refresh"):
        _refresh_and_rerun()

rating_filter = None if selected_rating == "(all)" else AgentFeedbackRating(selected_rating)
trigger_filter = None if selected_trigger == "(all)" else AgentSessionTrigger(selected_trigger)
agent_filter = None if selected_agent == "(all)" else selected_agent

with st.spinner("Loading feedbacks…"):
    try:
        feedbacks = run_coroutine_in_lit_worker(
            fetch_feedbacks(
                limit=int(fb_limit),
                rating=rating_filter,
                agent_name=agent_filter,
                trigger=trigger_filter,
            ),
            timeout=60,
        )
    except Exception as exc:
        st.error(f"Error loading feedbacks: {exc}")
        feedbacks = []

st.caption(f"{len(feedbacks)} feedback(s) loaded")

if not feedbacks:
    st.info("No feedback signals match the current filters.")
    st.stop()

# Header row
_BLUE_BOLD = 'style="color:#1976d2;font-weight:bold;font-size:0.85em;"'
_COL_WIDTHS = [1.0, 1.6, 1.0, 1.6, 2.6, 2.6, 1.4]
hdr = st.columns(_COL_WIDTHS)
for i, label in enumerate(("Rating", "Agent", "Trigger", "Session", "Comment", "Message preview", "When")):
    with hdr[i]:
        st.markdown(f"<span {_BLUE_BOLD}>{label}</span>", unsafe_allow_html=True)
st.divider()

for fb in feedbacks:
    row = st.columns(_COL_WIDTHS)

    with row[0]:
        st.markdown(_rating_badge(fb["rating"]), unsafe_allow_html=True)

    with row[1]:
        if fb["agent_display_name"]:
            st.markdown(
                f"**{html.escape(fb['agent_display_name'])}**<br>`{html.escape(fb['agent_name'] or '')}`",
                unsafe_allow_html=True,
            )
        else:
            st.markdown("—")

    with row[2]:
        st.markdown(_trigger_badge(fb["session_trigger"]), unsafe_allow_html=True)

    with row[3]:
        sess_id = fb["agent_session_id"]
        title_html = (
            f"<div style='font-weight:600;'>{html.escape(fb['session_title'])}</div>" if fb["session_title"] else ""
        )
        # Link to the session-detail view in the Agent Harness Console.
        # Streamlit page links use the page link slug (filename without .py).
        console_url = f"agent_harness_console?session_id={sess_id}"
        st.markdown(
            f"{title_html}<a href='{console_url}' target='_self'>Open <code>{sess_id[:8]}…</code></a>",
            unsafe_allow_html=True,
        )

    with row[4]:
        st.markdown(fb["comment"] or "—")
        if fb["structured"]:
            with st.expander("Structured data", expanded=False):
                st.json(fb["structured"])

    with row[5]:
        if fb["message_preview"]:
            preview = fb["message_preview"][:200]
            st.caption(preview + ("…" if len(fb["message_preview"]) >= 200 else ""))
        else:
            st.caption("(session-level)")

    with row[6]:
        st.caption(_to_local(fb["created_at"]))
        if fb["user_id"]:
            st.caption(f"by `{fb['user_id'][:12]}…`" if len(fb["user_id"]) > 12 else f"by `{fb['user_id']}`")

    st.divider()
