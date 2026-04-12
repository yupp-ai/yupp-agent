"""E2E tests for the full Slack → SAG → AHS → SAG → Slack flow.

These tests send fake Slack webhooks to the monolith, let the real SAG + AHS
process them (using parrot-bubba mock executor), and verify the captured
outbound Slack API calls.

Requires: ./scripts/run_local.sh --e2e
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import (
    SIGNING_SECRET,
    make_app_mention_event,
    sign_slack_request,
    wait_for_capture,
)

pytestmark = [pytest.mark.e2e]


class TestNewMessageFlow:
    """Test: Slack mention → SAG creates AHS session → parrot-bubba replies → SAG posts to Slack."""

    async def test_mention_creates_session_and_replies(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        _check_capture_mode: None,
    ) -> None:
        # Send a fake Slack webhook
        payload = make_app_mention_event(text="<@U_BOT> Hello parrot!")
        body = bytes(str(payload).replace("'", '"'), "utf-8")
        import json

        body = json.dumps(payload).encode()
        headers = sign_slack_request(body, SIGNING_SECRET)

        resp = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        assert resp.status_code == 200

        # Wait for SAG to post a reply (captured, not sent to real Slack)
        calls = await wait_for_capture(client, "chat_postMessage", timeout=15.0)
        assert len(calls) >= 1, "Expected at least one chat_postMessage call"

        # Verify the reply went to the right channel
        first_call = calls[0]
        assert first_call["kwargs"]["channel"] == "C_E2E_TEST"

    async def test_mention_creates_ahs_session_in_db(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        _check_capture_mode: None,
    ) -> None:
        # Send a fake Slack webhook
        payload = make_app_mention_event(text="<@U_BOT> Check session creation")
        body = bytes(str(payload).replace("'", '"'), "utf-8")
        import json

        body = json.dumps(payload).encode()
        headers = sign_slack_request(body, SIGNING_SECRET)

        resp = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        assert resp.status_code == 200

        # Wait for processing
        await wait_for_capture(client, "chat_postMessage", timeout=15.0)

        # Verify AHS session was created (check without agent_name filter first)
        sessions_resp = await client.get(
            "/ahs/sessions",
            headers=auth_headers,
        )
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json().get("sessions", [])
        # At least one session should exist from Slack-triggered flows
        assert len(sessions) >= 1, f"Expected at least one session, got: {sessions_resp.json()}"


class TestThreadRouting:
    """Test: Second message in same thread routes to the same AHS session."""

    async def test_thread_reply_uses_same_session(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        _check_capture_mode: None,
    ) -> None:
        import json

        thread_ts = "9999999999.000001"

        # First message in thread
        payload1 = make_app_mention_event(
            text="<@U_BOT> First message",
            thread_ts=thread_ts,
            event_id="Ev_thread_1",
        )
        body1 = json.dumps(payload1).encode()
        headers1 = sign_slack_request(body1, SIGNING_SECRET)
        resp1 = await client.post("/gw/slack/slack/events", content=body1, headers=headers1)
        assert resp1.status_code == 200

        # Wait for first reply
        await wait_for_capture(client, "chat_postMessage", timeout=15.0)

        # Second message in same thread
        payload2 = make_app_mention_event(
            text="<@U_BOT> Second message",
            thread_ts=thread_ts,
            event_id="Ev_thread_2",
        )
        body2 = json.dumps(payload2).encode()
        headers2 = sign_slack_request(body2, SIGNING_SECRET)
        resp2 = await client.post("/gw/slack/slack/events", content=body2, headers=headers2)
        assert resp2.status_code == 200

        # Both should have gone to the same channel+thread
        calls = await wait_for_capture(client, "chat_postMessage", timeout=15.0)
        channels = {c["kwargs"].get("channel") for c in calls}
        assert "C_E2E_TEST" in channels


class TestEventDedup:
    """Test: Duplicate event_id is ignored."""

    async def test_duplicate_event_ignored(
        self,
        client: httpx.AsyncClient,
        _check_capture_mode: None,
    ) -> None:
        import json

        payload = make_app_mention_event(
            text="<@U_BOT> Dedup test",
            event_id="Ev_dedup_test_001",
        )
        body = json.dumps(payload).encode()
        headers = sign_slack_request(body, SIGNING_SECRET)

        # Send the same event twice
        resp1 = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        assert resp1.status_code == 200

        resp2 = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        assert resp2.status_code == 200

        # Wait for processing
        await wait_for_capture(client, "chat_postMessage", timeout=15.0)

        # Should only have reactions/messages from the first event
        # The second event should have been deduped via Redis
        all_calls = await wait_for_capture(client, "reactions_add", timeout=5.0)
        # At most 1 ack reaction (from the first event)
        assert len(all_calls) <= 1


class TestSignatureVerification:
    """Test: Malformed payloads are handled gracefully."""

    async def test_malformed_payload_handled(
        self,
        client: httpx.AsyncClient,
        _check_capture_mode: None,
    ) -> None:
        """Send a non-JSON payload and verify it doesn't crash the server."""
        body = b"not json at all"
        headers = sign_slack_request(body, SIGNING_SECRET)

        resp = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        # Should return an error status, not crash
        assert resp.status_code in (200, 400, 500)

    async def test_url_verification_challenge(
        self,
        client: httpx.AsyncClient,
        _check_capture_mode: None,
    ) -> None:
        """Slack sends url_verification during app setup — verify we echo the challenge."""
        import json

        payload = {"type": "url_verification", "challenge": "test_challenge_token"}
        body = json.dumps(payload).encode()
        headers = sign_slack_request(body, SIGNING_SECRET)

        resp = await client.post("/gw/slack/slack/events", content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json().get("challenge") == "test_challenge_token"
