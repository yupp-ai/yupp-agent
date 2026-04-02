"""Startup and shutdown lifecycle helpers for the Slack Agent Gateway.

Extracted so that the monolith server can import and call these functions
directly, rather than embedding the logic inside a FastAPI lifespan context
manager.
"""

import asyncio
from dataclasses import dataclass

from ypl.slack_agent_gateway.buffer import run_flush_manager
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()

# Module-level guard to detect double-startup and track active state.
_sag_state: "SAGState | None" = None


@dataclass
class SAGState:
    """Runtime state held across the SAG lifespan."""

    flush_task: asyncio.Task[None]


def _on_flush_task_done(task: "asyncio.Task[None]") -> None:
    """Clear the module-level state guard if the flush task exits unexpectedly.

    Handles the edge case where ``run_flush_manager()`` crashes on its own
    (before ``sag_shutdown()`` is called).  Without this, ``_sag_state`` would
    remain non-None, causing any subsequent ``sag_startup()`` call — including
    in tests — to hit the double-startup RuntimeError even though nothing is
    actually running.
    """
    global _sag_state
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.critical("Flush manager exited unexpectedly — clearing state guard", exc_info=exc)
            _sag_state = None


async def sag_startup() -> SAGState:
    """Initialise the Slack Agent Gateway subsystem.

    - Configures asyncio-compatible structured logging.
    - Launches the background flush-manager task.

    Returns the :class:`SAGState` that must be passed to :func:`sag_shutdown`
    when the server is stopping.

    Raises:
        RuntimeError: If called while a previous startup is still active (i.e.
            ``sag_shutdown`` has not been called yet).  Must only be called
            once per process lifetime.
    """
    global _sag_state
    if _sag_state is not None:
        raise RuntimeError("sag_startup() called while already running — sag_shutdown() must be called first")
    setup_asyncio_logging()
    logger.info("Starting Slack Agent Gateway")
    flush_task: asyncio.Task[None] = asyncio.create_task(run_flush_manager())
    _sag_state = SAGState(flush_task=flush_task)
    flush_task.add_done_callback(_on_flush_task_done)
    return _sag_state


async def sag_shutdown(state: SAGState) -> None:
    """Tear down the Slack Agent Gateway subsystem.

    Cancels and awaits the flush-manager task that was started by
    :func:`sag_startup`.
    """
    global _sag_state
    logger.info("Shutting down Slack Agent Gateway")
    state.flush_task.cancel()
    try:
        await state.flush_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Flush manager exited with unexpected exception during shutdown")
    finally:
        _sag_state = None
