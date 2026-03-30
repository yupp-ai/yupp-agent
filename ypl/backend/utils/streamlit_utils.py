import asyncio
import atexit
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

import streamlit as st

from ypl.structured_logger import get_logger

T = TypeVar("T")

logger = get_logger()

# Module-level singleton for LoopWorker with thread-safe initialization
_loop_worker: "LoopWorker | None" = None
_loop_worker_lock = threading.Lock()


async def _initialize_streamlit_services() -> None:
    """Initialize services required for the streamlit server."""
    logger.info("STREAMLIT INIT: Services initialized successfully")


async def _teardown_streamlit_services() -> None:
    """Teardown services when the streamlit server shuts down."""
    logger.info("STREAMLIT TEARDOWN: Services stopped successfully")


class LoopWorker:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="LoopWorker", daemon=True)
        self._thread.start()
        self._started.wait()  # loop ready

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        # Initialize services before signaling readiness
        self._loop.run_until_complete(_initialize_streamlit_services())
        self._started.set()
        self._loop.run_forever()

    def is_healthy(self) -> bool:
        """Check if the worker is still running and can accept work."""
        return not self._stopped and self._thread.is_alive() and self._loop.is_running()

    def submit(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Thread-safe: schedule coro on the background loop and wait for result."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        self._stopped = True
        # Stop the event loop first
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        # Run teardown after the loop has stopped but before closing
        self._loop.run_until_complete(_teardown_streamlit_services())
        self._loop.close()
        # Reset global only if we're still the active singleton
        global _loop_worker
        with _loop_worker_lock:
            if _loop_worker is self:
                _loop_worker = None


@st.cache_resource
def get_loop_worker() -> LoopWorker:
    """Get the shared LoopWorker instance.

    Uses module-level singleton with thread-safe initialization to ensure
    only one LoopWorker is created even when called from multiple Streamlit
    threads. The @st.cache_resource decorator provides additional Streamlit-
    level caching but the singleton pattern is the primary safeguard.

    If the existing worker is unhealthy (stopped or loop died), a new worker
    is created to allow recovery without process restart.
    """
    global _loop_worker
    if _loop_worker is not None and _loop_worker.is_healthy():
        return _loop_worker

    with _loop_worker_lock:
        # Double-check after acquiring lock (another thread may have initialized it)
        if _loop_worker is not None and _loop_worker.is_healthy():
            return _loop_worker

        w = LoopWorker()
        atexit.register(w.stop)
        _loop_worker = w
        return w


def run_coroutine_in_lit_worker[T](coroutine: Coroutine[Any, Any, T], timeout: float | None = 30) -> T:
    # If already in an async context, don't block; let the caller await instead.
    try:
        asyncio.get_running_loop()
        raise RuntimeError(
            "run_coroutine_in_lit_worker called from an async context. "
            "Await the coroutine directly (or provide an async variant)."
        )
    except RuntimeError:
        pass

    worker = get_loop_worker()
    return worker.submit(coroutine, timeout=timeout)
