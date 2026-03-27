import inspect
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import wraps
from typing import ParamSpec, TypeVar

from ypl.backend.utils.monitoring import metric_inc, metric_inc_by, metric_record
from ypl.structured_logger import get_logger

logger = get_logger()

T = TypeVar("T")
P = ParamSpec("P")


LONG_SESSION_LOGGING_THRESHOLD_MS = 5000  # TODO(raghu): make this configurable.


def async_instrumenting_context_manager(
    metric_prefix: str,
) -> Callable[
    [Callable[P, AbstractAsyncContextManager[T]]],
    Callable[P, AbstractAsyncContextManager[T]],
]:
    """
    Decorator that adds metrics for an async context manager. See usage below.

    This decorator wraps an async context manager function to automatically collect
    metrics about its usage:
    - Entry/exit counts
    - Active context count (concurrent usage)
    - Duration of context usage in milliseconds

    Usage:
        # Add one line like this at the definition of the context manager function (e.g. get_async_session):
        @async_instrumenting_context_manager(metric_prefix="db/session/primary"")
        @asynccontextmanager
        async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
            # context manager implementation
            pass

        # No change is needed at the call site. It is transparent to the caller.

    Args:
        metric_prefix: Base name for the metrics (e.g. "db/session/primary"). The decorator will create
                        4 metrics with suffixes:
                        - {metric_prefix}/context/enter - incremented when entering context
                        - {metric_prefix}/context/exit - incremented when exiting context
                        - {metric_prefix}/context/time_spent_ms - cumulative duration in milliseconds
                        - {metric_prefix}/context/active - current number of active contexts
    """

    def decorator(cm_func: Callable[P, AbstractAsyncContextManager[T]]) -> Callable[P, AbstractAsyncContextManager[T]]:
        current_active = 0
        metric_enter = f"{metric_prefix}/context/enter"
        metric_exit = f"{metric_prefix}/context/exit"
        metric_duration_ms = f"{metric_prefix}/context/time_spent_ms"
        metric_active = f"{metric_prefix}/context/active"

        @asynccontextmanager
        @wraps(cm_func)
        async def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal current_active
            nonlocal metric_enter
            nonlocal metric_exit
            nonlocal metric_duration_ms
            nonlocal metric_active

            current_active += 1
            metric_inc(metric_enter)
            metric_record(metric_active, current_active)
            start = time.monotonic()

            try:
                async with cm_func(*args, **kwargs) as resource:
                    yield resource
            finally:
                current_active -= 1
                duration_ms = int((time.monotonic() - start) * 1000)
                metric_inc(metric_exit)
                metric_inc_by(metric_duration_ms, duration_ms)
                metric_record(metric_active, current_active)

                if duration_ms > LONG_SESSION_LOGGING_THRESHOLD_MS:
                    # Log a warning with call site and stack trace.
                    frames = inspect.stack()[:10]  # Limit to 10, to avoid a lot of framework traces.
                    stack_trace = [f"{frame.function}() at {frame.filename}:{frame.lineno}" for frame in frames]

                    if len(frames) > 2:
                        caller = frames[2].function
                        call_site = stack_trace[2]
                    else:
                        caller = "unknown"  # not expected.
                        call_site = "unknown"

                    logger.warning(
                        {
                            "message": f"Long async context: {caller}() spent {duration_ms}ms in {cm_func.__name__}()",
                            "caller": caller,
                            "call_site": call_site,
                            "duration_ms": duration_ms,
                            "stack_trace": stack_trace,
                        }
                    )

        return wrapper

    return decorator
