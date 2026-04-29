"""Session archival and pending-input queries.

Backs the SAG slash commands ``/archive`` (manual archive of the current
thread's session) and ``/pending`` (list sessions waiting on a turn from
the human or the agent within a recent window).

Also exposes :func:`auto_archive_stale_sessions`, called periodically by
the scheduler to archive Slack sessions whose last message is older than
a configurable threshold (default 7 days).
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlmodel import col, select

from ypl.agent_harness_service.common.types import (
    PendingSessionInfo,
    PendingSessionsResponse,
    SessionArchiveResponse,
)
from ypl.agent_harness_service.service.resolvers import _resolve_session
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()


# Trim the last-message preview to this many chars in /pending output.
_PENDING_PREVIEW_CHARS = 120


async def archive_session(session_id: str) -> SessionArchiveResponse:
    """Archive a session by setting ``status = ARCHIVED``.

    Idempotent — archiving an already-archived session is a no-op and
    returns ``status="already_archived"``.

    Args:
        session_id: AHS session UUID or SAG slack_session_id.

    Raises:
        ValueError: Session not found.
    """
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        if agent_session.status == AgentSessionStatus.ARCHIVED:
            return SessionArchiveResponse(
                session_id=str(agent_session.agent_session_id),
                status="already_archived",
            )

        agent_session.status = AgentSessionStatus.ARCHIVED
        await session.commit()

        logger.info(
            "Archived session",
            session_id=str(agent_session.agent_session_id),
            slack_session_id=agent_session.slack_session_id,
            previous_status=agent_session.status.value,
        )

    return SessionArchiveResponse(
        session_id=str(agent_session.agent_session_id),
        status="archived",
    )


async def list_pending_sessions(
    user_id: str,
    hours_back: int = 24,
) -> PendingSessionsResponse:
    """List the user's Slack sessions that are waiting on a turn.

    A session is considered "pending" if its **last completed**
    USER-or-AGENT message landed within the last ``hours_back`` hours and
    hasn't been followed by a turn from the other side.

    - ``waiting_on="human"``: last completed message is from the agent.
    - ``waiting_on="ai"``:    last completed message is from the user.

    Filters applied:
    - ``creator_user_id == user_id`` (caller's sessions only)
    - ``trigger == SLACK`` (only Slack-originated threads — others have no
      thread/permalink semantics for /pending output)
    - ``status != ARCHIVED``
    - ``parent_session_id IS NULL`` (root sessions only)
    - ``deleted_at IS NULL``

    Messages with ``completion_status = IN_PROGRESS`` (eager-persist
    drafts) and SYSTEM / FELLOW_AGENT roles are ignored when picking the
    "last message".
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    async with get_async_session() as session:
        # Step 1: candidate sessions — small enough to fetch fully.
        sessions_q = (
            select(AgentSession, Agent.name)
            .join(Agent, col(AgentSession.agent_id) == col(Agent.agent_id))
            .where(
                col(AgentSession.creator_user_id) == user_id,
                col(AgentSession.trigger) == AgentSessionTrigger.SLACK,
                col(AgentSession.status) != AgentSessionStatus.ARCHIVED,
                col(AgentSession.parent_session_id).is_(None),
                col(AgentSession.deleted_at).is_(None),
            )
        )
        rows = (await session.exec(sessions_q)).all()
        if not rows:
            return PendingSessionsResponse(hours_back=hours_back)

        session_by_id: dict = {row[0].agent_session_id: row for row in rows}

        # Step 2: latest completed USER/AGENT message per session, via
        # Postgres DISTINCT ON over (turn_number DESC, created_at DESC).
        latest_msgs_q = (
            select(AgentSessionMessage)
            .distinct(col(AgentSessionMessage.agent_session_id))
            .where(
                col(AgentSessionMessage.agent_session_id).in_(list(session_by_id.keys())),
                col(AgentSessionMessage.role).in_([AgentSessionMessageRole.USER, AgentSessionMessageRole.AGENT]),
                col(AgentSessionMessage.completion_status) != AgentSessionMessageCompletionStatus.IN_PROGRESS,
                col(AgentSessionMessage.deleted_at).is_(None),
            )
            .order_by(
                col(AgentSessionMessage.agent_session_id),
                col(AgentSessionMessage.turn_number).desc(),
                col(AgentSessionMessage.created_at).desc(),
            )
        )
        latest_msgs = (await session.exec(latest_msgs_q)).all()

    pending_human: list[PendingSessionInfo] = []
    pending_ai: list[PendingSessionInfo] = []

    for msg in latest_msgs:
        if msg.created_at is None or msg.created_at < cutoff:
            continue
        row = session_by_id.get(msg.agent_session_id)
        if row is None:
            continue
        agent_session: AgentSession = row[0]
        agent_name: str = row[1]

        ctx = agent_session.context or {}
        preview = _format_preview(msg.content)
        info = PendingSessionInfo(
            session_id=str(agent_session.agent_session_id),
            agent_name=agent_name,
            waiting_on="human" if msg.role == AgentSessionMessageRole.AGENT else "ai",
            last_message_at=msg.created_at,
            last_message_role=msg.role.value,
            last_message_preview=preview,
            slack_channel_id=ctx.get("slack_channel_id"),
            slack_channel_name=ctx.get("slack_channel_name"),
            slack_thread_ts=ctx.get("slack_thread_ts"),
            slack_permalink=None,  # SAG resolves permalinks on render.
            title=agent_session.title,
        )
        if info.waiting_on == "human":
            pending_human.append(info)
        else:
            pending_ai.append(info)

    # Most-recent activity first so the user sees what's hot.
    pending_human.sort(key=lambda s: s.last_message_at, reverse=True)
    pending_ai.sort(key=lambda s: s.last_message_at, reverse=True)

    return PendingSessionsResponse(
        pending_human=pending_human,
        pending_ai=pending_ai,
        hours_back=hours_back,
    )


def _format_preview(content: str | None) -> str | None:
    """Trim a message body to a single-line preview for /pending output."""
    if not content:
        return None
    flat = " ".join(content.split())
    if len(flat) <= _PENDING_PREVIEW_CHARS:
        return flat
    return flat[: _PENDING_PREVIEW_CHARS - 1].rstrip() + "…"


async def auto_archive_stale_sessions(days: int = 7) -> int:
    """Archive Slack sessions whose last activity is older than ``days``.

    "Last activity" is the most recent ``created_at`` across the session's
    messages, falling back to the session's own ``modified_at`` when it
    has no messages. Operates only on:

    - Slack-triggered sessions (``trigger == SLACK``)
    - Status in {ACTIVE, COMPLETED, STALE} (already-ARCHIVED rows are skipped)
    - Not soft-deleted (``deleted_at IS NULL``)
    - Root sessions only (``parent_session_id IS NULL``)

    Returns the number of sessions archived in this sweep.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    async with get_async_session() as session:
        # Per-session max(created_at) on agent_session_messages. Use a
        # correlated lateral subquery so sessions with no messages still
        # show up (last_message_at NULL → fall back to session.modified_at).
        last_msg_subq = (
            select(
                AgentSessionMessage.agent_session_id,
                sa.func.max(col(AgentSessionMessage.created_at)).label("last_message_at"),
            )
            .where(col(AgentSessionMessage.deleted_at).is_(None))
            .group_by(col(AgentSessionMessage.agent_session_id))
            .subquery()
        )

        candidates_q = (
            select(
                AgentSession,
                sa.func.coalesce(
                    last_msg_subq.c.last_message_at,
                    col(AgentSession.modified_at),
                ).label("last_activity_at"),
            )
            .join(
                last_msg_subq,
                col(AgentSession.agent_session_id) == last_msg_subq.c.agent_session_id,
                isouter=True,
            )
            .where(
                col(AgentSession.trigger) == AgentSessionTrigger.SLACK,
                col(AgentSession.status).in_(
                    [
                        AgentSessionStatus.ACTIVE,
                        AgentSessionStatus.COMPLETED,
                        AgentSessionStatus.STALE,
                    ]
                ),
                col(AgentSession.parent_session_id).is_(None),
                col(AgentSession.deleted_at).is_(None),
            )
        )

        result = await session.exec(candidates_q)
        archived = 0
        for row in result.all():
            agent_session: AgentSession = row[0]
            last_activity_at: datetime | None = row[1]
            if last_activity_at is None:
                continue
            # Treat naive timestamps as UTC; SAG / AHS only ever write UTC.
            if last_activity_at.tzinfo is None:
                last_activity_at = last_activity_at.replace(tzinfo=UTC)
            if last_activity_at >= cutoff:
                continue
            agent_session.status = AgentSessionStatus.ARCHIVED
            archived += 1
            logger.info(
                "Auto-archiving stale session",
                session_id=str(agent_session.agent_session_id),
                slack_session_id=agent_session.slack_session_id,
                last_activity_at=last_activity_at.isoformat(),
                threshold_days=days,
            )

        if archived:
            await session.commit()

    return archived
