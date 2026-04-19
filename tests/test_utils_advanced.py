"""Additional unit tests for ypl/utils.py — covering classes and async helpers
not covered by test_utils.py: SingletonMixin, RNGMixin, compiled_regex,
to_float, get_text_part, replace_text_part, async_timed_cache, Delegator.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from ypl.utils import (
    Delegator,
    EarlyTerminatedException,
    RNGMixin,
    SingletonMixin,
    async_timed_cache,
    async_timed_cache_with_background_refresh,
    compiled_regex,
    get_text_part,
    replace_text_part,
    to_float,
)

# ---------------------------------------------------------------------------
# SingletonMixin
# ---------------------------------------------------------------------------


class TestSingletonMixin:
    def setup_method(self) -> None:
        # Reset singleton state between tests
        _FreshSingleton._instance = None  # type: ignore[misc]

    def test_get_instance_returns_same_object(self) -> None:
        a = _FreshSingleton.get_instance()
        b = _FreshSingleton.get_instance()
        assert a is b

    def test_singleton_is_instance_of_class(self) -> None:
        obj = _FreshSingleton.get_instance()
        assert isinstance(obj, _FreshSingleton)

    def test_separate_subclasses_have_separate_singletons(self) -> None:
        _AnotherSingleton._instance = None  # type: ignore[misc]
        a = _FreshSingleton.get_instance()
        b = _AnotherSingleton.get_instance()
        assert id(a) != id(b)


class _FreshSingleton(SingletonMixin):
    pass


class _AnotherSingleton(SingletonMixin):
    pass


# ---------------------------------------------------------------------------
# RNGMixin
# ---------------------------------------------------------------------------


class TestRNGMixin:
    def _new_rng(self) -> RNGMixin:
        obj = RNGMixin()
        obj._seed = None
        obj._rng = None
        return obj

    def test_set_seed_and_get_seed(self) -> None:
        rng = self._new_rng()
        rng.set_seed(42)
        assert rng.get_seed() == 42

    def test_get_rng_returns_random_state(self) -> None:
        import numpy as np

        rng = self._new_rng()
        rng.set_seed(7)
        assert isinstance(rng.get_rng(), np.random.RandomState)

    def test_get_seed_raises_when_not_set(self) -> None:
        rng = self._new_rng()
        with pytest.raises(ValueError, match="Seed not set"):
            rng.get_seed()

    def test_set_seed_twice_raises(self) -> None:
        rng = self._new_rng()
        rng.set_seed(1)
        with pytest.raises(ValueError, match="Seed already set"):
            rng.set_seed(2)

    def test_set_seed_with_overwrite(self) -> None:
        rng = self._new_rng()
        rng.set_seed(1)
        rng.set_seed(2, overwrite_existing=True)
        assert rng.get_seed() == 2

    def test_with_seed_returns_self(self) -> None:
        rng = self._new_rng()
        result = rng.with_seed(99)
        assert result is rng
        assert rng.get_seed() == 99

    def test_rng_initialized_on_first_get(self) -> None:
        rng = self._new_rng()
        rng._seed = 5  # Set seed without calling set_seed
        rng._rng = None
        rng_obj = rng.get_rng()
        assert rng_obj is not None


# ---------------------------------------------------------------------------
# compiled_regex
# ---------------------------------------------------------------------------


class TestCompiledRegex:
    def test_returns_compiled_pattern(self) -> None:
        import re

        pat = compiled_regex(r"\d+")
        assert isinstance(pat, re.Pattern)

    def test_caches_same_pattern(self) -> None:
        p1 = compiled_regex(r"[a-z]+")
        p2 = compiled_regex(r"[a-z]+")
        assert p1 is p2  # Same object from LRU cache

    def test_different_patterns_are_different(self) -> None:
        p1 = compiled_regex(r"\d+")
        p2 = compiled_regex(r"\w+")
        assert p1 is not p2

    def test_flags_respected(self) -> None:
        import re

        p = compiled_regex("hello", re.IGNORECASE)
        assert p.match("HELLO") is not None

    def test_match_works(self) -> None:
        p = compiled_regex(r"^\d{4}$")
        assert p.match("1234") is not None
        assert p.match("abc") is None


# ---------------------------------------------------------------------------
# to_float
# ---------------------------------------------------------------------------


class TestToFloat:
    def test_non_none_value_converted(self) -> None:
        assert to_float(3, 0.0) == 3.0

    def test_string_number_converted(self) -> None:
        assert to_float("2.5", 0.0) == 2.5

    def test_none_returns_default(self) -> None:
        assert to_float(None, 99.9) == 99.9

    def test_zero_is_not_none(self) -> None:
        assert to_float(0, 42.0) == 0.0


# ---------------------------------------------------------------------------
# get_text_part / replace_text_part
# ---------------------------------------------------------------------------


class TestGetTextPart:
    def test_string_content(self) -> None:
        msg = HumanMessage(content="hello world")
        assert get_text_part(msg) == "hello world"

    def test_list_content_extracts_text(self) -> None:
        msg = HumanMessage(content=[{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}])
        result = get_text_part(msg)
        assert "part one" in result
        assert "part two" in result

    def test_list_content_skips_non_text(self) -> None:
        msg = HumanMessage(
            content=[
                {"type": "image_url", "image_url": "http://example.com/img.png"},
                {"type": "text", "text": "caption"},
            ]
        )
        result = get_text_part(msg)
        assert result == "caption"


class TestReplaceTextPart:
    def test_string_content_replaced(self) -> None:
        msg = HumanMessage(content="old text")
        result = replace_text_part(msg, "new text")
        assert isinstance(result, HumanMessage)
        assert result.content == "new text"

    def test_list_content_text_appended(self) -> None:
        msg = HumanMessage(content=[{"type": "text", "text": "original"}])
        result = replace_text_part(msg, "appended")
        assert isinstance(result, HumanMessage)
        assert isinstance(result.content, list)
        combined_text = result.content[0]["text"]  # type: ignore[index]
        assert "original" in combined_text
        assert "appended" in combined_text

    def test_non_text_parts_preserved(self) -> None:
        img_part = {"type": "image_url", "image_url": "http://img.example.com/x.png"}
        msg = HumanMessage(content=[img_part, {"type": "text", "text": "caption"}])
        result = replace_text_part(msg, "extra")
        assert isinstance(result.content, list)
        # image part should be in the output unchanged
        types = [p["type"] for p in result.content]  # type: ignore[index]
        assert "image_url" in types


# ---------------------------------------------------------------------------
# async_timed_cache
# ---------------------------------------------------------------------------


class TestAsyncTimedCache:
    async def test_cache_miss_calls_function(self) -> None:
        call_count = 0

        @async_timed_cache(seconds=60, jitter=False)
        async def fn() -> int:
            nonlocal call_count
            call_count += 1
            return 42

        fn.cache_clear()
        result = await fn()
        assert result == 42
        assert call_count == 1

    async def test_cache_hit_does_not_call_function(self) -> None:
        call_count = 0

        @async_timed_cache(seconds=60, jitter=False)
        async def fn2() -> int:
            nonlocal call_count
            call_count += 1
            return 99

        fn2.cache_clear()
        await fn2()
        await fn2()
        assert call_count == 1

    async def test_cache_clear_resets_stats(self) -> None:
        @async_timed_cache(seconds=60, jitter=False)
        async def fn3() -> str:
            return "hello"

        fn3.cache_clear()
        await fn3()
        info = fn3.cache_info()
        assert info.misses == 1
        fn3.cache_clear()
        info = fn3.cache_info()
        assert info.hits == 0
        assert info.misses == 0

    async def test_cache_with_args(self) -> None:
        call_count = 0

        @async_timed_cache(seconds=60, jitter=False)
        async def fn_args(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        fn_args.cache_clear()
        r1 = await fn_args(5)
        r2 = await fn_args(5)
        r3 = await fn_args(10)
        assert r1 == 10
        assert r2 == 10
        assert r3 == 20
        assert call_count == 2  # (5) cached, (10) is a new key

    async def test_cache_expired_refetches(self) -> None:
        call_count = 0

        @async_timed_cache(seconds=0, jitter=False)
        async def expiring_fn() -> int:
            nonlocal call_count
            call_count += 1
            return call_count

        expiring_fn.cache_clear()
        await expiring_fn()
        await asyncio.sleep(0.01)  # Let cache expire (ttl=0)
        # async_timed_cache uses stale-while-revalidate: on expiry the second call
        # returns the stale cached value (1) immediately and enqueues a background refresh.
        result = await expiring_fn()
        assert result == 1  # stale cached value returned, background refresh enqueued

    async def test_maxsize_evicts_old_entries(self) -> None:
        @async_timed_cache(seconds=60, maxsize=2, jitter=False)
        async def limited_fn(x: int) -> int:
            return x

        limited_fn.cache_clear()
        await limited_fn(1)
        await limited_fn(2)
        await limited_fn(3)  # This should evict the oldest entry
        info = limited_fn.cache_info()
        assert info.currsize <= 2


# ---------------------------------------------------------------------------
# async_timed_cache_with_background_refresh
# ---------------------------------------------------------------------------


class TestAsyncTimedCacheWithBackgroundRefresh:
    async def test_basic_caching_works(self) -> None:
        call_count = 0

        @async_timed_cache_with_background_refresh(seconds=60, jitter=False)
        async def bg_fn() -> int:
            nonlocal call_count
            call_count += 1
            return call_count

        bg_fn.cache_clear()  # type: ignore[attr-defined]
        r1 = await bg_fn()
        r2 = await bg_fn()
        assert r1 == 1
        assert r2 == 1  # Cache hit


# ---------------------------------------------------------------------------
# Delegator
# ---------------------------------------------------------------------------


class _SimpleDelegate:
    def __init__(self, name: str, return_val: Any, fail: bool = False, delay: float = 0) -> None:
        self.name = name
        self._return_val = return_val
        self._fail = fail
        self._delay = delay

    async def do_work(self) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError(f"{self.name} failed")
        return self._return_val


class TestDelegator:
    async def test_basic_delegation(self) -> None:
        delegates = {
            "a": _SimpleDelegate("a", "result_a"),
            "b": _SimpleDelegate("b", "result_b"),
        }
        d = Delegator(delegates)
        results = await d.delegate("do_work")
        assert results["a"] == "result_a"
        assert results["b"] == "result_b"

    async def test_failed_delegate_returns_exception(self) -> None:
        delegates = {
            "ok": _SimpleDelegate("ok", "fine"),
            "bad": _SimpleDelegate("bad", None, fail=True),
        }
        d = Delegator(delegates)
        results = await d.delegate("do_work")
        assert results["ok"] == "fine"
        assert isinstance(results["bad"], RuntimeError)

    async def test_early_terminate_on_first_success(self) -> None:
        """early_terminate_on: when 'fast' succeeds, 'slow' is cancelled."""
        delegates = {
            "fast": _SimpleDelegate("fast", "fast-result"),
            "slow": _SimpleDelegate("slow", "slow-result", delay=0.5),  # short delay: only needs to outlast 'fast'
        }
        d = Delegator(delegates, early_terminate_on=["fast"])
        results = await d.delegate("do_work")
        assert results["fast"] == "fast-result"
        # slow should be cancelled (EarlyTerminatedException)
        assert isinstance(results["slow"], EarlyTerminatedException | asyncio.CancelledError)

    async def test_early_terminate_on_requires_success(self) -> None:
        """If the early-terminate delegate fails, we wait for others."""
        delegates = {
            "primary": _SimpleDelegate("primary", None, fail=True),
            "secondary": _SimpleDelegate("secondary", "second"),
        }
        d = Delegator(delegates, early_terminate_on=["primary"])
        results = await d.delegate("do_work")
        # primary failed — secondary should still complete
        assert isinstance(results["primary"], Exception)
        assert results["secondary"] == "second"

    async def test_timeout_returns_exception(self) -> None:
        delegates = {
            "slow": _SimpleDelegate("slow", "never", delay=10),
        }
        d = Delegator(delegates, timeout_secs=0.01)
        results = await d.delegate("do_work")
        assert isinstance(results["slow"], Exception)

    async def test_priority_groups_invalid_name_raises(self) -> None:
        with pytest.raises(ValueError, match="not in delegates"):
            Delegator(
                {"a": _SimpleDelegate("a", 1)},
                priority_groups=[["nonexistent"]],
            )

    async def test_cannot_set_both_early_terminate_and_priority_groups(self) -> None:
        delegates = {"a": _SimpleDelegate("a", 1), "b": _SimpleDelegate("b", 2)}
        with pytest.raises(AssertionError):
            Delegator(
                delegates,
                early_terminate_on=["a"],
                priority_groups=[["b"]],
            )

    async def test_getattr_delegates_method(self) -> None:
        """__getattr__ should create a wrapper that calls delegate(name, ...)."""
        delegates = {"x": _SimpleDelegate("x", "via_attr")}
        d = Delegator(delegates)
        results = await d.do_work()
        assert results["x"] == "via_attr"

    async def test_sync_method_delegated_via_thread(self) -> None:
        class SyncDelegate:
            def do_work(self) -> str:
                return "sync_result"

        delegates = {"s": SyncDelegate()}
        d = Delegator(delegates)
        results = await d.delegate("do_work")
        assert results["s"] == "sync_result"

    async def test_all_results_covered_even_on_unexpected_error(self) -> None:
        """No delegate name should be missing from results."""
        delegates = {
            "a": _SimpleDelegate("a", 1),
            "b": _SimpleDelegate("b", 2, fail=True),
            "c": _SimpleDelegate("c", 3),
        }
        d = Delegator(delegates)
        results = await d.delegate("do_work")
        for name in delegates:
            assert name in results
