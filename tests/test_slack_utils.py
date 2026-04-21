"""Unit tests for ypl/backend/utils/slack_utils.py.

Tests signature verification, rich text parsing, user resolution,
and environment-based routing.
"""

from __future__ import annotations
import hashlib
import hmac
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from ypl.backend.utils.slack_utils import (
    SlackCommandType,
    SlackEventType,
    SlackPayloadType,
    create_slack_link,
    extract_plain_text_from_slack_blocks,
    get_abuse_alert_channel_id,
    get_backend_alert_channels,
    get_guest_management_channel_id,
    get_slack_client,
    get_slack_httpx_client,
    get_slack_sync_client,
    get_slack_user_by_email,
    get_user_email_from_slack,
    is_local_environment,
    is_prod_environment,
    is_staging_environment,
    post_threaded_slack_messages,
    post_to_slack,
    post_to_slack_response_url,
    post_to_slack_sync,
    resolve_slack_recipient,
    resolve_slack_user_to_yupp_user_id,
    verify_slack_signature,
)

# ---------------------------------------------------------------------------
# Tests: Environment helpers
# ---------------------------------------------------------------------------


class TestEnvironmentHelpers:
    def test_is_prod_environment(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            assert is_prod_environment() is True
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            assert is_prod_environment() is False

    def test_is_staging_environment(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            assert is_staging_environment() is True
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            assert is_staging_environment() is False

    def test_is_local_environment(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            assert is_local_environment() is True
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            assert is_local_environment() is False


# ---------------------------------------------------------------------------
# Tests: Channel ID helpers
# ---------------------------------------------------------------------------


class TestChannelIdHelpers:
    def test_abuse_alert_channel_production(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            channel = get_abuse_alert_channel_id()
        assert channel == "C08HZTSQ4GZ"

    def test_abuse_alert_channel_staging(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            channel = get_abuse_alert_channel_id()
        assert channel == "C0911Q988AK"

    def test_abuse_alert_channel_default(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            channel = get_abuse_alert_channel_id()
        assert channel == "C0911Q988AK"

    def test_backend_alert_channel_production(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            channel = get_backend_alert_channels()
        assert channel == "alert-backend"

    def test_backend_alert_channel_staging(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            channel = get_backend_alert_channels()
        assert channel == "alert-backend-staging"

    def test_backend_alert_channel_default(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            channel = get_backend_alert_channels()
        assert channel == "alert-test-only"

    def test_guest_management_channel_production(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            channel = get_guest_management_channel_id()
        assert channel == "C085UEJ6JM6"

    def test_guest_management_channel_staging(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            channel = get_guest_management_channel_id()
        assert channel == "C0826FU9JMB"


# ---------------------------------------------------------------------------
# Tests: verify_slack_signature
# ---------------------------------------------------------------------------


class TestVerifySlackSignature:
    def _make_request(self, headers: dict[str, str]) -> MagicMock:
        req = MagicMock()
        req.headers = headers
        return req

    def _make_valid_signature(self, signing_secret: str, timestamp: str, body: bytes) -> str:
        sig_basestring = f"v0:{timestamp}:".encode() + body
        computed = hmac.new(signing_secret.encode(), sig_basestring, hashlib.sha256).hexdigest()
        return f"v0={computed}"

    def test_skips_verification_in_local_env(self) -> None:
        req = self._make_request({})
        with patch("ypl.backend.utils.slack_utils.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "local"
            # Should not raise
            verify_slack_signature(req, b"body", "secret")

    def test_raises_when_signature_missing(self) -> None:
        req = self._make_request({"X-Slack-Request-Timestamp": str(int(time.time()))})
        with patch("ypl.backend.utils.slack_utils.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            with pytest.raises(HTTPException) as exc_info:
                verify_slack_signature(req, b"body", "secret")
        assert exc_info.value.status_code == 401

    def test_raises_when_timestamp_missing(self) -> None:
        req = self._make_request({"X-Slack-Signature": "v0=abc"})
        with patch("ypl.backend.utils.slack_utils.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            with pytest.raises(HTTPException) as exc_info:
                verify_slack_signature(req, b"body", "secret")
        assert exc_info.value.status_code == 401

    def test_raises_when_timestamp_too_old(self) -> None:
        old_ts = str(int(time.time()) - 400)  # 400 seconds ago
        req = self._make_request(
            {
                "X-Slack-Signature": "v0=dummy",
                "X-Slack-Request-Timestamp": old_ts,
            }
        )
        with (
            patch("ypl.backend.utils.slack_utils.settings") as mock_settings,
            patch("ypl.backend.utils.slack_utils.get_logger", return_value=MagicMock()),
        ):
            mock_settings.ENVIRONMENT = "production"
            with pytest.raises(HTTPException) as exc_info:
                verify_slack_signature(req, b"body", "secret")
        assert exc_info.value.status_code == 401

    def test_raises_when_invalid_timestamp_format(self) -> None:
        req = self._make_request(
            {
                "X-Slack-Signature": "v0=abc",
                "X-Slack-Request-Timestamp": "not-a-number",
            }
        )
        with (
            patch("ypl.backend.utils.slack_utils.settings") as mock_settings,
            patch("ypl.backend.utils.slack_utils.get_logger", return_value=MagicMock()),
        ):
            mock_settings.ENVIRONMENT = "production"
            with pytest.raises(HTTPException) as exc_info:
                verify_slack_signature(req, b"body", "secret")
        assert exc_info.value.status_code == 401

    def test_raises_on_signature_mismatch(self) -> None:
        timestamp = str(int(time.time()))
        req = self._make_request(
            {
                "X-Slack-Signature": "v0=wrongsignature",
                "X-Slack-Request-Timestamp": timestamp,
            }
        )
        with (
            patch("ypl.backend.utils.slack_utils.settings") as mock_settings,
            patch("ypl.backend.utils.slack_utils.get_logger", return_value=MagicMock()),
        ):
            mock_settings.ENVIRONMENT = "production"
            with pytest.raises(HTTPException) as exc_info:
                verify_slack_signature(req, b"body", "real-secret")
        assert exc_info.value.status_code == 401

    def test_passes_with_valid_signature(self) -> None:
        signing_secret = "test-signing-secret"
        body = b'{"key": "value"}'
        timestamp = str(int(time.time()))
        valid_sig = self._make_valid_signature(signing_secret, timestamp, body)

        req = self._make_request(
            {
                "X-Slack-Signature": valid_sig,
                "X-Slack-Request-Timestamp": timestamp,
            }
        )
        with patch("ypl.backend.utils.slack_utils.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            # Should not raise
            verify_slack_signature(req, body, signing_secret)


# ---------------------------------------------------------------------------
# Tests: extract_plain_text_from_slack_blocks
# ---------------------------------------------------------------------------


class TestExtractPlainTextFromSlackBlocks:
    def test_returns_empty_for_none(self) -> None:
        assert extract_plain_text_from_slack_blocks(None) == ""

    def test_returns_empty_for_empty_list(self) -> None:
        assert extract_plain_text_from_slack_blocks([]) == ""

    def test_extracts_text_from_rich_text(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "World"},
                        ],
                    }
                ],
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == "Hello World"

    def test_extracts_link_text_not_url(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "link", "text": "Click here", "url": "https://example.com"},
                        ],
                    }
                ],
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == "Click here"

    def test_skips_user_mentions(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "Hello "},
                            {"type": "user", "user_id": "U123456"},
                            {"type": "text", "text": "!"},
                        ],
                    }
                ],
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == "Hello !"

    def test_skips_non_rich_text_blocks(self) -> None:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "This should be ignored"},
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == ""

    def test_handles_nested_elements(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "Item 1"}],
                            }
                        ],
                    }
                ],
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == "Item 1"

    def test_strips_whitespace(self) -> None:
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "  trimmed  "},
                        ],
                    }
                ],
            }
        ]
        result = extract_plain_text_from_slack_blocks(blocks)
        assert result == "trimmed"


# ---------------------------------------------------------------------------
# Tests: create_slack_link
# ---------------------------------------------------------------------------


class TestCreateSlackLink:
    def test_creates_basic_link(self) -> None:
        link = create_slack_link("C123", "1234567890.123456")
        assert "C123" in link
        assert "1234567890123456" in link  # dots removed

    def test_includes_thread_ts_when_provided(self) -> None:
        link = create_slack_link("C123", "123.456", main_thread_ts="100.200")
        assert "thread_ts=100.200" in link

    def test_no_thread_ts_when_not_provided(self) -> None:
        link = create_slack_link("C123", "123.456")
        assert "thread_ts" not in link


# ---------------------------------------------------------------------------
# Tests: resolve_slack_recipient
# ---------------------------------------------------------------------------


class TestResolveSlackRecipient:
    async def test_returns_original_when_not_email(self) -> None:
        result = await resolve_slack_recipient("C123ABC", bot_token="xoxb-test")
        assert result == "C123ABC"

    async def test_resolves_email_to_user_id(self) -> None:
        with patch(
            "ypl.backend.utils.slack_utils.get_slack_user_by_email",
            AsyncMock(return_value="U12345"),
        ):
            result = await resolve_slack_recipient("user@example.com", bot_token="xoxb-test")
        assert result == "U12345"

    async def test_falls_back_to_original_when_email_not_found(self) -> None:
        with patch(
            "ypl.backend.utils.slack_utils.get_slack_user_by_email",
            AsyncMock(return_value=None),
        ):
            result = await resolve_slack_recipient("nobody@example.com", bot_token="xoxb-test")
        assert result == "nobody@example.com"


# ---------------------------------------------------------------------------
# Tests: get_user_email_from_slack
# ---------------------------------------------------------------------------


class TestGetUserEmailFromSlack:
    async def test_returns_email_on_success(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(
            return_value={
                "ok": True,
                "user": {"profile": {"email": "user@example.com"}},
            }
        )

        with patch(
            "ypl.backend.utils.slack_utils.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await get_user_email_from_slack("U123456", bot_token="xoxb-test")

        assert result == "user@example.com"

    async def test_returns_none_when_ok_is_false(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(return_value={"ok": False})

        with (
            patch(
                "ypl.backend.utils.slack_utils.AsyncWebClient",
                return_value=mock_client,
            ),
            patch("ypl.backend.utils.slack_utils.get_email_by_slack_user_id", new=AsyncMock(return_value=None)),
        ):
            result = await get_user_email_from_slack("U123456", bot_token="xoxb-test")

        assert result is None

    async def test_returns_none_on_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_info = AsyncMock(side_effect=Exception("API error"))

        with (
            patch(
                "ypl.backend.utils.slack_utils.AsyncWebClient",
                return_value=mock_client,
            ),
            patch("ypl.backend.utils.slack_utils.get_email_by_slack_user_id", new=AsyncMock(return_value=None)),
        ):
            result = await get_user_email_from_slack("U123456", bot_token="xoxb-test")

        assert result is None

    async def test_falls_back_to_db_lookup_without_token(self) -> None:
        with patch(
            "ypl.backend.utils.slack_utils.get_email_by_slack_user_id",
            new=AsyncMock(return_value="mapped@example.com"),
        ):
            result = await get_user_email_from_slack("UMAPPED")

        assert result == "mapped@example.com"


# ---------------------------------------------------------------------------
# Tests: get_slack_user_by_email
# ---------------------------------------------------------------------------


class TestGetSlackUserByEmail:
    async def test_returns_user_id_on_success(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_lookupByEmail = AsyncMock(return_value={"ok": True, "user": {"id": "U98765"}})

        with patch(
            "ypl.backend.utils.slack_utils.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await get_slack_user_by_email("user@example.com", bot_token="xoxb-test")

        assert result == "U98765"

    async def test_returns_none_when_not_found(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_lookupByEmail = AsyncMock(return_value={"ok": False, "error": "users_not_found"})

        with patch(
            "ypl.backend.utils.slack_utils.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await get_slack_user_by_email("nobody@example.com", bot_token="xoxb-test")

        assert result is None

    async def test_returns_none_on_exception(self) -> None:
        mock_client = AsyncMock()
        mock_client.users_lookupByEmail = AsyncMock(side_effect=Exception("API error"))

        with patch(
            "ypl.backend.utils.slack_utils.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await get_slack_user_by_email("user@example.com", bot_token="xoxb-test")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: resolve_slack_user_to_yupp_user_id
# ---------------------------------------------------------------------------


class TestResolveSlackUserToYuppUserId:
    async def test_returns_user_id_via_slack_api(self) -> None:
        from collections.abc import AsyncGenerator
        from contextlib import asynccontextmanager

        mock_db = AsyncMock()
        db_result = MagicMock()
        db_result.first.return_value = MagicMock(__getitem__=lambda self, i: "yupp-user-123")
        mock_db.execute = AsyncMock(return_value=db_result)

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[Any, None]:
            yield mock_db

        with (
            patch(
                "ypl.backend.utils.slack_utils.get_user_email_from_slack",
                AsyncMock(return_value="user@example.com"),
            ),
            patch(
                "ypl.backend.utils.slack_utils.get_async_session_read_replica",
                _ctx,
            ),
        ):
            result = await resolve_slack_user_to_yupp_user_id.__wrapped__("U123456", bot_token="xoxb-test")

        assert result == "yupp-user-123"

    async def test_returns_none_when_no_email_found(self) -> None:
        with (
            patch(
                "ypl.backend.utils.slack_utils.get_user_email_from_slack",
                AsyncMock(return_value=None),
            ),
            patch("ypl.backend.utils.slack_utils.get_email_by_slack_user_id", new=AsyncMock(return_value=None)),
        ):
            result = await resolve_slack_user_to_yupp_user_id.__wrapped__("UUNKNOWN")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: post_to_slack
# ---------------------------------------------------------------------------


class TestPostToSlack:
    async def test_skips_in_local_environment(self, capsys: Any) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            await post_to_slack("test message", "http://webhook.url")
        captured = capsys.readouterr()
        assert "Skipping" in captured.out

    async def test_returns_when_no_url(self) -> None:
        # Should warn and return without error
        with patch.dict(os.environ, {"ENVIRONMENT": "staging", "SLACK_WEBHOOK_URL": ""}, clear=False):
            clean_env = {k: v for k, v in os.environ.items() if k != "SLACK_WEBHOOK_URL"}
            with patch.dict(os.environ, clean_env, clear=True):
                await post_to_slack("test message")

    async def test_posts_message_successfully(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.send = AsyncMock(return_value=mock_response)

        with (
            patch.dict(os.environ, {"ENVIRONMENT": "staging"}),
            patch(
                "ypl.backend.utils.slack_utils.get_slack_client",
                return_value=mock_client,
            ),
        ):
            await post_to_slack("hello", "http://webhook.url")

        mock_client.send.assert_called_once()

    async def test_serializes_dict_message(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.send = AsyncMock(return_value=mock_response)

        with (
            patch.dict(os.environ, {"ENVIRONMENT": "staging"}),
            patch(
                "ypl.backend.utils.slack_utils.get_slack_client",
                return_value=mock_client,
            ),
        ):
            await post_to_slack({"key": "value"}, "http://webhook.url")

        mock_client.send.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: post_to_slack_sync
# ---------------------------------------------------------------------------


class TestPostToSlackSync:
    def test_skips_in_local_environment(self, capsys: Any) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            post_to_slack_sync("test message", "http://webhook.url")
        captured = capsys.readouterr()
        assert "Skipping" in captured.out

    def test_returns_when_no_url(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if k != "SLACK_WEBHOOK_URL"}
        clean_env["ENVIRONMENT"] = "staging"
        with patch.dict(os.environ, clean_env, clear=True):
            post_to_slack_sync("test message")  # Should not raise

    def test_posts_message_successfully(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.send = MagicMock(return_value=mock_response)

        with (
            patch.dict(os.environ, {"ENVIRONMENT": "staging"}),
            patch(
                "ypl.backend.utils.slack_utils.get_slack_sync_client",
                return_value=mock_client,
            ),
        ):
            post_to_slack_sync("hello", "http://webhook.url")

        mock_client.send.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_slack_httpx_client
# ---------------------------------------------------------------------------


class TestGetSlackHttpxClient:
    def test_returns_client_instance(self) -> None:
        # Reset the global client for a clean test
        import ypl.backend.utils.slack_utils as slack_utils

        original = slack_utils._slack_httpx_client
        slack_utils._slack_httpx_client = None
        try:
            client = get_slack_httpx_client()
            assert client is not None
            # Calling again returns the same instance
            client2 = get_slack_httpx_client()
            assert client is client2
        finally:
            slack_utils._slack_httpx_client = original


# ---------------------------------------------------------------------------
# Tests: get_slack_client and get_slack_sync_client
# ---------------------------------------------------------------------------


class TestGetSlackClients:
    def test_get_slack_client_creates_and_caches(self) -> None:
        from ypl.backend.utils.slack_utils import SLACK_CLIENTS

        url = "http://unique-webhook-url-for-test.com"
        SLACK_CLIENTS.pop(url, None)
        client = get_slack_client(url)
        assert client is not None
        client2 = get_slack_client(url)
        assert client is client2

    def test_get_slack_sync_client_creates_and_caches(self) -> None:
        from ypl.backend.utils.slack_utils import SLACK_SYNC_CLIENTS

        url = "http://unique-sync-webhook-url-test.com"
        SLACK_SYNC_CLIENTS.pop(url, None)
        client = get_slack_sync_client(url)
        assert client is not None
        client2 = get_slack_sync_client(url)
        assert client is client2


# ---------------------------------------------------------------------------
# Tests: post_threaded_slack_messages
# ---------------------------------------------------------------------------


class TestPostThreadedSlackMessages:
    async def test_returns_immediately_with_no_messages(self) -> None:
        # Should not raise
        await post_threaded_slack_messages("C123", [], bot_token="xoxb-test")

    async def test_posts_single_message_normally(self) -> None:
        with patch(
            "ypl.backend.utils.slack_utils.post_to_slack_channel",
            AsyncMock(return_value="12345.0"),
        ) as mock_post:
            await post_threaded_slack_messages("C123", ["single message"], bot_token="xoxb-test")

        mock_post.assert_called_once()

    async def test_posts_thread_for_multiple_messages(self) -> None:
        with (
            patch(
                "ypl.backend.utils.slack_utils.post_to_slack_channel",
                AsyncMock(return_value="12345.0"),
            ),
            patch(
                "ypl.backend.utils.slack_utils.post_thread_message",
                AsyncMock(),
            ) as mock_thread,
        ):
            await post_threaded_slack_messages("C123", ["first", "second", "third"], bot_token="xoxb-test")

        assert mock_thread.call_count == 2  # second and third threaded

    async def test_falls_back_to_regular_posts_when_first_fails(self) -> None:
        with (
            patch(
                "ypl.backend.utils.slack_utils.post_to_slack_channel",
                AsyncMock(side_effect=[None, None, None]),  # First fails (None ts), fallback
            ) as mock_post,
            patch(
                "ypl.backend.utils.slack_utils.post_thread_message",
                AsyncMock(),
            ),
        ):
            await post_threaded_slack_messages("C123", ["first", "second"], bot_token="xoxb-test")

        # First message + fallback second
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# Tests: post_to_slack_response_url
# ---------------------------------------------------------------------------


class TestPostToSlackResponseUrl:
    async def test_posts_successfully(self) -> None:
        mock_httpx_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "ok"
        mock_httpx_client.post = AsyncMock(return_value=mock_resp)

        with patch(
            "ypl.backend.utils.slack_utils.get_slack_httpx_client",
            return_value=mock_httpx_client,
        ):
            await post_to_slack_response_url("http://response.url", {"text": "Hello"})

        mock_httpx_client.post.assert_called_once()

    async def test_handles_exception_gracefully(self) -> None:
        mock_httpx_client = AsyncMock()
        mock_httpx_client.post = AsyncMock(side_effect=Exception("Network error"))

        with patch(
            "ypl.backend.utils.slack_utils.get_slack_httpx_client",
            return_value=mock_httpx_client,
        ):
            # Should not raise
            await post_to_slack_response_url("http://response.url", {"text": "Hello"})


# ---------------------------------------------------------------------------
# Tests: Enum coverage
# ---------------------------------------------------------------------------


class TestEnums:
    def test_slack_event_types(self) -> None:
        assert SlackEventType.MESSAGE.value == "message"
        assert SlackEventType.APP_MENTION.value == "app_mention"
        assert SlackEventType.REACTION_ADDED.value == "reaction_added"

    def test_slack_payload_types(self) -> None:
        assert SlackPayloadType.URL_VERIFICATION.value == "url_verification"
        assert SlackPayloadType.EVENT_CALLBACK.value == "event_callback"

    def test_slack_command_types(self) -> None:
        assert SlackCommandType.SOUL_SEARCH.value == "soul-search"
        assert SlackCommandType.SOUL_CASHOUT.value == "soul-cashout"
