"""E2E test fixtures for testing against a running local monolith with real Slack.

Prerequisites:
    - Monolith running in e2e mode: ./scripts/run_local.sh --e2e
    - ngrok exposing localhost:8090 to Slack
    - Slack Event Subscription URL configured
    - Bot invited to #ahs-e2e-testing

Environment variables (set in shell or read from .env.e2e):
    SLACK_E2E_USER_TOKEN    — Slack user token (xoxp-...) for sending messages as a user
    SLACK_E2E_CHANNEL_ID    — Channel ID for #ahs-e2e-testing (e.g., C0ASB9LJ41G)
    SLACK_E2E_BOT_USER_ID   — Bot's Slack user ID (for @mention, e.g., U0ARTXXXX)
    E2E_BASE_URL            — Monolith URL (default: http://localhost:8090)
    AGENT_HARNESS_SERVICE_API_KEY — AHS API key

Usage:
    SLACK_E2E_USER_TOKEN=xoxp-... SLACK_E2E_CHANNEL_ID=C0ASB9LJ41G SLACK_E2E_BOT_USER_ID=U0ART... \\
        poetry run pytest tests/e2e/ -v -m e2e --timeout=120
"""

from __future__ import annotations
import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from slack_sdk.web.async_client import AsyncWebClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

E2E_PREFIX = "e2e-"

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8090")

# AHS API key
API_KEY = os.environ.get("AGENT_HARNESS_SERVICE_API_KEY", "")
if not API_KEY:
    for env_file in [".env.e2e", ".env"]:
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("AGENT_HARNESS_SERVICE_API_KEY="):
                        API_KEY = line.split("=", 1)[1].strip().strip("'\"")
                        break
            if API_KEY:
                break

# MCP auth — use a dedicated e2e dev token (separate from production PLATFORM_MCP_TOKEN)
MCP_TOKEN = os.environ.get("PLATFORM_MCP_TOKEN_E2E", "")

# User email for resolving user_id (no default — must be set explicitly)
E2E_USER_EMAIL = os.environ.get("E2E_USER_EMAIL", "")
E2E_USER_ID = os.environ.get("E2E_USER_ID", "")

# Slack credentials
SLACK_USER_TOKEN = os.environ.get("SLACK_E2E_USER_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_E2E_CHANNEL_ID", "")
SLACK_BOT_USER_ID = os.environ.get("SLACK_E2E_BOT_USER_ID", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="session")
def slack_client() -> AsyncWebClient:
    """Slack client using a user token (xoxp-...) to send messages as a real user."""
    if not SLACK_USER_TOKEN:
        pytest.skip("SLACK_E2E_USER_TOKEN not set — cannot run Slack e2e tests")
    return AsyncWebClient(token=SLACK_USER_TOKEN)


@pytest.fixture(scope="session")
def slack_channel() -> str:
    if not SLACK_CHANNEL_ID:
        pytest.skip("SLACK_E2E_CHANNEL_ID not set")
    return SLACK_CHANNEL_ID


@pytest.fixture(scope="session")
def bot_user_id() -> str:
    if not SLACK_BOT_USER_ID:
        pytest.skip("SLACK_E2E_BOT_USER_ID not set")
    return SLACK_BOT_USER_ID


@pytest.fixture(scope="session")
def user_id(api_key: str) -> str:
    """Resolve a test user_id via /ahs/resolve_user or E2E_USER_ID env var."""
    if E2E_USER_ID:
        return E2E_USER_ID
    if not E2E_USER_EMAIL:
        pytest.skip("E2E_USER_EMAIL or E2E_USER_ID must be set in .env.e2e")
    try:
        resp = httpx.post(
            f"{BASE_URL}/ahs/resolve_user",
            json={"email": E2E_USER_EMAIL},
            headers={"X-API-Key": api_key},
            timeout=10.0,
        )
        if resp.status_code == 200:
            uid: str = resp.json()["user_id"]
            return uid
    except httpx.ConnectError:
        pass
    pytest.skip(f"Could not resolve user_id for {E2E_USER_EMAIL} — check E2E_USER_EMAIL or set E2E_USER_ID")


@pytest.fixture
def tag() -> str:
    """Unique tag for test isolation."""
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def mcp_headers() -> dict[str, str]:
    """Auth headers for calling MCP tools via /mcp/ endpoint using dev token."""
    if not MCP_TOKEN:
        pytest.skip("PLATFORM_MCP_TOKEN_E2E not set — cannot run MCP-dependent tests")
    return {"Authorization": f"Bearer {MCP_TOKEN}", "Content-Type": "application/json", "Accept": "application/json"}


@pytest.fixture
async def session_cleanup(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> AsyncGenerator[list[str], None]:
    """Track and stop sessions created during a test."""
    created: list[str] = []
    yield created
    for sid in created:
        await client.post("/ahs/session/stop", json={"session_id": sid}, headers=auth_headers)


@pytest.fixture
async def schedule_cleanup(
    client: httpx.AsyncClient, auth_headers: dict[str, str], user_id: str
) -> AsyncGenerator[list[str], None]:
    """Track and cancel schedules created during a test."""
    created: list[str] = []
    yield created
    for sid in created:
        await client.delete(f"/ahs/schedule/{sid}", params={"user_id": user_id}, headers=auth_headers)


@pytest.fixture(scope="session", autouse=True)
def _check_server_running() -> None:
    """Skip all e2e tests if the monolith isn't running."""
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5.0)
        if resp.status_code != 200:
            pytest.skip(f"Monolith not healthy at {BASE_URL}")
    except httpx.ConnectError:
        pytest.skip(f"Monolith not running at {BASE_URL} — start with: ./scripts/run_local.sh --e2e")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def send_slack_message(
    slack_client: AsyncWebClient,
    channel: str,
    text: str,
) -> dict[str, Any]:
    """Send a message to a Slack channel. Returns the API response."""
    resp = await slack_client.chat_postMessage(channel=channel, text=text)
    assert resp["ok"], f"Failed to send Slack message: {resp}"
    return dict(resp.data)  # type: ignore[arg-type]


async def wait_for_bot_reply(
    slack_client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    bot_user_id: str,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
) -> dict[str, Any] | None:
    """Poll a Slack thread until the bot replies. Returns the reply or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await slack_client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=20,
        )
        if resp["ok"]:
            messages: list[dict[str, Any]] = resp.get("messages", [])
            for msg in messages[1:]:  # skip original message
                if msg.get("user") == bot_user_id or msg.get("bot_id"):
                    return dict(msg)
        await asyncio.sleep(poll_interval)
    return None


async def wait_for_assistant_reply(
    client: httpx.AsyncClient,
    session_id: str,
    headers: dict[str, str],
    timeout: float = 15.0,
    poll_interval: float = 2.0,
) -> list[dict[str, Any]]:
    """Poll session history until an assistant message appears. Returns all messages."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await client.get(
            f"/ahs/session/{session_id}/history",
            params={"limit": 50},
            headers=headers,
        )
        if resp.status_code == 200:
            messages: list[dict[str, Any]] = resp.json().get("messages", [])
            if any(m.get("role") in ("assistant", "AGENT") for m in messages):
                return messages
        await asyncio.sleep(poll_interval)
    return []


async def call_mcp_tool(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call an MCP tool via JSON-RPC 2.0 over the /mcp/ endpoint."""
    resp = await client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, f"MCP call to {tool_name} failed: {resp.status_code} {resp.text}"
    result: dict[str, Any] = resp.json()
    return result
