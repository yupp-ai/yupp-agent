import asyncio
import contextlib
import functools
import hashlib
import inspect
import json
import time
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast, get_args

import redis
from cachetools import Cache, LRUCache
from pydantic import BaseModel, ValidationError
from redis.exceptions import LockNotOwnedError
from ypl.backend.utils.param_decorators import normalize_args
from ypl.backend.utils.type_utils import has_classmethod, has_instance_method, is_primitive_type
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()

T = TypeVar("T", bound=Callable[..., Any])


REDIS_LOCK_DEFAULT_TTL_SECONDS = 10

# If the difference between the redis TTL and the in-memory cache TTL is less than this threshold,
# we will not fetch from redis again.
TTL_DIFFERENCE_THRESHOLD_SECONDS = 300

# Rate limit action constants
RATE_LIMIT_ACTION_REMOVE = "remove_rate_limit"
RATE_LIMIT_ACTION_SET = "set_rate_limited"


def redis_async_cache(
    *,
    ttl_seconds: int | None = None,
    redis_lock_ttl_seconds: int = REDIS_LOCK_DEFAULT_TTL_SECONDS,
    blocking_timeout: float | None = None,
    ignore_self_arg: bool = False,
    in_memory_cache: Cache | None = None,
    key_prefix: str | None = None,
) -> Callable[[T], T]:
    """
    Cache a function that returns a Pydantic model
    Note: Please do not use redis_async_cache for functions that are cheap but called too frequently.
          It will send requests to redis on every call, which could be expensive.
          For frequent but inexpensive calls, a layered cache which combines redis and in-memory cache is more
          preferrable.


    Args:
        ttl_seconds: TTL in seconds for the cache
        redis_lock_ttl_seconds: TTL in seconds for the Redis lock
        blocking_timeout: Timeout in seconds for the Redis lock
        ignore_self_arg: If True, the self argument will be ignored when generating the cache key.
            This is useful for instance methods, but the result is instance irrelevant.
        in_memory_cache: In-memory cache to reduce the number of redis calls. When inconsistency happens,
            respect the redis cache.
        key_prefix: Optional prefix to add to the cache key. Useful for separating cache namespaces
            (e.g., for stable vs latest leaderboard backends sharing the same Redis instance).
    """
    in_mem_cache: Cache[Any, Any] = in_memory_cache if in_memory_cache is not None else LRUCache(maxsize=512)

    def _has_to_json_and_from_json(type_: type) -> bool:
        return has_instance_method(type_, "to_json") and has_classmethod(type_, "from_json")

    def _type_supports_json_serialization(type_: type | None) -> bool:
        # return True if type is a Pydantic model or a primitive type or type has a to_json instance method
        # and a from_json class method
        if type_ is None:
            return True
        if is_primitive_type(type_):
            return True
        if issubclass(type_, BaseModel):
            return True
        if isinstance(type_, types.UnionType):
            return all(_type_supports_json_serialization(tp) for tp in get_args(type_))  # type: ignore[unreachable]
        return bool(_has_to_json_and_from_json(type_))

    def _serialize_result(result: Any) -> str:
        if result is None:
            return "null"
        if isinstance(result, BaseModel):
            return result.model_dump_json()
        if is_primitive_type(type(result)):
            return json.dumps(result)
        if _type_supports_json_serialization(type(result)):
            return result.to_json()  # type: ignore
        raise ValueError(f"Type {type(result)} not supported for serialization")

    def _deserialize_result(json_str: str, type_: type | None) -> Any:
        if type_ is None:
            return None
        if json_str == "null":
            return None
        if issubclass(type_, BaseModel):
            return type_.model_validate_json(json_str)
        if is_primitive_type(type_):
            return json.loads(json_str)
        if isinstance(type_, types.UnionType):
            for tp in get_args(type_):  # type: ignore[unreachable]
                try:
                    return _deserialize_result(json_str, tp)
                except Exception:
                    continue
        elif _has_to_json_and_from_json(type_):
            return type_.from_json(json_str)  # type: ignore
        raise ValueError(f"Deserialization failed for type:{type_} and value: {json_str}.")

    def _get_expires_at(redis_ttl: int, ttl_seconds: int | None) -> int:
        """
        Args:
            redis_ttl: TTL in seconds for the Redis cache left for the cached entry.
            ttl_seconds: TTL in seconds setting for the decorator, also for Redis.

        Returns the time when the cache will expire.
        If redis_ttl is -1 or ttl_seconds is None, it means no TTL, return 0.
        If redis_ttl is -2 or 0, it means the cache is expired, return current time + ttl_seconds.
        If redis_ttl is greater than 0, it means the cache is valid, return current time + redis_ttl.
        """
        if redis_ttl == -1:
            return 0
        if ttl_seconds is None:
            return 0
        if redis_ttl > 0:
            return int(time.monotonic() + redis_ttl)
        return int(time.monotonic() + ttl_seconds)

    def decorator(func: T) -> T:
        hits = 0
        misses = 0
        force_update_cache = False

        unwrapped_func = inspect.unwrap(func)
        return_type = inspect.signature(cast(Callable[..., Any], unwrapped_func)).return_annotation
        if not _type_supports_json_serialization(return_type):
            raise ValueError(
                f"Type {return_type} in return type do not support json serialization. "
                "Please add a to_json instance method and a from_json class method."
            )

        def _get_redis_key_func_prefix() -> str:
            prefix = f"{key_prefix}:" if key_prefix else ""
            return f"func_cache:{prefix}{unwrapped_func.__name__}"

        def redis_key(args: tuple, kwargs: dict) -> str:
            args, kwargs = normalize_args(unwrapped_func, args, kwargs)

            def serialize(v: Any) -> str:
                if isinstance(v, BaseModel):
                    return json.dumps(v.model_dump(mode="json"), sort_keys=True)
                return str(v)

            args_str = [serialize(arg) for arg in args]
            kwargs_str = [f"{k}={serialize(v)}" for k, v in kwargs.items() if k != "self" or not ignore_self_arg]
            return f"{_get_redis_key_func_prefix()}-{args_str + kwargs_str}"

        async def invalidate_cache_for_params(*args: Any, **kwargs: Any) -> None:
            key = redis_key(args, kwargs)
            redis_client = await get_redis_client()
            await redis_client.delete(key)
            # Also clear from in-memory cache
            if key in in_mem_cache:
                del in_mem_cache[key]

        async def invalidate_all_cache() -> None:
            redis_client = await get_redis_client()
            pattern = f"{_get_redis_key_func_prefix()}:*"
            batch_size = 1000
            keys_batch: list[str] = []

            # Delete keys in batches to avoid memory issues
            async for key in redis_client.scan_iter(pattern):
                keys_batch.append(key)
                if len(keys_batch) >= batch_size:
                    await redis_client.delete(*keys_batch)
                    keys_batch.clear()

            # Delete any remaining keys
            if keys_batch:
                await redis_client.delete(*keys_batch)
            in_mem_cache.clear()

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal hits, misses

            key = wrapper.redis_key(args, kwargs)  # type: ignore
            logger.info("redis_async_cache lookup", key=key)

            redis_client = await get_redis_client()

            # expires_at is the time when redis cache will expire. if ttl_seconds is not provided,
            # expires_at is 0.
            expires_at = 0

            # If force update is enabled, doing calculation outside the lock to avoid blocking other requests.
            if force_update_cache:
                logger.info("Force updating redis cache", key=key)
                result = await func(*args, **kwargs) if inspect.iscoroutinefunction(func) else func(*args, **kwargs)
                serialized_result = _serialize_result(result)
                expires_at = _get_expires_at(0, ttl_seconds)
            else:
                try:
                    redis_ttl = await redis_client.ttl(key)
                except Exception:
                    logger.error("Error getting redis ttl", key=key, exc_info=True)
                    redis_ttl = 0
                expires_at = _get_expires_at(redis_ttl, ttl_seconds)
                if redis_ttl != -2 and key in in_mem_cache:
                    # if redis_ttl is -2, it means the key doesn't exist in redis, respect redis cache.
                    if expires_at <= in_mem_cache[key][1] + TTL_DIFFERENCE_THRESHOLD_SECONDS:
                        hits += 1
                        logger.info("redis_async_cache hit", key=key, source="memory")
                        return in_mem_cache[key][0]

            # Check if the result is already cached
            lock = redis_client.lock(f"lock:{key}", timeout=redis_lock_ttl_seconds, blocking_timeout=blocking_timeout)
            await lock.acquire()
            try:
                if force_update_cache:
                    await redis_client.set(key, serialized_result, ex=ttl_seconds)
                    # Force updates should not populate the in-memory cache. We also evict any existing
                    # entry to avoid serving stale results after force update is turned off.
                    if key in in_mem_cache:
                        del in_mem_cache[key]
                    logger.info("redis_async_cache store", key=key, reason="force_update")
                    return result

                if (cached := await redis_client.get(key)) is not None:
                    hits += 1
                    logger.info("redis_async_cache hit", key=key, source="redis")
                    if expires_at < 0:
                        # if we are here, it means the entry is newly added to redis cache.
                        expires_at = _get_expires_at(0, ttl_seconds)
                    # Deserialize from JSON based on the return type
                    try:
                        result = _deserialize_result(cached, return_type)
                        in_mem_cache[key] = (result, expires_at)
                        return result
                    except ValidationError as e:
                        # Delete the key from redis, since it's corrupted. (This will fix corrupted cache caused by
                        # deployment with changes to the result type or serialization function.)
                        # Do not delete in memory cache, since if it has value, it should be from local computation,
                        # instead of redis cache.
                        # We deliberately DO NOT recompute here, since there are several situations where the cache
                        # could be corrupted. One of such situations is the serialization function is buggy, and thus
                        # cannot ever be deserialized from redis successfully.
                        # In this case, if we have a expensive function that we call in a loop, a worst case could
                        # be it's never correctly cached on redis, and we keep trying to fetch from redis (as other
                        # server may update redis cache with the wrong serialization as well, and we will detect
                        # the redis cached result is newer than local result, and keep fetching from redis), which
                        # could lead to a lot of calls to the expensive service, + 2x the calls to redis (fetch and
                        # invalidate).
                        # And this pattern could cause cascading failures in the worst case.
                        redis_client.delete(key)
                        raise e

                # Call the function and cache its result
                misses += 1
                logger.info("redis_async_cache miss", key=key)
                result = await func(*args, **kwargs) if inspect.iscoroutinefunction(func) else func(*args, **kwargs)
                serialized_result = _serialize_result(result)
                await redis_client.set(key, serialized_result, ex=ttl_seconds)
                in_mem_cache[key] = (result, _get_expires_at(0, ttl_seconds))
                logger.info("redis_async_cache store", key=key, reason="miss")
            finally:
                # Handle lock release gracefully - if lock expired, it's already released
                try:
                    await lock.release()
                except LockNotOwnedError:
                    logger.warning("Lock expired before release", key=key)

            return result

        def cache_info() -> functools._CacheInfo:
            return functools._CacheInfo(hits=hits, misses=misses, maxsize=-1, currsize=-1)

        def set_force_update_cache(force: bool) -> None:
            nonlocal force_update_cache
            force_update_cache = force

        wrapper.redis_key = redis_key  # type: ignore
        wrapper.cache_info = cache_info  # type: ignore
        wrapper.set_force_update_cache = set_force_update_cache  # type: ignore
        wrapper.invalidate_cache_for_params = invalidate_cache_for_params  # type: ignore
        wrapper.invalidate_all_cache = invalidate_all_cache  # type: ignore
        return wrapper  # type: ignore

    return decorator


class RedisRateLimitWatcher:
    """
    A naive Redis-based rate limiter with pub/sub communication and in-memory caching.
    This implementation assumes that rate limited items in a topic are rare, thus pubsub
    based implementation is more efficient.

    WARNING: This class is not thread-safe. it's intended to be used in the async context.
    """

    def __init__(self, ttl_seconds: int, topic: str):
        """
        Initialize the rate limiter.

        Args:
            ttl_seconds: TTL in seconds for rate-limited items in Redis
            topic: Topic for Redis keys and pub/sub channel
        """
        self.ttl_seconds = ttl_seconds
        self.topic = topic
        self._rate_limited_cache: dict[str, float] = {}  # (expires_at)
        self._pubsub_channel = f"rate_limit_events:{self.topic}"
        self._pubsub_task: Any = None
        self._initialized = False

    def _get_redis_key(self, identifier: str) -> str:
        """Generate Redis key for the given identifier."""
        return f"rate_limited:{self.topic}:{identifier}"

    def _is_cached_rate_limited(self, identifier: str) -> bool:
        """Check if identifier is cached as rate-limited and cache is still valid."""
        if identifier not in self._rate_limited_cache:
            return False

        expires_at = self._rate_limited_cache[identifier]
        current_time = time.time()

        # Check if the cache entry has expired
        if current_time > expires_at:
            # Cache expired, remove it
            del self._rate_limited_cache[identifier]
            return False

        return True

    async def _start_pubsub_listener(self) -> None:
        """Start listening to pub/sub channel for rate limit events."""
        if self._pubsub_task is not None:
            return

        redis_client = await get_redis_client()
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(self._pubsub_channel)

        async def listen_for_events() -> None:
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        try:
                            data = json.loads(message["data"])
                            identifier = data.get("identifier")
                            action = data["action"]
                        except Exception:
                            logger.error(
                                "Error parsing pub/sub message",
                                pubsub_message=message,
                                exc_info=True,
                            )
                            continue

                        if action == RATE_LIMIT_ACTION_REMOVE:
                            self._rate_limited_cache.pop(identifier, None)
                            continue
                        if action == RATE_LIMIT_ACTION_SET:
                            expires_at = time.time() + self.ttl_seconds
                            # Update cache with the rate limit event
                            self._rate_limited_cache[identifier] = expires_at
                        else:
                            logger.warning("Unknown action in pub/sub listener", action=action)
            except Exception:
                logger.error("Error in pub/sub listener", exc_info=True)
            finally:
                await pubsub.close()

        # Start the listener task
        self._pubsub_task = asyncio.create_task(listen_for_events())

    async def is_rate_limited(self, identifier: str) -> bool:
        """
        Check if the given identifier is rate limited.
        Assumes not rate limited if not cached in memory.

        Args:
            identifier: Unique identifier to check (e.g., user ID, IP address)

        Returns:
            True if rate limited, False otherwise
        """

        # Ensure initialized
        await self.initialize()

        # Check in-memory cache only - assume not rate limited if not cached
        return self._is_cached_rate_limited(identifier)

    async def set_rate_limited(self, identifier: str) -> None:
        """
        Mark the given identifier as rate limited and notify via pub/sub.

        Args:
            identifier: Unique identifier to rate limit (e.g., user ID, IP address)
        """

        # Ensure initialized
        await self.initialize()

        redis_client = await get_redis_client()
        redis_key = self._get_redis_key(identifier)
        expires_at = time.time() + self.ttl_seconds

        # Set in Redis with TTL
        await redis_client.set(redis_key, "1", ex=self.ttl_seconds)

        # Publish rate limit event to all listeners
        event_data = {
            "identifier": identifier,
            "action": RATE_LIMIT_ACTION_SET,
        }
        await redis_client.publish(self._pubsub_channel, json.dumps(event_data))

        # Also cache in memory locally
        self._rate_limited_cache[identifier] = expires_at

    async def remove_rate_limit(self, identifier: str) -> None:
        """
        Remove rate limit for the given identifier and notify via pub/sub.
        This should only be needed for manual intervention.

        Args:
            identifier: Unique identifier to remove rate limit for
        """

        # Ensure initialized
        await self.initialize()

        redis_client = await get_redis_client()
        redis_key = self._get_redis_key(identifier)

        # Remove from Redis
        await redis_client.delete(redis_key)

        # Publish removal event
        event_data = {
            "identifier": identifier,
            "action": RATE_LIMIT_ACTION_REMOVE,
        }
        await redis_client.publish(self._pubsub_channel, json.dumps(event_data))

        # Remove from local cache
        self._rate_limited_cache.pop(identifier, None)

    def clear_memory_cache(self) -> None:
        """Clear the in-memory cache of rate-limited items."""
        self._rate_limited_cache.clear()

    def get_memory_cache_size(self) -> int:
        """Get the current size of the in-memory cache."""
        return len(self._rate_limited_cache)

    async def stop_pubsub_listener(self) -> None:
        """Stop the pub/sub listener task."""
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pubsub_task
            self._pubsub_task = None

    async def initialize(self) -> None:
        """
        Initialize the rate limiter from Redis.
        - Start the pub/sub listener
        - Populate the in-memory cache from Redis
        """
        if self._initialized:
            return

        self._initialized = True
        await self._start_pubsub_listener()
        redis_client = await get_redis_client()
        keys = await redis_client.keys(f"rate_limited:{self.topic}:*")
        for key in keys:
            ttl = await redis_client.ttl(key)
            if ttl > 0:
                self._rate_limited_cache[key] = time.time() + ttl


class StringRedisCache:
    """
    Simple Redis cache for string keys and values. The caller is responsible for serialization and
    deserialization of the value if they want to store non-string values.
    You are recommended to specify a namespace that indicates the purpose of the cache so it won't conflict
    with other caches.
    """

    def __init__(self, namespace: str, default_ttl_seconds: int | None = None) -> None:
        self._namespace: str = namespace
        self._redis_client: redis.asyncio.Redis | None = None
        self._default_ttl_seconds: int | None = default_ttl_seconds

    def _namespaced_key(self, key: str) -> str:
        return f"{self._namespace}::{key}"

    async def _get_client(self) -> redis.asyncio.Redis:
        if self._redis_client is None:
            self._redis_client = await get_redis_client()
        return self._redis_client

    async def put(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """
        Store a string value by key with optional TTL in seconds.
        If ttl_seconds is None, uses the default_ttl_seconds if set, otherwise the key will not expire.
        """
        redis_client = await self._get_client()
        namespaced_key = self._namespaced_key(key)
        effective_ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        if effective_ttl is not None:
            await redis_client.set(namespaced_key, value, ex=effective_ttl)
        else:
            await redis_client.set(namespaced_key, value)

    async def get(self, key: str) -> str | None:
        """
        Retrieve a string value by key.
        Returns None if the key does not exist.
        """
        redis_client = await self._get_client()
        namespaced_key = self._namespaced_key(key)
        value = await redis_client.get(namespaced_key)
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def exists(self, key: str) -> bool:
        """
        Check if a key exists in the cache.
        """
        redis_client = await self._get_client()
        namespaced_key = self._namespaced_key(key)
        existing_count = await redis_client.exists(namespaced_key)
        return bool(existing_count)

    async def exists_all(self, keys: list[str]) -> bool:
        """
        Check if all keys exist in the cache.
        Uses a single Redis call for all keys.
        """
        if not keys:
            return True
        redis_client = await self._get_client()
        namespaced_keys = [self._namespaced_key(key) for key in keys]
        existing_count = await redis_client.exists(*namespaced_keys)
        return int(existing_count) == len(namespaced_keys)

    async def exists_batch(self, keys: list[str], chunk_size: int = 5000) -> dict[str, bool]:
        """
        Check existence of multiple keys using chunked MGET calls.
        Returns a dict mapping each key to True if it exists (has a value), False otherwise.

        Args:
            keys: List of cache keys to check
            chunk_size: Max keys per MGET call to avoid oversized Redis requests (default 5000)
        """
        if not keys:
            return {}

        redis_client = await self._get_client()
        result: dict[str, bool] = {}

        # Process keys in chunks to avoid oversized Redis requests
        for i in range(0, len(keys), chunk_size):
            chunk_keys = keys[i : i + chunk_size]
            namespaced_keys = [self._namespaced_key(key) for key in chunk_keys]
            # MGET returns None for non-existent keys, the value otherwise
            values = await redis_client.mget(namespaced_keys)
            for key, value in zip(chunk_keys, values, strict=True):
                result[key] = value is not None

        return result

    async def invalidate(self, key: str) -> None:
        """
        Remove a key from the cache.
        """
        redis_client = await self._get_client()
        namespaced_key = self._namespaced_key(key)
        await redis_client.delete(namespaced_key)

    async def invalidate_all_cache_with_prefix(self, prefix: str, batch_size: int = 1000) -> int:
        """
        Remove all keys from the cache that match the given prefix.

        Args:
            prefix: The prefix to match keys against (will be namespaced automatically).
            batch_size: Number of keys to delete in each batch to avoid memory issues.

        Returns:
            The total number of keys deleted.
        """
        redis_client = await self._get_client()
        pattern = f"{self._namespaced_key(prefix)}*"
        keys_batch: list[str] = []
        total_deleted = 0

        # Delete keys in batches to avoid memory issues
        async for key in redis_client.scan_iter(pattern):
            keys_batch.append(key)
            if len(keys_batch) >= batch_size:
                total_deleted += await redis_client.delete(*keys_batch)
                keys_batch.clear()

        # Delete any remaining keys
        if keys_batch:
            total_deleted += await redis_client.delete(*keys_batch)

        return total_deleted


class RedisTokenBucketRateLimiter:
    """
    Simple Redis-based token bucket rate limiter using Lua scripts.

    Uses a single Lua script for atomic operations - much more efficient than
    Python with Redis locks as it eliminates network round trips and lock contention.

    NOTE: If you make any changes to this class, please run
    ```bash
    pytest ypl/backend/utils/tests/_test_redis_rate_limiter.py
    ```
    manually to ensure the changes work well with a real redis server.
    """

    def __init__(
        self,
        limit: int,
        interval_seconds: int,
        redis_key_prefix: str,
        burst_ratio: float = 2.0,  # Ratio of burst tokens to regular tokens
    ):
        """
        Initialize the rate limiter.

        Args:
            limit: Maximum number of requests allowed
            interval_seconds: Time window in seconds for the limit
            redis_key_prefix: Prefix for Redis keys
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")

        self.limit = limit
        self.interval_seconds = interval_seconds
        self.redis_key_prefix = redis_key_prefix
        self.burst_ratio = burst_ratio

        # Load Lua script from file
        script_path = Path(__file__).parent / "token_bucket_rate_limiter.lua"
        self._lua_script = script_path.read_text()

    def _get_redis_key(self, identifier: str) -> str:
        """Generate Redis key for the rate limiter."""
        return f"{self.redis_key_prefix}:{identifier}"

    async def is_allowed(self, identifier: str) -> bool:
        """
        Check if request is allowed and consume a token if so.

        Args:
            identifier: Unique identifier (e.g., user ID, IP address)

        Returns:
            True if request is allowed, False if rate limited
        """
        redis_client = await get_redis_client()
        key = self._get_redis_key(identifier)

        try:
            result: Any = await redis_client.eval(  # type: ignore
                self._lua_script,
                1,  # num_keys
                key,  # key
                str(self.limit),
                str(self.interval_seconds),
                str(self.burst_ratio),  # args
            )
            return bool(result)
        except Exception:
            logger.error("Error in rate limiter", identifier=identifier, exc_info=True)
            # Fail open - allow request on error
            return True

    async def get_remaining_tokens(self, identifier: str) -> int:
        """
        Get remaining tokens without consuming any. This is designed for debugging purposes only.
        e.g. print in logs when getting ratelimited prematurely.

        Args:
            identifier: Unique identifier

        Returns:
            Number of remaining tokens based on local computation (debugging purposes only)
        """
        redis_client = await get_redis_client()
        key = self._get_redis_key(identifier)

        try:
            now_result = await redis_client.time()
            now_seconds = int(now_result[0])

            tokens_str = await redis_client.hget(key, "tokens")  # type: ignore
            last_update_str = await redis_client.hget(key, "last_update")  # type: ignore

            if tokens_str is None or last_update_str is None:
                return self.limit

            tokens = int(tokens_str)
            last_update = int(last_update_str)

            # Refill tokens based on time elapsed
            delta = now_seconds - last_update
            return min(int(self.limit * self.burst_ratio), tokens + delta * self.limit // self.interval_seconds)

        except Exception:
            logger.error("Error getting remaining tokens", identifier=identifier, exc_info=True)
            return self.limit


# Default constants for hot queries tracking (fallbacks if dynamic settings unavailable)
HOT_QUERIES_DEFAULT_TTL_DAYS = 8
HOT_QUERIES_DEFAULT_MAX_ENTRIES = 1000

# Load Lua script at module load time (not per-request)
_HOT_QUERIES_LUA_SCRIPT = (Path(__file__).parent / "hot_queries.lua").read_text()


class HotQuerySettings(TypedDict, total=False):
    """Settings for hot query tracking."""

    enabled: bool
    ttl_days: int
    max_entries: int


def track_hot_queries(
    *,
    namespace: str,
    key_prefix: str | None = None,
    validator: Callable[[tuple[Any, ...], dict[str, Any]], None] | None = None,
    value_normalizer: Callable[[Any], Any] | None = None,
    settings_provider: Callable[[], Awaitable[HotQuerySettings]] | None = None,
) -> Callable[[T], T]:
    """
    Decorator that tracks function call frequency using Redis Sorted Sets.

    Use with get_hot_queries() to retrieve top-k calls for cache warmup.
    The data structure uses a fair trimming algorithm that allows thousands of unique
    queries to compete naturally while preventing memory explosion.

    FAIR TRIMMING ALGORITHM:
    - Allows 20% buffer over max_entries (e.g., 1200 for max=1000)
    - Removes one-off queries (score < 2) first - they don't deserve cache warmup
    - Only aggressively trims when significantly over capacity
    - New hot queries get fair chance to compete, old cold ones naturally decay

    Args:
        namespace: Unique namespace for this function (e.g., "leaderboard")
        key_prefix: Optional prefix for multi-tier deployments
        validator: Optional callable that validates (args, kwargs) before tracking.
                   If it raises an exception, the query is not tracked.
        value_normalizer: Optional callable to normalize parameter values to JSON-compatible dicts.
                         Receives a value and should return a JSON-serializable dict/primitive.
                         If not provided, uses default Pydantic model_dump.
        settings_provider: Optional async callable that returns HotQuerySettings dict with
                          'enabled', 'ttl_days', and 'max_entries' keys.
                          If not provided, uses defaults (enabled=True, ttl_days=8, max_entries=1000).

    Example:
        @track_hot_queries(namespace="search")
        async def search(query: str) -> list[Result]:
            ...

        # Later, for warmup:
        top_calls = await get_hot_queries("search", limit=500)
    """

    def decorator(func: T) -> T:
        unwrapped_func = inspect.unwrap(func)

        def _get_key_prefix() -> str:
            return f"{key_prefix}:" if key_prefix else ""

        def _serialize_and_normalize(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            """Serialize args/kwargs and normalize, return (hash, normalized_dict).

            Does normalization once to use for both hashing and storage.
            """
            normalized_args, normalized_kwargs = normalize_args(unwrapped_func, args, kwargs)

            def default_normalizer(v: Any) -> Any:
                if isinstance(v, BaseModel):
                    return v.model_dump(mode="json", exclude_unset=True, exclude_defaults=True)
                return v

            normalize = value_normalizer if value_normalizer is not None else default_normalizer

            normalized_kwargs_dict = {k: normalize(v) for k, v in normalized_kwargs.items()}

            params_dict = {
                "args": [normalize(a) for a in normalized_args],
                "kwargs": normalized_kwargs_dict,
            }

            # Hash for deduplication
            params_json = json.dumps(params_dict, sort_keys=True, default=str)
            params_hash = hashlib.sha256(params_json.encode()).hexdigest()[:16]

            return params_hash, params_dict

        async def _track_call_async(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            """Track this call in Redis (fire-and-forget)."""
            try:
                # Validate the query before tracking (if validator provided)
                if validator is not None:
                    try:
                        validator(args, kwargs)
                    except Exception as e:
                        logger.debug("Skipping invalid query", namespace=namespace, error=str(e))
                        return

                # Fetch settings (with fallback to defaults)
                try:
                    if settings_provider is not None:
                        hot_query_settings = await settings_provider()
                        if not hot_query_settings.get("enabled", True):
                            return
                        ttl_days = hot_query_settings.get("ttl_days", HOT_QUERIES_DEFAULT_TTL_DAYS)
                        max_entries = hot_query_settings.get("max_entries", HOT_QUERIES_DEFAULT_MAX_ENTRIES)
                    else:
                        ttl_days = HOT_QUERIES_DEFAULT_TTL_DAYS
                        max_entries = HOT_QUERIES_DEFAULT_MAX_ENTRIES
                except Exception:
                    ttl_days = HOT_QUERIES_DEFAULT_TTL_DAYS
                    max_entries = HOT_QUERIES_DEFAULT_MAX_ENTRIES

                # Serialize and normalize params (done once)
                params_hash, params_dict = _serialize_and_normalize(args, kwargs)
                params_json = json.dumps(params_dict, sort_keys=True, default=str)

                # Build Redis keys
                prefix = _get_key_prefix()
                date_str = datetime.now(UTC).strftime("%Y-%m-%d")
                daily_key = f"{prefix}hot_queries:{namespace}:{date_str}"
                params_key = f"{prefix}hot_queries:{namespace}:params:{params_hash}"

                # Single atomic Redis call via Lua script
                redis_client = await get_redis_client()
                ttl_seconds = ttl_days * 86400

                await redis_client.eval(  # type: ignore[misc]
                    _HOT_QUERIES_LUA_SCRIPT,
                    2,  # num_keys
                    daily_key,  # KEYS[1]
                    params_key,  # KEYS[2]
                    params_hash,  # ARGV[1]
                    str(max_entries),  # ARGV[2]
                    str(ttl_seconds),  # ARGV[3]
                    params_json,  # ARGV[4]
                )
            except Exception:
                logger.warning("Failed to track hot query", namespace=namespace, exc_info=True)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Fire-and-forget tracking (don't await, don't block)
            asyncio.create_task(_track_call_async(args, kwargs))
            return await func(*args, **kwargs)

        # Attach metadata for testing/debugging
        wrapper.hot_queries_namespace = namespace  # type: ignore
        wrapper.hot_queries_key_prefix = key_prefix  # type: ignore

        return cast(T, wrapper)

    return decorator


async def get_hot_queries(
    namespace: str,
    limit: int = 500,
    key_prefix: str | None = None,
    days: int = 7,
) -> list[dict[str, Any]]:
    """
    Get the most frequently called function parameters for warmup.

    Args:
        namespace: The namespace used in @track_hot_queries
        limit: Max number of calls to return
        key_prefix: Optional prefix (same as decorator)
        days: Number of days to aggregate

    Returns:
        List of {"args": [...], "kwargs": {...}} dicts representing hot queries
    """
    try:
        redis_client = await get_redis_client()
        prefix = f"{key_prefix}:" if key_prefix else ""

        # Get daily keys for last N days
        today = datetime.now(UTC)
        daily_keys = [
            f"{prefix}hot_queries:{namespace}:{(today - timedelta(days=i)).strftime('%Y-%m-%d')}" for i in range(days)
        ]

        # Filter to existing keys
        existing_keys = [k for k in daily_keys if await redis_client.exists(k)]

        if not existing_keys:
            logger.info("No hot queries data found", namespace=namespace)
            return []

        # Log the number of unique keys found for each day
        total_keys_across_days = 0
        for key in existing_keys:
            day_key_count = await redis_client.zcard(key)
            date_part = key.split(":")[-1]  # Extract date from key
            logger.info("Hot queries day stats", date=date_part, unique_keys=day_key_count)
            total_keys_across_days += day_key_count

        logger.info("Hot queries total keys", num_days=len(existing_keys), total_keys=total_keys_across_days)

        # Union all daily sets into temp key
        temp_key = f"{prefix}hot_queries:{namespace}:_weekly_temp"
        await redis_client.zunionstore(temp_key, existing_keys)
        await redis_client.expire(temp_key, 300)  # 5 min TTL for temp key

        # Get total unique keys after aggregation
        total_unique_keys = await redis_client.zcard(temp_key)
        logger.info("Hot queries aggregated", total_unique_keys=total_unique_keys)

        # Get top-k hashes
        top_hashes = await redis_client.zrevrange(temp_key, 0, limit - 1)
        if not top_hashes:
            return []

        # Retrieve params for each hash
        results: list[dict[str, Any]] = []
        for h in top_hashes:
            params_key = f"{prefix}hot_queries:{namespace}:params:{h}"
            params_json = await redis_client.get(params_key)
            if params_json:
                try:
                    results.append(json.loads(params_json))
                except json.JSONDecodeError:
                    continue

        logger.info("Hot queries retrieved", count=len(results), namespace=namespace)
        return results
    except Exception:
        logger.warning("Failed to get hot queries", namespace=namespace, exc_info=True)
        return []
