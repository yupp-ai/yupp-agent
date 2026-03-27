import asyncio
from typing import Any
from urllib.parse import urljoin

import sqlalchemy as sa
from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.middlewares import SmartRetryMiddleware
from taskiq.middlewares.taskiq_admin_middleware import TaskiqAdminMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_pipelines import PipelineMiddleware
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend, RedisStreamBroker

from ypl.backend.config import settings
from ypl.backend.db import close_cloud_sql_connector, get_async_engine, get_async_engine_read_replica
from ypl.backend.event.event import initialize_event_buffer
from ypl.backend.jobs.app_settings_schedule_source import AppSettingsScheduleSource
from ypl.backend.jobs.taskiq_logging_middleware import TaskiqLoggingMiddleware
from ypl.backend.llm.usage_metadata_tracker import initialize_usage_metrics_buffer
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.batch_utils import initialize_batch_system, stop_batch_system
from ypl.backend.utils.debugging_utils import log_resource_info
from ypl.backend.utils.dynamic_app_settings import refresh_dynamic_app_settings_in_redis
from ypl.db.log_utils import log_sql_query
from ypl.structured_logger import get_logger, setup_asyncio_logging

logger = get_logger()

_taskiq_admin_send_failure_count = 0


async def _patched_taskiq_admin_spawn_request(
    self: TaskiqAdminMiddleware,
    endpoint: str,
    payload: dict[str, Any],
) -> None:
    """
    Patched _spawn_request that logs _send() failures to avoid 'Task exception was never retrieved'.

    These requests are not critical and we can safely ignore any errors.
    """

    async def _send() -> None:
        try:
            client = self._get_client()
            async with asyncio.timeout(5):
                async with client.post(
                    urljoin(self.url, endpoint),
                    headers={"access-token": self.api_token},
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
        except Exception:
            global _taskiq_admin_send_failure_count
            _taskiq_admin_send_failure_count += 1
            if _taskiq_admin_send_failure_count % 100 == 1:
                logger.warning(
                    "TaskiqAdminMiddleware _send failed",
                    endpoint=endpoint,
                    failure_count=_taskiq_admin_send_failure_count,
                    exc_info=True,
                )

    task = asyncio.create_task(_send())
    self._pending.add(task)
    task.add_done_callback(self._pending.discard)


TaskiqAdminMiddleware._spawn_request = _patched_taskiq_admin_spawn_request  # type: ignore[method-assign]


result_backend: RedisAsyncResultBackend = RedisAsyncResultBackend(
    redis_url=settings.CELERY_RESULT_BACKEND,
    result_ex_time=60 * 60 * 24 * 1,  # 1 day
    prefix_str="taskiq-result",
)

DEFAULT_RETRY_COUNT = 5


def create_broker(queue_name: str | None = None) -> RedisStreamBroker:
    """
    Create a RedisStreamBroker with common configuration.

    Args:
        queue_name: Optional queue name for the broker. If None, uses default.

    Returns:
        Configured RedisStreamBroker instance.
    """
    # ~900k messages in the stream took up 1GB of memory.
    # Keeping it at 100k to avoid memory issues,
    # and enough number of backlog to avoid losing messages.
    if queue_name is not None:
        broker_instance = RedisStreamBroker(
            url=settings.CELERY_BROKER_URL,
            maxlen=100000,
            queue_name=queue_name,
        )
    else:
        broker_instance = RedisStreamBroker(
            url=settings.CELERY_BROKER_URL,
            maxlen=100000,
        )

    if settings.ENVIRONMENT not in ["local", "test"]:
        broker_instance = broker_instance.with_middlewares(
            TaskiqAdminMiddleware(
                url=settings.TASKIQ_ADMIN_URL,
                api_token=settings.TASKIQ_ADMIN_API_TOKEN,
                taskiq_broker_name=f"{(queue_name or 'taskiq')}-{settings.ENVIRONMENT}",
            )
        )

    return (
        broker_instance.with_result_backend(result_backend)
        .with_middlewares(PipelineMiddleware())
        .with_middlewares(TaskiqLoggingMiddleware(default_retry_count=DEFAULT_RETRY_COUNT))
        .with_middlewares(
            SmartRetryMiddleware(
                default_retry_label=True,
                default_retry_count=DEFAULT_RETRY_COUNT,
                default_delay=10,
                use_jitter=True,  # randomize delay
                use_delay_exponent=True,  # exponential backoff
                max_delay_exponent=60,  # max delay of 1 minute
            )
        )
    )


broker: RedisStreamBroker = create_broker()

# Separate broker + workers for live path tasks, so that they don't get blocked by the lower priority background tasks
# of the default broker.
# Note that this broker does not receive scheduled tasks intentionally.
broker_live_path: RedisStreamBroker = create_broker(queue_name="taskiq_live_path")

redis_schedule_source: ListRedisScheduleSource = ListRedisScheduleSource(
    url=settings.CELERY_BROKER_URL, prefix="taskiq-schedule"
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[
        LabelScheduleSource(broker),
        AppSettingsScheduleSource(),
        redis_schedule_source,
    ],
)

taskiq_app = broker
taskiq_app_live_path = broker_live_path


async def on_worker_startup(state: TaskiqState) -> None:
    setup_asyncio_logging()

    init_db()

    if settings.ENVIRONMENT == "local":
        logger.info("Taskiq worker starting - refreshing dynamic app settings in Redis (local env)...")
        await refresh_dynamic_app_settings_in_redis()

    await redis_schedule_source.startup()

    # Initialize all buffers using the unified batch system
    logger.info("Taskiq worker starting - initializing batch system...")

    await initialize_event_buffer()
    await initialize_usage_metrics_buffer()

    await initialize_batch_system()

    if settings.RESOURCE_LOGGING_INTERVAL_SECS > 0:
        create_background_task(log_resource_info(settings.RESOURCE_LOGGING_INTERVAL_SECS))


async def on_worker_shutdown(state: TaskiqState) -> None:
    logger.info("Taskiq worker shutting down - flushing batch system...")
    try:
        async with asyncio.timeout(5):
            await stop_batch_system()

    except TimeoutError:
        logger.warning("Timed out waiting for buffers flush during shutdown")
    except Exception:
        logger.warning("Error flushing buffers during shutdown", exc_info=True)

    await close_cloud_sql_connector()


# Register shared event handlers for both brokers
taskiq_app.on_event(TaskiqEvents.WORKER_STARTUP)(on_worker_startup)
taskiq_app.on_event(TaskiqEvents.WORKER_SHUTDOWN)(on_worker_shutdown)
taskiq_app_live_path.on_event(TaskiqEvents.WORKER_STARTUP)(on_worker_startup)
taskiq_app_live_path.on_event(TaskiqEvents.WORKER_SHUTDOWN)(on_worker_shutdown)


def init_db() -> None:
    logger.info("Taskiq worker starting - initializing database connections")
    try:
        get_async_engine()
        get_async_engine_read_replica()

        if settings.ENABLE_SQL_QUERY_LOGGING_ON_NON_PROD and settings.ENVIRONMENT != "production":
            sa.event.listen(sa.engine.Engine, "before_cursor_execute", log_sql_query)
        logger.info("Database engines initialized successfully")
    except Exception:
        logger.warning("Failed to initialize database engines", exc_info=True)
