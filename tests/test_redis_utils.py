"""Tests for ypl/backend/utils/redis_utils.py."""

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError
from ypl.backend.utils.redis_utils import (
    HotQuerySettings,
    RedisRateLimitWatcher,
    RedisTokenBucketRateLimiter,
    StringRedisCache,
    get_hot_queries,
    redis_async_cache,
    track_hot_queries,
)

REDIS_UTILS_MODULE = "ypl.backend.utils.redis_utils"


async def _drain_background_tasks() -> None:
    """Await all pending tasks created by fire-and-forget patterns (e.g. asyncio.create_task).

    More reliable than asyncio.sleep() — works regardless of CI runner speed.
    """
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _mock_redis(**overrides: Any) -> AsyncMock:
    """Build an AsyncMock Redis client with sensible defaults."""
    client = AsyncMock()
    client.get.return_value = overrides.get("get", None)
    client.set.return_value = True
    client.delete.return_value = 1
    client.ttl.return_value = overrides.get("ttl", -2)
    client.exists.return_value = overrides.get("exists", 0)
    client.mget.return_value = overrides.get("mget", [])
    client.keys.return_value = overrides.get("keys", [])
    client.eval.return_value = overrides.get("eval", 1)
    client.time.return_value = overrides.get("time", (int(time.time()), 0))
    client.hget.return_value = overrides.get("hget", None)
    client.publish.return_value = 1
    client.expire.return_value = True
    client.zcard.return_value = overrides.get("zcard", 0)
    client.zunionstore.return_value = 0
    client.zrevrange.return_value = overrides.get("zrevrange", [])

    # scan_iter returns an async iterator
    scan_iter_values = overrides.get("scan_iter", [])

    async def _scan_iter(match: str | None = None, **kwargs: Any) -> Any:
        for v in scan_iter_values:
            yield v

    client.scan_iter = _scan_iter

    # lock — redis_client.lock() is sync, but acquire/release are async
    lock = AsyncMock()
    lock.acquire.return_value = True
    lock.release.return_value = True
    client.lock = MagicMock(return_value=lock)

    # pubsub — redis_client.pubsub() is sync, but subscribe/close/listen are async
    pubsub = AsyncMock()
    pubsub.subscribe.return_value = None
    pubsub.close.return_value = None

    async def _empty_listen() -> Any:
        return
        yield  # make it an async generator

    pubsub.listen = _empty_listen
    client.pubsub = MagicMock(return_value=pubsub)

    return client


# =============================================================================
# StringRedisCache
# =============================================================================


class TestStringRedisCache:
    """Tests for StringRedisCache."""

    async def test_namespaced_key(self) -> None:
        cache = StringRedisCache(namespace="ns")
        assert cache._namespaced_key("foo") == "ns::foo"

    async def test_put_with_ttl(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            await cache.put("k", "v", ttl_seconds=60)
        mock.set.assert_awaited_once_with("ns::k", "v", ex=60)

    async def test_put_with_default_ttl(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns", default_ttl_seconds=120)
            await cache.put("k", "v")
        mock.set.assert_awaited_once_with("ns::k", "v", ex=120)

    async def test_put_explicit_ttl_overrides_default(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns", default_ttl_seconds=120)
            await cache.put("k", "v", ttl_seconds=30)
        mock.set.assert_awaited_once_with("ns::k", "v", ex=30)

    async def test_put_no_ttl(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            await cache.put("k", "v")
        mock.set.assert_awaited_once_with("ns::k", "v")

    async def test_get_returns_string(self) -> None:
        mock = _mock_redis(get=b"hello")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.get("k")
        assert result == "hello"

    async def test_get_returns_none_when_missing(self) -> None:
        mock = _mock_redis(get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.get("k")
        assert result is None

    async def test_get_returns_str_when_not_bytes(self) -> None:
        mock = _mock_redis()
        mock.get.return_value = 42  # not bytes, not None
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.get("k")
        assert result == "42"

    async def test_exists_true(self) -> None:
        mock = _mock_redis(exists=1)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            assert await cache.exists("k") is True

    async def test_exists_false(self) -> None:
        mock = _mock_redis(exists=0)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            assert await cache.exists("k") is False

    async def test_exists_all_empty_keys(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            assert await cache.exists_all([]) is True

    async def test_exists_all_true(self) -> None:
        mock = _mock_redis(exists=2)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            assert await cache.exists_all(["a", "b"]) is True

    async def test_exists_all_false(self) -> None:
        mock = _mock_redis(exists=1)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            assert await cache.exists_all(["a", "b"]) is False

    async def test_exists_batch_empty(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.exists_batch([])
        assert result == {}

    async def test_exists_batch_mixed(self) -> None:
        mock = _mock_redis(mget=[b"val", None, b"other"])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.exists_batch(["a", "b", "c"])
        assert result == {"a": True, "b": False, "c": True}

    async def test_exists_batch_chunked(self) -> None:
        mock = _mock_redis()
        mock.mget.side_effect = [[b"v1", b"v2"], [None]]
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            result = await cache.exists_batch(["a", "b", "c"], chunk_size=2)
        assert result == {"a": True, "b": True, "c": False}
        assert mock.mget.await_count == 2

    async def test_invalidate(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            await cache.invalidate("k")
        mock.delete.assert_awaited_once_with("ns::k")

    async def test_invalidate_all_cache_with_prefix(self) -> None:
        mock = _mock_redis(scan_iter=["ns::prefix:1", "ns::prefix:2"])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            mock.delete.return_value = 2
            total = await cache.invalidate_all_cache_with_prefix("prefix:")
        assert total == 2

    async def test_invalidate_all_cache_with_prefix_batched(self) -> None:
        keys = [f"ns::prefix:{i}" for i in range(3)]
        mock = _mock_redis(scan_iter=keys)
        mock.delete.return_value = 2  # first batch
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            cache = StringRedisCache(namespace="ns")
            await cache.invalidate_all_cache_with_prefix("prefix:", batch_size=2)
        # Two delete calls: batch of 2, then remainder of 1
        assert mock.delete.await_count == 2

    async def test_get_client_caches(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)) as get_client:
            cache = StringRedisCache(namespace="ns")
            await cache._get_client()
            await cache._get_client()
        # Only called once because client is cached
        get_client.assert_awaited_once()


# =============================================================================
# redis_async_cache
# =============================================================================


class _SampleModel(BaseModel):
    value: int


class _CustomSerializable:
    def __init__(self, data: str) -> None:
        self.data = data

    def to_json(self) -> str:
        return json.dumps({"data": self.data})

    @classmethod
    def from_json(cls, json_str: str) -> "_CustomSerializable":
        return cls(**json.loads(json_str))


class TestRedisAsyncCacheHelpers:
    """Test the inner helper functions via decorated functions."""

    async def test_cache_miss_then_stores(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> _SampleModel:
                return _SampleModel(value=x)

            result = await my_func(42)

        assert isinstance(result, _SampleModel)
        assert result.value == 42
        mock.set.assert_awaited_once()

    async def test_cache_hit_from_redis(self) -> None:
        cached_json = _SampleModel(value=99).model_dump_json()
        mock = _mock_redis(ttl=300, get=cached_json)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            call_count = 0

            @redis_async_cache(ttl_seconds=600)
            async def my_func(x: int) -> _SampleModel:
                nonlocal call_count
                call_count += 1
                return _SampleModel(value=x)

            result = await my_func(99)

        assert result.value == 99
        assert call_count == 0  # function not called, served from cache

    async def test_cache_hit_from_in_memory(self) -> None:
        mock = _mock_redis(ttl=300)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            call_count = 0

            @redis_async_cache(ttl_seconds=600)
            async def my_func(x: int) -> _SampleModel:
                nonlocal call_count
                call_count += 1
                return _SampleModel(value=x)

            # First call populates both redis and in-memory cache
            mock.get.return_value = None
            mock.ttl.return_value = -2
            await my_func(1)
            assert call_count == 1

            # Second call should hit in-memory cache (ttl > 0 means key exists)
            mock.ttl.return_value = 500
            result = await my_func(1)
            assert result.value == 1
            assert call_count == 1  # served from in-memory, function not called again

    async def test_cache_primitive_return_type(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x * 2

            result = await my_func(5)

        assert result == 10

    async def test_cache_primitive_hit_from_redis(self) -> None:
        mock = _mock_redis(ttl=300, get=json.dumps(42))
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=600)
            async def my_func(x: int) -> int:
                return x

            result = await my_func(42)

        assert result == 42

    async def test_cache_none_return(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func() -> None:
                return None

            await my_func()

    async def test_cache_null_hit_from_redis(self) -> None:
        mock = _mock_redis(ttl=300, get="null")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=600)
            async def my_func() -> None:
                return None

            await my_func()

    async def test_cache_custom_serializable(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func() -> _CustomSerializable:
                return _CustomSerializable(data="hello")

            result = await my_func()

        assert isinstance(result, _CustomSerializable)
        assert result.data == "hello"

    async def test_cache_custom_serializable_hit(self) -> None:
        mock = _mock_redis(ttl=300, get=json.dumps({"data": "world"}))
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=600)
            async def my_func() -> _CustomSerializable:
                return _CustomSerializable(data="world")

            result = await my_func()

        assert isinstance(result, _CustomSerializable)
        assert result.data == "world"

    async def test_unsupported_return_type_raises(self) -> None:
        with pytest.raises(ValueError, match="do not support json serialization"):

            @redis_async_cache(ttl_seconds=60)
            async def bad_func() -> dict[str, Any]:
                return {}

    async def test_key_prefix(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60, key_prefix="staging")
            async def my_func(x: int) -> int:
                return x

            await my_func(1)

        # Check the key used in redis.set includes the prefix
        set_call = mock.set.await_args
        assert "staging:" in set_call[0][0]

    async def test_cache_info(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            info = my_func.cache_info()  # type: ignore[attr-defined]

        assert info.misses == 1
        assert info.hits == 0

    async def test_force_update_cache(self) -> None:
        mock = _mock_redis(ttl=300, get=json.dumps(1))
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            call_count = 0

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 10

            # Enable force update
            my_func.set_force_update_cache(True)  # type: ignore[attr-defined]
            result = await my_func(5)

        assert result == 50
        assert call_count == 1  # Function was called despite cache existing

    async def test_invalidate_cache_for_params(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            await my_func.invalidate_cache_for_params(1)  # type: ignore[attr-defined]

        mock.delete.assert_awaited()

    async def test_invalidate_all_cache(self) -> None:
        # Track scan_iter calls to verify the pattern used
        captured_patterns: list[str | None] = []
        original_scan_iter_values = ["key1", "key2"]

        async def _tracking_scan_iter(match: str | None = None, **kwargs: Any) -> Any:
            captured_patterns.append(match)
            for v in original_scan_iter_values:
                yield v

        mock = _mock_redis()
        mock.scan_iter = _tracking_scan_iter
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(x: int) -> int:
                return x

            await my_func.invalidate_all_cache()  # type: ignore[attr-defined]

        mock.delete.assert_awaited()
        # Verify a scan pattern was passed (not scanning all keys)
        assert len(captured_patterns) == 1
        assert captured_patterns[0] is not None
        assert "my_func" in captured_patterns[0]

    async def test_redis_key_with_pydantic_arg(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func(model: _SampleModel) -> int:
                return model.value

            await my_func(_SampleModel(value=7))

        # Key should contain serialized pydantic model
        set_call = mock.set.await_args
        assert "7" in set_call[0][0]

    async def test_lock_expired_before_release(self) -> None:
        from redis.exceptions import LockNotOwnedError

        mock = _mock_redis(ttl=-2, get=None)
        lock = mock.lock.return_value
        lock.release.side_effect = LockNotOwnedError("expired")  # type: ignore[no-untyped-call]
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func() -> int:
                return 1

            # Should not raise — lock expiry is handled gracefully
            result = await my_func()

        assert result == 1

    async def test_ttl_error_falls_back(self) -> None:
        mock = _mock_redis(get=None)
        mock.ttl.side_effect = Exception("redis down")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func() -> int:
                return 42

            result = await my_func()

        assert result == 42

    async def test_corrupted_cache_deletes_key(self) -> None:
        # Cached value that won't validate as _SampleModel
        mock = _mock_redis(ttl=300, get='{"bad_field": 123}')
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=60)
            async def my_func() -> _SampleModel:
                return _SampleModel(value=1)

            with pytest.raises(ValidationError):
                await my_func()

        # NOTE: Production code at line 262 calls redis_client.delete(key) without await.
        # This means the delete coroutine is created but never awaited. assert_called()
        # confirms the coroutine was created (which is the current behavior).
        mock.delete.assert_called()

    async def test_ignore_self_arg(self) -> None:
        mock = _mock_redis(ttl=-2, get=None)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            class MyClass:
                @redis_async_cache(ttl_seconds=60, ignore_self_arg=True)
                async def method(self, x: int) -> int:
                    return x

            obj1 = MyClass()
            obj2 = MyClass()

            # With ignore_self_arg=True, different instances with same args should produce same key
            key1 = obj1.method.redis_key((obj1, 1), {})  # type: ignore[attr-defined]
            key2 = obj2.method.redis_key((obj2, 1), {})  # type: ignore[attr-defined]
            assert key1 == key2

    async def test_get_expires_at_no_ttl(self) -> None:
        """_get_expires_at returns 0 when redis_ttl is -1 (no expiry)."""
        mock = _mock_redis(ttl=-1)
        mock.get.return_value = None
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @redis_async_cache(ttl_seconds=None)
            async def my_func() -> int:
                return 1

            result = await my_func()
        assert result == 1


# =============================================================================
# RedisRateLimitWatcher
# =============================================================================


class TestRedisRateLimitWatcher:
    """Tests for RedisRateLimitWatcher."""

    def test_get_redis_key(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        assert watcher._get_redis_key("user123") == "rate_limited:test:user123"

    def test_is_cached_rate_limited_not_cached(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        assert watcher._is_cached_rate_limited("user123") is False

    def test_is_cached_rate_limited_valid(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        watcher._rate_limited_cache["user123"] = time.time() + 100
        assert watcher._is_cached_rate_limited("user123") is True

    def test_is_cached_rate_limited_expired(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        watcher._rate_limited_cache["user123"] = time.time() - 10  # expired
        assert watcher._is_cached_rate_limited("user123") is False
        assert "user123" not in watcher._rate_limited_cache

    async def test_is_rate_limited_false_when_not_cached(self) -> None:
        mock = _mock_redis(keys=[])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            result = await watcher.is_rate_limited("user123")
        assert result is False

    async def test_is_rate_limited_true_when_cached(self) -> None:
        mock = _mock_redis(keys=[])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            watcher._rate_limited_cache["user123"] = time.time() + 100
            watcher._initialized = True
            result = await watcher.is_rate_limited("user123")
        assert result is True

    async def test_set_rate_limited(self) -> None:
        mock = _mock_redis(keys=[])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            await watcher.set_rate_limited("user123")

        mock.set.assert_awaited_once_with("rate_limited:test:user123", "1", ex=60)
        mock.publish.assert_awaited_once()
        assert "user123" in watcher._rate_limited_cache

    async def test_remove_rate_limit(self) -> None:
        mock = _mock_redis(keys=[])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            watcher._rate_limited_cache["user123"] = time.time() + 100
            await watcher.remove_rate_limit("user123")

        mock.delete.assert_awaited_once_with("rate_limited:test:user123")
        mock.publish.assert_awaited_once()
        assert "user123" not in watcher._rate_limited_cache

    def test_clear_memory_cache(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        watcher._rate_limited_cache["a"] = time.time() + 100
        watcher._rate_limited_cache["b"] = time.time() + 100
        watcher.clear_memory_cache()
        assert watcher.get_memory_cache_size() == 0

    def test_get_memory_cache_size(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        assert watcher.get_memory_cache_size() == 0
        watcher._rate_limited_cache["a"] = time.time() + 100
        assert watcher.get_memory_cache_size() == 1

    async def test_stop_pubsub_listener_noop(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
        await watcher.stop_pubsub_listener()
        assert watcher._pubsub_task is None

    async def test_stop_pubsub_listener_cancels_task(self) -> None:
        watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")

        async def _hang_forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_hang_forever())
        watcher._pubsub_task = task
        await watcher.stop_pubsub_listener()
        assert watcher._pubsub_task is None
        assert task.cancelled()

    async def test_initialize_populates_cache_from_redis(self) -> None:
        mock = _mock_redis(keys=["rate_limited:test:user1", "rate_limited:test:user2"])
        mock.ttl.side_effect = [30, 0]  # user1 has TTL, user2 expired
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            await watcher.initialize()

        # NOTE: Production bug — initialize() stores full Redis keys (e.g.
        # "rate_limited:test:user1") but _is_cached_rate_limited() looks up by bare
        # identifier ("user1"). So data loaded by initialize() is unreachable via the
        # public is_rate_limited() API. This test documents the current (buggy) behavior.
        assert "rate_limited:test:user1" in watcher._rate_limited_cache
        assert "rate_limited:test:user2" not in watcher._rate_limited_cache

    async def test_initialize_only_once(self) -> None:
        mock = _mock_redis(keys=[])
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            watcher = RedisRateLimitWatcher(ttl_seconds=60, topic="test")
            await watcher.initialize()
            await watcher.initialize()  # second call should be no-op

        # keys() only called once
        mock.keys.assert_awaited_once()


# =============================================================================
# RedisTokenBucketRateLimiter
# =============================================================================


class TestRedisTokenBucketRateLimiter:
    """Tests for RedisTokenBucketRateLimiter."""

    def test_init_validates_limit(self) -> None:
        with pytest.raises(ValueError, match="limit must be positive"):
            RedisTokenBucketRateLimiter(limit=0, interval_seconds=60, redis_key_prefix="test")

    def test_init_validates_interval(self) -> None:
        with pytest.raises(ValueError, match="interval_seconds must be positive"):
            RedisTokenBucketRateLimiter(limit=10, interval_seconds=0, redis_key_prefix="test")

    def test_get_redis_key(self) -> None:
        limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
        assert limiter._get_redis_key("user1") == "rl:user1"

    async def test_is_allowed_returns_true(self) -> None:
        mock = _mock_redis(eval=1)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            assert await limiter.is_allowed("user1") is True

    async def test_is_allowed_returns_false(self) -> None:
        mock = _mock_redis(eval=0)
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            assert await limiter.is_allowed("user1") is False

    async def test_is_allowed_fails_open(self) -> None:
        mock = _mock_redis()
        mock.eval.side_effect = Exception("redis down")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            # Should return True (fail open) on error
            assert await limiter.is_allowed("user1") is True

    async def test_get_remaining_tokens_fresh(self) -> None:
        mock = _mock_redis()
        mock.hget.return_value = None  # no bucket yet
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            tokens = await limiter.get_remaining_tokens("user1")
        assert tokens == 10  # returns limit when no bucket exists

    async def test_get_remaining_tokens_existing_bucket(self) -> None:
        now = int(time.time())
        mock = _mock_redis(time=(now, 0))
        mock.hget.side_effect = [b"5", str(now - 30).encode()]  # 5 tokens, updated 30s ago
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            tokens = await limiter.get_remaining_tokens("user1")
        # 5 tokens + (30 seconds * 10 / 60) = 5 + 5 = 10, capped at limit * burst_ratio = 20
        assert tokens == 10

    async def test_get_remaining_tokens_error_returns_limit(self) -> None:
        mock = _mock_redis()
        mock.time.side_effect = Exception("redis down")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            limiter = RedisTokenBucketRateLimiter(limit=10, interval_seconds=60, redis_key_prefix="rl")
            tokens = await limiter.get_remaining_tokens("user1")
        assert tokens == 10


# =============================================================================
# track_hot_queries / get_hot_queries
# =============================================================================


class TestTrackHotQueries:
    """Tests for track_hot_queries decorator."""

    async def test_decorator_calls_function(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns")
            async def my_func(x: int) -> int:
                return x * 2

            result = await my_func(5)

        assert result == 10

    async def test_decorator_attaches_metadata(self) -> None:
        @track_hot_queries(namespace="test_ns", key_prefix="pfx")
        async def my_func(x: int) -> int:
            return x

        assert my_func.hot_queries_namespace == "test_ns"  # type: ignore[attr-defined]
        assert my_func.hot_queries_key_prefix == "pfx"  # type: ignore[attr-defined]

    async def test_decorator_tracks_in_redis(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns")
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            # Allow the fire-and-forget task to complete
            await _drain_background_tasks()

        mock.eval.assert_awaited()

    async def test_decorator_with_validator_skips_invalid(self) -> None:
        mock = _mock_redis()

        def reject_all(args: tuple, kwargs: dict) -> None:
            raise ValueError("rejected")

        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns", validator=reject_all)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            await _drain_background_tasks()

        # eval not called because validator rejected
        mock.eval.assert_not_awaited()

    async def test_decorator_with_settings_provider_disabled(self) -> None:
        mock = _mock_redis()

        async def disabled_settings() -> HotQuerySettings:
            return HotQuerySettings(enabled=False)

        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns", settings_provider=disabled_settings)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            await _drain_background_tasks()

        mock.eval.assert_not_awaited()

    async def test_decorator_with_settings_provider_custom_values(self) -> None:
        mock = _mock_redis()

        async def custom_settings() -> HotQuerySettings:
            return HotQuerySettings(enabled=True, ttl_days=3, max_entries=500)

        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns", settings_provider=custom_settings)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            await _drain_background_tasks()

        mock.eval.assert_awaited()

    async def test_decorator_with_settings_provider_error_falls_back(self) -> None:
        mock = _mock_redis()

        async def broken_settings() -> HotQuerySettings:
            raise RuntimeError("oops")

        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns", settings_provider=broken_settings)
            async def my_func(x: int) -> int:
                return x

            await my_func(1)
            await _drain_background_tasks()

        # Should still track with defaults
        mock.eval.assert_awaited()

    async def test_decorator_with_pydantic_model_arg(self) -> None:
        mock = _mock_redis()
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns")
            async def my_func(model: _SampleModel) -> int:
                return model.value

            await my_func(_SampleModel(value=7))
            await _drain_background_tasks()

        mock.eval.assert_awaited()

    async def test_decorator_with_value_normalizer(self) -> None:
        mock = _mock_redis()

        def custom_normalizer(v: Any) -> Any:
            if isinstance(v, _SampleModel):
                return {"v": v.value}
            return v

        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns", value_normalizer=custom_normalizer)
            async def my_func(model: _SampleModel) -> int:
                return model.value

            await my_func(_SampleModel(value=7))
            await _drain_background_tasks()

        mock.eval.assert_awaited()

    async def test_decorator_redis_error_does_not_break_function(self) -> None:
        mock = _mock_redis()
        mock.eval.side_effect = Exception("redis down")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):

            @track_hot_queries(namespace="test_ns")
            async def my_func(x: int) -> int:
                return x * 2

            result = await my_func(5)
            await _drain_background_tasks()

        assert result == 10  # function still works


class TestGetHotQueries:
    """Tests for get_hot_queries function."""

    async def test_no_data_returns_empty(self) -> None:
        mock = _mock_redis()
        mock.exists.return_value = 0
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns")
        assert results == []

    async def test_returns_hot_queries(self) -> None:
        mock = _mock_redis()
        # exists returns True for 2 daily keys
        mock.exists.side_effect = [1, 1, 0, 0, 0, 0, 0]
        mock.zcard.return_value = 5
        mock.zrevrange.return_value = ["hash1", "hash2"]
        mock.get.side_effect = [
            json.dumps({"args": [1], "kwargs": {}}),
            json.dumps({"args": [2], "kwargs": {"limit": 10}}),
        ]
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns", limit=10)

        assert len(results) == 2
        assert results[0]["args"] == [1]
        assert results[1]["kwargs"] == {"limit": 10}

    async def test_with_key_prefix(self) -> None:
        mock = _mock_redis()
        mock.exists.return_value = 0
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns", key_prefix="staging")
        assert results == []

    async def test_skips_invalid_json(self) -> None:
        mock = _mock_redis()
        mock.exists.side_effect = [1, 0, 0, 0, 0, 0, 0]
        mock.zcard.return_value = 2
        mock.zrevrange.return_value = ["hash1", "hash2"]
        mock.get.side_effect = [
            "not valid json{{{",
            json.dumps({"args": [], "kwargs": {}}),
        ]
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns")
        assert len(results) == 1

    async def test_missing_params_key_skipped(self) -> None:
        mock = _mock_redis()
        mock.exists.side_effect = [1, 0, 0, 0, 0, 0, 0]
        mock.zcard.return_value = 1
        mock.zrevrange.return_value = ["hash1"]
        mock.get.return_value = None  # params key missing
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns")
        assert results == []

    async def test_empty_zrevrange(self) -> None:
        mock = _mock_redis()
        mock.exists.side_effect = [1, 0, 0, 0, 0, 0, 0]
        mock.zcard.return_value = 0
        mock.zrevrange.return_value = []
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns")
        assert results == []

    async def test_redis_error_returns_empty(self) -> None:
        mock = _mock_redis()
        mock.exists.side_effect = Exception("redis down")
        with patch(f"{REDIS_UTILS_MODULE}.get_redis_client", AsyncMock(return_value=mock)):
            results = await get_hot_queries("test_ns")
        assert results == []
