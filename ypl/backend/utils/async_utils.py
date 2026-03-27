import asyncio
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from ypl.structured_logger import clear_logger_context, get_logger

logger = get_logger()

# Asyncio somehow doesn't consider their internal references from the event loop to be strong references.
# This means that if we don't track the tasks manually, they will be garbage collected prematurely.
BACKGROUND_TASKS: set[asyncio.Task] = set()


def handle_task_exception(task: asyncio.Task) -> None:
    """Default exception handler for background tasks."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass  # Task was cancelled, this is normal
    except Exception:
        logger.error(f"Background task {task.get_name()} failed", exc_info=True)


def create_background_task[T](
    coroutine: Coroutine[Any, Any, T],
    *,
    exception_handler: Callable[[asyncio.Task], None] = handle_task_exception,
    delay_secs: float = 0.0,
) -> asyncio.Task[T]:
    """
    Creates and schedules a background task with proper exception handling.

    Args:
        coroutine: The coroutine to run as a background task
        exception_handler: Optional custom exception handler function
        delay_secs: Optional delay in seconds before starting the coroutine

    Returns:
        The created task
    """

    async def delayed_coroutine() -> T:
        # Revert this if you see side effects of missing contextvars in the main task too.
        clear_logger_context()
        if delay_secs > 0:
            logger.info(f"Delaying '{coroutine.__name__}' by {delay_secs} seconds")
            await asyncio.sleep(delay_secs)
            logger.info(f"Executing '{coroutine.__name__}' after delay of {delay_secs} seconds")
        try:
            return await coroutine
        finally:
            clear_logger_context()

    task: asyncio.Task[T] = asyncio.create_task(delayed_coroutine(), name=coroutine.__name__)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(exception_handler)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


def background_task[T](
    exception_handler: Callable[[asyncio.Task], None] = handle_task_exception,
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., asyncio.Task[T]]]:
    """
    Decorator to automatically convert a coroutine into a background task.

    Args:
        exception_handler: Optional custom exception handler function

    Returns:
        Decorator function that wraps the coroutine
    """

    def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., asyncio.Task[T]]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> asyncio.Task[T]:
            return create_background_task(func(*args, **kwargs), exception_handler=exception_handler)

        return wrapper

    return decorator


async def time_coro[T](coro: Coroutine[Any, Any, T], durations: dict[str, float], key: str) -> T:
    """
    Time a coroutine execution and store its duration in milliseconds.

    Args:
        coro: The coroutine to execute and time
        durations: Dictionary to store the duration in
        key: Key to use for storing the duration

    Returns:
        The result of the coroutine

    Example:
        durations = {}
        result = await time_coro(some_async_func(), durations, "operation_name")
        print(f"Operation took {durations['operation_name']}ms")
    """
    if key in durations:
        logger.warning(f"Key '{key}' already exists in durations dictionary and will be overwritten")
    start = time.monotonic()
    try:
        return await coro
    finally:
        durations[key] = (time.monotonic() - start) * 1000


async def run_coroutines_sequentially(coroutines: list[Coroutine], delay_between_tasks: float = 0.0) -> None:
    """
    Execute coroutines sequentially with optional delay between tasks.

    Args:
        coroutines: List of coroutines to execute sequentially
        delay_between_tasks: Optional delay in seconds between task executions
    """

    logger.info(
        f"Running {len(coroutines)} coroutines sequentially with a delay of {delay_between_tasks} seconds between them"
    )
    for i, coro in enumerate(coroutines):
        log_str = f"'{coro.__name__}' #{i + 1} out of {len(coroutines)} serial tasks"
        logger.info(f"Executing {log_str}")
        start_time = time.monotonic()
        try:
            await coro
            logger.info(f"Completed {log_str} in {round((time.monotonic() - start_time) * 1000)}ms")
        except Exception as e:
            logger.error("Error while executing serial task", log_str=log_str, exc_info=e)

        if delay_between_tasks > 0.0:
            await asyncio.sleep(delay_between_tasks)
