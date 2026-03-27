"""API key authentication for Agent Harness Service.

Simple shared secret between gateway and AHS via X-API-Key header.
"""

import hmac

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN, HTTP_503_SERVICE_UNAVAILABLE

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


def _get_api_key() -> str:
    """Read the API key from settings."""
    return settings.AGENT_HARNESS_SERVICE_API_KEY


async def verify_api_key(request: Request) -> None:
    """Verify API key from X-API-Key header.

    Raises:
        HTTPException: If API key is missing or invalid.
    """
    expected_key = _get_api_key()
    if not expected_key:
        logger.critical("AGENT_HARNESS_SERVICE_API_KEY not configured — all requests will be rejected")
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service misconfigured",
        )

    provided_key = request.headers.get("X-API-Key")
    if not provided_key:
        logger.warning("Missing X-API-Key header")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    if not hmac.compare_digest(provided_key, expected_key):
        logger.warning("Invalid API key provided")
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
