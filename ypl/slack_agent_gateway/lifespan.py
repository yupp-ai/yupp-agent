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


@dataclass
class SAGState:
    """Runtime state held across the SAG lifespan."""

    flush_task: asyncio.Task[None]


async def sag_startup() -> SAGState:
    """Initialise the Slack Agent Gateway subsystem.

    - Configures asyncio-compatible structured logging.
    - Launches the background flush-manager task.

    Returns the :class:`SAGState` that must be passed to :func:`sag_shutdown`
    when the server is stopping.
    """
    setup_asyncio_logging()
    logger.info("Starting Slack Agent Gateway")
    flush_task: asyncio.Task[None] = asyncio.create_task(run_flush_manager())
    return SAGState(flush_task=flush_task)


async def sag_shutdown(state: SAGState) -> None:
    """Tear down the Slack Agent Gateway subsystem.

    Cancels and awaits the flush-manager task that was started by
    :func:`sag_startup`.
    """
    logger.info("Shutting down Slack Agent Gateway")
    state.flush_task.cancel()
    try:
        await state.flush_task
    except asyncio.CancelledError:
        pass
