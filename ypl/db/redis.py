import logging

import redis.asyncio as redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry
from upstash_redis.asyncio import Redis as UpstashRedis

from ypl.backend.config import settings

UPSTASH_REDIS_CLIENT: UpstashRedis | None = None

REDIS_CONNECTION_POOL: redis.ConnectionPool | None = None

REDIS_RETRY_POLICY = Retry(ExponentialBackoff(), 5)


async def get_redis_client() -> redis.Redis:
    """
    Returns a regular Redis client.
    Use this by default, especially for process-local state (e.g. rate limiting).
    """
    global REDIS_CONNECTION_POOL
    client = None
    if REDIS_CONNECTION_POOL is None:
        REDIS_CONNECTION_POOL = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            retry_on_timeout=True,
            health_check_interval=5,
            socket_keepalive=True,
            socket_connect_timeout=10,
        )
        client = redis.Redis().from_pool(REDIS_CONNECTION_POOL)
        try:
            ping_result = await client.ping()
            logging.info(f"Redis client ping result: {ping_result}")
        except Exception:
            logging.error("Error initializing Redis connection pool", exc_info=True)
            raise
    if client is None:
        client = redis.Redis().from_pool(REDIS_CONNECTION_POOL)
    return client


async def get_upstash_redis_client_for_stop_streaming_check() -> UpstashRedis:
    """
    Returns an Upstash Redis client.
    Use this ONLY if you need to share state with an external frontend service (e.g. stop-streaming signals).
    """
    global UPSTASH_REDIS_CLIENT
    if UPSTASH_REDIS_CLIENT is None:
        redis_url = settings.UPSTASH_REDIS_URL
        redis_token = settings.UPSTASH_REDIS_TOKEN
        if not (redis_url and redis_token):
            raise ValueError("UPSTASH_REDIS environment variables is not set")
        UPSTASH_REDIS_CLIENT = UpstashRedis(url=redis_url, token=redis_token)
        try:
            ping_result = await UPSTASH_REDIS_CLIENT.ping()
            logging.info(f"Upstash Redis client ping result: {ping_result}")
        except Exception as e:
            logging.error("Error initializing Upstash Redis client", exc_info=e)
            raise
    return UPSTASH_REDIS_CLIENT
