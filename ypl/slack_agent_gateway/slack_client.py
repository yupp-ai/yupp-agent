"""Universal rate-limited Slack Web API client.

Every outbound Slack call in SAG goes through ``build_slack_client`` instead
of constructing ``AsyncWebClient`` directly. Two layers of rate-limit
protection stack:

1. **Preventive** — before the call, a Redis ``SET NX EX`` gate keyed on
   ``(app_id, method)`` (and ``channel`` for ``chat.postMessage``) ensures we
   don't exceed Slack's published limits (Tier 3 = 50+/min ≈ 1.2s, plus
   ``chat.postMessage``'s per-channel 1/sec). Scope matches Slack's
   docs: limits are per (app × workspace × method). ``app_id`` uniquely
   identifies the Slack app, so it implies the workspace.

2. **Reactive** — on 429, either a) retry in-flight honoring the
   ``Retry-After`` header (for one-shot callers whose content is fixed), or
   b) raise ``RateLimitDeferred`` so accumulable callers (the buffer flush)
   can re-enqueue and let content continue accumulating.

Two ``retry_mode`` values pick the reactive behavior:

- ``"inflight"`` — default. Attaches ``slack_sdk``'s
  ``AsyncRateLimitErrorRetryHandler`` which sleeps ``Retry-After`` and retries
  with the same payload. Correct for fixed, one-shot payloads.
- ``"reenqueue"`` — no auto-retry. On 429, raise ``RateLimitDeferred`` with
  ``retry_after`` so ``flush_buffer`` can re-append the buffer and reschedule.

The wrapper proxies any method via ``__getattr__``, so new Slack methods work
without code changes — they just go through the gate by name.
"""

from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.async_handler import AsyncRetryHandler
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from ypl.slack_agent_gateway.constants import SLACK_RATELIMIT_MAX_RETRY_AFTER_SECONDS
from ypl.slack_agent_gateway.redis_client import try_acquire_slack_ratelimit
from ypl.structured_logger import get_logger

logger = get_logger()

RetryMode = Literal["inflight", "reenqueue"]


class RateLimitDeferred(Exception):
    """Raised in ``retry_mode='reenqueue'`` when Slack returns 429.

    The caller (typically ``flush_buffer``) catches this, re-adds its payload
    to the buffer, and reschedules the flush at ``now + retry_after``. New
    content that accumulates during the wait is flushed together with the
    deferred payload on the next cycle, producing one larger chat.update
    instead of two.
    """

    def __init__(self, method: str, retry_after: float) -> None:
        super().__init__(f"Slack rate limit deferred: method={method} retry_after={retry_after:.2f}s")
        self.method = method
        self.retry_after = retry_after


def _extract_retry_after(exc: SlackApiError) -> float:
    """Parse the Retry-After header from a 429 response. Returns a bounded value."""
    headers = exc.response.headers or {}
    # slack_sdk stores headers with lowercase keys, but be defensive.
    raw = headers.get("retry-after") or headers.get("Retry-After") or "1"
    try:
        retry_after = float(raw)
    except (TypeError, ValueError):
        retry_after = 1.0
    return max(0.1, min(retry_after, SLACK_RATELIMIT_MAX_RETRY_AFTER_SECONDS))


class RateLimitedSlackClient:
    """Thin wrapper around ``AsyncWebClient`` with preventive + reactive rate limiting.

    The wrapper proxies any attribute lookup to the inner client, replacing
    *async callables* with a gated variant. Non-callable attributes pass
    through unchanged.
    """

    def __init__(self, app_id: str, inner: AsyncWebClient, retry_mode: RetryMode) -> None:
        self._app_id = app_id
        self._inner = inner
        self._retry_mode = retry_mode

    def __getattr__(self, name: str) -> Any:
        # Only called for attributes not set on ``self`` — avoids recursing on _app_id etc.
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        return self._wrap(name, attr)

    def _wrap(
        self,
        method: str,
        inner_call: Callable[..., Awaitable[AsyncSlackResponse]],
    ) -> Callable[..., Awaitable[AsyncSlackResponse]]:
        async def gated(**kwargs: Any) -> AsyncSlackResponse:
            channel = kwargs.get("channel")
            # Preventive gate. If denied, block only in inflight mode — reenqueue
            # callers have their own scheduling and want the denial propagated.
            acquired = await try_acquire_slack_ratelimit(self._app_id, method, channel)
            if not acquired and self._retry_mode == "reenqueue":
                # Treat as deferred — caller reschedules and keeps accumulating.
                raise RateLimitDeferred(method=method, retry_after=0.0)

            try:
                return await inner_call(**kwargs)
            except SlackApiError as exc:
                status = getattr(exc.response, "status_code", None)
                if status != 429:
                    raise
                if self._retry_mode == "reenqueue":
                    retry_after = _extract_retry_after(exc)
                    logger.warning(
                        "Slack 429 deferred for accumulable caller",
                        app_id=self._app_id,
                        method=method,
                        retry_after=retry_after,
                    )
                    raise RateLimitDeferred(method=method, retry_after=retry_after) from exc
                # inflight: AsyncRateLimitErrorRetryHandler should have retried
                # internally; if we get here Slack still 429'd after retries.
                logger.warning(
                    "Slack 429 persisted after slack_sdk retry handler",
                    app_id=self._app_id,
                    method=method,
                )
                raise

        gated.__name__ = method
        return gated


def build_slack_client(
    app_id: str,
    bot_token: str,
    *,
    retry_mode: RetryMode = "inflight",
    max_retry_count: int = 3,
) -> RateLimitedSlackClient:
    """Construct a rate-limited Slack client.

    Args:
        app_id: Slack app id (``A123...``). Used as the scope for the preventive
            rate-limit gate.
        bot_token: Bot user OAuth token (``xoxb-...``).
        retry_mode:
            - ``"inflight"``: attach slack_sdk's Retry-After handler, retry with
              the same payload. Right for one-shot callers.
            - ``"reenqueue"``: raise ``RateLimitDeferred`` on 429. Right for
              accumulable callers (buffer flush) that re-enqueue their payload
              so new content merges in.
        max_retry_count: Retry budget for ``retry_mode='inflight'``. Ignored
            in ``"reenqueue"`` mode.

    Returns:
        A ``RateLimitedSlackClient`` that drop-in replaces ``AsyncWebClient``.
    """
    retry_handlers: list[AsyncRetryHandler] = []
    if retry_mode == "inflight":
        retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=max_retry_count))
    inner = AsyncWebClient(token=bot_token, retry_handlers=retry_handlers)
    return RateLimitedSlackClient(app_id=app_id, inner=inner, retry_mode=retry_mode)
