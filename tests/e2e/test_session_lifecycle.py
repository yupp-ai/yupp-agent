"""E2E tests for the AHS session lifecycle (create, message, history, stop).

Uses the parrot-bubba mock executor which echoes prompts back instantly at $0 cost.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import E2E_PREFIX, wait_for_assistant_reply

pytestmark = [pytest.mark.e2e]


class TestSessionCreate:
    """Test session creation via the REST API."""

    async def test_create_session(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Create a session with parrot-bubba and verify the response."""
        resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": "parrot-bubba",
                "trigger": "api",
                "user_id": user_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["session_id"]  # non-empty
        session_cleanup.append(data["session_id"])

    async def test_create_session_unknown_agent(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
    ) -> None:
        """Creating a session with a nonexistent agent returns an error."""
        resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": "nonexistent-agent-xyz-999",
                "trigger": "api",
                "user_id": user_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code in (404, 400, 422)


class TestSessionMessageAndHistory:
    """Test sending messages and retrieving history."""

    async def test_send_message_and_poll_history(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Create session, send message, poll history for assistant reply."""
        # Create session without initial message
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": "parrot-bubba", "trigger": "api", "user_id": user_id},
            headers=auth_headers,
        )
        assert create_resp.status_code == 200
        session_id = create_resp.json()["session_id"]
        session_cleanup.append(session_id)

        # Send a message
        msg_text = f"{E2E_PREFIX}echo-test {tag}"
        msg_resp = await client.post(
            "/ahs/session/message",
            json={"session_id": session_id, "message": msg_text, "user_id": user_id},
            headers=auth_headers,
        )
        assert msg_resp.status_code == 200

        # Poll history until assistant replies
        messages = await wait_for_assistant_reply(client, session_id, auth_headers)
        assert len(messages) >= 2, f"Expected user + assistant messages, got {len(messages)}"
        roles = [m["role"] for m in messages]
        assert "USER" in roles
        assert "AGENT" in roles

    async def test_create_with_initial_message(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Create session WITH initial message, verify assistant echoes it."""
        msg_text = f"{E2E_PREFIX}initial-msg {tag}"
        create_resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": "parrot-bubba",
                "trigger": "api",
                "user_id": user_id,
                "message": msg_text,
            },
            headers=auth_headers,
        )
        assert create_resp.status_code == 200
        session_id = create_resp.json()["session_id"]
        session_cleanup.append(session_id)

        messages = await wait_for_assistant_reply(client, session_id, auth_headers)
        assert len(messages) >= 2
        assistant_msgs = [m for m in messages if m["role"] in ("assistant", "AGENT")]
        assert len(assistant_msgs) >= 1

    async def test_multiple_turns(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        session_cleanup: list[str],
    ) -> None:
        """Send two messages in sequence, verify both get assistant replies."""
        create_resp = await client.post(
            "/ahs/session/create",
            json={
                "agent_id": "parrot-bubba",
                "trigger": "api",
                "user_id": user_id,
                "message": f"{E2E_PREFIX}turn-one {tag}",
            },
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]
        session_cleanup.append(session_id)

        # Wait for first reply
        messages = await wait_for_assistant_reply(client, session_id, auth_headers)
        assert any(m["role"] in ("assistant", "AGENT") for m in messages)

        # Send second message
        await client.post(
            "/ahs/session/message",
            json={"session_id": session_id, "message": f"{E2E_PREFIX}turn-two {tag}", "user_id": user_id},
            headers=auth_headers,
        )

        # Poll for second assistant reply (instead of fixed sleep)
        import asyncio
        import time

        deadline = time.time() + 15.0
        assistant_msgs: list[dict] = []
        while time.time() < deadline:
            history_resp = await client.get(
                f"/ahs/session/{session_id}/history",
                params={"limit": 50},
                headers=auth_headers,
            )
            if history_resp.status_code == 200:
                all_messages = history_resp.json()["messages"]
                assistant_msgs = [m for m in all_messages if m["role"] in ("assistant", "AGENT")]
                if len(assistant_msgs) >= 2:
                    break
            await asyncio.sleep(2)
        assert len(assistant_msgs) >= 2, f"Expected 2+ assistant replies, got {len(assistant_msgs)}"


class TestSessionDetail:
    """Test session detail and listing endpoints."""

    async def test_get_session_detail(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        session_cleanup: list[str],
    ) -> None:
        """Get session detail and verify fields."""
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": "parrot-bubba", "trigger": "api", "user_id": user_id},
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]
        session_cleanup.append(session_id)

        detail_resp = await client.get(f"/ahs/session/{session_id}", headers=auth_headers)
        assert detail_resp.status_code == 200
        data = detail_resp.json()
        assert data["session"]["session_id"] == session_id
        assert data["session"]["agent_name"] == "parrot-bubba"
        assert data["session"]["trigger"].upper() == "API"

    async def test_list_sessions(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        session_cleanup: list[str],
    ) -> None:
        """List sessions filtered by agent_name."""
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": "parrot-bubba", "trigger": "api", "user_id": user_id},
            headers=auth_headers,
        )
        session_cleanup.append(create_resp.json()["session_id"])

        list_resp = await client.get(
            "/ahs/sessions",
            params={"agent_name": "parrot-bubba", "include_all": "true", "limit": 5},
            headers=auth_headers,
        )
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert len(data["sessions"]) >= 1
        assert "total" in data


class TestSessionStop:
    """Test stopping a session."""

    async def test_stop_session(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        session_cleanup: list[str],
    ) -> None:
        """Stop a session and verify response."""
        create_resp = await client.post(
            "/ahs/session/create",
            json={"agent_id": "parrot-bubba", "trigger": "api", "user_id": user_id},
            headers=auth_headers,
        )
        session_id = create_resp.json()["session_id"]
        session_cleanup.append(session_id)

        stop_resp = await client.post(
            "/ahs/session/stop",
            json={"session_id": session_id},
            headers=auth_headers,
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["status"] in ("stopped", "no_inflight_turn")
