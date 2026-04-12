"""E2E test fixtures for testing against a running local monolith.

Prerequisites:
    - Monolith running in e2e mode: ./scripts/run_local.sh --e2e
    - Postgres and Redis running

Usage:
    poetry run pytest tests/e2e/ -v -m e2e --timeout=60
"""

from __future__ import annotations
import hashlib
import hmac
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

# Base URL for the monolith — override via E2E_BASE_URL env var
BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8090")

# API key — always read from .env.e2e first (matches what the server loads in --e2e mode),
# then fall back to .env, then env var.
API_KEY = ""
for env_file in [".env.e2e", ".env"]:
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", env_file)
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("AGENT_HARNESS_SERVICE_API_KEY="):
                    API_KEY = line.split("=", 1)[1].strip().strip("'\"")
                    break
        if API_KEY:
            break
if not API_KEY:
    API_KEY = os.environ.get("AGENT_HARNESS_SERVICE_API_KEY", "")

# Signing secret for the e2e test bot
SIGNING_SECRET = os.environ.get("SLACK_AGENT_GATEWAY_E2E_TEST_BOT_SIGNING_SECRET", "e2e-test-signing-secret")
TEST_APP_ID = os.environ.get("SLACK_AGENT_GATEWAY_E2E_TEST_BOT_APP_ID", "A_E2E_TEST")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def api_key() -> str:
    if not API_KEY:
        pytest.skip("AGENT_HARNESS_SERVICE_API_KEY not set")
    return API_KEY


@pytest.fixture(scope="session")
def auth_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


@pytest.fixture
async def client(base_url: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def _check_server_running() -> None:
    """Skip all e2e tests if the monolith isn't running."""
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5.0)
        if resp.status_code != 200:
            pytest.skip(f"Monolith not healthy at {BASE_URL}")
    except httpx.ConnectError:
        pytest.skip(f"Monolith not running at {BASE_URL} — start with: ./scripts/run_local.sh --e2e")


@pytest.fixture(scope="session")
def _check_capture_mode() -> None:
    """Skip Slack flow tests if capture mode is not enabled."""
    try:
        resp = httpx.get(f"{BASE_URL}/gw/slack/_captured", timeout=5.0)
        if resp.status_code == 404:
            pytest.skip("Slack capture mode not enabled — start with: ./scripts/run_local.sh --e2e")
    except httpx.ConnectError:
        pytest.skip("Monolith not running")


@pytest.fixture(autouse=True)
async def _clear_captures(client: httpx.AsyncClient) -> AsyncGenerator[None, None]:
    """Clear captured Slack calls before each test."""
    await client.delete("/gw/slack/_captured")
    yield


def sign_slack_request(body: bytes, signing_secret: str, timestamp: str | None = None) -> dict[str, str]:
    """Compute Slack request signature headers for a fake webhook.

    Returns headers dict with X-Slack-Signature and X-Slack-Request-Timestamp.
    """
    if timestamp is None:
        timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    signature = "v0=" + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Slack-Signature": signature,
        "X-Slack-Request-Timestamp": timestamp,
        "Content-Type": "application/json",
    }


def make_app_mention_event(
    text: str,
    channel: str = "C_E2E_TEST",
    user: str = "U_E2E_USER",
    thread_ts: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build a realistic Slack app_mention event payload."""
    ts = f"{time.time():.6f}"
    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": channel,
        "user": user,
        "ts": ts,
        "text": text,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts

    return {
        "type": "event_callback",
        "event": event,
        "event_id": event_id or f"Ev_e2e_{ts.replace('.', '_')}",
        "event_time": int(time.time()),
        "api_app_id": TEST_APP_ID,
    }


async def wait_for_capture(
    client: httpx.AsyncClient,
    method: str,
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> list[dict[str, Any]]:
    """Poll the capture endpoint until a matching call appears or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await client.get("/gw/slack/_captured", params={"method": method})
        if resp.status_code == 200:
            calls = resp.json().get("calls", [])
            if calls:
                return calls
        await _async_sleep(poll_interval)

    return []


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
