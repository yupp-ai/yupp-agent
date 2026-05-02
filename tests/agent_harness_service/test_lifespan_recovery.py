"""Unit tests for the AHS startup recovery scan in ``lifespan.py``.

PR 1 of the AHS-restart-courtesy fix:

- Drops the 15-minute idle filter from ``_recover_stale_sessions`` so the
  recovery scan no longer skips exactly the sessions a fresh deploy just
  killed.
- Broadens the restart-side courtesy audience to *every* Slack session in
  the recovery scan (not only those classified as STALE).
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

# ---------------------------------------------------------------------------
# Stub heavy SDK deps so service modules can be imported in a plain test env.
# ---------------------------------------------------------------------------
_together = types.ModuleType("together")
_together_types = types.ModuleType("together.types")
_croniter = types.ModuleType("croniter")


class _APITimeoutError(Exception):
    pass


class _AsyncTogether:
    pass


class _Together:
    pass


class _ChatCompletion:
    pass


_together.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
_together.AsyncTogether = _AsyncTogether  # type: ignore[attr-defined]
_together.Together = _Together  # type: ignore[attr-defined]
_together_types.ChatCompletion = _ChatCompletion  # type: ignore[attr-defined]
sys.modules.setdefault("together", _together)
sys.modules.setdefault("together.types", _together_types)
try:
    import croniter as _real_croniter  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
except ImportError:
    _croniter.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", _croniter)


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from ypl.agent_harness_service import lifespan  # noqa: E402
from ypl.db.agent_harness import (  # noqa: E402
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_active_session(
    *,
    slack: bool = True,
    modified_at: datetime | None = None,
) -> AgentSession:
    return AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=AgentSessionStatus.ACTIVE,
        trigger=AgentSessionTrigger.SLACK if slack else AgentSessionTrigger.API,
        slack_session_id="C123:1234.567:A456" if slack else None,
        modified_at=modified_at or datetime.now(UTC),
        parent_session_id=None,
    )


def _make_terminal_msg(
    session_id: uuid.UUID,
    *,
    completion: AgentSessionMessageCompletionStatus,
    turn: int = 1,
    role: AgentSessionMessageRole = AgentSessionMessageRole.AGENT,
) -> AgentSessionMessage:
    return AgentSessionMessage(
        agent_session_message_id=uuid.uuid4(),
        agent_session_id=session_id,
        role=role,
        turn_number=turn,
        completion_status=completion,
        content_blocks=[],
    )


class _FakeDBSession:
    """Tiny DB-session stub that returns canned ``exec(...)`` results in order.

    Avoids having to teach mypy / sqlmodel how to compare statement
    expressions.  Each ``exec(...)`` call pops the next pre-canned response
    and returns a result-like object exposing ``.all()``, ``.one()``,
    ``.one_or_none()``.
    """

    def __init__(self, exec_responses: list[Any]) -> None:
        self._responses = list(exec_responses)
        self.exec_calls: list[Any] = []
        self.commit_called = False

    async def exec(self, statement: Any) -> Any:
        self.exec_calls.append(statement)
        if not self._responses:
            raise AssertionError(
                f"FakeDBSession exhausted; got {len(self.exec_calls)} exec call(s) but no canned responses left"
            )
        items = self._responses.pop(0)
        result = MagicMock()
        result.all.return_value = items
        result.one_or_none.return_value = items[0] if items else None
        # ``func.max(turn_number)`` returns a scalar, not a row — model that.
        if items and not isinstance(items[0], int | type(None)):
            result.one.return_value = items[0]
        else:
            result.one.return_value = items[0] if items else None
        return result

    async def commit(self) -> None:
        self.commit_called = True


def _async_session_factory(fake_db: _FakeDBSession) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield fake_db

    return _ctx


# ===========================================================================
# Tests: 15-minute filter is gone
# ===========================================================================


class TestRecoverStaleSessionsNoIdleFilter:
    """Sessions modified within the last 15 minutes are still classified."""

    async def test_recent_session_still_processed(self) -> None:
        # Session modified 5 *seconds* ago — would have been skipped under
        # the old 15-minute filter.
        recent = _make_active_session(modified_at=datetime.now(UTC) - timedelta(seconds=5))

        # Recovery sequence per session: (1) latest non-USER msg → none;
        # (2) latest USER turn number → None.  An empty terminal-msg list +
        # no USER turn classifies as STALE.
        fake_db = _FakeDBSession(
            exec_responses=[
                [recent],  # initial ACTIVE-session query
                [],  # latest non-USER message — none
                [None],  # latest USER turn number — None (treated as "no users yet")
            ]
        )

        restart_courtesy = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await lifespan._recover_stale_sessions()

        # Session was transitioned to STALE because no terminal message
        # existed (the recovery classifier kept working without the time gate).
        assert recent.status == AgentSessionStatus.STALE
        assert fake_db.commit_called
        # Restart courtesy fired with the session ID — even though it was
        # modified <15 min ago, which the old code would have filtered out.
        restart_courtesy.assert_awaited_once()
        forwarded = restart_courtesy.call_args[0][0]
        assert forwarded == [recent.agent_session_id]

    async def test_completed_slack_session_still_pinged(self) -> None:
        """A Slack session that completed cleanly right before the deploy is
        still notified on restart."""
        completed = _make_active_session(modified_at=datetime.now(UTC) - timedelta(seconds=2))

        # Latest non-USER message says SUCCESS at turn 1; latest USER turn
        # also 1 → has_unanswered_user_turn = False → COMPLETED branch.
        terminal_msg = _make_terminal_msg(
            completed.agent_session_id,
            completion=AgentSessionMessageCompletionStatus.SUCCESS,
            turn=1,
        )

        fake_db = _FakeDBSession(
            exec_responses=[
                [completed],
                [terminal_msg],
                [1],  # latest USER turn number
            ]
        )

        restart_courtesy = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await lifespan._recover_stale_sessions()

        assert completed.status == AgentSessionStatus.COMPLETED
        # The COMPLETED Slack session must still be in the restart-courtesy
        # list — that is the change relative to the old behaviour, which
        # only notified STALE sessions.
        restart_courtesy.assert_awaited_once()
        assert restart_courtesy.call_args[0][0] == [completed.agent_session_id]


# ===========================================================================
# Tests: the broader restart-courtesy audience
# ===========================================================================


class TestRestartCourtesyBroadAudience:
    """Restart courtesy targets every Slack session in the recovery scan."""

    async def test_mixed_classifications_all_in_audience(self) -> None:
        slack_stale = _make_active_session()
        slack_completed = _make_active_session()
        api_session = _make_active_session(slack=False)  # not Slack-triggered

        completed_msg = _make_terminal_msg(
            slack_completed.agent_session_id,
            completion=AgentSessionMessageCompletionStatus.SUCCESS,
            turn=1,
        )

        fake_db = _FakeDBSession(
            exec_responses=[
                [slack_stale, slack_completed, api_session],
                # slack_stale: no terminal msg, no USER turn → STALE
                [],
                [None],
                # slack_completed: SUCCESS at turn 1, USER turn 1 → COMPLETED
                [completed_msg],
                [1],
                # api_session: no terminal msg, no USER turn → STALE
                [],
                [None],
            ]
        )

        restart_courtesy = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await lifespan._recover_stale_sessions()

        # Both Slack sessions get the restart ping; the API session does not.
        restart_courtesy.assert_awaited_once()
        forwarded = restart_courtesy.call_args[0][0]
        assert set(forwarded) == {
            slack_stale.agent_session_id,
            slack_completed.agent_session_id,
        }
        assert api_session.agent_session_id not in forwarded

    async def test_no_active_sessions_skips_courtesy(self) -> None:
        fake_db = _FakeDBSession(exec_responses=[[]])
        restart_courtesy = AsyncMock()
        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await lifespan._recover_stale_sessions()
        restart_courtesy.assert_not_awaited()

    async def test_recovery_does_not_filter_by_modified_at(self) -> None:
        """The DB query has no ``modified_at`` predicate."""
        old = _make_active_session(modified_at=datetime.now(UTC) - timedelta(days=2))
        new = _make_active_session(modified_at=datetime.now(UTC) - timedelta(seconds=1))

        fake_db = _FakeDBSession(
            exec_responses=[
                [old, new],
                [],
                [None],
                [],
                [None],
            ]
        )

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                AsyncMock(),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            await lifespan._recover_stale_sessions()

        # Both sessions were classified — neither was filtered out by age.
        assert old.status == AgentSessionStatus.STALE
        assert new.status == AgentSessionStatus.STALE


# ===========================================================================
# Tests: shutdown timeout was bumped (constant-level guard)
# ===========================================================================


class TestShutdownCourtesyTimeout:
    """The wrapper timeout is the new 25 s value (was 5 s)."""

    def test_constant_is_25_seconds(self) -> None:
        assert lifespan._SHUTDOWN_COURTESY_WRAPPER_TIMEOUT_S == 25.0
