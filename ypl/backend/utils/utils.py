import asyncio
import math
import re
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from enum import Enum
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import httpx
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ypl.backend.db import get_async_engine
from ypl.backend.utils.monitoring import metric_record
from ypl.db.users import (
    User,
    WaitlistedUser,
)
from ypl.structured_logger import get_logger

logger = get_logger()

UNKNOWN_USER = "Unknown User"
WAITLISTED_SUFFIX = " (**Waitlisted**)"
PAYMENT_TOS_VIOLATION_TEXT = (
    "This payment was not processed because our automated systems detected potential violation of our Terms of Service."
)

# Retryable exceptions for HTTPX client (network/transport errors only)
# TransportError covers: TimeoutException, NetworkError, ProtocolError, ProxyError, UnsupportedProtocol
# See: https://www.python-httpx.org/exceptions/
# Note: HTTPStatusError is intentionally excluded - handle it per call site
# since 4xx errors should not be retried, only 5xx in some cases
HTTPX_RETRYABLE_EXCEPTIONS = (httpx.TransportError, ssl.SSLError)


async def fetch_user_names(user_ids: list[str]) -> dict[str, str]:
    """Fetch multiple user names from the database in a single query."""
    try:
        engine = get_async_engine()
        async with AsyncSession(engine) as session:
            # First try to get names from users table
            query = select(User).where(
                col(User.user_id).in_(user_ids),
            )
            users = (await session.exec(query)).all()

            name_dict = {user_id: user_id for user_id in user_ids}
            name_dict.update({user.user_id: str(user.name) for user in users if user.name})

            # For any remaining user_ids without names, check waitlisted_users table
            remaining_ids = [uid for uid in user_ids if name_dict[uid] == uid]
            if remaining_ids:
                waitlist_query = select(WaitlistedUser).where(
                    col(WaitlistedUser.waitlisted_user_id).in_([cast(uuid.UUID, uid) for uid in remaining_ids]),
                )
                waitlisted_users = (await session.exec(waitlist_query)).all()

                # Update names from waitlisted users if found, adding the waitlisted suffix
                name_dict.update(
                    {
                        str(waitlisted_user.waitlisted_user_id): WAITLISTED_SUFFIX + str(waitlisted_user.name)
                        for waitlisted_user in waitlisted_users
                        if waitlisted_user.name
                    }
                )

            return name_dict

    except Exception:
        logger.exception("Failed to fetch users from database", exc_info=True)
        return {user_id: user_id for user_id in user_ids}


async def fetch_user_name(user_id: str) -> str:
    """Fetch a single user name from the database."""
    try:
        engine = get_async_engine()
        async with AsyncSession(engine) as session:
            # First try users table
            query = select(User).where(
                User.user_id == user_id,
            )
            user = (await session.exec(query)).first()

            if user and user.name:
                return str(user.name)

            # If not found or no name, check waitlisted_users table
            waitlist_query = select(WaitlistedUser).where(
                WaitlistedUser.waitlisted_user_id == cast(uuid.UUID, user_id),
            )
            waitlisted_user = (await session.exec(waitlist_query)).first()

            if waitlisted_user and waitlisted_user.name:
                return WAITLISTED_SUFFIX + str(waitlisted_user.name)

            return UNKNOWN_USER

    except Exception:
        logger.exception("Failed to fetch user name from database", exc_info=True)
        return UNKNOWN_USER


class CapabilityType(str, Enum):
    """Enum representing different capability types in the system."""

    CASHOUT = "cashout"
    REFERRAL = "referral"
    WIN_REWARDS = "win_rewards"
    PROVIDE_APP_FEEDBACK = "provide_app_feedback"
    PROVIDE_MODEL_FEEDBACK = "provide_model_feedback"
    PROVIDE_NOPE_FEEDBACK = "provide_nope_feedback"


class DeviceIdSource(str, Enum):
    """Source of a device identifier."""

    AMPLITUDE = "AMPLITUDE"
    YUPP = "YUPP"
    SARDINE = "SARDINE"


class StopWatch:
    """
    StopWatch is a utility class for recording the time taken to execute a block of code.
    You can use it as a context manager or manually record splits.

        with StopWatch("latency/my_function"):
        x = my_function()

    Or use it manually, with a few splits in between:

        stopwatch = StopWatch()
        doStepA()
        stopwatch.record_split("step_a")
        doStepB()
        stopwatch.record_split("step_b")
        doStepC()
        stopwatch.end("step_c")
        stopwatch.export_metrics("latency/")  # split names will be appended to the prefix when exporting metrics

    You can also use it record laps with a start and end point you care about, rather than calculating from last split.

        stopwatch = StopWatch("latency/", auto_export=True)  # will export metrics when .end() is called
        doStuff()
        stopwatch.start_lap("core_step")
        doCoreStep()
        stopwatch.end_lap("core_step")
        doMoreStuff()
        stopwatch.end("more_stuff")
    """

    TOTAL_KEY = "TOTAL"

    def __init__(self, name: str | None = None, auto_export: bool = False) -> None:
        self.name = name
        self.split_start_time = time.time() * 1000
        self.stopwatch_start_time = self.split_start_time  # the overall start time
        self.splits: dict[str, int] = {}
        self.lap_starts: dict[str, float] = {}
        self.ended = False
        self.auto_export = auto_export

    def __enter__(self) -> "StopWatch":
        # nothing really to do here
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.end()
        if self.auto_export:
            self.export_metrics()
        # self.pretty_print()  # uncomment this line for debugging purposes

    # Recording time for specific laps with its own start and end

    def get_start_time(self) -> float:
        """Return the start time of the stopwatch."""
        return self.stopwatch_start_time

    def get_end_time(self) -> float:
        """Return the end time of the stopwatch."""
        return time.time() * 1000

    def record_split(self, split_name: str) -> None:
        """Record time since last split and update start time."""
        current = time.time() * 1000
        self.splits[split_name] = int(current - self.split_start_time)
        self.split_start_time = current

    def end(self, last_split_name: str | None = None) -> None:
        """Record final split and total time."""
        if not self.ended:
            if last_split_name:
                self.record_split(last_split_name)
            self.splits[self.TOTAL_KEY] = int(time.time() * 1000 - self.stopwatch_start_time)
            self.ended = True
            if self.auto_export:
                self.export_metrics()

    # Recording time for specific laps with its own start and end

    def start_lap(self, lap_name: str) -> None:
        """Start a new lap with the given name."""
        self.lap_starts[lap_name] = time.time() * 1000

    def end_lap(self, lap_name: str) -> None:
        """End a lap and record its duration."""
        if lap_name not in self.lap_starts:
            raise ValueError(f"No lap named '{lap_name}' was started")
        current = time.time() * 1000
        self.splits[lap_name] = int(current - self.lap_starts[lap_name])
        del self.lap_starts[lap_name]

    # Getting results

    def get_total_time(self) -> int:
        """Return total time in milliseconds."""
        if not self.ended:
            return int(time.time() * 1000 - self.stopwatch_start_time)
        return self.splits[self.TOTAL_KEY]

    def get_splits(self) -> dict[str, int]:
        """Return a copy of the dictionary of all recorded splits."""
        return self.splits.copy()

    def pretty_print(self, print_total: bool = True) -> None:
        """Print splits one per row with millisecond suffix."""
        print(f"StopWatch results: {self.name}")
        for split_name, duration in self.splits.items():
            if not print_total and split_name == self.TOTAL_KEY:
                continue
            print(f"-- {split_name:40} {int(duration):8} ms")

    def export_metrics(self, prefix: str | None = None) -> None:
        """Export all splits as metrics with the given prefix."""
        if prefix is None:
            prefix = self.name or ""

        for split_name, duration in self.splits.items():
            metric_record(f"{prefix}{split_name}_ms", duration)


async def yield_all(async_iter: AsyncIterator[Any]) -> AsyncIterator[Any]:
    async for msg in async_iter:
        yield msg


def merge_base_url_with_port(base_url: str, port: int | None) -> str:
    """Merge a base URL with a port number"""
    if not port:
        return base_url
    parsed_url = urlparse(base_url)
    netloc = f"{parsed_url.hostname}:{port}"
    return urlunparse(
        (
            parsed_url.scheme,
            netloc,
            parsed_url.path,
            parsed_url.params,
            parsed_url.query,
            parsed_url.fragment,
        )
    )


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance between two points on the earth (specified in decimal degrees).

    Args:
        lat1: Latitude of first point in decimal degrees
        lon1: Longitude of first point in decimal degrees
        lat2: Latitude of second point in decimal degrees
        lon2: Longitude of second point in decimal degrees

    Returns:
        Distance in kilometers between the two points
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    # Radius of earth in kilometers
    r = 6371

    return c * r


class AsyncString:
    """a string safe to update in various asyncio tasks"""

    def __init__(self, initial: str = "") -> None:
        self._value = initial
        self._lock = asyncio.Lock()

    async def append(self, suffix: str) -> None:
        async with self._lock:
            self._value += suffix

    async def get(self) -> str:
        async with self._lock:
            return self._value

    async def set(self, new_val: str) -> None:
        async with self._lock:
            self._value = new_val


class AsyncTask:
    """
    A task safe to start by different asyncio tasks, and it will start only once.
    If can be cancelled even before it starts (so it won't start at all).
    """

    def __init__(self, name: str | None = None) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._name = name
        self._cancelled = False

    async def start(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> None:
        async with self._lock:
            if self._task is None and not self._cancelled:
                self._task = asyncio.create_task(func(*args, **kwargs))  # type: ignore

    def cancel(self) -> None:
        self._cancelled = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def get_task(self) -> asyncio.Task | None:
        return self._task

    async def wait(self) -> None:
        if self._task is not None and not self._task.done():
            await self._task

    def done(self) -> bool:
        """Note that the job is considered done if it's cancelled or never started."""
        return self._cancelled or (self._task.done() if self._task else True)


def sanitize_search_query(query: str) -> str:
    """
    Sanitize a search query for PostgreSQL text search.

    Args:
        query: The raw search query string

    Returns:
        A sanitized query string safe for use with PostgreSQL text search
    """
    # Keep only valid escape sequences (\n, \t, \\) and remove invalid ones
    query = re.sub(r"\\(?![\n\t\\])", "", query)

    # Basic cleanup - remove excessive whitespace and normalize
    query = " ".join(query.split())

    return query.strip()


# Pattern: must contain at least one digit, only alphanumeric lowercase + dash + underscore, min 24 chars
REQUEST_PATH_ID_RE = re.compile(r"^(?=.*\d)[a-z0-9\-_]{24,}$")


def sanitize_request_path(path: str) -> str:
    """Sanitize the request path and replace ID-like parts with a placeholder "ID"."""
    parts = path.strip("/").split("/")
    return "_".join(["ID" if REQUEST_PATH_ID_RE.fullmatch(p) else p for p in parts]) or "root"


def recursive_to_dict(obj: Any) -> Any:
    """
    Recursively convert an object (with __dict__) or a dict or a list to a dict, preserving nested fields.
    """
    if isinstance(obj, dict):
        return {k: recursive_to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: recursive_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [recursive_to_dict(i) for i in obj]
    return obj


def safe_decimal_equal(
    a: Decimal | None,
    b: Decimal | None,
    *,
    is_near_match: bool = False,
) -> bool:
    """Safely compare two Decimal values.

    When ``is_near_match`` is False (default), performs strict equality (``a == b``).
    When ``is_near_match`` is True, applies a tolerance-based comparison using fixed
    tolerances: relative 1.00% (1e-2) and absolute 1e-8, with reference ``|b|``.

    Args:
        a: First value to compare (actual). If None, returns False.
        b: Second value to compare (expected). If None, returns False.
        is_near_match: Whether to allow a tolerance-based near match.

    Returns:
        True if values match according to the selected strategy, otherwise False.
    """
    if a is None or b is None:
        return False

    if not is_near_match:
        return a == b

    rel_tol = Decimal("1e-2")  # 1.00%
    abs_tol = Decimal("1e-8")

    diff = (a - b).copy_abs()
    reference = b.copy_abs()
    threshold = max(abs_tol, rel_tol * reference)
    return diff <= threshold
