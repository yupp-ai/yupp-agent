"""Authentication for Slack Agent Gateway callbacks.

Agent Service authenticates to the Gateway using API key in X-API-Key header.
Uses the shared X_API_KEY from backend config.
"""

import hmac

from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


async def verify_api_key(request: Request) -> None:
    """Verify API key from X-API-Key header.

    Accepts both primary (X_API_KEY) and secondary (X_API_KEY_SECONDARY) keys
    to support key rotation without downtime.

    Args:
        request: The incoming FastAPI request

    Raises:
        HTTPException: If API key is missing or invalid
    """
    primary_key = settings.X_API_KEY
    secondary_key = getattr(settings, "X_API_KEY_SECONDARY", "")

    if not primary_key:
        logger.error("X_API_KEY not configured")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API key authentication not configured",
        )

    provided_key = request.headers.get("X-API-Key")
    if not provided_key:
        logger.warning("Missing X-API-Key header")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    # Use constant-time comparison to prevent timing attacks
    # Accept either primary or secondary key for key rotation support
    valid = hmac.compare_digest(provided_key, primary_key)
    if not valid and secondary_key:
        valid = hmac.compare_digest(provided_key, secondary_key)

    if not valid:
        logger.warning("Invalid API key provided")
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
