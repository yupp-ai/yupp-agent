from typing import Any

import psutil
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from ypl.backend.config import settings
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.debugging_utils import AsyncTaskList, get_async_task_statuses, get_resource_leak_info
from ypl.db.redis import get_redis_client
from ypl.loggers.config import CONTAINER_INSTANCE_ID
from ypl.structured_logger import get_logger

router = APIRouter()
public_router = APIRouter()

logger = get_logger()


@router.get("/health")
async def health() -> dict[str, str]:
    logger.info("Processing started for health")
    try:
        async with get_async_session() as session:
            await session.exec(text("SELECT 1"))  # type: ignore
            return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


HIGH_FD_THRESHOLD = 10000
RESOURCE_INFO_LOG_INTERVAL_SECONDS = 600  # Rate limit resource info logging to once per 10 minutes


async def _log_resource_info() -> None:
    """Log resource info for debugging high file descriptor counts."""
    try:
        resource_info = get_resource_leak_info()
        logger.info("Resource info logged due to high num_fds", resource_info=resource_info)
    except Exception as e:
        logger.warning(f"Error logging resource info: {e}")


@public_router.get("/healthz")
async def healthz() -> dict[str, Any]:
    logger.info("Processing started for healthz")

    try:
        process = psutil.Process()
        num_fds = process.num_fds()
        result = {
            "num_fds": num_fds,
            "memory_percent": process.memory_percent(),
        }
        logger.info("Processing finished for healthz", **result)

        # If num_fds exceeds threshold, log resource info in background (rate limited)
        if num_fds > HIGH_FD_THRESHOLD:
            redis_client = await get_redis_client()
            rate_limit_key = f"healthz_resource_info_log::{CONTAINER_INSTANCE_ID}"
            # Use atomic set-if-not-exists to prevent race conditions
            if await redis_client.set(rate_limit_key, "1", ex=RESOURCE_INFO_LOG_INTERVAL_SECONDS, nx=True):
                create_background_task(_log_resource_info())
    except Exception:
        # psutil may fail when there's a process replacement, so we just log the error and return 200.
        logger.warning("Error getting process info for healthz")
    return {"status": "ok"}


@public_router.get("/readyz")
async def readyz() -> dict[str, str]:
    logger.info("Processing started for readyz")

    if settings.BACKEND_OPERATING_MODE == "leaderboard":
        # Import lazily to avoid pulling leaderboard code into non-leaderboard services.
        from ypl.leaderboard.ranking import refresh_data_in_progress

        if refresh_data_in_progress:
            logger.info("readyz: returning 503 because refresh_data_in_progress")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="refresh_data_in_progress",
            )

    return {"status": "ok"}


@router.get("/debug/tasks")
async def get_task_statuses() -> AsyncTaskList:
    """Return status information on currently running asyncio tasks."""
    logger.info("Processing started for get_task_statuses")

    task_list = get_async_task_statuses()

    # Log it so that there is a record of the tasks in the logs.
    logger.info(
        {
            "message": f"Returning {task_list.num_active_tasks} active async tasks in /debug/tasks",
            "task_list": task_list,
        }
    )
    return task_list


@router.get("/debug/resources")
async def get_resource_info(
    top_n: int = Query(default=50, ge=1, le=500, description="Number of top resources to return"),
) -> dict[str, Any]:
    logger.info("Processing started for get_resource_info", top_n=top_n)

    resource_info = get_resource_leak_info(top_n=top_n)

    logger.info(
        "Processing finished for get_resource_info",
        resource_info=resource_info,
    )
    return resource_info
