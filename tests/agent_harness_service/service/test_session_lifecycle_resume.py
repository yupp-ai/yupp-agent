"""Unit tests for the auto-resume preamble behaviour in session_lifecycle.py.

PR 2 of the AHS-restart-courtesy-+-auto-resume project. PR 1 wired up the
shutdown / restart Slack courtesy *delivery*; this PR makes the restart
real by giving the LLM enough context on the next user message to pick up
where it left off.

Tests cover:

1. ``build_resume_context`` produces the expected preamble for a session
   with an IN_PROGRESS AGENT draft and a prior USER request.
2. ``build_resume_context`` returns an empty string when the session has
   no useful history (so callers can fall through to the plain message
   path) — and tolerates DB lookup failures.
3. ``send_message`` on a session with ``was_interrupted_by_restart=True``
   prepends the preamble to the runner input, leaves the persisted USER
   message unchanged, and clears the flag.
4. ``send_message`` on a session with the flag clear behaves exactly as
   before — no preamble, no DB write to flip the flag.
5. The auto-stale sweep does NOT flip ``was_interrupted_by_restart`` when
   it transitions ACTIVE rows to STALE.
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
from ypl.agent_harness_service.service import session_lifecycle  # noqa: E402
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


def _make_session(
    *,
    flag: bool = False,
    status: AgentSessionStatus = AgentSessionStatus.STALE,
    trigger: AgentSessionTrigger = AgentSessionTrigger.SLACK,
) -> AgentSession:
    sess = AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=status,
        trigger=trigger,
        slack_session_id="C123:1234.567:A456" if trigger == AgentSessionTrigger.SLACK else None,
        modified_at=datetime.now(UTC),
        parent_session_id=None,
        creator_user_id="user-abc",
        workspace="/tmp/ws",
    )
    sess.was_interrupted_by_restart = flag
    return sess


def _make_msg(
    session_id: uuid.UUID,
    *,
    role: AgentSessionMessageRole,
    turn: int = 1,
    content: str = "",
    completion: AgentSessionMessageCompletionStatus = AgentSessionMessageCompletionStatus.SUCCESS,
) -> AgentSessionMessage:
    return AgentSessionMessage(
        agent_session_message_id=uuid.uuid4(),
        agent_session_id=session_id,
        role=role,
        turn_number=turn,
        completion_status=completion,
        content=content,
    )


class _FakeDBSession:
    """Tiny DB-session stub used by the resume-context tests.

    Returns canned ``exec(...)`` results in order — see ``test_lifespan_recovery.py``
    for the same pattern used by PR 1.
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
# Tests: build_resume_context
# ===========================================================================


class TestBuildResumeContext:
    """``build_resume_context`` produces the expected preamble."""

    async def test_with_in_progress_draft_includes_partial_response(self) -> None:
        session = _make_session()
        user_msg = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.USER,
            turn=3,
            content="Please refactor the authentication flow to use OAuth.",
        )
        draft = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            turn=3,
            content="Sure — I'll start by looking at the current auth provider.",
            completion=AgentSessionMessageCompletionStatus.IN_PROGRESS,
        )
        fake_db = _FakeDBSession(exec_responses=[[user_msg], [draft]])

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            _async_session_factory(fake_db),
        ):
            preamble = await session_lifecycle.build_resume_context(session.agent_session_id)

        assert preamble  # non-empty
        assert "interrupted by an AHS server restart" in preamble
        # User excerpt is included.
        assert "refactor the authentication flow" in preamble
        # Partial response is labelled as a draft, not a completed reply.
        assert "Your partial response so far was" in preamble
        assert "I'll start by looking at the current auth provider" in preamble
        # The user's actual new message gets appended after the separator.
        assert preamble.endswith("---\n\n")

    async def test_with_completed_reply_uses_last_completed_phrasing(self) -> None:
        """A SUCCESS draft (turn finished but status flag never landed) is
        labelled as the last *completed* response, not a partial one."""
        session = _make_session()
        user_msg = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.USER,
            turn=2,
            content="Show me the recent commits.",
        )
        agent_msg = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            turn=2,
            content="Here are the last five commits…",
            completion=AgentSessionMessageCompletionStatus.SUCCESS,
        )
        fake_db = _FakeDBSession(exec_responses=[[user_msg], [agent_msg]])

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            _async_session_factory(fake_db),
        ):
            preamble = await session_lifecycle.build_resume_context(session.agent_session_id)

        assert "Your last completed response was" in preamble
        assert "Your partial response so far was" not in preamble

    async def test_no_messages_returns_empty_string(self) -> None:
        """Caller falls through to plain message path when there's nothing
        to anchor the preamble to."""
        session = _make_session()
        fake_db = _FakeDBSession(exec_responses=[[], []])

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            _async_session_factory(fake_db),
        ):
            preamble = await session_lifecycle.build_resume_context(session.agent_session_id)

        assert preamble == ""

    async def test_db_failure_returns_empty_string(self) -> None:
        """Any DB-side error degrades to an empty preamble — never raises.

        Patching ``get_async_session`` to a callable that raises on entry
        models the case where DB connection setup itself blows up
        (transient pool exhaustion, network blip), since
        ``build_resume_context`` opens its own session.
        """
        session = _make_session()

        def _broken() -> Any:
            raise RuntimeError("db down")

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            _broken,
        ):
            preamble = await session_lifecycle.build_resume_context(session.agent_session_id)

        assert preamble == ""

    async def test_long_content_is_truncated(self) -> None:
        """Excerpts respect the per-section character caps so a runaway draft
        cannot push the real user message out of attention."""
        session = _make_session()
        long_user = "x" * 5_000
        long_draft = "y" * 5_000
        user_msg = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.USER,
            content=long_user,
        )
        draft = _make_msg(
            session.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            content=long_draft,
            completion=AgentSessionMessageCompletionStatus.IN_PROGRESS,
        )
        fake_db = _FakeDBSession(exec_responses=[[user_msg], [draft]])

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            _async_session_factory(fake_db),
        ):
            preamble = await session_lifecycle.build_resume_context(session.agent_session_id)

        # Total preamble is bounded — well under the sum of the raw inputs.
        assert len(preamble) < len(long_user) + len(long_draft)
        # Truncation marker is present (the helper appends "…" when it cuts).
        assert "…" in preamble


# ===========================================================================
# Tests: _truncate_for_preamble
# ===========================================================================


class TestTruncateForPreamble:
    """Plain string-trim helper used by ``build_resume_context``."""

    def test_returns_empty_for_none(self) -> None:
        assert session_lifecycle._truncate_for_preamble(None, 10) == ""

    def test_returns_empty_for_empty(self) -> None:
        assert session_lifecycle._truncate_for_preamble("", 10) == ""

    def test_collapses_whitespace(self) -> None:
        assert session_lifecycle._truncate_for_preamble("a\n  b\tc", 100) == "a b c"

    def test_truncates_with_ellipsis(self) -> None:
        out = session_lifecycle._truncate_for_preamble("abcdefghijk", 6)
        assert len(out) == 6
        assert out.endswith("…")

    def test_no_truncation_when_under_limit(self) -> None:
        assert session_lifecycle._truncate_for_preamble("hi there", 100) == "hi there"


# ===========================================================================
# Tests: send_message uses the preamble when the flag is set
# ===========================================================================


def _stub_send_message_environment(session: AgentSession) -> dict[str, Any]:
    """Build the patch dict needed to drive ``send_message`` end-to-end without
    a real DB / runner / executor.

    Returns a dict of ``patches`` and the ``run_task_mock`` whose ``message``
    kwarg the test inspects to confirm preamble prepending.
    """
    # _resolve_session returns our stub AgentSession.
    resolve_session = AsyncMock(return_value=session)

    # _has_inflight_turn -> False so send_message takes the "create-turn" branch.
    has_inflight = AsyncMock(return_value=False)

    # _next_turn_number returns 4 (arbitrary).
    next_turn = AsyncMock(return_value=4)

    # _load_agent_config_with_db_fallback returns a stub config object — the
    # only attr ``send_message`` reads from it later is implicitly via
    # _run_agent_task, which we mock.
    fake_agent_cfg = MagicMock()
    load_cfg = AsyncMock(return_value=fake_agent_cfg)

    # _run_agent_task is replaced — no real subprocess work happens.
    run_task_mock = AsyncMock(return_value=None)

    # Every DB session call inside ``send_message`` gets a writable fake.
    db_session = AsyncMock()
    db_session.commit = AsyncMock()
    db_session.add = MagicMock()
    db_session.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=session)))
    db_session.get = AsyncMock(return_value=MagicMock(name="agent_row"))

    return {
        "resolve_session": resolve_session,
        "has_inflight": has_inflight,
        "next_turn": next_turn,
        "load_cfg": load_cfg,
        "run_task_mock": run_task_mock,
        "db_session": db_session,
    }


class TestSendMessageAutoResume:
    """``send_message`` prepends the preamble iff the session flag is set."""

    async def test_flag_set_prepends_preamble_and_clears_flag(self) -> None:
        session = _make_session(flag=True, status=AgentSessionStatus.STALE)
        env = _stub_send_message_environment(session)

        # The preamble we expect to see prepended.
        fake_preamble = "[SYSTEM] interrupted; previous request was X. ---\n\n"

        from ypl.agent_harness_service.common.types import SessionMessageRequest

        request = SessionMessageRequest(
            session_id=str(session.agent_session_id),
            message="please continue",
            user_id=session.creator_user_id,
        )

        with (
            patch.object(session_lifecycle, "_resolve_session", env["resolve_session"]),
            patch.object(session_lifecycle, "_has_inflight_turn", env["has_inflight"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_load_agent_config_with_db_fallback", env["load_cfg"]),
            patch.object(session_lifecycle, "build_resume_context", AsyncMock(return_value=fake_preamble)),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(session_lifecycle, "create_background_task", lambda coro: MagicMock(name="bg-task")),
            patch(
                "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
                _async_session_factory(env["db_session"]),
            ),
            patch.object(session_lifecycle, "set_session_current_user", MagicMock()),
            patch.object(session_lifecycle, "has_permission_by_user_id_cached", AsyncMock(return_value=True)),
        ):
            await session_lifecycle.send_message(request)

        # Flag cleared on the in-memory session row before the DB commit
        # that records the USER message.
        assert session.was_interrupted_by_restart is False
        # Preamble was prepended to whatever was passed to _run_agent_task.
        # ``create_background_task`` consumed the coroutine, so we inspect
        # the last call's ``message`` kwarg via ``_run_agent_task`` itself.
        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"].startswith(fake_preamble)
        assert call_kwargs["message"].endswith("please continue")

    async def test_flag_unset_skips_preamble(self) -> None:
        session = _make_session(flag=False, status=AgentSessionStatus.COMPLETED)
        env = _stub_send_message_environment(session)
        build_mock = AsyncMock(return_value="should-not-be-seen")

        from ypl.agent_harness_service.common.types import SessionMessageRequest

        request = SessionMessageRequest(
            session_id=str(session.agent_session_id),
            message="hi again",
            user_id=session.creator_user_id,
        )

        with (
            patch.object(session_lifecycle, "_resolve_session", env["resolve_session"]),
            patch.object(session_lifecycle, "_has_inflight_turn", env["has_inflight"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_load_agent_config_with_db_fallback", env["load_cfg"]),
            patch.object(session_lifecycle, "build_resume_context", build_mock),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(session_lifecycle, "create_background_task", lambda coro: MagicMock(name="bg-task")),
            patch(
                "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
                _async_session_factory(env["db_session"]),
            ),
            patch.object(session_lifecycle, "set_session_current_user", MagicMock()),
            patch.object(session_lifecycle, "has_permission_by_user_id_cached", AsyncMock(return_value=True)),
        ):
            await session_lifecycle.send_message(request)

        # build_resume_context was NEVER called — the flag short-circuits the path.
        build_mock.assert_not_awaited()
        # Message reached the runner unchanged.
        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"] == "hi again"

    async def test_empty_preamble_does_not_add_separator(self) -> None:
        """If ``build_resume_context`` yields ``""`` (e.g. no messages,
        or a DB hiccup), ``send_message`` must still pass the original
        text — no stray ``---`` or empty bracket."""
        session = _make_session(flag=True)
        env = _stub_send_message_environment(session)

        from ypl.agent_harness_service.common.types import SessionMessageRequest

        request = SessionMessageRequest(
            session_id=str(session.agent_session_id),
            message="raw message",
            user_id=session.creator_user_id,
        )

        with (
            patch.object(session_lifecycle, "_resolve_session", env["resolve_session"]),
            patch.object(session_lifecycle, "_has_inflight_turn", env["has_inflight"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_load_agent_config_with_db_fallback", env["load_cfg"]),
            patch.object(session_lifecycle, "build_resume_context", AsyncMock(return_value="")),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(session_lifecycle, "create_background_task", lambda coro: MagicMock(name="bg-task")),
            patch(
                "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
                _async_session_factory(env["db_session"]),
            ),
            patch.object(session_lifecycle, "set_session_current_user", MagicMock()),
            patch.object(session_lifecycle, "has_permission_by_user_id_cached", AsyncMock(return_value=True)),
        ):
            await session_lifecycle.send_message(request)

        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"] == "raw message"
        assert session.was_interrupted_by_restart is False


# ===========================================================================
# Tests: auto-stale sweep does NOT flip the resume flag
# ===========================================================================


class TestAutoStaleSweepDoesNotSetResumeFlag:
    """The 6h idle sweep is not a crash — the flag must stay False."""

    async def test_auto_stale_leaves_flag_false(self) -> None:
        # An idle ACTIVE session that the periodic sweep would mark STALE.
        idle = _make_session(flag=False, status=AgentSessionStatus.ACTIVE)
        # Force ``modified_at`` far enough in the past to land in the sweep.
        idle.modified_at = datetime.now(UTC) - timedelta(hours=12)

        fake_db = _FakeDBSession(exec_responses=[[idle]])

        with patch(
            "ypl.agent_harness_service.lifespan.get_async_session",
            _async_session_factory(fake_db),
        ):
            await lifespan._run_auto_stale_check()

        assert idle.status == AgentSessionStatus.STALE
        # Critically, the flag was NOT flipped — auto-stale rows must not
        # trigger a "previous turn was interrupted" preamble next time.
        assert idle.was_interrupted_by_restart is False
        assert fake_db.commit_called


# ===========================================================================
# Tests: _recover_stale_sessions sets the flag on mid-turn-interrupted rows
# ===========================================================================


class TestRecoverStaleSessionsSetsResumeFlag:
    """The startup recovery scan flags only mid-turn-interrupted sessions."""

    async def test_interrupted_session_gets_flag(self) -> None:
        interrupted = _make_session(flag=False, status=AgentSessionStatus.ACTIVE)
        # No terminal message AND no USER turn → STALE branch fires.
        fake_db = _FakeDBSession(
            exec_responses=[
                [interrupted],
                [],  # latest non-USER msg
                [None],  # latest USER turn
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
        ):
            await lifespan._recover_stale_sessions()

        assert interrupted.status == AgentSessionStatus.STALE
        assert interrupted.was_interrupted_by_restart is True

    async def test_completed_session_does_not_get_flag(self) -> None:
        """A session whose last turn finished SUCCESS but was never marked
        COMPLETED is recovered to COMPLETED and must not have the flag set."""
        completed = _make_session(flag=False, status=AgentSessionStatus.ACTIVE)
        terminal_msg = _make_msg(
            completed.agent_session_id,
            role=AgentSessionMessageRole.AGENT,
            turn=1,
            completion=AgentSessionMessageCompletionStatus.SUCCESS,
        )
        fake_db = _FakeDBSession(
            exec_responses=[
                [completed],
                [terminal_msg],
                [1],  # latest USER turn matches latest_terminal_msg.turn_number
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
        ):
            await lifespan._recover_stale_sessions()

        assert completed.status == AgentSessionStatus.COMPLETED
        assert completed.was_interrupted_by_restart is False
