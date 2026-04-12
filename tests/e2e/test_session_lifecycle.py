"""E2E session lifecycle tests using parrot-bubba (mock executor, no LLM calls).

Tests the full flow: create session → send message → get history → stop → verify status.
"""

import asyncio

import httpx
import pytest


pytestmark = pytest.mark.e2e

# parrot-bubba uses the mock executor — no API calls, returns synthetic response
AGENT_ID = "parrot-bubba"


class TestSessionCreate:
    async def test_create_session_returns_session_id(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": AGENT_ID, "trigger": "api"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["session_id"]  # non-empty

    async def test_create_session_with_message(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": AGENT_ID,
                "trigger": "api",
                "message": "Hello parrot!",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data

    async def test_create_session_unknown_agent_returns_error(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": "nonexistent-agent-xyz", "trigger": "api"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404)


class TestSessionMessage:
    async def test_send_message_to_session(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Create session first
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": AGENT_ID, "trigger": "api"},
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]

        # Send a message
        msg_resp = await client.post(
            "/ahs/session/message",
            json={"session_id": session_id, "message": "Echo this back"},
            headers=auth_headers,
        )
        assert msg_resp.status_code == 200

    async def test_send_message_to_nonexistent_session(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/session/message",
            json={
                "session_id": "00000000-0000-0000-0000-000000000000",
                "message": "Hello",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404)


class TestSessionHistory:
    async def test_get_history_after_message(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Create session with a message
        create_resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": AGENT_ID,
                "trigger": "api",
                "message": "Hello parrot!",
            },
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]

        # Give the mock executor a moment to process
        await asyncio.sleep(2)

        # Get history
        hist_resp = await client.get(
            f"/ahs/session/{session_id}/history",
            headers=auth_headers,
        )
        assert hist_resp.status_code == 200
        data = hist_resp.json()
        assert "messages" in data
        # Should have at least the user message
        assert len(data["messages"]) >= 1


class TestSessionStop:
    async def test_stop_session(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Create session
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": AGENT_ID, "trigger": "api"},
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]

        # Stop it
        stop_resp = await client.post(
            "/ahs/session/stop",
            json={"session_id": session_id},
            headers=auth_headers,
        )
        assert stop_resp.status_code == 200

    async def test_stop_nonexistent_session(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/session/stop",
            json={"session_id": "00000000-0000-0000-0000-000000000000"},
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404)


class TestSessionDetail:
    async def test_get_session_detail(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Create session
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": AGENT_ID, "trigger": "api"},
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]

        # Get detail
        detail_resp = await client.get(
            f"/ahs/session/{session_id}",
            headers=auth_headers,
        )
        assert detail_resp.status_code == 200
        data = detail_resp.json()
        assert data["session_id"] == session_id

    async def test_get_nonexistent_session_detail(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.get(
            "/ahs/session/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert resp.status_code in (400, 404)


class TestSessionList:
    async def test_list_sessions(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.get("/ahs/sessions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data

    async def test_list_sessions_with_agent_filter(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.get(
            "/ahs/sessions",
            params={"agent_name": AGENT_ID},
            headers=auth_headers,
        )
        assert resp.status_code == 200
