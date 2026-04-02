"""MCP server startup and shutdown lifecycle helpers.

These functions are extracted so they can be imported and called from both the
standalone MCP server (ypl/mcp_server/server.py) and any monolith that mounts
the MCP app.  The FastMCP session-manager lifespan
(``async with mcp_http_app.lifespan(...)``) is intentionally *not* included
here — that is the responsibility of whoever actually mounts the
``mcp_http_app`` ASGI application.
"""

import asyncio

from ypl.backend.utils.batch_utils import initialize_batch_system, stop_batch_system
from ypl.logger import flush_and_close_google_cloud_logging
from ypl.loggers.config import flush_and_close_google_logging_client
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()


async def mcp_startup() -> None:
    """Initialise shared services required by the MCP server.

    Call this once at application startup, *before* yielding control to the
    request-handling loop.  It is safe to call from any asyncio context.

    Steps:
    1. Configure asyncio-aware structured logging.
    2. Start the batch-processing system (background workers, flush loops, …).
    """
    setup_asyncio_logging()

    logger.info("APP INIT: Initializing batch system...")
    await initialize_batch_system()


async def mcp_shutdown() -> None:
    """Tear down shared services gracefully at application shutdown.

    Call this once after the request-handling loop has finished (i.e. after the
    ``yield`` in the lifespan context manager).

    Steps:
    1. Flush batch-system buffers — hard timeout of 5 s to avoid blocking
       the process shutdown indefinitely.
    2. Close the Sentry aiohttp session.
    3. Flush and close the Google Cloud Logging client.
    """
    logger.info("MCP Server shutting down...")

    # Flush batch-system buffers within 5 seconds.
    try:
        async with asyncio.timeout(5):
            await stop_batch_system()
    except TimeoutError:
        logger.warning("Timed out waiting for buffers flush during shutdown")
    except Exception:
        logger.warning("Error flushing buffers during shutdown", exc_info=True)

    # Close Sentry aiohttp session.
    # Guard with try/except so a Sentry error never prevents the GCP log flush
    # from running (pre-existing latent bug, fixed here on extraction).
    try:
        from ypl.mcp_server.tools.sentry import close_sentry_session

        await close_sentry_session()
    except Exception:
        logger.warning("Error closing Sentry session during shutdown", exc_info=True)

    # Flush and close Google Cloud Logging.
    flush_and_close_google_cloud_logging()
    flush_and_close_google_logging_client()

    logger.info("MCP Server shut down complete.")
