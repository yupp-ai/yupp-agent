"""Extended unit tests for SAG events module.

Covers additional branches not in test_events_core.py:
- _matches_any_pattern
- _check_channel_allowed
- _handle_stop_command
- _handle_attach_command
- handle_app_mention (channel denied, invalid event, stop/attach commands, new/existing session)
- _add_ack_reaction
- _post_placeholder
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.slack_agent_gateway.events import (
    _check_channel_allowed,
    _matches_any_pattern,
    handle_app_mention,
)
from ypl.slack_agent_gateway.types import AgentAppConfig, AgentSession, SessionStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_config(app_id: str = "A001") -> AgentAppConfig:
    return AgentAppConfig(
        app_id=app_id,
        agent_name="test-agent",
        slack_name="testbot",
        bot_token="xoxb-test-token",
        signing_secret="test-secret",
        display_name="Test Bot",
    )


def _make_session(
    session_id: str = "C123:1234567890.000:A001",
    channel_id: str = "C123",
    thread_ts: str = "1234567890.000",
    app_id: str = "A001",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> AgentSession:
    now = datetime.now(UTC)
    return AgentSession(
        session_id=session_id,
        channel_id=channel_id,
        channel_name="general",
        thread_ts=thread_ts,
        creator_slack_user_id="U999",
        creator_slack_username="testuser",
        app_id=app_id,
        agent_name="test-agent",
        status=status,
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(hours=8),
    )


def _make_mention_event(
    channel: str = "C123",
    user: str = "U456",
    ts: str = "111.000",
    thread_ts: str | None = None,
    text: str = "<@UBOT> hello",
    files: list | None = None,
) -> dict:
    event: dict = {
        "type": "app_mention",
        "channel": channel,
        "user": user,
        "ts": ts,
        "text": text,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if files is not None:
        event["files"] = files
    return event


# ---------------------------------------------------------------------------
# _matches_any_pattern
# ---------------------------------------------------------------------------


class TestMatchesAnyPattern:
    def test_exact_match(self) -> None:
        assert _matches_any_pattern("general", ["general"]) is True

    def test_wildcard_matches_all(self) -> None:
        assert _matches_any_pattern("random-channel", [".*"]) is True

    def test_prefix_regex(self) -> None:
        assert _matches_any_pattern("alert-backend", ["alert-.*"]) is True

    def test_no_match(self) -> None:
        assert _matches_any_pattern("general", ["alert-.*", "monitoring-.*"]) is False

    def test_empty_patterns(self) -> None:
        assert _matches_any_pattern("general", []) is False

    def test_invalid_regex_skipped(self) -> None:
        # Invalid regex should be skipped, not crash
        assert _matches_any_pattern("general", ["[invalid", "general"]) is True

    def test_partial_match_not_counted(self) -> None:
        # fullmatch is used, so "gen" does not match "general"
        assert _matches_any_pattern("general", ["gen"]) is False


# ---------------------------------------------------------------------------
# _check_channel_allowed
# ---------------------------------------------------------------------------


class TestCheckChannelAllowed:
    @pytest.mark.asyncio
    async def test_channel_not_on_allowlist_denied(self) -> None:
        mock_settings = MagicMock()
        mock_settings.channel_denylist = []
        mock_settings.channel_allowlist = ["allowed-.*"]

        with (
            patch(
                "ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings",
                new_callable=AsyncMock,
                return_value=mock_settings,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_channel_name_by_id",
                new_callable=AsyncMock,
                return_value="random-channel",
            ),
        ):
            # Clear the cache so we always hit the mock
            _check_channel_allowed.cache_clear()
            is_allowed, patterns, name = await _check_channel_allowed("xoxb-tok", "C000")

        assert is_allowed is False
        assert name == "random-channel"

    @pytest.mark.asyncio
    async def test_channel_on_allowlist_allowed(self) -> None:
        mock_settings = MagicMock()
        mock_settings.channel_denylist = []
        mock_settings.channel_allowlist = ["allowed-.*"]

        with (
            patch(
                "ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings",
                new_callable=AsyncMock,
                return_value=mock_settings,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_channel_name_by_id",
                new_callable=AsyncMock,
                return_value="allowed-eng",
            ),
        ):
            _check_channel_allowed.cache_clear()
            is_allowed, patterns, name = await _check_channel_allowed("xoxb-tok", "C001")

        assert is_allowed is True
        assert name == "allowed-eng"

    @pytest.mark.asyncio
    async def test_channel_on_denylist_denied(self) -> None:
        mock_settings = MagicMock()
        mock_settings.channel_denylist = ["blocked-.*"]
        mock_settings.channel_allowlist = [".*"]

        with (
            patch(
                "ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings",
                new_callable=AsyncMock,
                return_value=mock_settings,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_channel_name_by_id",
                new_callable=AsyncMock,
                return_value="blocked-alerts",
            ),
        ):
            _check_channel_allowed.cache_clear()
            is_allowed, patterns, name = await _check_channel_allowed("xoxb-tok", "C002")

        assert is_allowed is False
        assert name == "blocked-alerts"

    @pytest.mark.asyncio
    async def test_unresolvable_channel_denied(self) -> None:
        mock_settings = MagicMock()
        mock_settings.channel_denylist = []
        mock_settings.channel_allowlist = [".*"]

        with (
            patch(
                "ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings",
                new_callable=AsyncMock,
                return_value=mock_settings,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_channel_name_by_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            _check_channel_allowed.cache_clear()
            is_allowed, patterns, name = await _check_channel_allowed("xoxb-tok", "C003")

        assert is_allowed is False
        assert name is None

    @pytest.mark.asyncio
    async def test_empty_allowlist_denies_all(self) -> None:
        mock_settings = MagicMock()
        mock_settings.channel_denylist = []
        mock_settings.channel_allowlist = []

        with (
            patch(
                "ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings",
                new_callable=AsyncMock,
                return_value=mock_settings,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_channel_name_by_id",
                new_callable=AsyncMock,
                return_value="general",
            ),
        ):
            _check_channel_allowed.cache_clear()
            is_allowed, patterns, name = await _check_channel_allowed("xoxb-tok", "C004")

        assert is_allowed is False


# ---------------------------------------------------------------------------
# handle_app_mention — invalid event
# ---------------------------------------------------------------------------


class TestHandleAppMentionInvalidEvent:
    @pytest.mark.asyncio
    async def test_missing_channel_returns_invalid(self) -> None:
        event = {"user": "U1", "ts": "111.0", "text": "hi"}
        app_config = _make_app_config()
        result = await handle_app_mention(event, app_config)
        assert result["status"] == "invalid_event"

    @pytest.mark.asyncio
    async def test_missing_user_returns_invalid(self) -> None:
        event = {"channel": "C1", "ts": "111.0", "text": "hi"}
        app_config = _make_app_config()
        result = await handle_app_mention(event, app_config)
        assert result["status"] == "invalid_event"

    @pytest.mark.asyncio
    async def test_missing_ts_returns_invalid(self) -> None:
        event = {"channel": "C1", "user": "U1", "text": "hi"}
        app_config = _make_app_config()
        result = await handle_app_mention(event, app_config)
        assert result["status"] == "invalid_event"


# ---------------------------------------------------------------------------
# handle_app_mention — channel denied
# ---------------------------------------------------------------------------


class TestHandleAppMentionChannelDenied:
    @pytest.mark.asyncio
    async def test_channel_denied_returns_channel_denied(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event()

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(False, ["allowed-.*"], "random-channel"),
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "channel_denied"
        assert result["channel_id"] == "C123"


# ---------------------------------------------------------------------------
# handle_app_mention — stop command
# ---------------------------------------------------------------------------


class TestHandleAppMentionStopCommand:
    @pytest.mark.asyncio
    async def test_stop_command_no_session(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /stop")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "no_session"

    @pytest.mark.asyncio
    async def test_stop_command_with_session_stopped(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /stop")
        session = _make_session()

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()
        mock_client.chat_delete = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session",
                new_callable=AsyncMock,
                return_value=session,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.stop_agent_session",
                new_callable=AsyncMock,
                return_value={"status": "stopped"},
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.save_session",
                new_callable=AsyncMock,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_command_no_inflight_turn(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /stop")
        session = _make_session()

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session",
                new_callable=AsyncMock,
                return_value=session,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.stop_agent_session",
                new_callable=AsyncMock,
                return_value={"status": "no_inflight_turn"},
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.save_session",
                new_callable=AsyncMock,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "no_inflight_turn"

    @pytest.mark.asyncio
    async def test_stop_command_with_placeholder_cleaned_up(self) -> None:
        """Placeholder should be deleted before stopping."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /stop")
        session = _make_session()
        session.placeholder_ts = "999.000"

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()
        mock_client.chat_delete = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session",
                new_callable=AsyncMock,
                return_value=session,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.stop_agent_session",
                new_callable=AsyncMock,
                return_value={"status": "stopped"},
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.save_session",
                new_callable=AsyncMock,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "stopped"
        mock_client.chat_delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_app_mention — attach command
# ---------------------------------------------------------------------------


class TestHandleAppMentionAttachCommand:
    @pytest.mark.asyncio
    async def test_attach_rejected_not_top_level(self) -> None:
        """attach command only works on top-level messages."""
        app_config = _make_app_config()
        # thread_ts != ts → not a top-level message
        event = _make_mention_event(
            text="<@UBOT> /attach some-uuid",
            ts="111.000",
            thread_ts="100.000",
        )

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attach_rejected"
        assert result["reason"] == "not_top_level"

    @pytest.mark.asyncio
    async def test_attach_rejected_missing_session_id(self) -> None:
        """attach without a UUID should be rejected."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /attach", ts="111.000")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attach_rejected"
        assert result["reason"] == "missing_session_id"

    @pytest.mark.asyncio
    async def test_attach_rejected_invalid_uuid(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /attach not-a-uuid", ts="111.000")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attach_rejected"
        assert result["reason"] == "invalid_uuid"

    @pytest.mark.asyncio
    async def test_attach_rejected_session_not_found(self) -> None:
        import uuid as _uuid

        app_config = _make_app_config()
        valid_uuid = str(_uuid.uuid4())
        event = _make_mention_event(text=f"<@UBOT> /attach {valid_uuid}", ts="111.000")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session_info",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attach_rejected"
        assert result["reason"] == "session_not_found"

    @pytest.mark.asyncio
    async def test_attach_failed_ahs_error(self) -> None:
        import uuid as _uuid

        app_config = _make_app_config()
        valid_uuid = str(_uuid.uuid4())
        event = _make_mention_event(text=f"<@UBOT> /attach {valid_uuid}", ts="111.000")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        session_detail = {"session": {"agent_name": "test-agent", "message_count": 5, "status": "active"}}

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session_info",
                new_callable=AsyncMock,
                return_value=session_detail,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.attach_slack_to_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attach_failed"
        assert result["reason"] == "ahs_attach_error"

    @pytest.mark.asyncio
    async def test_attach_success(self) -> None:
        import uuid as _uuid

        app_config = _make_app_config()
        valid_uuid = str(_uuid.uuid4())
        event = _make_mention_event(text=f"<@UBOT> /attach {valid_uuid}", ts="111.000")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        session_detail = {
            "session": {
                "agent_name": "test-agent",
                "message_count": 3,
                "status": "active",
                "created_at": "2025-01-01T00:00:00",
            }
        }
        fake_session = _make_session()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.get_session_info",
                new_callable=AsyncMock,
                return_value=session_detail,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.attach_slack_to_session",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.store_thread_session_mapping",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.mention_commands.create_sag_session",
                new_callable=AsyncMock,
                return_value=fake_session,
            ),
            patch("ypl.slack_agent_gateway.mention_commands.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "attached"
        assert result["ahs_session_id"] == valid_uuid


# ---------------------------------------------------------------------------
# handle_app_mention — normal message flow
# ---------------------------------------------------------------------------


class TestHandleAppMentionNormalFlow:
    @pytest.mark.asyncio
    async def test_new_session_created(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> hello, can you help?")
        session = _make_session()

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "777.000"})

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_or_create_session",
                new_callable=AsyncMock,
                return_value=(session, True),
            ),
            patch(
                "ypl.slack_agent_gateway.events._post_placeholder",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.events.create_agent_session",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "session_created"
        assert result["forwarded"] is True

    @pytest.mark.asyncio
    async def test_existing_session_updated(self) -> None:
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> follow-up question", thread_ts="1234567890.000")
        session = _make_session()

        mock_client = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_or_create_session",
                new_callable=AsyncMock,
                return_value=(session, False),
            ),
            patch(
                "ypl.slack_agent_gateway.events._post_placeholder",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_message_to_agent",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "session_updated"
        assert result["forwarded"] is True

    @pytest.mark.asyncio
    async def test_ahs_failure_cleans_up_placeholder(self) -> None:
        """When AHS returns None, placeholder should be cleaned up."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> do something")
        session = _make_session()
        session.placeholder_ts = "555.000"

        mock_client = AsyncMock()
        mock_client.chat_delete = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_or_create_session",
                new_callable=AsyncMock,
                return_value=(session, True),
            ),
            patch(
                "ypl.slack_agent_gateway.events._post_placeholder",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.events.create_agent_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.save_session",
                new_callable=AsyncMock,
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["forwarded"] is False
        mock_client.chat_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_queued_result_updates_placeholder(self) -> None:
        """Queued status should update placeholder message."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> run a task")
        session = _make_session()
        session.placeholder_ts = "333.000"

        mock_client = AsyncMock()
        mock_client.chat_update = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_or_create_session",
                new_callable=AsyncMock,
                return_value=(session, True),
            ),
            patch(
                "ypl.slack_agent_gateway.events._post_placeholder",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.events.create_agent_session",
                new_callable=AsyncMock,
                return_value={"status": "queued"},
            ),
            patch(
                "ypl.slack_agent_gateway.events.save_session",
                new_callable=AsyncMock,
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["forwarded"] is True
        mock_client.chat_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_ahs_session_sends_message(self) -> None:
        """When a thread was started by an agent, message is sent to existing AHS session."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> reply to agent", thread_ts="1234567890.000")
        session = _make_session()

        mock_client = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value="existing-ahs-session-uuid",
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_or_create_session",
                new_callable=AsyncMock,
                return_value=(session, False),
            ),
            patch(
                "ypl.slack_agent_gateway.events._post_placeholder",
                new_callable=AsyncMock,
            ),
            patch(
                "ypl.slack_agent_gateway.events.send_message_to_agent",
                new_callable=AsyncMock,
                return_value={"status": "ok"},
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "session_updated"
        assert result["forwarded"] is True

    @pytest.mark.asyncio
    async def test_model_directive_invalid_model(self) -> None:
        """Invalid /model: directive should post an error and bail."""
        app_config = _make_app_config()
        event = _make_mention_event(text="<@UBOT> /model:nonexistent/model do task")

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        with (
            patch(
                "ypl.slack_agent_gateway.events._check_channel_allowed",
                new_callable=AsyncMock,
                return_value=(True, [".*"], "general"),
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_ahs_session_for_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.events.get_available_models",
                new_callable=AsyncMock,
                return_value={"harnessed": ["claude-3-5"], "raw": ["gpt-4o"]},
            ),
            patch("ypl.slack_agent_gateway.events.build_slack_client", return_value=mock_client),
            patch("ypl.slack_agent_gateway.events.create_background_task"),
        ):
            result = await handle_app_mention(event, app_config)

        assert result["status"] == "model_not_found"
        assert result["model"] == "nonexistent/model"
