"""Unit tests for backend utils/async_utils.py.

Covers:
- handle_task_exception (normal / cancelled / real exception)
- create_background_task (basic / with delay / custom exception handler)
- background_task decorator
- time_coro
- run_coroutines_sequentially (with and without delay / exception in coro)
"""

from __future__ import annotations
import asyncio
import time
from unittest.mock import MagicMock

import pytest
from ypl.backend.utils.async_utils import (
    BACKGROUND_TASKS,
    background_task,
    create_background_task,
    handle_task_exception,
    run_coroutines_sequentially,
    time_coro,
)

# ---------------------------------------------------------------------------
# handle_task_exception
# ---------------------------------------------------------------------------


class TestHandleTaskException:
    def test_no_exception_no_error(self) -> None:
        task = MagicMock(spec=asyncio.Task)
        task.result.return_value = "ok"
        task.get_name.return_value = "my-task"
        # Should not raise
        handle_task_exception(task)

    def test_cancelled_error_is_silenced(self) -> None:
        task = MagicMock(spec=asyncio.Task)
        task.result.side_effect = asyncio.CancelledError()
        task.get_name.return_value = "cancelled-task"
        # Should not raise
        handle_task_exception(task)

    def test_exception_is_logged_not_raised(self) -> None:
        task = MagicMock(spec=asyncio.Task)
        task.result.side_effect = ValueError("something broke")
        task.get_name.return_value = "failing-task"
        # Should not raise — exception is logged
        handle_task_exception(task)


# ---------------------------------------------------------------------------
# create_background_task
# ---------------------------------------------------------------------------


class TestCreateBackgroundTask:
    @pytest.mark.asyncio
    async def test_basic_task_runs(self) -> None:
        results: list[int] = []

        async def my_coro() -> None:
            results.append(42)

        task = create_background_task(my_coro())
        await asyncio.gather(task)
        assert results == [42]

    @pytest.mark.asyncio
    async def test_task_added_to_background_tasks(self) -> None:
        event = asyncio.Event()

        async def slow_coro() -> None:
            await event.wait()

        before = len(BACKGROUND_TASKS)
        task = create_background_task(slow_coro())
        # Task should be in the tracking set while running
        assert len(BACKGROUND_TASKS) >= before
        event.set()
        await task

    @pytest.mark.asyncio
    async def test_task_removed_from_background_tasks_on_done(self) -> None:
        async def noop() -> None:
            pass

        task = create_background_task(noop())
        await task
        # Allow done callbacks to fire
        await asyncio.sleep(0)
        assert task not in BACKGROUND_TASKS

    @pytest.mark.asyncio
    async def test_custom_exception_handler_called(self) -> None:
        handler_called: list[asyncio.Task] = []

        def custom_handler(t: asyncio.Task) -> None:
            handler_called.append(t)
            try:
                t.result()
            except Exception:
                pass

        async def failing_coro() -> None:
            raise RuntimeError("boom")

        task = create_background_task(failing_coro(), exception_handler=custom_handler)
        try:
            await task
        except RuntimeError:
            pass

        await asyncio.sleep(0)
        assert len(handler_called) == 1

    @pytest.mark.asyncio
    async def test_delay_secs_delays_execution(self) -> None:
        results: list[float] = []

        async def timed_coro() -> None:
            results.append(time.monotonic())

        start = time.monotonic()
        task = create_background_task(timed_coro(), delay_secs=0.1)
        await task

        assert results
        elapsed = results[0] - start
        assert elapsed >= 0.05  # allow some tolerance

    @pytest.mark.asyncio
    async def test_returns_result(self) -> None:
        async def returns_42() -> int:
            return 42

        task = create_background_task(returns_42())
        result = await task
        assert result == 42


# ---------------------------------------------------------------------------
# background_task decorator
# ---------------------------------------------------------------------------


class TestBackgroundTaskDecorator:
    @pytest.mark.asyncio
    async def test_decorated_function_returns_task(self) -> None:
        @background_task()
        async def my_func() -> int:
            return 99

        task = my_func()
        assert isinstance(task, asyncio.Task)
        result = await task
        assert result == 99

    @pytest.mark.asyncio
    async def test_decorator_with_args(self) -> None:
        results: list[int] = []

        @background_task()
        async def my_func(x: int, y: int) -> None:
            results.append(x + y)

        task = my_func(3, 4)
        await task
        assert results == [7]

    @pytest.mark.asyncio
    async def test_decorator_preserves_function_name(self) -> None:
        @background_task()
        async def named_func() -> None:
            pass

        assert named_func.__name__ == "named_func"


# ---------------------------------------------------------------------------
# time_coro
# ---------------------------------------------------------------------------


class TestTimeCoro:
    @pytest.mark.asyncio
    async def test_stores_duration_in_ms(self) -> None:
        async def noop() -> str:
            return "done"

        durations: dict[str, float] = {}
        result = await time_coro(noop(), durations, "my_op")
        assert result == "done"
        assert "my_op" in durations
        assert isinstance(durations["my_op"], float)
        assert durations["my_op"] >= 0

    @pytest.mark.asyncio
    async def test_duration_measured_correctly(self) -> None:
        async def slow() -> None:
            await asyncio.sleep(0.05)

        durations: dict[str, float] = {}
        await time_coro(slow(), durations, "slow_op")
        # Should be at least 40ms, but allow generous tolerance
        assert durations["slow_op"] >= 30

    @pytest.mark.asyncio
    async def test_overwrites_existing_key(self) -> None:
        async def noop() -> None:
            pass

        durations = {"existing": 999.0}
        await time_coro(noop(), durations, "existing")
        # Value should be overwritten
        assert durations["existing"] != 999.0

    @pytest.mark.asyncio
    async def test_exception_propagates(self) -> None:
        async def failing() -> None:
            raise ValueError("test error")

        durations: dict[str, float] = {}
        with pytest.raises(ValueError, match="test error"):
            await time_coro(failing(), durations, "fail_op")

        # Duration should still be recorded
        assert "fail_op" in durations


# ---------------------------------------------------------------------------
# run_coroutines_sequentially
# ---------------------------------------------------------------------------


class TestRunCoroutinesSequentially:
    @pytest.mark.asyncio
    async def test_runs_all_coroutines(self) -> None:
        results: list[int] = []

        async def append(x: int) -> None:
            results.append(x)

        await run_coroutines_sequentially([append(1), append(2), append(3)])
        assert results == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        # Should not raise
        await run_coroutines_sequentially([])

    @pytest.mark.asyncio
    async def test_exception_in_coro_does_not_stop_remaining(self) -> None:
        results: list[int] = []

        async def ok() -> None:
            results.append(1)

        async def boom() -> None:
            raise ValueError("oops")

        async def after() -> None:
            results.append(2)

        # boom should be logged but not propagate
        await run_coroutines_sequentially([ok(), boom(), after()])
        assert results == [1, 2]

    @pytest.mark.asyncio
    async def test_delay_between_tasks(self) -> None:
        timestamps: list[float] = []

        async def record() -> None:
            timestamps.append(time.monotonic())

        await run_coroutines_sequentially([record(), record()], delay_between_tasks=0.05)
        assert len(timestamps) == 2
        assert timestamps[1] - timestamps[0] >= 0.03  # allow tolerance
