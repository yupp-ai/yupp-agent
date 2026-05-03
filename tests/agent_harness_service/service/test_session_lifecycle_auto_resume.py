"""Unit tests for the PR 3 auto-continue path.

PR 3 of the AHS-restart-courtesy + auto-resume project drops the user-action
ask from the courtesy strings and makes the system fire its own continuation
turn for sessions whose previous turn was killed mid-flight.

These tests cover:

* The reworded courtesy strings (no "send me a message" wording).
* ``list_resume_pending_session_ids`` SCANs the right Redis namespace and
  swallows Redis errors.
* ``dispatch_resume_turn`` happy path: claims the Redis flag, writes a
  synthetic USER row tagged ``is_system_continuation=True``, fires
  ``_run_agent_task`` with the preamble, and emits
  ``ahs/courtesy_broadcast{event=auto_resume,outcome=dispatched}``.
* ``dispatch_resume_turn`` skipped paths: empty preamble, race with a real
  user message that already cleared the Redis flag, missing session/agent.
* ``dispatch_resume_turn`` failed path: persistence-time DB exception emits
  ``outcome=failed`` (the Redis flag is consumed, accept the loss; the prior
  preamble path is no longer needed because the auto-continue did fire).
* Synthetic resume turn inherits ``creator_user_id`` from the session — never
  escalates to a different identity.

The race-with-real-user-message case is verified at the queue/dedupe layer
(see ``test_session_lifecycle_resume.py::TestSendMessageAutoResume``): the
synthetic USER row written by ``dispatch_resume_turn`` is what makes
``_has_inflight_turn`` return True for racing user messages so they get
queued via ``_pending_messages`` instead of starting a parallel turn.
"""

from __future__ import annotations
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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


from ypl.agent_harness_service import lifespan  # noqa: E402
from ypl.agent_harness_service.service import session_lifecycle  # noqa: E402
from ypl.agent_harness_service.service import state as ahs_state  # noqa: E402
from ypl.db.agent_harness import (  # noqa: E402
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)

# ---------------------------------------------------------------------------
# Helpers — shared between dispatch_resume_turn and the partition tests.
# ---------------------------------------------------------------------------


def _make_slack_session(
    *,
    status: AgentSessionStatus = AgentSessionStatus.STALE,
    creator_user_id: str = "user-abc",
) -> AgentSession:
    return AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=status,
        trigger=AgentSessionTrigger.SLACK,
        slack_session_id="C123:1234.567:A456",
        modified_at=datetime.now(UTC),
        parent_session_id=None,
        creator_user_id=creator_user_id,
        workspace="/tmp/ws",
    )


def _make_agent(name: str = "yuppclaw") -> Agent:
    return Agent(
        agent_id=uuid.uuid4(),
        name=name,
        display_name=name,
        description="",
    )


class _FakeRedis:
    """Minimal in-memory Redis stand-in covering the commands the resume helpers use."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self._store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    async def getdel(self, key: str) -> str | None:
        return self._store.pop(key, None)

    async def scan(self, *, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        if cursor != 0:
            return 0, []
        prefix = match.rstrip("*")
        return 0, [k for k in self._store if k.startswith(prefix)]


def _patch_redis(client: _FakeRedis) -> Any:
    return patch.object(session_lifecycle, "get_redis_client", AsyncMock(return_value=client))


# ===========================================================================
# Tests: courtesy strings — wording must drop the "send me a message" ask.
# ===========================================================================


class TestCourtesyStringWording:
    """The three courtesy strings must not tell the user to send a message.

    The whole point of PR 3 is that the system self-continues: if a string
    still says "send me a message" we have re-introduced the bug the smoke
    test caught.
    """

    def test_shutdown_string_has_no_send_ask(self) -> None:
        msg = ahs_state._SLACK_SHUTDOWN_COURTESY_MSG
        assert "send me a message" not in msg.lower()
        assert "send a message" not in msg.lower()
        # Still says it's restarting.
        assert "restart" in msg.lower()

    def test_restart_interrupted_string_has_no_send_ask(self) -> None:
        msg = ahs_state._SLACK_RESTART_COURTESY_MSG_INTERRUPTED
        assert "send me a message" not in msg.lower()
        assert "send a message" not in msg.lower()
        # Conveys "I'm continuing on your behalf".
        assert "picking up" in msg.lower()

    def test_restart_idle_string_has_no_send_ask(self) -> None:
        msg = ahs_state._SLACK_RESTART_COURTESY_MSG_IDLE
        assert "send me a message" not in msg.lower()
        assert "send a message" not in msg.lower()
        assert "back online" in msg.lower()


# ===========================================================================
# Tests: list_resume_pending_session_ids — startup partition input.
# ===========================================================================


class TestListResumePendingSessionIds:
    """``list_resume_pending_session_ids`` SCANs the resume_pending namespace."""

    async def test_returns_session_ids_from_scan(self) -> None:
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        fake = _FakeRedis(
            initial={
                session_lifecycle._resume_pending_key(sid_a): "1",
                session_lifecycle._resume_pending_key(sid_b): "1",
                # Unrelated key — must not be returned.
                "ahs:stream:other": "noise",
            }
        )

        with _patch_redis(fake):
            result = await session_lifecycle.list_resume_pending_session_ids()

        assert result == {sid_a, sid_b}

    async def test_returns_empty_set_on_redis_error(self) -> None:
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.object(session_lifecycle, "get_redis_client", broken):
            result = await session_lifecycle.list_resume_pending_session_ids()
        # Falls through to "everything is idle" rather than raising into startup.
        assert result == set()

    async def test_skips_malformed_keys(self) -> None:
        sid_good = uuid.uuid4()
        fake = _FakeRedis(
            initial={
                session_lifecycle._resume_pending_key(sid_good): "1",
                "ahs:resume_pending:not-a-uuid": "x",
            }
        )
        with _patch_redis(fake):
            result = await session_lifecycle.list_resume_pending_session_ids()
        assert result == {sid_good}


# ===========================================================================
# Tests: dispatch_resume_turn — happy + skipped + failed paths.
# ===========================================================================


def _stub_dispatch_environment(
    session: AgentSession,
    agent: Agent,
    *,
    preamble: str = "[SYSTEM] resume preamble. ---\n\n",
    flag_set: bool = True,
) -> dict[str, Any]:
    """Build the patches common to every ``dispatch_resume_turn`` test.

    Returns a dict with:
      * ``redis``         — the in-memory ``_FakeRedis`` (caller can poke it
                             before the test, e.g. to clear the flag for the
                             race scenario).
      * ``run_task_mock`` — stand-in for ``_run_agent_task``; tests assert
                             on its kwargs.
      * ``record_metric`` — stand-in for ``_record_courtesy_metric``; tests
                             read its calls to assert outcome labels.
      * ``persisted_msgs`` — list captured from ``db_session.add(...)`` so
                             tests can assert on the synthetic USER row.
    """
    fake_redis = _FakeRedis(
        initial={session_lifecycle._resume_pending_key(session.agent_session_id): "1"} if flag_set else {}
    )

    persisted_msgs: list[Any] = []

    db_session = AsyncMock()
    db_session.commit = AsyncMock()
    db_session.add = MagicMock(side_effect=persisted_msgs.append)

    # Two ``get`` calls inside dispatch_resume_turn:
    #   metadata-load tx: AgentSession then Agent
    #   FOR UPDATE tx: AgentSession (after lock)
    db_session.get = AsyncMock(side_effect=[session, agent, session])
    db_session.exec = AsyncMock(return_value=MagicMock())  # FOR UPDATE statement

    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield db_session

    run_task_mock = AsyncMock(return_value=None)
    record_metric = MagicMock()
    build_preamble = AsyncMock(return_value=preamble)
    next_turn = AsyncMock(return_value=7)

    return {
        "redis": fake_redis,
        "db_session": db_session,
        "ctx": _ctx,
        "run_task_mock": run_task_mock,
        "record_metric": record_metric,
        "build_preamble": build_preamble,
        "next_turn": next_turn,
        "persisted_msgs": persisted_msgs,
    }


class TestDispatchResumeTurnHappyPath:
    """Successful auto-continuation: synthetic row + fire runner + dispatched metric."""

    async def test_writes_synthetic_user_row_with_system_continuation_flag(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        env = _stub_dispatch_environment(session, agent)

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            try:
                ahs_state._active_tasks.pop(session.agent_session_id, None)
                await session_lifecycle.dispatch_resume_turn(session.agent_session_id)
            finally:
                ahs_state._active_tasks.pop(session.agent_session_id, None)

        # A single AgentSessionMessage was added (the synthetic USER row).
        assert len(env["persisted_msgs"]) == 1
        synthetic = env["persisted_msgs"][0]
        assert isinstance(synthetic, AgentSessionMessage)
        assert synthetic.role == AgentSessionMessageRole.USER
        # Identity inheritance — must NOT escalate beyond the session creator.
        assert synthetic.creator_user_id == session.creator_user_id
        # raw_events carries the ``is_system_continuation`` marker so the
        # frontends can render this row distinctly from a user-typed message.
        assert synthetic.raw_events
        assert synthetic.raw_events[0]["is_system_continuation"] is True
        assert synthetic.raw_events[0]["type"] == "auto_resume"
        # Content is the explicit AUTO-RESUME marker, NOT the preamble.
        assert "AUTO-RESUME" in (synthetic.content or "")
        # Turn number was bumped via ``_next_turn_number``.
        assert synthetic.turn_number == 7

    async def test_runs_agent_task_with_preamble_as_runner_input(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        preamble = "[SYSTEM] previous turn was interrupted. ---\n\n"
        env = _stub_dispatch_environment(session, agent, preamble=preamble)

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            try:
                ahs_state._active_tasks.pop(session.agent_session_id, None)
                await session_lifecycle.dispatch_resume_turn(session.agent_session_id)
            finally:
                ahs_state._active_tasks.pop(session.agent_session_id, None)

        env["run_task_mock"].assert_called_once()
        kwargs = env["run_task_mock"].call_args.kwargs
        # The runner input is the preamble — NOT the synthetic USER row's content.
        assert kwargs["message"] == preamble
        assert kwargs["agent_session_id"] == session.agent_session_id
        assert kwargs["agent_config_name"] == agent.name
        assert kwargs["turn_number"] == 7
        assert kwargs["is_slack"] is True
        assert kwargs["slack_session_id"] == session.slack_session_id

    async def test_clears_redis_flag_exactly_once(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        env = _stub_dispatch_environment(session, agent)
        flag_key = session_lifecycle._resume_pending_key(session.agent_session_id)
        assert flag_key in env["redis"]._store  # sanity

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            try:
                ahs_state._active_tasks.pop(session.agent_session_id, None)
                await session_lifecycle.dispatch_resume_turn(session.agent_session_id)
            finally:
                ahs_state._active_tasks.pop(session.agent_session_id, None)

        # GETDEL cleared the key.  A subsequent ``send_message`` must NOT see
        # the preamble path fire a second time.
        assert flag_key not in env["redis"]._store

    async def test_emits_dispatched_metric(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        env = _stub_dispatch_environment(session, agent)

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            try:
                ahs_state._active_tasks.pop(session.agent_session_id, None)
                await session_lifecycle.dispatch_resume_turn(session.agent_session_id)
            finally:
                ahs_state._active_tasks.pop(session.agent_session_id, None)

        # The single metric call was the success outcome.
        env["record_metric"].assert_called_once_with("auto_resume", "dispatched", count=1)


class TestDispatchResumeTurnSkipped:
    """Paths that bail out gracefully without firing the runner."""

    async def test_skipped_when_redis_flag_already_cleared(self) -> None:
        """User message races and clears the flag first → dispatch must bail.

        The user's ``send_message`` path already runs the preamble for the
        racing message; firing a parallel auto-resume turn would produce
        two concurrent runs.
        """
        session = _make_slack_session()
        agent = _make_agent()
        # ``flag_set=False`` simulates a racing send_message that already
        # consumed the resume_pending key.
        env = _stub_dispatch_environment(session, agent, flag_set=False)

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            await session_lifecycle.dispatch_resume_turn(session.agent_session_id)

        # No persistence, no runner fire.
        assert env["persisted_msgs"] == []
        env["run_task_mock"].assert_not_called()
        env["record_metric"].assert_called_once_with("auto_resume", "skipped", count=1)

    async def test_skipped_when_preamble_is_empty(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        env = _stub_dispatch_environment(session, agent, preamble="")

        with (
            patch.object(session_lifecycle, "get_async_session", env["ctx"]),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            await session_lifecycle.dispatch_resume_turn(session.agent_session_id)

        # Empty preamble → bail before claiming the flag.  The Redis key is
        # left intact so the user's eventual reply still picks up the
        # (admittedly empty) safety-net path.
        flag_key = session_lifecycle._resume_pending_key(session.agent_session_id)
        assert flag_key in env["redis"]._store
        env["run_task_mock"].assert_not_called()
        env["record_metric"].assert_called_once_with("auto_resume", "skipped", count=1)

    async def test_skipped_when_session_missing(self) -> None:
        session = _make_slack_session()
        # ``get`` returns None on the very first call (session lookup) → bail.
        db_session = AsyncMock()
        db_session.get = AsyncMock(return_value=None)
        db_session.exec = AsyncMock()
        db_session.commit = AsyncMock()
        db_session.add = MagicMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[Any, None]:
            yield db_session

        record_metric = MagicMock()
        run_task_mock = AsyncMock()

        with (
            patch.object(session_lifecycle, "get_async_session", _ctx),
            patch.object(session_lifecycle, "_run_agent_task", run_task_mock),
            patch.object(session_lifecycle, "_record_courtesy_metric", record_metric),
        ):
            await session_lifecycle.dispatch_resume_turn(session.agent_session_id)

        run_task_mock.assert_not_called()
        record_metric.assert_called_once_with("auto_resume", "skipped", count=1)


class TestDispatchResumeTurnFailed:
    """Persistence-time failure must emit ``outcome=failed``, not ``dispatched``."""

    async def test_emit_failed_when_persistence_raises(self) -> None:
        session = _make_slack_session()
        agent = _make_agent()
        env = _stub_dispatch_environment(session, agent)

        # Make the FOR UPDATE-tx ``commit`` blow up to model a transient DB
        # failure between consume_resume_pending and the runner fire.
        # Reset db_session.get to give the FOR UPDATE branch the session row
        # before commit explodes.
        db_session = AsyncMock()
        db_session.get = AsyncMock(side_effect=[session, agent, session])
        db_session.exec = AsyncMock(return_value=MagicMock())
        db_session.add = MagicMock()
        db_session.commit = AsyncMock(side_effect=RuntimeError("db down"))

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[Any, None]:
            yield db_session

        with (
            patch.object(session_lifecycle, "get_async_session", _ctx),
            patch.object(session_lifecycle, "build_resume_context", env["build_preamble"]),
            patch.object(session_lifecycle, "_next_turn_number", env["next_turn"]),
            patch.object(session_lifecycle, "_run_agent_task", env["run_task_mock"]),
            patch.object(
                session_lifecycle,
                "create_background_task",
                lambda coro: MagicMock(name="bg-task"),
            ),
            patch.object(session_lifecycle, "_record_courtesy_metric", env["record_metric"]),
            _patch_redis(env["redis"]),
        ):
            await session_lifecycle.dispatch_resume_turn(session.agent_session_id)

        env["run_task_mock"].assert_not_called()
        env["record_metric"].assert_called_once_with("auto_resume", "failed", count=1)


# ===========================================================================
# Tests: _recover_stale_sessions partitioning logic.
# ===========================================================================


class _FakeDBSession:
    """Stub matching the pattern used by test_lifespan_recovery.py."""

    def __init__(self, exec_responses: list[Any]) -> None:
        self._responses = list(exec_responses)
        self.exec_calls: list[Any] = []
        self.commit_called = False

    async def exec(self, statement: Any) -> Any:
        self.exec_calls.append(statement)
        if not self._responses:
            raise AssertionError("FakeDBSession exhausted")
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


def _make_active_slack_session() -> AgentSession:
    return AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status=AgentSessionStatus.ACTIVE,
        trigger=AgentSessionTrigger.SLACK,
        slack_session_id="C1:ts:A1",
        modified_at=datetime.now(UTC),
        parent_session_id=None,
    )


class TestRecoverStaleSessionsPartition:
    """``_recover_stale_sessions`` partitions Slack sessions correctly."""

    async def test_interrupted_bucket_gets_interrupted_courtesy_and_dispatch(self) -> None:
        interrupted = _make_active_slack_session()
        idle = _make_active_slack_session()

        fake_db = _FakeDBSession(
            exec_responses=[
                # initial ACTIVE-session query
                [interrupted, idle],
                # interrupted: no terminal msg, no USER turn → STALE
                [],
                [None],
                # idle: same canned classification — both end up in ``all_slack_session_ids``
                [],
                [None],
            ]
        )

        # Redis says only ``interrupted`` is in the resume-pending bucket.
        list_pending = AsyncMock(return_value={interrupted.agent_session_id})
        restart_courtesy = AsyncMock()
        dispatch = AsyncMock()
        # Capture asyncio.create_task calls so we can assert ``dispatch_resume_turn``
        # was scheduled exactly once for the interrupted session.
        scheduled: list[tuple[Any, str | None]] = []

        def _capture(coro: Any, *, name: str | None = None) -> Any:
            scheduled.append((coro, name))
            # Avoid leaking unawaited coroutine warnings — close it.
            coro.close()
            return MagicMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                list_pending,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch("ypl.agent_harness_service.lifespan.dispatch_resume_turn", dispatch),
            patch("ypl.agent_harness_service.lifespan.asyncio.create_task", _capture),
        ):
            await lifespan._recover_stale_sessions()

        # Two restart-courtesy calls — one per bucket.
        assert restart_courtesy.await_count == 2
        # Sort by the ``interrupted`` kwarg so the test isn't order-dependent.
        calls_by_flag = {call.kwargs["interrupted"]: call.args[0] for call in restart_courtesy.await_args_list}
        assert calls_by_flag[True] == [interrupted.agent_session_id]
        assert calls_by_flag[False] == [idle.agent_session_id]
        # Exactly one auto-resume scheduled — for the interrupted session.
        assert len(scheduled) == 1
        _, name = scheduled[0]
        assert name == f"auto-resume-{interrupted.agent_session_id}"

    async def test_all_idle_skips_dispatch_entirely(self) -> None:
        """Pre-restart turns finished cleanly → no auto-resume fire, no
        ``interrupted=True`` courtesy."""
        idle = _make_active_slack_session()

        fake_db = _FakeDBSession(
            exec_responses=[
                [idle],
                [],  # no terminal
                [None],  # no USER turn
            ]
        )

        list_pending = AsyncMock(return_value=set())  # empty → all idle
        restart_courtesy = AsyncMock()
        dispatch = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                list_pending,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch("ypl.agent_harness_service.lifespan.dispatch_resume_turn", dispatch),
        ):
            await lifespan._recover_stale_sessions()

        # Only the idle bucket fired.
        restart_courtesy.assert_awaited_once()
        await_args = restart_courtesy.await_args
        assert await_args is not None
        assert await_args.kwargs.get("interrupted") is False
        dispatch.assert_not_called()

    async def test_all_interrupted_skips_idle_courtesy(self) -> None:
        """Every Slack session was killed mid-flight → only the
        ``interrupted=True`` wording fires, no idle ping."""
        interrupted = _make_active_slack_session()

        fake_db = _FakeDBSession(
            exec_responses=[
                [interrupted],
                [],
                [None],
            ]
        )

        list_pending = AsyncMock(return_value={interrupted.agent_session_id})
        restart_courtesy = AsyncMock()
        scheduled: list[tuple[Any, str | None]] = []

        def _capture(coro: Any, *, name: str | None = None) -> Any:
            scheduled.append((coro, name))
            coro.close()
            return MagicMock()

        with (
            patch(
                "ypl.agent_harness_service.lifespan.get_async_session",
                _async_session_factory(fake_db),
            ),
            patch(
                "ypl.agent_harness_service.lifespan.list_resume_pending_session_ids",
                list_pending,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.send_slack_restart_courtesy",
                restart_courtesy,
            ),
            patch(
                "ypl.agent_harness_service.lifespan.dispatch_resume_turn",
                AsyncMock(),
            ),
            patch("ypl.agent_harness_service.lifespan.asyncio.create_task", _capture),
        ):
            await lifespan._recover_stale_sessions()

        # Single restart-courtesy call — interrupted bucket only.
        restart_courtesy.assert_awaited_once()
        await_args = restart_courtesy.await_args
        assert await_args is not None
        assert await_args.kwargs.get("interrupted") is True
        # One dispatch scheduled.
        assert len(scheduled) == 1


class TestSendSlackRestartCourtesyVariants:
    """``send_slack_restart_courtesy`` text differs by ``interrupted`` flag."""

    async def test_interrupted_uses_picking_up_text(self) -> None:
        send_mock = AsyncMock()
        with patch.object(session_lifecycle, "_send_slack_courtesy", send_mock):
            await session_lifecycle.send_slack_restart_courtesy([uuid.uuid4()], interrupted=True)
        sent_text = send_mock.call_args[0][1]
        assert "picking up" in sent_text.lower()
        assert "send me a message" not in sent_text.lower()

    async def test_idle_uses_bare_back_online_text(self) -> None:
        send_mock = AsyncMock()
        with patch.object(session_lifecycle, "_send_slack_courtesy", send_mock):
            await session_lifecycle.send_slack_restart_courtesy([uuid.uuid4()], interrupted=False)
        sent_text = send_mock.call_args[0][1]
        assert "back online" in sent_text.lower()
        assert "picking up" not in sent_text.lower()

    async def test_default_is_idle_for_backward_compat(self) -> None:
        send_mock = AsyncMock()
        with patch.object(session_lifecycle, "_send_slack_courtesy", send_mock):
            # Default kwargs — must be the idle wording.
            await session_lifecycle.send_slack_restart_courtesy([uuid.uuid4()])
        sent_text = send_mock.call_args[0][1]
        assert "back online" in sent_text.lower()
