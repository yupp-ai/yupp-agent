"""Unit tests for ypl/agent_harness_service/service/archive.py.

Covers:
- :func:`archive_session` (manual archive endpoint backing /archive)
- :func:`list_pending_sessions` (read-only query backing /pending)
- :func:`auto_archive_stale_sessions` (scheduler sweep, 7-day default)
- :func:`_format_preview` (single-line trimming used in /pending output)

DB calls are stubbed via :func:`get_async_session` overrides; we exercise
the in-Python ordering, classification, and idempotence logic, not the SQL
itself (covered by alembic + integration tests).
"""

from __future__ import annotations
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Heavy SDK stubs so ypl.agent_harness_service.service imports cleanly.
_together = types.ModuleType("together")
_together_types = types.ModuleType("together.types")
_croniter = types.ModuleType("croniter")


class _APITimeoutError(Exception):
    pass


class _Together:
    pass


_together.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
_together.AsyncTogether = _Together  # type: ignore[attr-defined]
_together.Together = _Together  # type: ignore[attr-defined]
_together_types.ChatCompletion = _Together  # type: ignore[attr-defined]
sys.modules.setdefault("together", _together)
sys.modules.setdefault("together.types", _together_types)
try:
    import croniter as _real_croniter  # type: ignore[import-untyped,unused-ignore]  # noqa: F401
except ImportError:
    _croniter.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", _croniter)


from ypl.agent_harness_service.service.archive import (  # noqa: E402
    _format_preview,
    archive_session,
    auto_archive_stale_sessions,
    list_pending_sessions,
)
from ypl.db.agent_harness import (  # noqa: E402
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)


def _ctx_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


def _make_session(
    *,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    trigger: AgentSessionTrigger = AgentSessionTrigger.SLACK,
    creator_user_id: str = "user-123",
    title: str | None = None,
    context: dict[str, Any] | None = None,
) -> AgentSession:
    return AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        creator_user_id=creator_user_id,
        slack_session_id=f"C123:1700000000.0:A123:{uuid.uuid4().hex[:6]}",
        status=status,
        trigger=trigger,
        context=context or {"slack_channel_id": "C123", "slack_thread_ts": "1700000000.0"},
        title=title,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )


def _make_message(
    *,
    session_id: uuid.UUID,
    role: AgentSessionMessageRole,
    turn_number: int = 1,
    content: str = "hello",
    created_at: datetime | None = None,
    completion_status: AgentSessionMessageCompletionStatus = AgentSessionMessageCompletionStatus.SUCCESS,
) -> AgentSessionMessage:
    return AgentSessionMessage(
        agent_session_message_id=uuid.uuid4(),
        agent_session_id=session_id,
        turn_number=turn_number,
        role=role,
        content=content,
        completion_status=completion_status,
        created_at=created_at or datetime.now(UTC),
    )


# ===========================================================================
# _format_preview
# ===========================================================================


class TestFormatPreview:
    def test_none_returns_none(self) -> None:
        assert _format_preview(None) is None

    def test_empty_returns_none(self) -> None:
        assert _format_preview("") is None

    def test_short_message_passthrough(self) -> None:
        assert _format_preview("hello world") == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert _format_preview("hello\n\n  world\t!") == "hello world !"

    def test_long_message_trimmed_with_ellipsis(self) -> None:
        text = "x" * 500
        out = _format_preview(text)
        assert out is not None
        assert out.endswith("…")
        assert len(out) <= 120


# ===========================================================================
# archive_session
# ===========================================================================


class TestArchiveSession:
    async def test_raises_when_session_not_found(self) -> None:
        mock_db = AsyncMock()
        with (
            patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)),
            patch(
                "ypl.agent_harness_service.service.archive._resolve_session",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ValueError, match="Session not found"),
        ):
            await archive_session("missing-id")

    async def test_archives_active_session(self) -> None:
        mock_db = AsyncMock()
        sess = _make_session(status=AgentSessionStatus.ACTIVE)

        with (
            patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)),
            patch(
                "ypl.agent_harness_service.service.archive._resolve_session",
                AsyncMock(return_value=sess),
            ),
        ):
            response = await archive_session(str(sess.agent_session_id))

        assert response.status == "archived"
        assert response.session_id == str(sess.agent_session_id)
        assert sess.status == AgentSessionStatus.ARCHIVED
        mock_db.commit.assert_awaited_once()

    async def test_idempotent_when_already_archived(self) -> None:
        mock_db = AsyncMock()
        sess = _make_session(status=AgentSessionStatus.ARCHIVED)

        with (
            patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)),
            patch(
                "ypl.agent_harness_service.service.archive._resolve_session",
                AsyncMock(return_value=sess),
            ),
        ):
            response = await archive_session(str(sess.agent_session_id))

        assert response.status == "already_archived"
        # No commit on the no-op path
        mock_db.commit.assert_not_called()


# ===========================================================================
# list_pending_sessions
# ===========================================================================


class TestListPendingSessions:
    async def test_empty_when_no_sessions(self) -> None:
        mock_db = AsyncMock()
        empty_result = MagicMock()
        empty_result.all = MagicMock(return_value=[])
        mock_db.exec = AsyncMock(return_value=empty_result)

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            response = await list_pending_sessions(user_id="user-123", hours_back=24)

        assert response.pending_human == []
        assert response.pending_ai == []
        assert response.hours_back == 24

    async def test_classifies_by_last_message_role(self) -> None:
        # Two sessions: one waiting on human (last msg AGENT), one waiting on
        # AI (last msg USER). Both within the 24h window.
        mock_db = AsyncMock()

        s1 = _make_session()
        s2 = _make_session()
        sessions_row = [(s1, "raccoon"), (s2, "raccoon")]

        recent = datetime.now(UTC) - timedelta(hours=1)
        m1 = _make_message(session_id=s1.agent_session_id, role=AgentSessionMessageRole.AGENT, created_at=recent)
        m2 = _make_message(session_id=s2.agent_session_id, role=AgentSessionMessageRole.USER, created_at=recent)
        msgs = [m1, m2]

        sess_result = MagicMock()
        sess_result.all = MagicMock(return_value=sessions_row)
        msg_result = MagicMock()
        msg_result.all = MagicMock(return_value=msgs)
        mock_db.exec = AsyncMock(side_effect=[sess_result, msg_result])

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            response = await list_pending_sessions(user_id="user-123", hours_back=24)

        assert len(response.pending_human) == 1
        assert response.pending_human[0].session_id == str(s1.agent_session_id)
        assert response.pending_human[0].waiting_on == "human"

        assert len(response.pending_ai) == 1
        assert response.pending_ai[0].session_id == str(s2.agent_session_id)
        assert response.pending_ai[0].waiting_on == "ai"

    async def test_filters_out_messages_outside_window(self) -> None:
        mock_db = AsyncMock()

        s = _make_session()
        sess_result = MagicMock()
        sess_result.all = MagicMock(return_value=[(s, "raccoon")])

        # Last message 30 hours ago — outside 24h window.
        old_msg = _make_message(
            session_id=s.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            created_at=datetime.now(UTC) - timedelta(hours=30),
        )
        msg_result = MagicMock()
        msg_result.all = MagicMock(return_value=[old_msg])
        mock_db.exec = AsyncMock(side_effect=[sess_result, msg_result])

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            response = await list_pending_sessions(user_id="user-123", hours_back=24)

        assert response.pending_human == []
        assert response.pending_ai == []

    async def test_sort_most_recent_first(self) -> None:
        mock_db = AsyncMock()

        s_old = _make_session()
        s_new = _make_session()
        sess_result = MagicMock()
        sess_result.all = MagicMock(return_value=[(s_old, "raccoon"), (s_new, "raccoon")])

        now = datetime.now(UTC)
        m_old = _make_message(
            session_id=s_old.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            created_at=now - timedelta(hours=5),
        )
        m_new = _make_message(
            session_id=s_new.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            created_at=now - timedelta(minutes=2),
        )
        msg_result = MagicMock()
        msg_result.all = MagicMock(return_value=[m_old, m_new])
        mock_db.exec = AsyncMock(side_effect=[sess_result, msg_result])

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            response = await list_pending_sessions(user_id="user-123", hours_back=24)

        assert [s.session_id for s in response.pending_human] == [
            str(s_new.agent_session_id),
            str(s_old.agent_session_id),
        ]


# ===========================================================================
# auto_archive_stale_sessions
# ===========================================================================


class TestAutoArchiveStale:
    async def test_archives_sessions_older_than_threshold(self) -> None:
        mock_db = AsyncMock()
        old = _make_session(status=AgentSessionStatus.ACTIVE)
        fresh = _make_session(status=AgentSessionStatus.ACTIVE)
        now = datetime.now(UTC)
        rows = [
            (old, now - timedelta(days=10)),
            (fresh, now - timedelta(days=2)),
        ]
        result = MagicMock()
        result.all = MagicMock(return_value=rows)
        mock_db.exec = AsyncMock(return_value=result)

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            archived = await auto_archive_stale_sessions(days=7)

        assert archived == 1
        assert old.status == AgentSessionStatus.ARCHIVED
        assert fresh.status == AgentSessionStatus.ACTIVE
        mock_db.commit.assert_awaited_once()

    async def test_skips_when_last_activity_unknown(self) -> None:
        mock_db = AsyncMock()
        sess = _make_session(status=AgentSessionStatus.ACTIVE)
        result = MagicMock()
        result.all = MagicMock(return_value=[(sess, None)])
        mock_db.exec = AsyncMock(return_value=result)

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            archived = await auto_archive_stale_sessions(days=7)

        assert archived == 0
        assert sess.status == AgentSessionStatus.ACTIVE
        mock_db.commit.assert_not_called()

    async def test_no_op_when_nothing_to_archive(self) -> None:
        mock_db = AsyncMock()
        result = MagicMock()
        result.all = MagicMock(return_value=[])
        mock_db.exec = AsyncMock(return_value=result)

        with patch("ypl.agent_harness_service.service.archive.get_async_session", _ctx_factory(mock_db)):
            archived = await auto_archive_stale_sessions(days=7)

        assert archived == 0
        mock_db.commit.assert_not_called()
