"""Unit tests for the auto-resume preamble behaviour in session_lifecycle.py.

PR 2 of the AHS-restart-courtesy-+-auto-resume project. PR 1 wired up the
shutdown / restart Slack courtesy *delivery*; this PR makes the restart
real by giving the LLM enough context on the next user message to pick up
where it left off.

The "did the previous turn need a resume preamble?" signal lives entirely in
Redis (no new DB column).  The lifecycle is:

  1. ``_run_agent_task`` enters → ``mark_executor_running`` SETs
     ``ahs:executor_running:{session_id}`` (TTL 30 min).
  2. ``_run_agent_task`` exits (success / failure / cancellation / SIGTERM)
     → ``mark_executor_finished`` DELs the key.  If SIGTERM beats the DEL,
     the key survives.
  3. Next AHS startup → ``promote_executor_running_to_resume_pending``
     SCANs leftover ``executor_running`` keys and renames each to
     ``ahs:resume_pending:{session_id}`` (TTL 30 days).
  4. Next ``send_message`` → ``consume_resume_pending`` does a single
     ``GETDEL``; if non-empty, ``build_resume_context`` produces the
     preamble and ``send_message`` prepends it to the runner input.

Tests cover:

1. ``build_resume_context`` produces the expected preamble for a session
   with an IN_PROGRESS AGENT draft and a prior USER request.
2. ``build_resume_context`` returns an empty string when the session has
   no useful history (so callers can fall through to the plain message
   path) — and tolerates DB lookup failures.
3. ``send_message`` on a session whose ``resume_pending`` Redis key is set
   prepends the preamble to the runner input, leaves the persisted USER
   message unchanged, and clears the key (via GETDEL).
4. ``send_message`` on a session with no flag behaves exactly as before —
   no preamble, no Redis SET to flip a flag.
5. ``consume_resume_pending`` is GETDEL — two concurrent calls cannot both
   observe the flag set.
6. ``promote_executor_running_to_resume_pending`` SCANs ``executor_running:*``
   keys, SETs ``resume_pending:*`` keys with the long TTL, then DELetes the
   originals.
7. ``mark_executor_finished`` errors are swallowed — the TTL on the
   executor_running key self-heals.
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
    status: AgentSessionStatus = AgentSessionStatus.STALE,
    trigger: AgentSessionTrigger = AgentSessionTrigger.SLACK,
) -> AgentSession:
    return AgentSession(
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


class _FakeRedis:
    """Minimal in-memory Redis stand-in covering only the commands the resume
    helpers use: ``get``, ``set`` (with ``ex`` kwarg), ``delete``, ``getdel``,
    and ``scan`` (cursor-based).

    Records every method call on ``self.calls`` so tests can assert the exact
    command sequence (e.g. "SET happened before DEL" for the promote sweep).
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})
        self._ttls: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def get(self, key: str) -> str | None:
        self.calls.append(("get", (key,), {}))
        return self._store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.calls.append(("set", (key, value), {"ex": ex}))
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self.calls.append(("delete", keys, {}))
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._ttls.pop(k, None)
                removed += 1
        return removed

    async def getdel(self, key: str) -> str | None:
        self.calls.append(("getdel", (key,), {}))
        value = self._store.pop(key, None)
        self._ttls.pop(key, None)
        return value

    async def scan(self, *, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        self.calls.append(("scan", (), {"cursor": cursor, "match": match, "count": count}))
        # Single-shot SCAN: return everything that matches and a cursor of 0
        # to signal completion.  Real Redis pages, but for a few-hundred-key
        # recovery sweep this is equivalent.
        if cursor != 0:
            return 0, []
        prefix = match.rstrip("*")
        keys = [k for k in self._store if k.startswith(prefix)]
        return 0, keys

    def ttl_of(self, key: str) -> int | None:
        return self._ttls.get(key)


def _patch_redis(client: _FakeRedis) -> Any:
    return patch.object(session_lifecycle, "get_redis_client", AsyncMock(return_value=client))


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
# Tests: Redis-backed flag helpers
# ===========================================================================


class TestRedisFlagHelpers:
    """``mark_executor_running``, ``mark_executor_finished``, and
    ``consume_resume_pending`` round-trip through Redis exactly as expected."""

    async def test_mark_executor_running_sets_key_with_ttl(self) -> None:
        session_id = uuid.uuid4()
        fake = _FakeRedis()
        with _patch_redis(fake):
            await session_lifecycle.mark_executor_running(session_id, turn_number=7)

        key = session_lifecycle._executor_running_key(session_id)
        assert fake._store[key] == "7"
        # TTL matches the constant so an operator alert cannot flag a stale
        # key as "permanent".
        assert fake.ttl_of(key) == session_lifecycle._EXECUTOR_RUNNING_TTL_SECONDS

    async def test_mark_executor_running_swallows_errors(self) -> None:
        """A Redis hiccup at SET time must never propagate up — the worst
        outcome is that one user misses the resume preamble."""
        session_id = uuid.uuid4()
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.object(session_lifecycle, "get_redis_client", broken):
            await session_lifecycle.mark_executor_running(session_id, turn_number=1)
            # No raise = pass.

    async def test_mark_executor_finished_deletes_key(self) -> None:
        session_id = uuid.uuid4()
        key = session_lifecycle._executor_running_key(session_id)
        fake = _FakeRedis(initial={key: "3"})

        with _patch_redis(fake):
            await session_lifecycle.mark_executor_finished(session_id)

        assert key not in fake._store

    async def test_mark_executor_finished_swallows_errors(self) -> None:
        session_id = uuid.uuid4()
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.object(session_lifecycle, "get_redis_client", broken):
            # Must not raise — TTL self-heals if the DEL is lost.
            await session_lifecycle.mark_executor_finished(session_id)

    async def test_consume_resume_pending_returns_true_and_clears(self) -> None:
        session_id = uuid.uuid4()
        key = session_lifecycle._resume_pending_key(session_id)
        fake = _FakeRedis(initial={key: "1"})

        with _patch_redis(fake):
            result = await session_lifecycle.consume_resume_pending(session_id)

        assert result is True
        # GETDEL cleared the key — second call returns False.
        assert key not in fake._store

    async def test_consume_resume_pending_returns_false_when_unset(self) -> None:
        session_id = uuid.uuid4()
        fake = _FakeRedis()

        with _patch_redis(fake):
            result = await session_lifecycle.consume_resume_pending(session_id)

        assert result is False

    async def test_consume_resume_pending_is_atomic(self) -> None:
        """Two back-to-back ``consume_resume_pending`` calls cannot both see
        the flag set — the second one observes the cleared key.

        This is the property that lets us drop the ``FOR UPDATE`` lock the
        DB-column version of this code needed.
        """
        session_id = uuid.uuid4()
        key = session_lifecycle._resume_pending_key(session_id)
        fake = _FakeRedis(initial={key: "1"})

        with _patch_redis(fake):
            first = await session_lifecycle.consume_resume_pending(session_id)
            second = await session_lifecycle.consume_resume_pending(session_id)

        assert (first, second) == (True, False)

    async def test_consume_resume_pending_swallows_errors_to_false(self) -> None:
        """A Redis-side error must be treated as "no flag" — we'd rather skip
        the preamble than raise into the user message path."""
        session_id = uuid.uuid4()
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.object(session_lifecycle, "get_redis_client", broken):
            assert await session_lifecycle.consume_resume_pending(session_id) is False


# ===========================================================================
# Tests: promote_executor_running_to_resume_pending — startup sweep
# ===========================================================================


class TestPromoteExecutorRunningToResumePending:
    """The startup sweep converts every leftover ``executor_running`` key
    into a ``resume_pending`` key with the long TTL, then deletes the
    original."""

    async def test_promotes_all_keys_with_long_ttl(self) -> None:
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        running_a = session_lifecycle._executor_running_key(sid_a)
        running_b = session_lifecycle._executor_running_key(sid_b)
        # Also drop in an unrelated key — the sweep must not touch it.
        fake = _FakeRedis(initial={running_a: "5", running_b: "12", "ahs:stream:other": "noise"})

        with _patch_redis(fake):
            count = await session_lifecycle.promote_executor_running_to_resume_pending()

        assert count == 2
        # Both executor_running keys are gone; both resume_pending keys are
        # present with the 30-day TTL.
        for sid in (sid_a, sid_b):
            assert session_lifecycle._executor_running_key(sid) not in fake._store
            pending = session_lifecycle._resume_pending_key(sid)
            assert fake._store[pending] == "1"
            assert fake.ttl_of(pending) == session_lifecycle._RESUME_PENDING_TTL_SECONDS
        # Unrelated key is preserved.
        assert fake._store["ahs:stream:other"] == "noise"

    async def test_no_keys_is_a_noop(self) -> None:
        fake = _FakeRedis()
        with _patch_redis(fake):
            count = await session_lifecycle.promote_executor_running_to_resume_pending()
        assert count == 0

    async def test_malformed_key_is_skipped_not_fatal(self) -> None:
        """A stray ``ahs:executor_running:not-a-uuid`` key must not take down
        the rest of the sweep."""
        sid_good = uuid.uuid4()
        good_key = session_lifecycle._executor_running_key(sid_good)
        bad_key = "ahs:executor_running:not-a-uuid"
        fake = _FakeRedis(initial={good_key: "1", bad_key: "x"})

        with _patch_redis(fake):
            count = await session_lifecycle.promote_executor_running_to_resume_pending()

        # Good one promoted, bad one left in place (logged + skipped).
        assert count == 1
        assert good_key not in fake._store
        assert bad_key in fake._store
        assert session_lifecycle._resume_pending_key(sid_good) in fake._store

    async def test_redis_failure_does_not_raise(self) -> None:
        """A Redis-side error must not block AHS startup."""
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.object(session_lifecycle, "get_redis_client", broken):
            count = await session_lifecycle.promote_executor_running_to_resume_pending()
        assert count == 0


# ===========================================================================
# Tests: send_message uses the preamble when the resume_pending flag is set
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
    """``send_message`` prepends the preamble iff the Redis flag is set."""

    async def test_flag_set_prepends_preamble_and_clears_key(self) -> None:
        session = _make_session(status=AgentSessionStatus.STALE)
        env = _stub_send_message_environment(session)

        # Pre-load the resume_pending key — simulates a prior AHS restart that
        # ran ``promote_executor_running_to_resume_pending``.
        fake_redis = _FakeRedis(initial={session_lifecycle._resume_pending_key(session.agent_session_id): "1"})

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
            _patch_redis(fake_redis),
        ):
            await session_lifecycle.send_message(request)

        # Redis key cleared by the GETDEL inside ``consume_resume_pending``.
        assert session_lifecycle._resume_pending_key(session.agent_session_id) not in fake_redis._store
        # Preamble was prepended to whatever was passed to _run_agent_task.
        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"].startswith(fake_preamble)
        assert call_kwargs["message"].endswith("please continue")

    async def test_flag_unset_skips_preamble(self) -> None:
        session = _make_session(status=AgentSessionStatus.COMPLETED)
        env = _stub_send_message_environment(session)
        build_mock = AsyncMock(return_value="should-not-be-seen")
        # Empty Redis = no flag set = no preamble path.
        fake_redis = _FakeRedis()

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
            _patch_redis(fake_redis),
        ):
            await session_lifecycle.send_message(request)

        # build_resume_context was NEVER called — the GETDEL miss short-circuits
        # the preamble path.
        build_mock.assert_not_awaited()
        # Message reached the runner unchanged.
        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"] == "hi again"

    async def test_empty_preamble_does_not_add_separator(self) -> None:
        """If ``build_resume_context`` yields ``""`` (e.g. no messages,
        or a DB hiccup), ``send_message`` must still pass the original
        text — no stray ``---`` or empty bracket."""
        session = _make_session()
        env = _stub_send_message_environment(session)
        # Flag set, but preamble builder returns "" — fall-through path.
        fake_redis = _FakeRedis(initial={session_lifecycle._resume_pending_key(session.agent_session_id): "1"})

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
            _patch_redis(fake_redis),
        ):
            await session_lifecycle.send_message(request)

        env["run_task_mock"].assert_called_once()
        call_kwargs = env["run_task_mock"].call_args.kwargs
        assert call_kwargs["message"] == "raw message"
        # GETDEL still cleared the key — we observed it, just produced no preamble.
        assert session_lifecycle._resume_pending_key(session.agent_session_id) not in fake_redis._store


# ===========================================================================
# Tests: lifespan recovery — STALE marking unchanged, no DB-column writes
# ===========================================================================


class TestRecoverStaleSessions:
    """The DB classifier still distinguishes STALE vs COMPLETED, but it no
    longer writes any "needs preamble" flag — that signal lives in Redis."""

    async def test_interrupted_session_marked_stale(self) -> None:
        interrupted = _make_session(status=AgentSessionStatus.ACTIVE)
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

    async def test_completed_session_marked_completed(self) -> None:
        """A session whose last turn finished SUCCESS but was never marked
        COMPLETED is recovered to COMPLETED."""
        completed = _make_session(status=AgentSessionStatus.ACTIVE)
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


# ===========================================================================
# Tests: auto-stale sweep does NOT touch the resume Redis key
# ===========================================================================


class TestAutoStaleSweepDoesNotSetResumeFlag:
    """The 6h idle sweep is not a crash — no Redis SET should happen.

    The auto-stale path runs purely on the DB side (no Redis call), so this
    test simply verifies the status transition still works and asserts that
    nothing in the path imports / calls the Redis client.
    """

    async def test_auto_stale_marks_stale_without_redis(self) -> None:
        idle = _make_session(status=AgentSessionStatus.ACTIVE)
        # Force ``modified_at`` far enough in the past to land in the sweep.
        idle.modified_at = datetime.now(UTC) - timedelta(hours=12)

        fake_db = _FakeDBSession(exec_responses=[[idle]])
        # If the sweep ever started touching Redis, this AsyncMock would record
        # a call.  We assert it did not.
        redis_mock = AsyncMock(side_effect=AssertionError("auto-stale sweep must not touch Redis"))

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch.object(session_lifecycle, "get_redis_client", redis_mock),
        ):
            await lifespan._run_auto_stale_check()

        assert idle.status == AgentSessionStatus.STALE
        assert fake_db.commit_called
        redis_mock.assert_not_called()
