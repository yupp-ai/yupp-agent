import asyncio
import contextlib
import inspect
import logging
import random
import re
import time
from collections import namedtuple
from collections.abc import Callable
from copy import deepcopy
from enum import Enum
from functools import _CacheInfo, cache, lru_cache, wraps
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Self, TypeVar, Union, cast, no_type_check

import numpy as np
import tiktoken
from cachetools.func import ttl_cache
from langchain_core.messages import BaseMessage, HumanMessage
from tenacity import RetryCallState, stop_after_attempt

from ypl.backend.utils.async_utils import create_background_task


class SingletonMixin:
    _instance: Self | None = None
    _instance_lock = Lock()

    @classmethod
    def get_instance(cls, *args: Any, **kwargs: Any) -> Self:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(*args, **kwargs)

        return cls._instance


class RNGMixin:
    """
    Mixin class to add a random number generator to a class.
    """

    _rng: np.random.RandomState | None = None
    _seed: int | None = None
    _lock: Lock = Lock()

    def set_seed(self, seed: int, overwrite_existing: bool = False) -> None:
        with self._lock:
            if overwrite_existing:
                self._seed = None
                self._rng = None

            if self._seed is not None:
                raise ValueError("Seed already set")

            self._seed = seed
            self._rng = np.random.RandomState(self._seed)

    def with_seed(self, seed: int) -> Self:
        self.set_seed(seed)
        return self

    def get_seed(self) -> int:
        if self._seed is None:
            raise ValueError("Seed not set")

        return self._seed

    def get_rng(self) -> np.random.RandomState:
        with self._lock:
            if self._rng is None:
                self._rng = np.random.RandomState(self._seed)

        return self._rng


K = TypeVar("K")
V = TypeVar("V")


FuncType = TypeVar("FuncType")


def ttl_cache_with_jitter(
    maxsize: int = 128,
    ttl: int = 600,
    timer: Callable[[], float] = time.monotonic,
    typed: bool = False,
    jitter: bool = True,
) -> Callable[[FuncType], FuncType]:
    ttl = int(ttl * random.uniform(0.85, 1.15)) if jitter else ttl
    return ttl_cache(maxsize, ttl, timer, typed)


@no_type_check
def async_timed_cache(*, seconds: int, maxsize: int = 128, jitter: bool = True) -> Callable[[FuncType], FuncType]:
    """
    Decorator to cache the result of a function for a given number of seconds. Each argument must be hashable. This is
    an LRU cache.

    Args:
        seconds: The number of seconds to cache the result for.
        maxsize: The maximum number of items to cache.
        jitter: Whether to add a random jitter of +/- 15% to the TTL.
    """

    # add some random jitter to avoid synchonized cache expiry
    seconds = seconds * random.uniform(0.85, 1.15) if jitter else seconds

    def decorator(func: FuncType) -> FuncType:
        hits = 0
        misses = 0
        refreshes = 0

        def _should_refresh(key: tuple[tuple, frozenset], time_added: float, seconds_since_added: float) -> bool:
            return False

        def set_should_refresh(should_refresh_func: Callable[[tuple[tuple, frozenset], float, float], bool]) -> None:
            nonlocal _should_refresh
            _should_refresh = should_refresh_func

        async def refresh_cache_entry(*args: Any, **kwargs: Any) -> None:
            key = wrapper.cache_key(*args, **kwargs)
            if key in wrapper.__is_refreshing:
                return
            wrapper.__is_refreshing[key] = True
            try:
                res = (await func(*args, **kwargs), time.time())
                wrapper.__cache_kv[key] = res
            finally:
                del wrapper.__is_refreshing[key]

        def _handle_cache_hit(
            key: tuple[tuple, frozenset], cache_time: float, age: float, args: tuple, kwargs: dict
        ) -> V:
            """Handle cache hit: update LRU ordering, check for refresh, and return cached value."""
            nonlocal hits, refreshes

            if age <= seconds:
                # Cache is valid - check if refresh is needed
                if _should_refresh(key, cache_time, age):
                    if not wrapper.__is_refreshing.get(key):
                        refreshes += 1
                        create_background_task(wrapper.refresh_cache_entry(*args, **kwargs))
                # Move to end for LRU
                wrapper.__cache_kv[key] = wrapper.__cache_kv.pop(key)
                hits += 1
                return wrapper.__cache_kv[key][0]

            # Cache expired - stale-while-revalidate: return stale value immediately
            stale_value = wrapper.__cache_kv[key][0]
            # Trigger background refresh if not already refreshing
            if not wrapper.__is_refreshing.get(key):
                refreshes += 1
                create_background_task(wrapper.refresh_cache_entry(*args, **kwargs))
            hits += 1
            return stale_value

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> V:
            nonlocal hits, misses, refreshes

            key = wrapper.cache_key(*args, **kwargs)
            now = time.time()

            # Fast path: Check cache without lock
            if key in wrapper.__cache_kv:
                cache_time = wrapper.__cache_kv[key][1]
                age = now - cache_time
                return _handle_cache_hit(key, cache_time, age, args, kwargs)

            # Slow path: Complete cache miss - acquire lock to prevent concurrent execution
            lock = wrapper.__locks.setdefault(key, asyncio.Lock())

            async with lock:
                # Double-check cache after acquiring lock (another coroutine may have populated it)
                if key in wrapper.__cache_kv:
                    cache_time = wrapper.__cache_kv[key][1]
                    age = now - cache_time
                    return _handle_cache_hit(key, cache_time, age, args, kwargs)

                # Complete cache miss - execute function with lock held
                misses += 1
                if len(wrapper.__cache_kv) >= maxsize:
                    min_key = next(iter(wrapper.__cache_kv))
                    del wrapper.__cache_kv[min_key]
                    # Clean up lock for evicted key
                    if min_key in wrapper.__locks:
                        del wrapper.__locks[min_key]

                wrapper.__cache_kv[key] = (await func(*args, **kwargs), now)
                return wrapper.__cache_kv[key][0]

        def cache_info() -> _CacheInfo:
            return _CacheInfo(hits=hits, misses=misses, maxsize=maxsize, currsize=len(wrapper.__cache_kv))

        _RefreshableCacheStats = namedtuple(
            "_RefreshableCacheStats",
            ["hits", "misses", "refreshes", "maxsize", "currsize"],
        )

        def cache_stats() -> _RefreshableCacheStats:
            nonlocal hits, misses, refreshes
            return _RefreshableCacheStats(
                hits=hits,
                misses=misses,
                refreshes=refreshes,
                maxsize=maxsize,
                currsize=len(wrapper.__cache_kv),
            )

        def cache_clear() -> None:
            nonlocal hits, misses, refreshes
            wrapper.__cache_kv.clear()
            wrapper.__locks.clear()
            hits = 0
            misses = 0
            refreshes = 0

        def cache_key(*args: Any, **kwargs: Any) -> tuple[tuple, frozenset]:
            return (args, frozenset(kwargs.items()))

        # values are tuples of (value, timestamp updated)
        # As of 3.6, dicts are now ordered
        wrapper.__cache_kv = {}
        wrapper.__is_refreshing = {}
        wrapper.__locks = {}  # Per-key locks to prevent concurrent execution
        wrapper.cache_info = cache_info
        wrapper.cache_clear = cache_clear
        wrapper.cache_key = cache_key
        wrapper.refresh_cache_entry = refresh_cache_entry
        wrapper.cache_stats = cache_stats
        wrapper.__wrapped__ = func
        wrapper.set_should_refresh = set_should_refresh
        return wrapper

    return decorator


def async_timed_cache_with_background_refresh(
    *, seconds: int, maxsize: int = 128, jitter: bool = True, refresh_thres: float = 0.95
) -> Callable[[FuncType], FuncType]:
    """
    Like async_timed_cache, but schedules a background refresh of keys once refresh_thres of the cache TTL has elapsed.
    """
    base_decorator = async_timed_cache(seconds=seconds, maxsize=maxsize, jitter=jitter)

    def decorator(func: FuncType) -> FuncType:
        wrapped = base_decorator(func)

        def should_refresh(key: tuple[tuple, frozenset], time_added: float, seconds_since_added: float) -> bool:
            return seconds_since_added >= refresh_thres * seconds

        wrapped.set_should_refresh(should_refresh)
        return wrapped  # type: ignore[no-any-return]

    return decorator


T = TypeVar("T")
Result = Union[T, Exception]  # noqa


class EarlyTerminatedException(Exception):
    pass


class Delegator:
    def __init__(
        self,
        delegates: dict[str, Any],
        timeout_secs: float | None = None,
        early_terminate_on: list[str] | None = None,
        priority_groups: list[list[str]] | None = None,
    ) -> None:
        """
        Creates a delegator that fans out method calls to underlying named objects.

        Args:
            delegates: Dictionary mapping names to delegate objects
            timeout: Timeout for method calls
            return_when: Whether to return the first result or all results
            early_terminate_on: List of delegate names that will trigger early termination
            priority_groups: groups of delegates for finer early termination control. You should only set
                either `early_terminate_on` or `priority_groups`, but not both.
        """
        self.delegates = delegates
        self.timeout_secs = timeout_secs
        assert not (early_terminate_on and priority_groups), (
            "Only one of early_terminate_on or priority_groups can be set"
        )

        self.priority_groups = priority_groups or list([list(early_terminate_on)] if early_terminate_on else [[]])

        # do some sanity checks
        self.priority_groups_set = {name for group in self.priority_groups for name in group}
        missing_names = self.priority_groups_set - set(self.delegates.keys())
        if missing_names:
            raise ValueError(
                f"early_terminate_on or priority_groups contains names that are not in delegates: {missing_names}"
            )

    def _can_early_terminate(self, name: str, results: dict[str, Result]) -> bool:
        """
        We can early terminate and cancel all pending tasks if:
        1. this name is in the first priority group, OR
        2. all tasks in all higher priority group's delegates have failed.
        """
        if len(self.priority_groups_set) == 0:
            # No priority groups, no early termination.
            return False

        # Find which priority group this name belongs to
        current_group_idx = next(i for i, group in enumerate(self.priority_groups) if name in group)
        if current_group_idx == 0:
            # First priority group - can terminate early
            return True

        # Check if all results from higher priority groups failed
        all_failed = True  # whether all higher priority tasks have failed
        for group_idx in range(current_group_idx):
            for delegate_name in self.priority_groups[group_idx]:
                if delegate_name not in results or not isinstance(results[delegate_name], Exception):
                    all_failed = False
                    break
            if not all_failed:
                break
        return all_failed

    async def delegate(self, method_name: str, *args: Any, **kwargs: Any) -> dict[str, Result]:
        """
        Delegates method calls to underlying objects, with optional timeout.

        Args:
            method_name: Name of the method to call on delegates
            *args, **kwargs: Arguments to pass to delegate methods

        Returns:
            Dictionary mapping delegate names to their results or exceptions
        """

        async def execute_method(name: str, delegate: Any) -> tuple[str, Result]:
            """Returns the name of the delegate and the result of the method call."""
            try:
                method = getattr(delegate, method_name)
                if inspect.iscoroutinefunction(method):
                    coro = method(*args, **kwargs)
                else:
                    coro = asyncio.to_thread(method, *args, **kwargs)

                result = await asyncio.wait_for(coro, self.timeout_secs)
                return name, result
            except asyncio.CancelledError:
                # Some other task cancelled this one
                return name, EarlyTerminatedException()
            except Exception as e:
                # Timeout, or underlying method actually raised
                return name, e

        try:
            pending = set()
            results = {}

            # Create tasks for all delegates
            for name, delegate in self.delegates.items():
                task = asyncio.create_task(execute_method(name, delegate))
                pending.add(task)

            # Keep processing until all tasks complete or early termination
            while pending:
                # Wait for any tasks to complete
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

                # Process completed tasks
                for task in done:
                    name, result = await task
                    results[name] = result
                    # Check if this result should trigger early termination
                    if (
                        name in self.priority_groups_set
                        and not isinstance(result, Exception)
                        and self._can_early_terminate(name, results)
                    ):
                        # Cancel remaining tasks
                        for task in pending:
                            task.cancel()
                        # Wait for cancellations to complete
                        if pending:
                            cancelled_done, pending = await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
                            # `pending` is now empty, as there is no timeout for wait().
                            for task in cancelled_done:
                                name, result = await task
                                results[name] = result
                        break

                    # TODO(Raghu): tweak: if all early_terminate_on models refuse, we wait for all of the remaining.
                    #              We only need one of the remaining to respond.

        except Exception as e:
            # Handle any unexpected errors
            for task in pending:
                if not task.done():
                    task.cancel()
            results.update({name: e for name in self.delegates if name not in results})

        # A catch-all for any unexpected errors that are not caught by execute_method()
        results.update({name: RuntimeError("Unexpected") for name in self.delegates if not results.get(name)})

        return results

    def __getattr__(self, name: str) -> Any:
        """Delegate any attribute access to the underlying delegates."""

        async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Result]:
            return await self.delegate(name, *args, **kwargs)

        return wrapper


@lru_cache(maxsize=1000)
def compiled_regex(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(pattern, flags)


def tiktoken_trim(
    text: str, max_length: int, *, model: str = "gpt-4o", direction: Literal["left", "right"] = "left"
) -> str:
    enc = tiktoken.encoding_for_model(model)
    encoded = enc.encode(text, disallowed_special=())
    match direction:
        case "left":
            return enc.decode(encoded[:max_length])
        case "right":
            return enc.decode(encoded[-max_length:])
        case _:
            raise ValueError(f"Invalid direction: {direction}")


def get_text_part(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    text_parts = [part["text"] for part in message.content if isinstance(part, dict) and part.get("type") == "text"]
    return "\n".join(text_parts)


def replace_text_part(message: BaseMessage, new_text: str) -> HumanMessage:
    if isinstance(message.content, str):
        return HumanMessage(content=new_text)
    new_content: list[str | dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, str):
            new_content.append(part)
            continue
        part = cast(dict[str, Any], part)
        if part["type"] != "text":
            new_content.append(part)
            continue
        new_content.append({"type": "text", "text": part["text"] + "\n" + new_text})
    return HumanMessage(content=new_content)


def ifnull[T](value: Any, default_value: T) -> T:
    """
    Similar to ifnull() in SQL. Returns the value if it is not None, otherwise returns the default value.
    This is useful for avoiding type check errors while accessing ORM fields which are often defined as 'type | None'.
    """
    return default_value if value is None else value


def extract_json_dict_from_text(text: str) -> str:
    """
    Returns the substring in text between first '{' and last '}', including the braces.
    This does not validate if the returned string is valid json.
    This is often used to extract json response from an LLM. This response can contain extra text
    before and after the actual json.

    If the braces are not found or not in the correct order, it returns the original text.
    It does not raise an error.
    """

    first, last = text.find("{"), text.rfind("}")
    if first == -1 or last == -1 or first > last:
        return text
    return text[first : last + 1]


MAX_LENGTH_PER_MESSAGE = 1024
MAX_HISTORY_MESSAGES = 5
MAX_SHORT_PROMPT_WORDS = 3
MAX_SHORT_PROMPT_CHARS = 15

_TRUNCATE_WITH = "... (truncated)"


def maybe_truncate_list(
    input: list[str],
    max_total_length: int,
    max_num_messages: int = MAX_HISTORY_MESSAGES,
    truncate_with: str = _TRUNCATE_WITH,
) -> list[str]:
    """Truncates the strings in the list with "... (truncated)" if the combined length of the strings are longer
    than `max_length`."""
    if not input:
        return input

    if len(input) > max_num_messages:
        # take the last max_num_messages messages
        input = input[-max_num_messages:]

    total_length = sum(len(s) for s in input)
    if total_length <= max_total_length:
        return input
    # This guarantees that the total length of the truncated strings is less than `max_length`.
    # As there are messages whose length is less than `per_message_max_length`, we can truncate the longer messages.
    per_message_max_length = max_total_length // len(input)
    if per_message_max_length <= len(truncate_with):
        raise ValueError(
            f"This is a bug. max_total_length is too short to truncate the list: {max_total_length}, "
            f"num_messages: {len(input)}, truncate with string length: {len(truncate_with)}."
        )

    ret = []
    for msg in input:
        if len(msg) <= per_message_max_length:
            ret.append(msg)
        else:
            ret.append(msg[: (per_message_max_length - len(truncate_with))] + truncate_with)

    return ret


def maybe_truncate(input: str, max_length: int, truncate_with: str = _TRUNCATE_WITH) -> str:
    """Truncates the string with "... (truncated)' if it is longer than `max_length`."""
    if input and len(input) > max_length:
        if max_length < len(truncate_with):  # in unlikely case when max_length < len(_TRUNCATE_WITH)
            return input[:max_length]
        return input[: (max_length - len(truncate_with))] + truncate_with
    return input


_TRUNCATE_HISTORY_WITH = "... (truncated prior {base_turn} user turns)\n"


def concatenate_after_maybe_truncate(
    messages: list[str], max_turn_length: int = MAX_LENGTH_PER_MESSAGE, max_turns: int = MAX_HISTORY_MESSAGES
) -> str:
    """Concatenates the messages after maybe truncating them."""
    prompt = ""
    base_turn = 0
    if max_turns == 1:
        return maybe_truncate(messages[-1], max_turn_length)
    if len(messages) > max_turns:
        base_turn = len(messages) - max_turns
        messages = messages[-max_turns:]
        prompt += _TRUNCATE_HISTORY_WITH.format(base_turn=base_turn)
    for i, message in enumerate(messages):
        prompt += f"<User Turn {base_turn + i + 1}>\n{maybe_truncate(message, max_turn_length)}\n</User Turn {base_turn + i + 1}>\n\n"  # noqa: E501
    return prompt.strip()


def coalesce(*args: bool | None) -> bool:
    for arg in args:
        if arg is not None:
            return arg
    return False


def not_empty(value: str | None) -> bool:
    """
    Return True if the value is not None and not empty, otherwise False.
    Note: do not use this function for streaming chunks, as streaming chunks could have content
    that contains only whitespace characters.
    """
    return value is not None and value.strip() != ""


def to_float(value: Any | None, default: float) -> float:
    if value is not None:
        return float(value)
    return default


def parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


E = TypeVar("E", bound=Enum)


def validate_all_enums_are_defined_in_dict(enum_class: type[E], mapping: dict[E, Any], dict_name: str) -> dict[E, Any]:
    """
    Validate that a dictionary contains all enum values.
    This is useful for validating that a dictionary contains all enum values,
    especially for constant dictionaries that use enums.
    When a new enum value is added, this will raise an error that the enum value is not in the dictionary.

    Args:
        enum_class: The enum class to validate against.
        mapping: The dictionary to validate.
        dict_name: The name of the dictionary to validate.

    Returns:
        The mapping if it is valid.

    Raises:
        ValueError: If the mapping is missing any enum values.
    """
    missing_keys = set(enum_class) - set(mapping.keys())
    if missing_keys:
        missing_values = [key.value for key in missing_keys]
        raise ValueError(f"{dict_name} is missing keys for enum values: {missing_values}")
    return mapping


def get_fully_qualified_name_of_callable(cb: Callable[..., Any]) -> str:
    """Get the fully-qualified name of the callable.

    Copied from tenacity._utils.get_callback_name.
    """
    segments = []
    try:
        segments.append(cb.__qualname__)
    except AttributeError:
        with contextlib.suppress(AttributeError):
            segments.append(cb.__name__)
    if not segments:
        return repr(cb)
    try:
        # When running under sphinx it appears this can be none?
        if cb.__module__:
            segments.insert(0, cb.__module__)
    except AttributeError:
        pass
    return ".".join(segments)


def log_retry_attempt(
    logger: logging.Logger, log_level: int, sec_format: str = "%0.3f", finally_log_error: bool = True
) -> Callable[[RetryCallState], None]:
    """After call strategy that logs to some logger the finished attempt.

    Most of the code is copied from tenacity.after_log.

    Args:
        logger: The logger to log to.
        log_level: The log level to log at.
        sec_format: The format string for the seconds since start.
        finally_log_error: Whether to log the error on the last attempt.

    Returns:
        A function that logs the retry attempt, and optionally also logs the error on the last attempt.
    """

    def log_it(retry_state: RetryCallState) -> None:
        fn_name = "<unknown>" if retry_state.fn is None else get_fully_qualified_name_of_callable(retry_state.fn)
        if (
            finally_log_error
            and isinstance(retry_state.retry_object.stop, stop_after_attempt)
            and retry_state.attempt_number >= retry_state.retry_object.stop.max_attempt_number
            and retry_state.outcome is not None
            and retry_state.outcome.exception() is not None
        ):
            logger.log(
                logging.ERROR,
                f"Error in call to '{fn_name}', this was attempt#{retry_state.attempt_number}, and the last one.",
                exc_info=retry_state.outcome.exception(),
            )
            # Return to avoid logging twice.
            return
        logger.log(
            log_level,
            f"Finished call to '{fn_name}' "
            f"after {sec_format % retry_state.seconds_since_start}(s), "
            f"this was attempt#{retry_state.attempt_number}. Will try again.",
        )

    return log_it


def deep_merge_dicts(dict1: dict[str, Any], dict2: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries, preserving nested structures.

    Args:
        dict1: The base dictionary
        dict2: The dictionary to merge into dict1

    Returns:
        dict[str, Any]: The merged dictionary
    """
    result = deepcopy(dict1)

    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)

    return result


def is_short_prompt(
    prompt: str, max_words: int = MAX_SHORT_PROMPT_WORDS, max_chars: int = MAX_SHORT_PROMPT_CHARS
) -> tuple[bool, int, int]:
    """Determines if a prompt is considered "short" based on word and character count thresholds.

    Args:
        prompt: The text prompt to evaluate
        max_words: Maximum number of words for a prompt to be considered short
        max_chars: Maximum number of characters for a prompt to be considered short

    Returns:
        tuple[bool, int, int]: A tuple containing:
            - is_short: Whether the prompt is considered short
            - num_words: The number of words in the prompt
            - num_chars: The number of characters in the prompt (excluding spaces)
    """
    if not prompt:
        return True, 0, 0
    num_chars = len(prompt.replace(" ", ""))
    num_words = len(prompt.split())
    is_short_prompt = num_words < max_words and num_chars < max_chars
    return is_short_prompt, num_words, num_chars


@cache
def find_repo_root() -> Path:
    """Find the repository root by searching for .git or pyproject.toml starting from this file's location."""
    path = Path(__file__).resolve()
    for p in [path, *path.parents]:
        if (p / ".git").exists() or (p / "pyproject.toml").exists():
            return p
    raise FileNotFoundError("Could not find repository root.")
