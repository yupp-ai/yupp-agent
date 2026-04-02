"""Standalone FastAPI server for Slack Agent Gateway.

This server handles:
- Slack webhooks from multiple agent apps
- Callback APIs for Agent Service
- Background buffer flush manager
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse, PlainTextResponse

from ypl.backend.routes.v1.health import public_router as health_router
from ypl.slack_agent_gateway.lifespan import sag_shutdown, sag_startup
from ypl.slack_agent_gateway.routes import router
from ypl.structured_logger import get_logger

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle."""
    state = await sag_startup()
    yield
    await sag_shutdown(state)


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


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Root health check handler for Cloud Run GFE probes.

    The Cloud Run Google Front End (GFE) periodically probes GET / and logs
    any non-2xx response as a WARNING-severity request log entry with a null
    message payload. Since the SAG has no content to serve at the root, we
    return a minimal 200 OK to silence those spurious WARNING log entries.
    Use /api/v1/healthz for real liveness checks.
    """
    return {"status": "ok"}


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> PlainTextResponse:
    """Robots.txt handler for Cloud Run GFE probes.

    The Cloud Run GFE occasionally probes GET /robots.txt, also logged as a
    WARNING when a 404 is returned. Return a deny-all robots.txt to keep
    crawlers out and eliminate the GFE probe WARNING entries.
    """
    return PlainTextResponse("User-agent: *\nDisallow: /\n")
