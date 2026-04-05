"""Tests for ypl.agent_harness_service.core.subagent_queue.

Covers:
- SubagentResult dataclass fields and defaults
- register_delivery_callback
- put_result / _drain: queue creation, drain, callback invocation, cleanup
- _drain with no callback: logs error, does not raise
- _drain with failing callback: handles exception gracefully
- MAX_SUBAGENT_DEPTH constant
"""

from __future__ import annotations
import asyncio
from collections.abc import Generator
from typing import Any

import pytest

# Module-level reference for resetting the callback between tests
import ypl.agent_harness_service.core.subagent_queue as _queue_module
from ypl.agent_harness_service.core.subagent_queue import (
    MAX_SUBAGENT_DEPTH,
    SubagentResult,
    _drain,
    _get_or_create_queue,
    _queues,
    put_result,
    register_delivery_callback,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def _clean_state() -> Generator[None]:
    """Reset module state before each test to prevent cross-test pollution."""
    _queues.clear()
    _queue_module._delivery_callback = None
    yield
    # Cancel any outstanding drain tasks spawned by put_result()
    try:
        loop = asyncio.get_running_loop()
        for task in asyncio.all_tasks(loop):
            coro = task.get_coro()
            if coro is not None and getattr(coro, "__qualname__", None) == "_drain":
                task.cancel()
    except RuntimeError:
        pass  # No running loop — nothing to cancel
    _queues.clear()
    _queue_module._delivery_callback = None


# ===========================================================================
# SubagentResult
# ===========================================================================


class TestSubagentResult:
    def test_required_fields(self) -> None:
        result = SubagentResult(
            db_session_id="sess-123",
            agent_type="reviewer",
            model="anthropic/claude-sonnet-4-6",
            status="completed",
            text="Review done.",
        )
        assert result.db_session_id == "sess-123"
        assert result.agent_type == "reviewer"
        assert result.model == "anthropic/claude-sonnet-4-6"
        assert result.status == "completed"
        assert result.text == "Review done."

    def test_optional_fields_default_to_none(self) -> None:
        result = SubagentResult(
            db_session_id="sess-123",
            agent_type="fixer",
            model="anthropic/claude-haiku",
            status="error",
            text="",
        )
        assert result.duration_ms is None
        assert result.cost_usd is None

    def test_description_defaults_to_empty_string(self) -> None:
        result = SubagentResult(
            db_session_id="sess-123",
            agent_type="reviewer",
            model="m",
            status="completed",
            text="done",
        )
        assert result.description == ""

    def test_extra_defaults_to_empty_dict(self) -> None:
        result = SubagentResult(
            db_session_id="sess-123",
            agent_type="reviewer",
            model="m",
            status="completed",
            text="done",
        )
        assert result.extra == {}

    def test_extra_field_stores_arbitrary_data(self) -> None:
        result = SubagentResult(
            db_session_id="sess-123",
            agent_type="reviewer",
            model="m",
            status="completed",
            text="done",
            extra={"pr_url": "https://github.com/pr/123"},
        )
        assert result.extra["pr_url"] == "https://github.com/pr/123"

    def test_valid_status_values(self) -> None:
        for status in ("completed", "error", "timeout", "cancelled"):
            r = SubagentResult(db_session_id="s", agent_type="a", model="m", status=status, text="")
            assert r.status == status

    def test_with_cost_and_duration(self) -> None:
        result = SubagentResult(
            db_session_id="s",
            agent_type="a",
            model="m",
            status="completed",
            text="done",
            cost_usd=0.05,
            duration_ms=3000,
        )
        assert result.cost_usd == 0.05
        assert result.duration_ms == 3000


# ===========================================================================
# MAX_SUBAGENT_DEPTH
# ===========================================================================


class TestMaxSubagentDepth:
    def test_depth_is_positive_integer(self) -> None:
        assert isinstance(MAX_SUBAGENT_DEPTH, int)
        assert MAX_SUBAGENT_DEPTH > 0

    def test_depth_is_reasonable(self) -> None:
        assert 1 <= MAX_SUBAGENT_DEPTH <= 10


# ===========================================================================
# register_delivery_callback
# ===========================================================================


class TestRegisterDeliveryCallback:
    def test_registers_callback(self) -> None:
        async def my_callback(parent_id: str, result: SubagentResult) -> None:
            pass

        register_delivery_callback(my_callback)
        assert _queue_module._delivery_callback is my_callback

    def test_overwrites_existing_callback(self) -> None:
        async def cb1(parent_id: str, result: SubagentResult) -> None:
            pass

        async def cb2(parent_id: str, result: SubagentResult) -> None:
            pass

        register_delivery_callback(cb1)
        register_delivery_callback(cb2)
        assert _queue_module._delivery_callback is cb2


# ===========================================================================
# _get_or_create_queue
# ===========================================================================


class TestGetOrCreateQueue:
    def test_creates_queue_for_new_session(self) -> None:
        q = _get_or_create_queue("parent-001")
        assert isinstance(q, asyncio.Queue)

    def test_returns_same_queue_for_same_session(self) -> None:
        q1 = _get_or_create_queue("parent-002")
        q2 = _get_or_create_queue("parent-002")
        assert q1 is q2

    def test_different_sessions_get_different_queues(self) -> None:
        q1 = _get_or_create_queue("parent-003")
        q2 = _get_or_create_queue("parent-004")
        assert q1 is not q2


# ===========================================================================
# _drain
# ===========================================================================


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_empty_queue_is_noop(self) -> None:
        """Draining a non-existent queue is a safe no-op."""
        await _drain("nonexistent-parent")  # should not raise

    @pytest.mark.asyncio
    async def test_drain_calls_callback_for_each_result(self) -> None:
        delivered: list[tuple[str, SubagentResult]] = []

        async def cb(parent_id: str, result: SubagentResult) -> None:
            delivered.append((parent_id, result))

        register_delivery_callback(cb)
        queue = _get_or_create_queue("drain-parent-1")
        r1 = SubagentResult(db_session_id="s1", agent_type="a", model="m", status="completed", text="r1")
        r2 = SubagentResult(db_session_id="s2", agent_type="b", model="m", status="completed", text="r2")
        queue.put_nowait(r1)
        queue.put_nowait(r2)

        await _drain("drain-parent-1")

        assert len(delivered) == 2
        assert delivered[0][0] == "drain-parent-1"
        assert delivered[0][1].text == "r1"
        assert delivered[1][1].text == "r2"

    @pytest.mark.asyncio
    async def test_drain_cleans_up_empty_queue(self) -> None:
        delivered: list[Any] = []

        async def cb(parent_id: str, result: SubagentResult) -> None:
            delivered.append(result)

        register_delivery_callback(cb)
        queue = _get_or_create_queue("drain-parent-2")
        queue.put_nowait(SubagentResult(db_session_id="s", agent_type="a", model="m", status="completed", text="done"))
        await _drain("drain-parent-2")

        # Queue should be cleaned up after draining
        assert "drain-parent-2" not in _queues

    @pytest.mark.asyncio
    async def test_drain_without_callback_does_not_raise(self) -> None:
        """When no callback is registered, results are dropped without exception."""
        queue = _get_or_create_queue("drain-parent-3")
        queue.put_nowait(SubagentResult(db_session_id="s", agent_type="a", model="m", status="completed", text="done"))
        await _drain("drain-parent-3")  # should not raise

    @pytest.mark.asyncio
    async def test_drain_continues_after_callback_exception(self) -> None:
        """Callback exceptions should not prevent subsequent results from being processed."""
        delivered: list[str] = []
        call_count = 0

        async def failing_then_ok_cb(parent_id: str, result: SubagentResult) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated delivery failure")
            delivered.append(result.text)

        register_delivery_callback(failing_then_ok_cb)
        queue = _get_or_create_queue("drain-parent-4")
        r1 = SubagentResult(db_session_id="s1", agent_type="a", model="m", status="error", text="r1")
        r2 = SubagentResult(db_session_id="s2", agent_type="b", model="m", status="completed", text="r2")
        queue.put_nowait(r1)
        queue.put_nowait(r2)

        await _drain("drain-parent-4")

        # Second result should still be delivered
        assert delivered == ["r2"]


# ===========================================================================
# put_result (integration)
# ===========================================================================


class TestPutResult:
    @pytest.mark.asyncio
    async def test_put_result_queues_and_drains(self) -> None:
        delivered: list[SubagentResult] = []

        async def cb(parent_id: str, result: SubagentResult) -> None:
            delivered.append(result)

        register_delivery_callback(cb)

        result = SubagentResult(
            db_session_id="sess-put-test",
            agent_type="reviewer",
            model="anthropic/claude-sonnet-4-6",
            status="completed",
            text="All good.",
        )
        put_result("put-parent", result)
        # Let asyncio run the drain task
        await asyncio.sleep(0)

        assert len(delivered) == 1
        assert delivered[0].text == "All good."

    @pytest.mark.asyncio
    async def test_put_multiple_results(self) -> None:
        delivered: list[str] = []

        async def cb(parent_id: str, result: SubagentResult) -> None:
            delivered.append(result.agent_type)

        register_delivery_callback(cb)

        for agent_type in ("reviewer", "fixer", "deployer"):
            put_result(
                "multi-parent",
                SubagentResult(
                    db_session_id=f"sess-{agent_type}",
                    agent_type=agent_type,
                    model="m",
                    status="completed",
                    text="done",
                ),
            )

        await asyncio.sleep(0)
        assert set(delivered) == {"reviewer", "fixer", "deployer"}
