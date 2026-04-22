"""Unit tests for ypl/slack_agent_gateway/slack_client.py.

Covers both mechanisms of the universal Slack rate limiter:

1. **Preventive gate** — ``try_acquire_slack_ratelimit`` is consulted before every
   call; denials raise ``RateLimitDeferred`` in reenqueue mode.
2. **Reactive 429** — 429 responses produce ``RateLimitDeferred`` in reenqueue
   mode (with Retry-After honored) and re-raise in inflight mode.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError
from ypl.slack_agent_gateway.slack_client import (
    RateLimitDeferred,
    RateLimitedSlackClient,
    _extract_retry_after,
    build_slack_client,
)


def _make_slack_api_error(status_code: int, retry_after: str | None = "2") -> SlackApiError:
    """Construct a SlackApiError mimicking a real 429 response."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"retry-after": retry_after} if retry_after is not None else {}
    # slack_sdk.errors.SlackApiError.__init__ is untyped — silence mypy here.
    return SlackApiError("rate limited", response)  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# Tests: _extract_retry_after
# ---------------------------------------------------------------------------


class TestExtractRetryAfter:
    def test_reads_lowercase_header(self) -> None:
        exc = _make_slack_api_error(429, retry_after="3")
        assert _extract_retry_after(exc) == 3.0

    def test_reads_mixedcase_header(self) -> None:
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "5"}
        exc = SlackApiError("rate limited", response)  # type: ignore[no-untyped-call]
        assert _extract_retry_after(exc) == 5.0

    def test_defaults_to_one_when_missing(self) -> None:
        exc = _make_slack_api_error(429, retry_after=None)
        assert _extract_retry_after(exc) == 1.0

    def test_clamps_above_max(self) -> None:
        # SLACK_RATELIMIT_MAX_RETRY_AFTER_SECONDS = 30
        exc = _make_slack_api_error(429, retry_after="999")
        assert _extract_retry_after(exc) == 30.0

    def test_clamps_below_min(self) -> None:
        exc = _make_slack_api_error(429, retry_after="0")
        assert _extract_retry_after(exc) == 0.1


# ---------------------------------------------------------------------------
# Tests: RateLimitedSlackClient — preventive gate
# ---------------------------------------------------------------------------


class TestPreventiveGate:
    async def test_reenqueue_raises_deferred_when_gate_denied(self) -> None:
        inner = MagicMock()
        inner.chat_update = AsyncMock()
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="reenqueue")

        with (
            patch(
                "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
                AsyncMock(return_value=False),
            ),
            pytest.raises(RateLimitDeferred) as exc,
        ):
            await client.chat_update(channel="C1", ts="1", text="hi")

        # The inner API must NOT be called when the gate denied us.
        inner.chat_update.assert_not_called()
        assert exc.value.method == "chat_update"
        # Deny-case Retry-After is 0 — caller uses the configured gate interval.
        assert exc.value.retry_after == 0.0

    async def test_inflight_proceeds_even_when_gate_denied(self) -> None:
        # In inflight mode, denial doesn't block — we still call Slack and rely
        # on the SDK's Retry-After handler to catch a real 429.
        inner = MagicMock()
        inner.chat_update = AsyncMock(return_value=MagicMock(status_code=200))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")

        with patch(
            "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
            AsyncMock(return_value=False),
        ):
            await client.chat_update(channel="C1", ts="1", text="hi")

        inner.chat_update.assert_called_once()

    async def test_gate_key_includes_channel_for_post_message(self) -> None:
        inner = MagicMock()
        inner.chat_postMessage = AsyncMock(return_value=MagicMock(status_code=200))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")

        acquire = AsyncMock(return_value=True)
        with patch("ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit", acquire):
            await client.chat_postMessage(channel="C1", text="hi")

        # Wrapper must pass method name AND channel to the gate for postMessage.
        acquire.assert_awaited_once_with("A1", "chat_postMessage", "C1")

    async def test_gate_key_omits_channel_for_non_message_methods(self) -> None:
        inner = MagicMock()
        inner.views_open = AsyncMock(return_value=MagicMock(status_code=200))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")

        acquire = AsyncMock(return_value=True)
        with patch("ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit", acquire):
            # views.open takes trigger_id + view, no channel
            await client.views_open(trigger_id="T1", view={"type": "modal"})

        # channel arg is None because kwargs has no 'channel'
        acquire.assert_awaited_once_with("A1", "views_open", None)


# ---------------------------------------------------------------------------
# Tests: RateLimitedSlackClient — reactive 429 handling
# ---------------------------------------------------------------------------


class TestReactive429:
    async def test_reenqueue_mode_raises_deferred_on_429(self) -> None:
        inner = MagicMock()
        inner.chat_update = AsyncMock(side_effect=_make_slack_api_error(429, retry_after="4"))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="reenqueue")

        with (
            patch(
                "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
                AsyncMock(return_value=True),
            ),
            pytest.raises(RateLimitDeferred) as exc,
        ):
            await client.chat_update(channel="C1", ts="1", text="hi")

        assert exc.value.method == "chat_update"
        assert exc.value.retry_after == 4.0

    async def test_inflight_mode_reraises_429_after_handler_exhausted(self) -> None:
        # When inflight mode is used, the slack_sdk handler runs *inside* the SDK;
        # our wrapper only sees the 429 if the handler gave up. The wrapper must
        # then re-raise the SlackApiError (not RateLimitDeferred).
        inner = MagicMock()
        inner.chat_update = AsyncMock(side_effect=_make_slack_api_error(429, retry_after="2"))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")

        with (
            patch(
                "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
                AsyncMock(return_value=True),
            ),
            pytest.raises(SlackApiError),
        ):
            await client.chat_update(channel="C1", ts="1", text="hi")

    async def test_non_429_errors_pass_through_unchanged(self) -> None:
        inner = MagicMock()
        inner.chat_update = AsyncMock(side_effect=_make_slack_api_error(500))
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="reenqueue")

        # 500 is not 429 — should propagate as SlackApiError, not RateLimitDeferred.
        with (
            patch(
                "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
                AsyncMock(return_value=True),
            ),
            pytest.raises(SlackApiError),
        ):
            await client.chat_update(channel="C1", ts="1", text="hi")


# ---------------------------------------------------------------------------
# Tests: build_slack_client factory
# ---------------------------------------------------------------------------


class TestBuildSlackClient:
    def test_inflight_default_attaches_retry_handler(self) -> None:
        client = build_slack_client("A1", "xoxb-fake")
        # Underlying SDK client should have exactly one retry handler
        # (AsyncRateLimitErrorRetryHandler).
        inner_handlers = client._inner.retry_handlers
        assert len(inner_handlers) == 1

    def test_reenqueue_mode_skips_retry_handler(self) -> None:
        # reenqueue callers handle their own retry scheduling — the SDK handler
        # would sleep + retry with stale content, which is wrong for them.
        client = build_slack_client("A1", "xoxb-fake", retry_mode="reenqueue")
        inner_handlers = client._inner.retry_handlers
        assert inner_handlers == []


# ---------------------------------------------------------------------------
# Tests: RateLimitedSlackClient — proxy of non-callable attrs
# ---------------------------------------------------------------------------


class TestAttributeProxy:
    def test_non_callable_attrs_pass_through(self) -> None:
        inner = MagicMock()
        inner.token = "xoxb-fake"  # non-callable attribute
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")
        assert client.token == "xoxb-fake"

    async def test_methods_are_wrapped_even_when_absent_on_parent(self) -> None:
        # The SDK exposes methods via a dynamic proxy too — our wrapper must
        # work for anything callable on the inner client.
        inner = MagicMock()

        async def fake_call(**kwargs: Any) -> Any:
            return MagicMock(status_code=200, data={"ok": True})

        inner.some_new_method = fake_call
        client = RateLimitedSlackClient(app_id="A1", inner=inner, retry_mode="inflight")

        with patch(
            "ypl.slack_agent_gateway.slack_client.try_acquire_slack_ratelimit",
            AsyncMock(return_value=True),
        ):
            result = await client.some_new_method(foo="bar")

        assert result.status_code == 200
