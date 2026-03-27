"""Standalone FastAPI server for Slack Agent Gateway.

This server handles:
- Slack webhooks from multiple agent apps
- Callback APIs for Agent Service
- Background buffer flush manager
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from ypl.backend.routes.v1.health import public_router as health_router
from ypl.slack_agent_gateway.buffer import run_flush_manager
from ypl.slack_agent_gateway.routes import router
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle."""
    setup_asyncio_logging()
    logger.info("Starting Slack Agent Gateway server")

    # Start the background flush manager
    flush_task = asyncio.create_task(run_flush_manager())

    yield

    # Cleanup
    logger.info("Shutting down Slack Agent Gateway server")
    flush_task.cancel()
    try:
        await flush_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Slack Agent Gateway",
    description="Gateway service for multiple AI agents on Slack",
    version="1.0.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

# Include the main router at both prefixes for backward compatibility
# /api/v1 is the new standard prefix
# /slack-agent-gateway is the legacy prefix (to be deprecated)
app.include_router(router, prefix="/api/v1")
app.include_router(router, prefix="/slack-agent-gateway")

# Include health check endpoints (/api/v1/healthz, /api/v1/readyz)
app.include_router(health_router, prefix="/api/v1", tags=["health"])
