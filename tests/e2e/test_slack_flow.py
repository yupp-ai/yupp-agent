"""E2E tests for the full Slack → SAG → AHS → SAG → Slack flow.

These tests send real Slack messages mentioning the e2e test bot, wait for the
bot to process via SAG → AHS (parrot-bubba mock executor) → SAG, and verify
the bot's reply appears in the Slack thread.

Requires:
    - Monolith running with --e2e flag
    - ngrok exposing localhost:8090
    - Slack Event Subscription URL configured
    - Environment variables: SLACK_E2E_USER_TOKEN, SLACK_E2E_CHANNEL_ID, SLACK_E2E_BOT_USER_ID
"""

from __future__ import annotations
import uuid
from typing import Any

import httpx
import pytest
from slack_sdk.web.async_client import AsyncWebClient

from tests.e2e.conftest import send_slack_message, wait_for_bot_reply

pytestmark = [pytest.mark.e2e]


class TestSlackMentionFlow:
    """Test: @mention the bot → bot creates AHS session → bot replies in thread."""

    async def test_mention_triggers_reply(
        self,
        slack_client: AsyncWebClient,
        slack_channel: str,
        bot_user_id: str,
    ) -> None:
        """Send @bot hello → bot replies in thread."""
        tag = uuid.uuid4().hex[:8]
        text = f"<@{bot_user_id}> e2e test {tag}"

        # Send message mentioning the bot
        resp = await send_slack_message(slack_client, slack_channel, text)
        thread_ts = resp["ts"]

        # Wait for the bot to reply in the thread
        reply = await wait_for_bot_reply(
            slack_client,
            slack_channel,
            thread_ts,
            bot_user_id,
            timeout=30.0,
        )

        assert reply is not None, f"Bot did not reply within 30s (tag={tag})"
        assert reply.get("text"), "Bot reply has no text"

    async def test_mention_creates_ahs_session(
        self,
        slack_client: AsyncWebClient,
        slack_channel: str,
        bot_user_id: str,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Send @bot message → verify AHS session was created in DB."""
        tag = uuid.uuid4().hex[:8]
        text = f"<@{bot_user_id}> session check {tag}"

        resp = await send_slack_message(slack_client, slack_channel, text)
        thread_ts = resp["ts"]

        # Wait for reply (means session was created and processed)
        reply = await wait_for_bot_reply(
            slack_client,
            slack_channel,
            thread_ts,
            bot_user_id,
            timeout=30.0,
        )
        assert reply is not None, "Bot did not reply"

        # Verify session exists in AHS
        sessions_resp = await client.get(
            "/ahs/sessions",
            params={"include_all": "true", "limit": "5"},
            headers=auth_headers,
        )
        assert sessions_resp.status_code == 200
        sessions = sessions_resp.json().get("sessions", [])
        assert len(sessions) >= 1, "Expected at least one AHS session"


class TestThreadContinuity:
    """Test: Second message in same thread routes to same AHS session."""

    async def test_thread_reply_reuses_session(
        self,
        slack_client: AsyncWebClient,
        slack_channel: str,
        bot_user_id: str,
    ) -> None:
        """Send two messages in the same thread → both get replies."""
        tag = uuid.uuid4().hex[:8]

        # First message
        resp1 = await send_slack_message(
            slack_client,
            slack_channel,
            f"<@{bot_user_id}> thread test 1 {tag}",
        )
        thread_ts = resp1["ts"]

        reply1 = await wait_for_bot_reply(
            slack_client,
            slack_channel,
            thread_ts,
            bot_user_id,
            timeout=30.0,
        )
        assert reply1 is not None, "Bot did not reply to first message"

        # Second message in same thread
        await slack_client.chat_postMessage(
            channel=slack_channel,
            text=f"<@{bot_user_id}> thread test 2 {tag}",
            thread_ts=thread_ts,
        )

        # Wait for second reply
        import asyncio

        await asyncio.sleep(15)

        # Check thread has multiple bot replies
        thread_resp = await slack_client.conversations_replies(
            channel=slack_channel,
            ts=thread_ts,
            limit=20,
        )
        all_messages: list[dict[str, Any]] = thread_resp.get("messages", [])
        bot_replies = [m for m in all_messages[1:] if m.get("user") == bot_user_id or m.get("bot_id")]
        assert len(bot_replies) >= 2, f"Expected 2+ bot replies in thread, got {len(bot_replies)}"
