"""E2E tests for AHS WebSocket streaming endpoint.

Uses aiohttp for WebSocket client connections.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import httpx
import pytest

from tests.e2e.conftest import BASE_URL, E2E_PREFIX

pytestmark = [pytest.mark.e2e]

# Convert http:// base URL to ws://
WS_BASE = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")


async def _create_session_for_ws(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    user_id: str,
    message: str | None = None,
) -> str:
    """Create a session via REST and return session_id."""
    body: dict = {"agent_id": "parrot-bubba", "trigger": "api", "user_id": user_id}
    if message:
        body["message"] = message
    resp = await client.post("/ahs/session/create", json=body, headers=auth_headers)
    assert resp.status_code == 200
    return resp.json()["session_id"]


class TestWebSocketConnect:
    """Test WebSocket connection and initial events."""

    async def test_ws_connect_receives_thread_started(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        api_key: str,
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Connect to WS and verify thread/started is the first event."""
        session_id = await _create_session_for_ws(client, auth_headers, user_id)
        session_cleanup.append(session_id)

        ws_url = f"{WS_BASE}/ahs/session/{session_id}/ws?api_key={api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                assert msg.type == aiohttp.WSMsgType.TEXT
                data = json.loads(msg.data)
                assert data["type"] == "thread/started"
                assert data["thread_id"] == session_id
                await ws.close()

    async def test_ws_ping_pong(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        api_key: str,
        user_id: str,
        session_cleanup: list[str],
    ) -> None:
        """Send ping, receive pong."""
        session_id = await _create_session_for_ws(client, auth_headers, user_id)
        session_cleanup.append(session_id)

        ws_url = f"{WS_BASE}/ahs/session/{session_id}/ws?api_key={api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Consume thread/started
                await asyncio.wait_for(ws.receive(), timeout=5.0)

                # Send ping
                await ws.send_json({"type": "ping"})
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                assert msg.type == aiohttp.WSMsgType.TEXT
                data = json.loads(msg.data)
                assert data["type"] == "pong"
                await ws.close()


class TestWebSocketStreaming:
    """Test sending messages via WS and receiving streaming events."""

    async def test_ws_send_message_receives_events(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        api_key: str,
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Send a user_message via WS, collect streaming events from parrot-bubba."""
        session_id = await _create_session_for_ws(client, auth_headers, user_id)
        session_cleanup.append(session_id)

        ws_url = f"{WS_BASE}/ahs/session/{session_id}/ws?api_key={api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Consume thread/started
                await asyncio.wait_for(ws.receive(), timeout=5.0)

                # Send message
                await ws.send_json({
                    "type": "user_message",
                    "content": f"{E2E_PREFIX}ws-test {tag}",
                    "user_id": user_id,
                })

                # Collect events until we see turn/completed or timeout
                events = []
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            events.append(data)
                            if data.get("type") in ("turn/completed", "error"):
                                break
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                except TimeoutError:
                    pass

                await ws.close()

        event_types = [e["type"] for e in events]
        assert len(events) >= 1, f"Expected streaming events, got none"
        # Should have at least some agent response events
        assert any("item" in t or "turn" in t for t in event_types), f"No item/turn events: {event_types}"


class TestWebSocketAuth:
    """Test WebSocket authentication."""

    async def test_ws_invalid_api_key_rejected(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        session_cleanup: list[str],
    ) -> None:
        """Connecting with a wrong API key should be rejected."""
        session_id = await _create_session_for_ws(client, auth_headers, user_id)
        session_cleanup.append(session_id)

        ws_url = f"{WS_BASE}/ahs/session/{session_id}/ws?api_key=wrong-key-xyz"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.ws_connect(ws_url) as ws:
                    # If we get here, the connection was accepted (unexpected)
                    msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
                    # Should get a close frame
                    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR)
            except (aiohttp.WSServerHandshakeError, aiohttp.ClientResponseError) as e:
                # Expected — server rejects the handshake
                assert e.status in (403, 1008, 1003)
