"""Slack API call capture for e2e testing.

When ``SLACK_CAPTURE_MODE=true``, all outbound Slack API calls are intercepted
and recorded instead of being sent to Slack. Tests can then query the captured
calls to verify the correct messages were sent.

Usage::

    # In e2e tests:
    resp = await client.get("/gw/slack/_captured")
    calls = resp.json()["calls"]
    assert any(c["method"] == "chat_postMessage" for c in calls)
"""

from __future__ import annotations
import os
import time
from typing import Any
from unittest.mock import AsyncMock

from ypl.structured_logger import get_logger

logger = get_logger()

CAPTURE_MODE = os.environ.get("SLACK_CAPTURE_MODE", "").lower() == "true"


class SlackCapture:
    """Registry that records all intercepted Slack API calls."""

    def __init__(self) -> None:
        self._calls: list[dict[str, Any]] = []

    def record(self, method: str, kwargs: dict[str, Any]) -> None:
        """Record an API call."""
        self._calls.append(
            {
                "method": method,
                "kwargs": kwargs,
                "timestamp": time.time(),
            }
        )
        logger.info("Slack capture: recorded call", method=method, channel=kwargs.get("channel"))

    def get_calls(self, method: str | None = None) -> list[dict[str, Any]]:
        """Return captured calls, optionally filtered by method."""
        if method is None:
            return list(self._calls)
        return [c for c in self._calls if c["method"] == method]

    def clear(self) -> None:
        """Reset all captured calls."""
        self._calls.clear()


# Global singleton
_capture = SlackCapture()


def get_capture() -> SlackCapture:
    """Return the global capture singleton."""
    return _capture


def create_slack_client(token: str) -> Any:
    """Create a Slack AsyncWebClient — real or captured depending on mode.

    This is the single interception point. All SAG code should use this
    instead of ``AsyncWebClient(token=...)`` directly.
    """
    if CAPTURE_MODE:
        return _make_capture_client(token)

    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=token)


def install_capture_mode() -> None:
    """Replace AsyncWebClient in all SAG modules with the capture factory.

    Call once at startup (e.g., in SAG lifespan) when SLACK_CAPTURE_MODE is on.
    This patches the class in each module's namespace so that ``AsyncWebClient(token=...)``
    calls throughout SAG go through the capture registry.
    """
    if not CAPTURE_MODE:
        return

    logger.info("Slack capture mode: installing interceptors in SAG modules")

    # A callable that behaves like AsyncWebClient(token=...) but returns capture clients
    class _CaptureWebClient:
        def __init__(self, token: str = "", **kwargs: Any) -> None:
            self._delegate = _make_capture_client(token)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._delegate, name)

    modules_to_patch = [
        "ypl.slack_agent_gateway.callbacks",
        "ypl.slack_agent_gateway.events",
        "ypl.slack_agent_gateway.buffer",
        "ypl.slack_agent_gateway.queue",
        "ypl.slack_agent_gateway.interactions",
        "ypl.slack_agent_gateway.bot_father",
        "ypl.slack_agent_gateway.commands",
        "ypl.backend.utils.slack_utils",  # used by get_channel_name_by_id
    ]

    import sys

    for mod_name in modules_to_patch:
        if mod_name in sys.modules:
            setattr(sys.modules[mod_name], "AsyncWebClient", _CaptureWebClient)
            logger.info("Slack capture: patched", module=mod_name)


def _make_capture_client(token: str) -> AsyncMock:
    """Create a mock client that records all calls to the capture registry."""
    client = AsyncMock()

    def _make_handler(method_name: str) -> Any:
        async def handler(**kwargs: Any) -> dict[str, Any]:
            _capture.record(method_name, kwargs)
            # Return realistic Slack API responses
            if method_name == "chat_postMessage":
                return {"ok": True, "ts": f"{time.time():.6f}", "channel": kwargs.get("channel", "")}
            if method_name == "chat_update":
                return {"ok": True, "ts": kwargs.get("ts", ""), "channel": kwargs.get("channel", "")}
            if method_name == "chat_delete":
                return {"ok": True}
            if method_name == "reactions_add":
                return {"ok": True}
            if method_name == "views_open":
                return {"ok": True}
            return {"ok": True}

        return handler

    for method in ("chat_postMessage", "chat_update", "chat_delete", "reactions_add", "views_open"):
        setattr(client, method, _make_handler(method))

    # conversations.info — returns a fake channel so the channel allowlist check passes
    async def _conversations_info(**kwargs: Any) -> dict[str, Any]:
        channel = kwargs.get("channel", "C_UNKNOWN")
        return {
            "ok": True,
            "channel": {
                "id": channel,
                "name": "e2e-test-channel",
                "is_channel": True,
                "is_member": True,
            },
        }

    client.conversations_info = _conversations_info

    return client
