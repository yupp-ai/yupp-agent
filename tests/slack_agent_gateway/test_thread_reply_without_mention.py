"""Tests for handle_thread_message (thread replies without an @mention)."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from ypl.slack_agent_gateway.events import handle_event_callback, handle_thread_message
from ypl.slack_agent_gateway.types import AgentAppConfig, AgentSession, SessionStatus

THREAD_TS = "1234567890.000"


def _config(enabled: bool = True) -> AgentAppConfig:
    return AgentAppConfig(
        app_id="A001",
        agent_name="test-agent",
        slack_name="testbot",
        bot_token="xoxb-test-token",
        signing_secret="test-secret",
        display_name="Test Bot",
        reply_in_thread_without_mention=enabled,
    )


def _session(status: SessionStatus = SessionStatus.ACTIVE) -> AgentSession:
    now = datetime.now(UTC)
    return AgentSession(
        session_id=f"C123:{THREAD_TS}:A001",
        channel_id="C123",
        thread_ts=THREAD_TS,
        creator_slack_user_id="U999",
        app_id="A001",
        agent_name="test-agent",
        status=status,
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(hours=8),
    )


def _event(**overrides: object) -> dict:
    event: dict = {
        "type": "message",
        "channel": "C123",
        "user": "U456",
        "ts": "1234567999.000",
        "thread_ts": THREAD_TS,
        "text": "and what about yesterday?",
    }
    event.update(overrides)
    return {k: v for k, v in event.items() if v is not None}


@pytest.mark.asyncio
async def test_forwards_reply_when_enabled_and_session_active() -> None:
    with (
        patch("ypl.slack_agent_gateway.events.get_session", new_callable=AsyncMock, return_value=_session()) as gs,
        patch(
            "ypl.slack_agent_gateway.events.handle_app_mention",
            new_callable=AsyncMock,
            return_value={"status": "session_updated"},
        ) as ham,
    ):
        result = await handle_thread_message(_event(), _config())

    assert result == {"status": "session_updated"}
    gs.assert_awaited_once_with(f"C123:{THREAD_TS}:A001")
    ham.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_share_subtype_is_forwarded() -> None:
    with (
        patch("ypl.slack_agent_gateway.events.get_session", new_callable=AsyncMock, return_value=_session()),
        patch(
            "ypl.slack_agent_gateway.events.handle_app_mention",
            new_callable=AsyncMock,
            return_value={"status": "session_updated"},
        ) as ham,
    ):
        await handle_thread_message(_event(subtype="file_share"), _config())
    ham.assert_awaited_once()


@pytest.mark.parametrize(
    ("config", "event", "reason"),
    [
        (_config(enabled=False), _event(), "reply_in_thread_without_mention disabled"),
        (_config(), _event(subtype="message_changed"), "ignored subtype message_changed"),
        (_config(), _event(bot_id="B123"), "bot message"),
        (_config(), _event(thread_ts=None), "not a thread reply"),
        (_config(), _event(thread_ts="1234567999.000"), "not a thread reply"),
        (_config(), _event(text="<@UBOT> hi"), "message contains a mention"),
        (_config(), _event(text="cc <@U777|alice> can you check"), "message contains a mention"),
    ],
)
@pytest.mark.asyncio
async def test_skipped_without_session_lookup(config: AgentAppConfig, event: dict, reason: str) -> None:
    with (
        patch("ypl.slack_agent_gateway.events.get_session", new_callable=AsyncMock) as gs,
        patch("ypl.slack_agent_gateway.events.handle_app_mention", new_callable=AsyncMock) as ham,
    ):
        result = await handle_thread_message(event, config)

    assert result == {"status": "skipped", "reason": reason}
    gs.assert_not_awaited()
    ham.assert_not_awaited()


@pytest.mark.parametrize("session", [None, _session(SessionStatus.EXPIRED)])
@pytest.mark.asyncio
async def test_skipped_without_active_session(session: AgentSession | None) -> None:
    with (
        patch("ypl.slack_agent_gateway.events.get_session", new_callable=AsyncMock, return_value=session),
        patch("ypl.slack_agent_gateway.events.handle_app_mention", new_callable=AsyncMock) as ham,
    ):
        result = await handle_thread_message(_event(), _config())

    assert result == {"status": "skipped", "reason": "no active session for thread"}
    ham.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_callback_routes_message_events() -> None:
    payload = {"event_id": "Ev1", "api_app_id": "A001", "event": _event()}
    with (
        patch("ypl.slack_agent_gateway.events.try_claim_event", new_callable=AsyncMock, return_value=True),
        patch(
            "ypl.slack_agent_gateway.events.get_agent_config_by_app_id",
            new_callable=AsyncMock,
            return_value=_config(),
        ),
        patch(
            "ypl.slack_agent_gateway.events.handle_thread_message",
            new_callable=AsyncMock,
            return_value={"status": "skipped", "reason": "x"},
        ) as htm,
    ):
        response = await handle_event_callback(payload)

    assert response.status_code == 200
    htm.assert_awaited_once()
