"""Unit tests for SAG redis_client module.

Tests cover session operations, event deduplication, buffer operations,
message queue operations, flush scheduling, and reply/thread mappings.
All Redis calls are mocked via AsyncMock.
"""

from __future__ import annotations
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from ypl.slack_agent_gateway import redis_client as rc
from ypl.slack_agent_gateway.constants import (
    MAX_QUEUE_LENGTH,
    REDIS_KEY_PREFIX_BUFFER,
    REDIS_KEY_PREFIX_BUFFER_TYPE,
    REDIS_KEY_PREFIX_CLUSTER_ACTIVE,
    REDIS_KEY_PREFIX_EVENT,
    REDIS_KEY_PREFIX_FEEDBACK_REQUESTED,
    REDIS_KEY_PREFIX_FLUSH_SCHEDULE,
    REDIS_KEY_PREFIX_REPLY,
    REDIS_KEY_PREFIX_SESSION,
    REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE,
    REDIS_KEY_PREFIX_STATUS_RATELIMIT,
    REDIS_KEY_PREFIX_SURVEY_RESPONSE,
    REDIS_KEY_PREFIX_THREAD_SESSION,
    REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING,
    REDIS_KEY_PREFIX_TOOL_ENTRIES,
)
from ypl.slack_agent_gateway.types import (
    AgentSession,
    Message,
    MessageSender,
    SessionStatus,
    ToolResultStatus,
    ToolUseEntry,
)


def _make_session(session_id: str = "C123:1234.5678:A001", expired: bool = False) -> AgentSession:
    """Create a test AgentSession."""
    now = datetime.now(UTC)
    expires_at = now - timedelta(minutes=1) if expired else now + timedelta(minutes=30)
    return AgentSession(
        session_id=session_id,
        channel_id="C123",
        thread_ts="1234.5678",
        creator_slack_user_id="U456",
        app_id="A001",
        agent_name="sre",
        expires_at=expires_at,
    )


def _make_message(text: str = "hello") -> Message:
    return Message(
        text=text,
        sender=MessageSender(slack_user_id="U456"),
        ts="1234.5678",
    )


@pytest.fixture()
def mock_redis() -> AsyncMock:
    """Return a mock Redis client."""
    return AsyncMock()


@pytest.fixture(autouse=True)
def patch_get_redis(mock_redis: AsyncMock) -> Generator[AsyncMock]:
    """Patch get_redis_client globally for every test in this module."""
    with patch("ypl.slack_agent_gateway.redis_client.get_redis_client", return_value=mock_redis):
        yield mock_redis


# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------


class TestSaveAndGetSession:
    async def test_save_session_sets_key(self, patch_get_redis: AsyncMock) -> None:
        session = _make_session()
        await rc.save_session(session)
        patch_get_redis.set.assert_awaited_once()
        call_args = patch_get_redis.set.call_args
        assert call_args[0][0] == f"{REDIS_KEY_PREFIX_SESSION}:{session.session_id}"
        assert session.session_id in call_args[0][1]

    async def test_get_session_returns_session(self, patch_get_redis: AsyncMock) -> None:
        session = _make_session()
        patch_get_redis.get.return_value = session.model_dump_json()

        result = await rc.get_session(session.session_id)

        assert result is not None
        assert result.session_id == session.session_id
        assert result.status == SessionStatus.ACTIVE

    async def test_get_session_returns_none_when_missing(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.get_session("nonexistent")

        assert result is None

    async def test_get_session_marks_expired(self, patch_get_redis: AsyncMock) -> None:
        expired_session = _make_session(expired=True)
        patch_get_redis.get.return_value = expired_session.model_dump_json()

        result = await rc.get_session(expired_session.session_id)

        assert result is not None
        assert result.status == SessionStatus.EXPIRED

    async def test_get_session_uses_correct_key(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None
        session_id = "C999:9999.0000:A999"

        await rc.get_session(session_id)

        patch_get_redis.get.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_SESSION}:{session_id}")


class TestUpdateSessionActivity:
    async def test_updates_timestamps(self, patch_get_redis: AsyncMock) -> None:
        session = _make_session()
        patch_get_redis.get.return_value = session.model_dump_json()

        result = await rc.update_session_activity(session.session_id)

        assert result is not None
        assert result.status == SessionStatus.ACTIVE
        assert result.expires_at > session.expires_at

    async def test_reactivates_expired_session(self, patch_get_redis: AsyncMock) -> None:
        expired = _make_session(expired=True)
        patch_get_redis.get.return_value = expired.model_dump_json()

        result = await rc.update_session_activity(expired.session_id)

        assert result is not None
        assert result.status == SessionStatus.ACTIVE

    async def test_returns_none_if_not_found(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.update_session_activity("missing")

        assert result is None


class TestUpdateSessionReply:
    async def test_updates_reply_fields(self, patch_get_redis: AsyncMock) -> None:
        session = _make_session()
        patch_get_redis.get.return_value = session.model_dump_json()

        result = await rc.update_session_reply(session.session_id, "1111.2222", "Hi there", "thinking")

        assert result is not None
        assert result.last_reply_ts == "1111.2222"
        assert result.last_reply_content == "Hi there"
        assert result.last_reply_type == "thinking"

    async def test_returns_none_if_missing(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.update_session_reply("missing", "ts", "content")

        assert result is None


class TestDeleteSession:
    async def test_returns_true_when_deleted(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.delete.return_value = 1

        result = await rc.delete_session("some-session")

        assert result is True

    async def test_returns_false_when_not_found(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.delete.return_value = 0

        result = await rc.delete_session("missing")

        assert result is False

    async def test_uses_correct_key(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.delete.return_value = 0
        session_id = "C123:ts:A001"

        await rc.delete_session(session_id)

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_SESSION}:{session_id}")


# ---------------------------------------------------------------------------
# Event deduplication
# ---------------------------------------------------------------------------


class TestTryClaimEvent:
    async def test_first_claim_returns_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = True

        result = await rc.try_claim_event("evt-123")

        assert result is True
        patch_get_redis.set.assert_awaited_once()
        call_args = patch_get_redis.set.call_args
        assert call_args[0][0] == f"{REDIS_KEY_PREFIX_EVENT}:evt-123"
        assert call_args[1].get("nx") is True

    async def test_duplicate_event_returns_false(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = None  # NX returns None when already set

        result = await rc.try_claim_event("evt-123")

        assert result is False


# ---------------------------------------------------------------------------
# Feedback deduplication
# ---------------------------------------------------------------------------


class TestTryClaimFeedback:
    async def test_first_claim_returns_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = True

        result = await rc.try_claim_feedback_request("sess-1")

        assert result is True
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_FEEDBACK_REQUESTED}:sess-1"

    async def test_duplicate_returns_false(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = None

        result = await rc.try_claim_feedback_request("sess-1")

        assert result is False

    async def test_release_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.release_feedback_claim("sess-1")

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_FEEDBACK_REQUESTED}:sess-1")


# ---------------------------------------------------------------------------
# Survey response deduplication
# ---------------------------------------------------------------------------


class TestSurveyResponse:
    async def test_first_response_returns_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = True

        result = await rc.try_claim_survey_response("sess-1", "U001")

        assert result is True
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_SURVEY_RESPONSE}:sess-1:U001"

    async def test_duplicate_response_returns_false(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = None

        result = await rc.try_claim_survey_response("sess-1", "U001")

        assert result is False

    async def test_different_users_get_different_keys(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = True

        await rc.try_claim_survey_response("sess-1", "U001")
        key1 = patch_get_redis.set.call_args[0][0]

        await rc.try_claim_survey_response("sess-1", "U002")
        key2 = patch_get_redis.set.call_args[0][0]

        assert key1 != key2

    async def test_release_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.release_survey_response_claim("sess-1", "U001")

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_SURVEY_RESPONSE}:sess-1:U001")


# ---------------------------------------------------------------------------
# Buffer operations
# ---------------------------------------------------------------------------


class TestBufferOperations:
    async def test_get_buffer_returns_data(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = "hello world"

        result = await rc.get_buffer("sess-1")

        assert result == "hello world"
        patch_get_redis.get.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER}:sess-1")

    async def test_get_buffer_returns_empty_when_missing(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.get_buffer("sess-1")

        assert result == ""

    async def test_append_to_buffer_returns_length(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.append.return_value = 11

        result = await rc.append_to_buffer("sess-1", " world")

        assert result == 11
        patch_get_redis.append.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER}:sess-1", " world")
        patch_get_redis.expire.assert_awaited_once()

    async def test_clear_buffer_returns_content(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.getdel.return_value = "buffered text"

        result = await rc.clear_buffer("sess-1")

        assert result == "buffered text"
        patch_get_redis.getdel.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER}:sess-1")

    async def test_clear_buffer_returns_empty_when_missing(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.getdel.return_value = None

        result = await rc.clear_buffer("sess-1")

        assert result == ""

    async def test_get_buffer_size(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.strlen.return_value = 42

        result = await rc.get_buffer_size("sess-1")

        assert result == 42
        patch_get_redis.strlen.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER}:sess-1")


# ---------------------------------------------------------------------------
# Buffer type operations
# ---------------------------------------------------------------------------


class TestBufferTypeOperations:
    async def test_set_buffer_type_with_value(self, patch_get_redis: AsyncMock) -> None:
        await rc.set_buffer_type("sess-1", "thinking")

        patch_get_redis.set.assert_awaited_once()
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:sess-1"

    async def test_set_buffer_type_none_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.set_buffer_type("sess-1", None)

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:sess-1")
        patch_get_redis.set.assert_not_awaited()

    async def test_get_buffer_type_returns_value(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = "thinking"

        result = await rc.get_buffer_type("sess-1")

        assert result == "thinking"

    async def test_get_buffer_type_returns_none_when_missing(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.get_buffer_type("sess-1")

        assert result is None

    async def test_clear_buffer_type_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.clear_buffer_type("sess-1")

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:sess-1")


# ---------------------------------------------------------------------------
# Flush schedule operations
# ---------------------------------------------------------------------------


class TestFlushSchedule:
    async def test_schedule_flush_adds_to_sorted_set(self, patch_get_redis: AsyncMock) -> None:
        await rc.schedule_flush("sess-1", 9999.0)

        patch_get_redis.zadd.assert_awaited_once_with(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, {"sess-1": 9999.0})

    async def test_get_due_flushes(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrangebyscore.return_value = ["sess-1", "sess-2"]

        result = await rc.get_due_flushes(before=10000.0, limit=50)

        assert result == ["sess-1", "sess-2"]
        call_args = patch_get_redis.zrangebyscore.call_args
        assert call_args[0][0] == REDIS_KEY_PREFIX_FLUSH_SCHEDULE
        assert call_args[1].get("num") == 50

    async def test_remove_from_flush_schedule(self, patch_get_redis: AsyncMock) -> None:
        await rc.remove_from_flush_schedule("sess-1")

        patch_get_redis.zrem.assert_awaited_once_with(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, "sess-1")

    async def test_get_next_flush_time_returns_score(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrange.return_value = [("sess-1", 1234.5)]

        result = await rc.get_next_flush_time()

        assert result == 1234.5

    async def test_get_next_flush_time_returns_none_when_empty(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrange.return_value = []

        result = await rc.get_next_flush_time()

        assert result is None


# ---------------------------------------------------------------------------
# Status update operations
# ---------------------------------------------------------------------------


class TestStatusOperations:
    async def test_try_acquire_status_ratelimit_returns_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = True

        result = await rc.try_acquire_status_ratelimit("sess-1")

        assert result is True
        call_args = patch_get_redis.set.call_args
        assert call_args[0][0] == f"{REDIS_KEY_PREFIX_STATUS_RATELIMIT}:sess-1"
        assert call_args[1].get("nx") is True

    async def test_try_acquire_status_ratelimit_returns_false_when_limited(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.set.return_value = None

        result = await rc.try_acquire_status_ratelimit("sess-1")

        assert result is False

    async def test_set_tool_cluster_pending(self, patch_get_redis: AsyncMock) -> None:
        await rc.set_tool_cluster_pending("sess-1")

        patch_get_redis.set.assert_awaited_once()
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING}:sess-1"

    async def test_get_and_clear_tool_cluster_pending_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.getdel.return_value = "1"

        result = await rc.get_and_clear_tool_cluster_pending("sess-1")

        assert result is True

    async def test_get_and_clear_tool_cluster_pending_false(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.getdel.return_value = None

        result = await rc.get_and_clear_tool_cluster_pending("sess-1")

        assert result is False

    async def test_peek_tool_cluster_pending_true(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = "1"

        result = await rc.peek_tool_cluster_pending("sess-1")

        assert result is True

    async def test_peek_tool_cluster_pending_false(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.peek_tool_cluster_pending("sess-1")

        assert result is False


# ---------------------------------------------------------------------------
# Status flush schedule operations
# ---------------------------------------------------------------------------


class TestStatusFlushSchedule:
    async def test_schedule_status_flush(self, patch_get_redis: AsyncMock) -> None:
        await rc.schedule_status_flush("sess-1", 8888.0)

        patch_get_redis.zadd.assert_awaited_once_with(REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, {"sess-1": 8888.0})

    async def test_get_due_status_flushes(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrangebyscore.return_value = ["sess-3"]

        result = await rc.get_due_status_flushes(before=9000.0, limit=10)

        assert result == ["sess-3"]

    async def test_remove_from_status_flush_schedule(self, patch_get_redis: AsyncMock) -> None:
        await rc.remove_from_status_flush_schedule("sess-1")

        patch_get_redis.zrem.assert_awaited_once_with(REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, "sess-1")

    async def test_get_next_status_flush_time_returns_score(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrange.return_value = [("sess-1", 7777.0)]

        result = await rc.get_next_status_flush_time()

        assert result == 7777.0

    async def test_get_next_status_flush_time_none_when_empty(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.zrange.return_value = []

        result = await rc.get_next_status_flush_time()

        assert result is None


# ---------------------------------------------------------------------------
# Tool entry operations
# ---------------------------------------------------------------------------


class TestToolEntries:
    def _make_entry(self, tool_use_id: str = "tool-1") -> ToolUseEntry:
        return ToolUseEntry(
            tool_use_id=tool_use_id,
            name="Bash",
            command="ls -la",
        )

    async def test_append_tool_entry(self, patch_get_redis: AsyncMock) -> None:
        entry = self._make_entry()
        await rc.append_tool_entry("sess-1", entry)

        patch_get_redis.rpush.assert_awaited_once()
        key_arg = patch_get_redis.rpush.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:sess-1"
        patch_get_redis.expire.assert_awaited_once()

    async def test_get_tool_entries_returns_list(self, patch_get_redis: AsyncMock) -> None:
        entry = self._make_entry()
        patch_get_redis.lrange.return_value = [json.dumps(entry.model_dump())]

        result = await rc.get_tool_entries("sess-1")

        assert len(result) == 1
        assert result[0].tool_use_id == "tool-1"
        assert result[0].name == "Bash"

    async def test_get_tool_entries_empty(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.lrange.return_value = []

        result = await rc.get_tool_entries("sess-1")

        assert result == []

    async def test_clear_tool_entries_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.clear_tool_entries("sess-1")

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:sess-1")

    async def test_update_tool_result_calls_eval(self, patch_get_redis: AsyncMock) -> None:
        await rc.update_tool_result(
            "sess-1",
            "tool-1",
            ToolResultStatus.DONE,
            result_content="output line",
        )

        patch_get_redis.eval.assert_awaited_once()
        # eval(script, numkeys, key, tool_use_id, status, is_error_flag, error_msg, result_content)
        eval_args = patch_get_redis.eval.call_args[0]
        assert len(eval_args) >= 7  # guard against positional arg changes
        assert eval_args[2] == f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:sess-1"
        assert eval_args[3] == "tool-1"

    async def test_update_tool_result_with_error(self, patch_get_redis: AsyncMock) -> None:
        await rc.update_tool_result(
            "sess-1",
            "tool-2",
            ToolResultStatus.FAILED,
            error_msg="Command failed",
        )

        # eval(script, numkeys, key, tool_use_id, status, is_error_flag, error_msg, result_content)
        eval_args = patch_get_redis.eval.call_args[0]
        assert len(eval_args) >= 7  # guard against positional arg changes
        assert eval_args[5] == "1"
        assert eval_args[6] == "Command failed"


# ---------------------------------------------------------------------------
# Cluster-active TTL key (idle window for the live tool cluster)
# ---------------------------------------------------------------------------


class TestClusterActiveKey:
    async def test_mark_cluster_active_sets_with_ttl(self, patch_get_redis: AsyncMock) -> None:
        await rc.mark_cluster_active("sess-9", ttl_seconds=180)

        patch_get_redis.set.assert_awaited_once_with(
            f"{REDIS_KEY_PREFIX_CLUSTER_ACTIVE}:sess-9",
            "1",
            ex=180,
        )

    async def test_is_cluster_active_returns_true_when_key_exists(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.exists.return_value = 1

        assert await rc.is_cluster_active("sess-9") is True
        patch_get_redis.exists.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_CLUSTER_ACTIVE}:sess-9")

    async def test_is_cluster_active_returns_false_when_absent(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.exists.return_value = 0

        assert await rc.is_cluster_active("sess-9") is False

    async def test_clear_cluster_active_deletes_key(self, patch_get_redis: AsyncMock) -> None:
        await rc.clear_cluster_active("sess-9")

        patch_get_redis.delete.assert_awaited_once_with(f"{REDIS_KEY_PREFIX_CLUSTER_ACTIVE}:sess-9")


# ---------------------------------------------------------------------------
# Message queue operations
# ---------------------------------------------------------------------------


class TestMessageQueue:
    async def test_queue_message_returns_length(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.llen.return_value = 0
        patch_get_redis.rpush.return_value = 1

        result = await rc.queue_message("sess-1", _make_message())

        assert result == 1
        patch_get_redis.rpush.assert_awaited_once()
        patch_get_redis.expire.assert_awaited_once()

    async def test_queue_message_returns_none_when_full(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.llen.return_value = MAX_QUEUE_LENGTH

        result = await rc.queue_message("sess-1", _make_message())

        assert result is None
        patch_get_redis.rpush.assert_not_awaited()

    async def test_requeue_message_front_uses_lpush(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.lpush.return_value = 2

        result = await rc.requeue_message_front("sess-1", _make_message())

        assert result == 2
        patch_get_redis.lpush.assert_awaited_once()

    async def test_get_queued_messages_returns_list(self, patch_get_redis: AsyncMock) -> None:
        msg = _make_message("queued msg")
        patch_get_redis.lrange.return_value = [msg.model_dump_json()]

        result = await rc.get_queued_messages("sess-1")

        assert len(result) == 1
        assert result[0].text == "queued msg"

    async def test_get_queued_messages_skips_invalid(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.lrange.return_value = ["not-valid-json{{{"]

        result = await rc.get_queued_messages("sess-1")

        assert result == []

    async def test_pop_queued_message_returns_message(self, patch_get_redis: AsyncMock) -> None:
        msg = _make_message("pop me")
        patch_get_redis.lpop.return_value = msg.model_dump_json()

        result = await rc.pop_queued_message("sess-1")

        assert result is not None
        assert result.text == "pop me"

    async def test_pop_queued_message_returns_none_when_empty(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.lpop.return_value = None

        result = await rc.pop_queued_message("sess-1")

        assert result is None

    async def test_clear_message_queue_returns_count(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.llen.return_value = 3

        result = await rc.clear_message_queue("sess-1")

        assert result == 3
        patch_get_redis.delete.assert_awaited_once()

    async def test_get_queue_length(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.llen.return_value = 5

        result = await rc.get_queue_length("sess-1")

        assert result == 5


# ---------------------------------------------------------------------------
# Reply mapping operations
# ---------------------------------------------------------------------------


class TestReplyMapping:
    async def test_store_reply_mapping(self, patch_get_redis: AsyncMock) -> None:
        await rc.store_reply_mapping("C123", "1234.5678", "sess-1")

        patch_get_redis.set.assert_awaited_once()
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_REPLY}:C123:1234.5678"
        assert patch_get_redis.set.call_args[0][1] == "sess-1"

    async def test_get_session_for_reply_returns_session_id(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = "sess-1"

        result = await rc.get_session_for_reply("C123", "1234.5678")

        assert result == "sess-1"

    async def test_get_session_for_reply_returns_none(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.get_session_for_reply("C123", "missing-ts")

        assert result is None


# ---------------------------------------------------------------------------
# Thread-to-session mapping operations
# ---------------------------------------------------------------------------


class TestThreadSessionMapping:
    async def test_store_thread_session_mapping(self, patch_get_redis: AsyncMock) -> None:
        await rc.store_thread_session_mapping("C123", "9999.0000", "ahs-session-uuid")

        patch_get_redis.set.assert_awaited_once()
        key_arg = patch_get_redis.set.call_args[0][0]
        assert key_arg == f"{REDIS_KEY_PREFIX_THREAD_SESSION}:C123:9999.0000"
        assert patch_get_redis.set.call_args[0][1] == "ahs-session-uuid"

    async def test_get_ahs_session_for_thread_returns_id(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = "ahs-session-uuid"

        result = await rc.get_ahs_session_for_thread("C123", "9999.0000")

        assert result == "ahs-session-uuid"

    async def test_get_ahs_session_for_thread_returns_none(self, patch_get_redis: AsyncMock) -> None:
        patch_get_redis.get.return_value = None

        result = await rc.get_ahs_session_for_thread("C123", "missing-ts")

        assert result is None
