"""Unit tests for SAG sessions module.

Covers:
- create_session
- get_or_create_session (new / existing / expired)
- get_session_info (found / not found)
- record_reply
- _sanitize_filename
- _is_slack_host
- _download_slack_file
- process_slack_attachments
- build_message_from_event
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from ypl.slack_agent_gateway.sessions import (
    _is_slack_host,
    _sanitize_filename,
    build_message_from_event,
    create_session,
    get_or_create_session,
    get_session_info,
    process_slack_attachments,
    record_reply,
)
from ypl.slack_agent_gateway.types import AgentSession, SessionStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    channel_id: str = "C123",
    thread_ts: str = "111.000",
    app_id: str = "A001",
    status: SessionStatus = SessionStatus.ACTIVE,
) -> AgentSession:
    now = datetime.now(UTC)
    return AgentSession(
        session_id=AgentSession.build_session_id(channel_id, thread_ts, app_id),
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


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_safe_filename_unchanged(self) -> None:
        assert _sanitize_filename("hello.txt") == "hello.txt"

    def test_spaces_replaced_with_underscore(self) -> None:
        result = _sanitize_filename("my file name.pdf")
        assert " " not in result
        assert result.endswith(".pdf")

    def test_special_chars_replaced(self) -> None:
        result = _sanitize_filename("file!@#$%.txt")
        assert "!" not in result
        assert "@" not in result
        assert "#" not in result

    def test_dash_underscore_dot_preserved(self) -> None:
        result = _sanitize_filename("my-file_name.txt")
        assert result == "my-file_name.txt"

    def test_long_filename_truncated(self) -> None:
        long_name = "a" * 300 + ".txt"
        result = _sanitize_filename(long_name, max_length=200)
        assert len(result) <= 200

    def test_extension_preserved_on_truncation(self) -> None:
        long_name = "a" * 300 + ".txt"
        result = _sanitize_filename(long_name, max_length=200)
        assert result.endswith(".txt")

    def test_empty_string(self) -> None:
        result = _sanitize_filename("")
        assert result == ""

    def test_no_extension_truncated(self) -> None:
        long_name = "a" * 300
        result = _sanitize_filename(long_name, max_length=50)
        assert len(result) == 50

    def test_very_long_extension(self) -> None:
        """Extension longer than max_length — truncate without extension."""
        long_ext = "a" * 250
        name = f"file.{long_ext}"
        result = _sanitize_filename(name, max_length=200)
        assert len(result) <= 200


# ---------------------------------------------------------------------------
# _is_slack_host
# ---------------------------------------------------------------------------


class TestIsSlackHost:
    def test_slack_com(self) -> None:
        assert _is_slack_host("https://files.slack.com/files/some-file") is True

    def test_slack_edge_com(self) -> None:
        assert _is_slack_host("https://a.slack-edge.com/image.png") is True

    def test_slack_msgs_com(self) -> None:
        assert _is_slack_host("https://b.slack-msgs.com/file.txt") is True

    def test_external_cdn(self) -> None:
        assert _is_slack_host("https://cdn.example.com/image.png") is False

    def test_non_slack_with_slack_in_path(self) -> None:
        assert _is_slack_host("https://notslack.com/slack/file") is False

    def test_empty_url(self) -> None:
        assert _is_slack_host("") is False


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_and_saves_session(self) -> None:
        with patch(
            "ypl.slack_agent_gateway.sessions.save_session",
            new_callable=AsyncMock,
        ) as mock_save:
            session = await create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                creator_slack_user_id="U999",
            )

        mock_save.assert_awaited_once()
        assert session.channel_id == "C123"
        assert session.thread_ts == "111.000"
        assert session.app_id == "A001"
        assert session.agent_name == "test-agent"
        assert session.creator_slack_user_id == "U999"
        assert session.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_session_id_format(self) -> None:
        with patch("ypl.slack_agent_gateway.sessions.save_session", new_callable=AsyncMock):
            session = await create_session(
                channel_id="C999",
                thread_ts="222.000",
                app_id="A002",
                agent_name="my-agent",
                creator_slack_user_id="U111",
            )

        assert session.session_id == "C999:222.000:A002"

    @pytest.mark.asyncio
    async def test_optional_fields(self) -> None:
        with patch("ypl.slack_agent_gateway.sessions.save_session", new_callable=AsyncMock):
            session = await create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                creator_slack_user_id="U999",
                creator_slack_username="alice",
                channel_name="general",
            )

        assert session.creator_slack_username == "alice"
        assert session.channel_name == "general"

    @pytest.mark.asyncio
    async def test_expires_at_in_future(self) -> None:
        with patch("ypl.slack_agent_gateway.sessions.save_session", new_callable=AsyncMock):
            before = datetime.now(UTC)
            session = await create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                creator_slack_user_id="U999",
            )
            after = datetime.now(UTC)

        assert session.expires_at > before
        assert session.expires_at > after


# ---------------------------------------------------------------------------
# get_or_create_session
# ---------------------------------------------------------------------------


class TestGetOrCreateSession:
    @pytest.mark.asyncio
    async def test_creates_new_session_when_none_exists(self) -> None:
        with (
            patch(
                "ypl.slack_agent_gateway.sessions.get_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.save_session",
                new_callable=AsyncMock,
            ),
        ):
            session, is_new = await get_or_create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                user_id="U456",
            )

        assert is_new is True
        assert session.channel_id == "C123"

    @pytest.mark.asyncio
    async def test_reuses_existing_session(self) -> None:
        existing = _make_session()

        with (
            patch(
                "ypl.slack_agent_gateway.sessions.get_session",
                new_callable=AsyncMock,
                return_value=existing,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.update_session_activity",
                new_callable=AsyncMock,
                return_value=existing,
            ),
        ):
            session, is_new = await get_or_create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                user_id="U456",
            )

        assert is_new is False
        assert session.session_id == existing.session_id

    @pytest.mark.asyncio
    async def test_creates_new_when_update_activity_returns_none(self) -> None:
        """If update_session_activity returns None, fall back to creating a new session."""
        existing = _make_session()

        with (
            patch(
                "ypl.slack_agent_gateway.sessions.get_session",
                new_callable=AsyncMock,
                return_value=existing,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.update_session_activity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.save_session",
                new_callable=AsyncMock,
            ),
        ):
            session, is_new = await get_or_create_session(
                channel_id="C123",
                thread_ts="111.000",
                app_id="A001",
                agent_name="test-agent",
                user_id="U456",
            )

        assert is_new is True


# ---------------------------------------------------------------------------
# get_session_info
# ---------------------------------------------------------------------------


class TestGetSessionInfo:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_session(self) -> None:
        with patch(
            "ypl.slack_agent_gateway.sessions.get_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_session_info("nonexistent-id")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_session_info_response(self) -> None:
        session = _make_session()
        with patch(
            "ypl.slack_agent_gateway.sessions.get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await get_session_info(session.session_id)

        assert result is not None
        assert result.session_id == session.session_id
        assert result.channel_id == session.channel_id
        assert result.agent_name == session.agent_name
        assert result.status == session.status

    @pytest.mark.asyncio
    async def test_response_includes_all_fields(self) -> None:
        session = _make_session()
        with patch(
            "ypl.slack_agent_gateway.sessions.get_session",
            new_callable=AsyncMock,
            return_value=session,
        ):
            result = await get_session_info(session.session_id)

        assert result is not None
        assert result.creator_slack_user_id == "U999"
        assert result.creator_slack_username == "testuser"
        assert result.channel_name == "general"


# ---------------------------------------------------------------------------
# record_reply
# ---------------------------------------------------------------------------


class TestRecordReply:
    @pytest.mark.asyncio
    async def test_delegates_to_update_session_reply(self) -> None:
        session = _make_session()
        with patch(
            "ypl.slack_agent_gateway.sessions.update_session_reply",
            new_callable=AsyncMock,
            return_value=session,
        ) as mock_update:
            result = await record_reply(
                session_id="C123:111.000:A001",
                reply_ts="222.000",
                reply_content="hello reply",
                reply_type="thinking",
            )

        mock_update.assert_awaited_once_with(
            "C123:111.000:A001",
            "222.000",
            "hello reply",
            reply_type="thinking",
        )
        assert result == session

    @pytest.mark.asyncio
    async def test_returns_none_when_session_not_found(self) -> None:
        with patch(
            "ypl.slack_agent_gateway.sessions.update_session_reply",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await record_reply(
                session_id="nonexistent",
                reply_ts="222.000",
                reply_content="hi",
            )

        assert result is None


# ---------------------------------------------------------------------------
# build_message_from_event
# ---------------------------------------------------------------------------


class TestBuildMessageFromEvent:
    def test_basic_event(self) -> None:
        event = {"text": "hello", "user": "U123", "ts": "111.000"}
        msg = build_message_from_event(event)
        assert msg.text == "hello"
        assert msg.sender.slack_user_id == "U123"
        assert msg.ts == "111.000"
        assert msg.attachments == []

    def test_with_attachments(self) -> None:
        from ypl.slack_agent_gateway.types import Attachment

        event = {"text": "check this", "user": "U123", "ts": "111.000"}
        att = Attachment(filename="test.pdf", content_type="application/pdf", size=100, gcs_url="gs://b/test.pdf")
        msg = build_message_from_event(event, attachments=[att])
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "test.pdf"

    def test_empty_event_uses_defaults(self) -> None:
        msg = build_message_from_event({})
        assert msg.text == ""
        assert msg.sender.slack_user_id == ""
        assert msg.ts == ""


# ---------------------------------------------------------------------------
# process_slack_attachments
# ---------------------------------------------------------------------------


class TestProcessSlackAttachments:
    @pytest.mark.asyncio
    async def test_skips_file_missing_download_url(self) -> None:
        files = [{"id": "F001", "name": "test.txt"}]
        result = await process_slack_attachments(files, "sess-1", "xoxb-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_oversized_file(self) -> None:
        files = [
            {
                "id": "F001",
                "name": "big.txt",
                "url_private_download": "https://files.slack.com/big.txt",
                "mimetype": "text/plain",
                "size": 30 * 1024 * 1024,  # 30 MB > 20 MB limit
            }
        ]
        result = await process_slack_attachments(files, "sess-1", "xoxb-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_upload(self) -> None:
        files = [
            {
                "id": "F001",
                "name": "report.pdf",
                "url_private_download": "https://files.slack.com/report.pdf",
                "mimetype": "application/pdf",
                "size": 1024,
            }
        ]
        mock_data = b"pdf content here"

        with (
            patch(
                "ypl.slack_agent_gateway.sessions._download_slack_file",
                new_callable=AsyncMock,
                return_value=mock_data,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.upload_to_gcs",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_slack_attachments(files, "sess-abc", "xoxb-token")

        assert len(result) == 1
        assert result[0].filename == "report.pdf"
        assert result[0].content_type == "application/pdf"
        assert result[0].size == len(mock_data)
        assert "sess-abc" in result[0].gcs_url

    @pytest.mark.asyncio
    async def test_deduplicates_filenames(self) -> None:
        files = [
            {
                "id": "F001",
                "name": "file.txt",
                "url_private_download": "https://files.slack.com/1",
                "mimetype": "text/plain",
                "size": 100,
            },
            {
                "id": "F002",
                "name": "file.txt",
                "url_private_download": "https://files.slack.com/2",
                "mimetype": "text/plain",
                "size": 100,
            },
        ]
        mock_data = b"content"

        with (
            patch(
                "ypl.slack_agent_gateway.sessions._download_slack_file",
                new_callable=AsyncMock,
                return_value=mock_data,
            ),
            patch(
                "ypl.slack_agent_gateway.sessions.upload_to_gcs",
                new_callable=AsyncMock,
            ),
        ):
            result = await process_slack_attachments(files, "sess-abc", "xoxb-token")

        assert len(result) == 2
        filenames = [r.filename for r in result]
        assert len(set(filenames)) == 2  # Both should be unique

    @pytest.mark.asyncio
    async def test_download_error_skips_file(self) -> None:
        files = [
            {
                "id": "F001",
                "name": "error.txt",
                "url_private_download": "https://files.slack.com/error",
                "mimetype": "text/plain",
                "size": 100,
            }
        ]

        with patch(
            "ypl.slack_agent_gateway.sessions._download_slack_file",
            new_callable=AsyncMock,
            side_effect=ValueError("File too large"),
        ):
            result = await process_slack_attachments(files, "sess-1", "xoxb-token")

        assert result == []

    @pytest.mark.asyncio
    async def test_unexpected_error_skips_file(self) -> None:
        files = [
            {
                "id": "F001",
                "name": "boom.txt",
                "url_private_download": "https://files.slack.com/boom",
                "mimetype": "text/plain",
                "size": 100,
            }
        ]

        with patch(
            "ypl.slack_agent_gateway.sessions._download_slack_file",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Connection error"),
        ):
            result = await process_slack_attachments(files, "sess-1", "xoxb-token")

        assert result == []
