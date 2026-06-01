"""Regression tests for ``ypl.agent_harness_service.tools.fork.fork_session``.

The bug: ``fork_session`` is always invoked from within the source session's
current in-flight turn (the same turn that called the ``/fork`` skill / the
``fork_session`` MCP tool).  At snapshot time, the source's most recent USER
message has ``completion_status=SUCCESS`` but the matching AGENT response is
still ``IN_PROGRESS``.  The snapshot filter copies only SUCCESS rows, so the
new session ends up with a USER turn that has no AGENT or SYSTEM row at the
same ``turn_number``.

``send_message`` (called next) consults ``_has_inflight_turn``, which finds
the latest USER turn and requires at least one non-inbound, non-IN_PROGRESS
sibling row at that ``turn_number`` to treat the turn as "answered".  With
the marker placed at ``max_turn + 1`` the check fails, ``send_message``
queues the additional-instructions message into the in-memory
``_pending_messages`` queue, and (because there is no active task on a
freshly created session to drain that queue) the fork never runs.

The fix is to place the SYSTEM marker at the same ``turn_number`` as the
latest snapshotted USER message.  The marker has
``completion_status=SUCCESS`` so it counts as a completed response for that
turn, the inflight check returns False, and ``send_message`` dispatches the
new turn normally.

These tests pin down that marker placement directly so the regression cannot
silently come back.
"""

from __future__ import annotations
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy SDK deps so the fork module can be imported in a plain test env.
# Mirrors the pattern used by test_session_lifecycle_resume.py.
# ---------------------------------------------------------------------------

_together = types.ModuleType("together")
_together_types = types.ModuleType("together.types")
_croniter = types.ModuleType("croniter")


class _APITimeoutError(Exception):
    pass


_together.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
_together.AsyncTogether = type("AsyncTogether", (), {})  # type: ignore[attr-defined]
_together.Together = type("Together", (), {})  # type: ignore[attr-defined]
_together_types.ChatCompletion = type("ChatCompletion", (), {})  # type: ignore[attr-defined]
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

from ypl.agent_harness_service.tools import fork as fork_mod  # noqa: E402
from ypl.db.agent_harness import (  # noqa: E402
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)

# Unwrap the MCP FunctionTool wrapper to get the raw async callable.
fork_session = fork_mod.fork_session.fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source_session(
    *,
    session_uuid: uuid.UUID,
    user_id: str = "user-abc",
) -> AgentSession:
    return AgentSession(
        agent_session_id=session_uuid,
        agent_id=uuid.uuid4(),
        status=AgentSessionStatus.ACTIVE,
        trigger=AgentSessionTrigger.SLACK,
        slack_session_id="C123:1234.5678:A456",
        creator_user_id=user_id,
        context={
            "user_id": user_id,
            "slack_channel_id": "C123",
            "slack_thread_ts": "1234.5678",
            "current_turn_user_id": user_id,
        },
        workspace="/tmp/ws",
    )


def _make_msg(
    session_uuid: uuid.UUID,
    *,
    role: AgentSessionMessageRole,
    turn: int,
    content: str = "",
    completion: AgentSessionMessageCompletionStatus = AgentSessionMessageCompletionStatus.SUCCESS,
) -> AgentSessionMessage:
    return AgentSessionMessage(
        agent_session_message_id=uuid.uuid4(),
        agent_session_id=session_uuid,
        role=role,
        turn_number=turn,
        completion_status=completion,
        content=content,
    )


class _SnapshotRecorder:
    """Captures rows added to the snapshot DB session so the test can inspect
    the marker placement and the copied turn list without a real database.
    """

    def __init__(
        self,
        *,
        source_session: AgentSession,
        source_agent: Agent,
        source_messages: list[AgentSessionMessage],
        new_session_uuid: uuid.UUID,
        new_session: AgentSession,
    ) -> None:
        self._source_session = source_session
        self._source_agent = source_agent
        self._source_messages = source_messages
        self._new_session_uuid = new_session_uuid
        self._new_session = new_session
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, ident: Any) -> Any:
        if model is AgentSession:
            if ident == self._source_session.agent_session_id:
                return self._source_session
            if ident == self._new_session_uuid:
                return self._new_session
            return None
        if model is Agent and ident == self._source_session.agent_id:
            return self._source_agent
        return None

    async def execute(self, _statement: Any) -> Any:
        # fork_session issues a single SELECT for completed source messages
        # inside the snapshot block; return them in turn order.
        scalars = MagicMock()
        scalars.all.return_value = list(self._source_messages)
        result = MagicMock()
        result.scalars.return_value = scalars
        return result

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


def _async_session_factory(*recorders: Any) -> Any:
    """Yield each recorder once, in order, across successive ``get_async_session()`` calls.

    fork_session opens TWO ``async with get_async_session()`` blocks: one to
    load + authorize the source, one for the snapshot commit.  We feed a
    different fake DB into each.
    """
    queue = list(recorders)

    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        if not queue:
            raise AssertionError("fork_session opened more DB sessions than the test prepared")
        yield queue.pop(0)

    return _ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestForkSnapshotMarkerPlacement:
    """The SYSTEM marker MUST land on the same turn_number as the latest USER
    message in the snapshot — otherwise ``_has_inflight_turn`` queues the
    follow-up send_message instead of dispatching it, and the fork dies
    silently."""

    async def test_marker_turn_equals_latest_user_turn_when_agent_response_in_progress(self) -> None:
        """The fork-from-inside-an-inflight-turn case.

        Source has:
          turn 1: USER (SUCCESS), AGENT (SUCCESS)
          turn 2: USER (SUCCESS), AGENT (IN_PROGRESS)  ← the turn that called /fork

        Snapshot filter copies SUCCESS rows only, so the new session sees
        turn 1: USER + AGENT, turn 2: USER (no AGENT yet).  The marker must
        be co-located with turn 2's USER (turn_number=2), not at turn 3.
        """
        src_uuid = uuid.uuid4()
        new_uuid = uuid.uuid4()
        source = _make_source_session(session_uuid=src_uuid)
        agent = Agent(agent_id=source.agent_id, name="eng-raccoon")

        source_messages = [
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=1, content="hi"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.AGENT, turn=1, content="hello"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=2, content="/fork keep digging"),
            # The IN_PROGRESS AGENT row is intentionally absent — the snapshot
            # filter excludes it, so it never makes it into source_messages.
        ]

        new_session_row = AgentSession(
            agent_session_id=new_uuid,
            agent_id=source.agent_id,
            status=AgentSessionStatus.ACTIVE,
            trigger=AgentSessionTrigger.API,
            creator_user_id=source.creator_user_id,
            context={"user_id": source.creator_user_id},
            workspace="/tmp/ws-new",
        )

        load_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=[],
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )
        snapshot_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=source_messages,
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )

        create_resp = MagicMock()
        create_resp.session_id = str(new_uuid)
        fake_create_session = AsyncMock(return_value=create_resp)
        fake_send_message = AsyncMock(return_value=MagicMock(status="processing", turn_number=3))

        # Stub out the lazy import inside fork_session so we don't drag the
        # real session_lifecycle module + its DB dependencies into the test.
        fake_lifecycle_module = types.ModuleType("session_lifecycle_stub")
        fake_lifecycle_module.create_session = fake_create_session  # type: ignore[attr-defined]
        fake_lifecycle_module.send_message = fake_send_message  # type: ignore[attr-defined]
        sys.modules["ypl.agent_harness_service.service.session_lifecycle"] = fake_lifecycle_module

        with (
            patch.object(
                fork_mod,
                "get_async_session",
                _async_session_factory(load_recorder, snapshot_recorder),
            ),
            patch.object(fork_mod, "mcp_session_id_var", MagicMock(get=MagicMock(return_value=str(src_uuid)))),
        ):
            result = await fork_session(
                additional_instructions="now explore the alternate angle",
                source_session_id=str(src_uuid),
                target="headless",
                include_tool_calls=False,
            )

        assert result["status"] == "ok", result
        assert result["new_session_id"] == str(new_uuid)
        assert result["forked_from"] == str(src_uuid)

        # Inspect what got written to the snapshot DB session.
        marker_rows = [
            m
            for m in snapshot_recorder.added
            if isinstance(m, AgentSessionMessage) and m.role == AgentSessionMessageRole.SYSTEM
        ]
        assert len(marker_rows) == 1, f"expected exactly one SYSTEM marker, got {marker_rows!r}"
        marker = marker_rows[0]

        # The critical assertion: marker shares turn_number with the latest USER
        # message in the snapshot.  ``_has_inflight_turn(new_session)`` consults
        # the same turn_number; a SYSTEM row with completion_status=SUCCESS at
        # that turn is what allows send_message to dispatch instead of queue.
        assert marker.turn_number == 2, (
            f"SYSTEM marker placed at turn {marker.turn_number}; must equal latest USER turn (2) "
            "so _has_inflight_turn does not queue the follow-up send_message into _pending_messages "
            "with no active task to drain it. See fork.py marker_turn comment."
        )
        assert marker.completion_status == AgentSessionMessageCompletionStatus.SUCCESS
        assert marker.content is not None
        assert f"turn {2}" in marker.content

        # send_message must actually be called — if marker placement reverted
        # to turn+1, send_message would still be called but its returned
        # status would be "queued" (not asserted here because we mocked it).
        # The placement assertion above is the load-bearing one.
        assert fake_send_message.await_count == 1

    async def test_marker_text_references_source_session_and_max_turn(self) -> None:
        """The marker copy still reads naturally — references the source UUID
        and the snapshot's max turn so a user reading the new session's
        message stream knows where the replay ended."""
        src_uuid = uuid.uuid4()
        new_uuid = uuid.uuid4()
        source = _make_source_session(session_uuid=src_uuid)
        agent = Agent(agent_id=source.agent_id, name="eng-raccoon")

        source_messages = [
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=1, content="hi"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.AGENT, turn=1, content="hello"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=2, content="continue"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.AGENT, turn=2, content="continuing"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=3, content="/fork branch off"),
        ]

        new_session_row = AgentSession(
            agent_session_id=new_uuid,
            agent_id=source.agent_id,
            status=AgentSessionStatus.ACTIVE,
            trigger=AgentSessionTrigger.API,
            creator_user_id=source.creator_user_id,
            context={"user_id": source.creator_user_id},
            workspace="/tmp/ws-new",
        )

        load_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=[],
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )
        snapshot_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=source_messages,
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )

        create_resp = MagicMock()
        create_resp.session_id = str(new_uuid)
        fake_lifecycle_module = types.ModuleType("session_lifecycle_stub")
        fake_lifecycle_module.create_session = AsyncMock(return_value=create_resp)  # type: ignore[attr-defined]
        fake_lifecycle_module.send_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(status="processing", turn_number=4)
        )
        sys.modules["ypl.agent_harness_service.service.session_lifecycle"] = fake_lifecycle_module

        with (
            patch.object(
                fork_mod,
                "get_async_session",
                _async_session_factory(load_recorder, snapshot_recorder),
            ),
            patch.object(fork_mod, "mcp_session_id_var", MagicMock(get=MagicMock(return_value=str(src_uuid)))),
        ):
            await fork_session(
                additional_instructions="branch off",
                source_session_id=str(src_uuid),
                target="headless",
            )

        marker_rows = [
            m
            for m in snapshot_recorder.added
            if isinstance(m, AgentSessionMessage) and m.role == AgentSessionMessageRole.SYSTEM
        ]
        assert len(marker_rows) == 1
        marker = marker_rows[0]
        # max_turn is 3 (last USER) — marker shares that turn.
        assert marker.turn_number == 3
        assert marker.content is not None
        assert str(src_uuid) in marker.content
        assert "turn 3" in marker.content

    async def test_empty_snapshot_clamps_marker_turn_to_one(self) -> None:
        """``max_turn`` of an empty source-message list is 0; the marker must be
        clamped to ``turn_number=1`` (a 0-turn row would be a phantom row
        below the natural USER turn numbering) and the rendered marker text
        must reference the clamped turn, not the raw ``max_turn=0``.

        This is the edge case the ``max(max_turn, 1)`` guard exists to handle.
        Without it a debugger would see a phantom row at ``turn_number=0``;
        without using ``marker_turn`` in the text it would read
        "Forked … at turn 0" while the row sits at turn 1.
        """
        src_uuid = uuid.uuid4()
        new_uuid = uuid.uuid4()
        source = _make_source_session(session_uuid=src_uuid)
        agent = Agent(agent_id=source.agent_id, name="eng-raccoon")

        # No completed source messages — the snapshot-filter SELECT returns
        # nothing, so max_turn=0 and the clamp kicks in.
        source_messages: list[AgentSessionMessage] = []

        new_session_row = AgentSession(
            agent_session_id=new_uuid,
            agent_id=source.agent_id,
            status=AgentSessionStatus.ACTIVE,
            trigger=AgentSessionTrigger.API,
            creator_user_id=source.creator_user_id,
            context={"user_id": source.creator_user_id},
            workspace="/tmp/ws-new",
        )

        load_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=[],
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )
        snapshot_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=source_messages,
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )

        create_resp = MagicMock()
        create_resp.session_id = str(new_uuid)
        fake_lifecycle_module = types.ModuleType("session_lifecycle_stub")
        fake_lifecycle_module.create_session = AsyncMock(return_value=create_resp)  # type: ignore[attr-defined]
        fake_lifecycle_module.send_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(status="processing", turn_number=2)
        )
        sys.modules["ypl.agent_harness_service.service.session_lifecycle"] = fake_lifecycle_module

        with (
            patch.object(
                fork_mod,
                "get_async_session",
                _async_session_factory(load_recorder, snapshot_recorder),
            ),
            patch.object(fork_mod, "mcp_session_id_var", MagicMock(get=MagicMock(return_value=str(src_uuid)))),
        ):
            result = await fork_session(
                additional_instructions="start fresh on this branch",
                source_session_id=str(src_uuid),
                target="headless",
            )

        assert result["status"] == "ok", result

        marker_rows = [
            m
            for m in snapshot_recorder.added
            if isinstance(m, AgentSessionMessage) and m.role == AgentSessionMessageRole.SYSTEM
        ]
        assert len(marker_rows) == 1
        marker = marker_rows[0]
        # Clamped to 1 — NOT 0, NOT max_turn + 1 (which would be 1 by coincidence
        # but only because max_turn happens to be 0; the clamp is the intent).
        assert marker.turn_number == 1
        assert marker.content is not None
        # The rendered text must use the clamped turn, not max_turn=0.
        # A row at turn_number=1 saying "at turn 0" would mislead anyone
        # debugging fork lineage by reading the message stream.
        assert "turn 1" in marker.content
        assert "turn 0" not in marker.content

    async def test_marker_added_to_db_before_send_message_is_awaited(self) -> None:
        """The marker MUST be visible to ``_has_inflight_turn`` by the time
        ``send_message`` is awaited — otherwise the inflight-gate sees the
        latest USER row with no completed response and queues the new turn,
        exactly the bug this PR fixes.

        A future refactor that moves ``db.add(marker)`` to *after* the
        ``send_message`` call (or reorders to commit later) would silently
        re-break the fork without changing the marker_turn value the other
        tests assert on. This test pins down the ordering by capturing
        ``snapshot_recorder.added`` at the moment ``send_message`` is awaited
        and asserting the SYSTEM marker is already present.
        """
        src_uuid = uuid.uuid4()
        new_uuid = uuid.uuid4()
        source = _make_source_session(session_uuid=src_uuid)
        agent = Agent(agent_id=source.agent_id, name="eng-raccoon")

        source_messages = [
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=1, content="hi"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.AGENT, turn=1, content="hello"),
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=2, content="/fork"),
        ]

        new_session_row = AgentSession(
            agent_session_id=new_uuid,
            agent_id=source.agent_id,
            status=AgentSessionStatus.ACTIVE,
            trigger=AgentSessionTrigger.API,
            creator_user_id=source.creator_user_id,
            context={"user_id": source.creator_user_id},
            workspace="/tmp/ws-new",
        )

        load_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=[],
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )
        snapshot_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=source_messages,
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )

        # Snapshot the `added` list at the moment send_message is awaited.
        seen_at_send: list[Any] = []

        async def _capture_then_succeed(_req: Any) -> Any:
            seen_at_send.extend(snapshot_recorder.added)
            return MagicMock(status="processing", turn_number=3)

        create_resp = MagicMock()
        create_resp.session_id = str(new_uuid)
        fake_lifecycle_module = types.ModuleType("session_lifecycle_stub")
        fake_lifecycle_module.create_session = AsyncMock(return_value=create_resp)  # type: ignore[attr-defined]
        fake_lifecycle_module.send_message = AsyncMock(side_effect=_capture_then_succeed)  # type: ignore[attr-defined]
        sys.modules["ypl.agent_harness_service.service.session_lifecycle"] = fake_lifecycle_module

        with (
            patch.object(
                fork_mod,
                "get_async_session",
                _async_session_factory(load_recorder, snapshot_recorder),
            ),
            patch.object(fork_mod, "mcp_session_id_var", MagicMock(get=MagicMock(return_value=str(src_uuid)))),
        ):
            result = await fork_session(
                additional_instructions="keep going",
                source_session_id=str(src_uuid),
                target="headless",
            )

        assert result["status"] == "ok", result

        # The SYSTEM marker for the snapshotted USER turn must already be in
        # snapshot_recorder.added by the time send_message is awaited.
        marker_rows_at_send = [
            m for m in seen_at_send if isinstance(m, AgentSessionMessage) and m.role == AgentSessionMessageRole.SYSTEM
        ]
        assert len(marker_rows_at_send) == 1, (
            "SYSTEM marker was not yet added when send_message was awaited — "
            "marker placement+timing invariant violated; the inflight-gate will "
            "queue this turn and the fork will die silently."
        )
        assert marker_rows_at_send[0].turn_number == 2

    async def test_send_message_returning_queued_yields_partial_status(self) -> None:
        """If a future refactor reverts marker placement, ``send_message``
        returns ``SessionMessageResponse(status="queued")`` without raising.
        That path used to silently return ``status="ok"`` and the fork would
        die in process memory. After this PR, the defensive check converts
        ``queued`` into ``status="partial"`` so the regression is loud.
        """
        src_uuid = uuid.uuid4()
        new_uuid = uuid.uuid4()
        source = _make_source_session(session_uuid=src_uuid)
        agent = Agent(agent_id=source.agent_id, name="eng-raccoon")

        source_messages = [
            _make_msg(src_uuid, role=AgentSessionMessageRole.USER, turn=1, content="/fork"),
        ]

        new_session_row = AgentSession(
            agent_session_id=new_uuid,
            agent_id=source.agent_id,
            status=AgentSessionStatus.ACTIVE,
            trigger=AgentSessionTrigger.API,
            creator_user_id=source.creator_user_id,
            context={"user_id": source.creator_user_id},
            workspace="/tmp/ws-new",
        )

        load_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=[],
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )
        snapshot_recorder = _SnapshotRecorder(
            source_session=source,
            source_agent=agent,
            source_messages=source_messages,
            new_session_uuid=new_uuid,
            new_session=new_session_row,
        )

        create_resp = MagicMock()
        create_resp.session_id = str(new_uuid)
        fake_lifecycle_module = types.ModuleType("session_lifecycle_stub")
        fake_lifecycle_module.create_session = AsyncMock(return_value=create_resp)  # type: ignore[attr-defined]
        # Simulate the regression path: send_message returns queued (no exception).
        fake_lifecycle_module.send_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(status="queued", turn_number=-1)
        )
        sys.modules["ypl.agent_harness_service.service.session_lifecycle"] = fake_lifecycle_module

        with (
            patch.object(
                fork_mod,
                "get_async_session",
                _async_session_factory(load_recorder, snapshot_recorder),
            ),
            patch.object(fork_mod, "mcp_session_id_var", MagicMock(get=MagicMock(return_value=str(src_uuid)))),
        ):
            result = await fork_session(
                additional_instructions="next branch",
                source_session_id=str(src_uuid),
                target="headless",
            )

        assert result["status"] == "partial", result
        assert result["new_session_id"] == str(new_uuid)
        assert result["forked_from"] == str(src_uuid)
        assert "queued" in result["error"]
