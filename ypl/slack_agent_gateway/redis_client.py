"""Redis client operations for Slack Agent Gateway.

Handles session storage, event deduplication, append buffering, and message queuing.
"""

import json
from datetime import UTC, datetime, timedelta

from ypl.db.redis import get_redis_client
from ypl.slack_agent_gateway.constants import (
    BUFFER_TTL_SECONDS,
    DEFAULT_SESSION_EXPIRATION_SECONDS,
    EVENT_DEDUP_TTL_SECONDS,
    FEEDBACK_REQUESTED_TTL_SECONDS,
    MAX_QUEUE_LENGTH,
    QUEUE_TTL_SECONDS,
    REDIS_KEY_PREFIX_BUFFER,
    REDIS_KEY_PREFIX_BUFFER_TYPE,
    REDIS_KEY_PREFIX_EVENT,
    REDIS_KEY_PREFIX_FEEDBACK_REQUESTED,
    REDIS_KEY_PREFIX_FLUSH_SCHEDULE,
    REDIS_KEY_PREFIX_QUEUE,
    REDIS_KEY_PREFIX_REPLY,
    REDIS_KEY_PREFIX_SESSION,
    REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE,
    REDIS_KEY_PREFIX_STATUS_PENDING,
    REDIS_KEY_PREFIX_STATUS_RATELIMIT,
    REDIS_KEY_PREFIX_SURVEY_RESPONSE,
    REDIS_KEY_PREFIX_THREAD_SESSION,
    REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING,
    REDIS_KEY_PREFIX_TOOL_ENTRIES,
    REPLY_MAPPING_TTL_SECONDS,
    SESSION_REDIS_TTL_SECONDS,
    STATUS_PENDING_TTL_SECONDS,
    STATUS_RATELIMIT_SECONDS,
    SURVEY_RESPONSE_TTL_SECONDS,
    THREAD_SESSION_MAPPING_TTL_SECONDS,
    TOOL_ENTRIES_TTL_SECONDS,
)
from ypl.slack_agent_gateway.types import AgentSession, Message, SessionStatus, ToolResultStatus, ToolUseEntry
from ypl.structured_logger import get_logger

logger = get_logger()


# Session operations


async def save_session(session: AgentSession) -> None:
    """Save a session to Redis.

    Args:
        session: The session to save
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_SESSION}:{session.session_id}"
    data = session.model_dump_json()
    await redis.set(key, data, ex=SESSION_REDIS_TTL_SECONDS)
    logger.debug("Saved session to Redis", session_id=session.session_id)


async def get_session(session_id: str) -> AgentSession | None:
    """Get a session from Redis.

    Args:
        session_id: The session ID

    Returns:
        AgentSession if found, None otherwise
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_SESSION}:{session_id}"
    data = await redis.get(key)
    if not data:
        return None

    session = AgentSession.model_validate_json(data)

    # Update status based on expiration
    if session.is_expired():
        session.status = SessionStatus.EXPIRED

    return session


async def update_session_activity(session_id: str) -> AgentSession | None:
    """Update session activity timestamp and extend expiration.

    Reactivates expired sessions.

    Args:
        session_id: The session ID

    Returns:
        Updated AgentSession if found, None otherwise
    """
    session = await get_session(session_id)
    if not session:
        return None

    now = datetime.now(UTC)
    session.last_activity_at = now
    session.expires_at = now + timedelta(seconds=DEFAULT_SESSION_EXPIRATION_SECONDS)
    session.status = SessionStatus.ACTIVE

    await save_session(session)
    return session


async def update_session_reply(
    session_id: str,
    reply_ts: str,
    reply_content: str,
    reply_type: str | None = None,
) -> AgentSession | None:
    """Update session with new reply information.

    Also extends session expiration and reactivates expired sessions.

    Args:
        session_id: The session ID
        reply_ts: Slack timestamp of the reply
        reply_content: Content of the reply
        reply_type: Content type hint (e.g. 'thinking')

    Returns:
        Updated AgentSession if found, None otherwise
    """
    session = await get_session(session_id)
    if not session:
        return None

    now = datetime.now(UTC)
    session.last_reply_ts = reply_ts
    session.last_reply_content = reply_content
    session.last_reply_type = reply_type
    session.last_activity_at = now
    session.expires_at = now + timedelta(seconds=DEFAULT_SESSION_EXPIRATION_SECONDS)
    session.status = SessionStatus.ACTIVE

    await save_session(session)
    return session


async def delete_session(session_id: str) -> bool:
    """Delete a session from Redis.

    Args:
        session_id: The session ID

    Returns:
        True if deleted, False if not found
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_SESSION}:{session_id}"
    result: int = await redis.delete(key)
    return result > 0


# Event deduplication


async def try_claim_event(event_id: str) -> bool:
    """Atomically check and claim an event for processing (deduplication).

    Uses SET NX to atomically check-and-set, preventing race conditions
    when multiple workers receive the same Slack event.

    Args:
        event_id: Slack event ID

    Returns:
        True if this is the first time seeing the event (claimed successfully),
        False if already processed by another worker
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_EVENT}:{event_id}"
    # SET NX returns True if key was set (first claim), False if already exists
    result = await redis.set(key, "1", ex=EVENT_DEDUP_TTL_SECONDS, nx=True)
    return result is True


# Feedback request deduplication


async def try_claim_feedback_request(session_id: str) -> bool:
    """Atomically claim a feedback request for a session (at most once per session).

    Uses SET NX so that only the first call per session succeeds.

    Args:
        session_id: The session ID

    Returns:
        True if this is the first feedback request for the session,
        False if feedback was already requested
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_FEEDBACK_REQUESTED}:{session_id}"
    result = await redis.set(key, "1", ex=FEEDBACK_REQUESTED_TTL_SECONDS, nx=True)
    return result is True


async def release_feedback_claim(session_id: str) -> None:
    """Release a feedback request claim so it can be retried.

    Called when posting the survey to Slack fails after the claim was acquired.

    Args:
        session_id: The session ID
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_FEEDBACK_REQUESTED}:{session_id}"
    await redis.delete(key)


# Survey response deduplication


async def try_claim_survey_response(session_id: str, user_id: str) -> bool:
    """Atomically claim a survey response to prevent duplicate submissions.

    Keyed by session + user so each user can only submit one response
    per survey, regardless of which button they click.

    Args:
        session_id: The session ID from the survey button value
        user_id: Slack user ID of the respondent

    Returns:
        True if this is the first submission (claimed successfully),
        False if already submitted
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_SURVEY_RESPONSE}:{session_id}:{user_id}"
    result = await redis.set(key, "1", ex=SURVEY_RESPONSE_TTL_SECONDS, nx=True)
    return result is True


async def release_survey_response_claim(session_id: str, user_id: str) -> None:
    """Release a survey response claim so the user can retry.

    Called when feedback persistence fails after the claim was acquired.

    Args:
        session_id: The session ID from the survey button value
        user_id: Slack user ID of the respondent
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_SURVEY_RESPONSE}:{session_id}:{user_id}"
    await redis.delete(key)


# Append buffer operations


async def get_buffer(session_id: str) -> str:
    """Get the append buffer for a session.

    Args:
        session_id: The session ID

    Returns:
        Buffered text (empty string if no buffer)
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER}:{session_id}"
    data = await redis.get(key)
    return data or ""


async def append_to_buffer(session_id: str, text: str) -> int:
    """Append text to the session buffer.

    Args:
        session_id: The session ID
        text: Text to append

    Returns:
        New buffer size in characters
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER}:{session_id}"

    # Use APPEND for atomic operation
    new_length: int = await redis.append(key, text)

    # Set/refresh TTL
    await redis.expire(key, BUFFER_TTL_SECONDS)

    return new_length


async def clear_buffer(session_id: str) -> str:
    """Clear and return the buffer for a session.

    Args:
        session_id: The session ID

    Returns:
        The buffer content that was cleared
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER}:{session_id}"
    data = await redis.getdel(key)
    return data or ""


async def get_buffer_size(session_id: str) -> int:
    """Get the current buffer size for a session.

    Args:
        session_id: The session ID

    Returns:
        Buffer size in characters
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER}:{session_id}"
    size: int = await redis.strlen(key)
    return size


# Buffer type operations


async def set_buffer_type(session_id: str, reply_type: str | None) -> None:
    """Set the reply type for the current buffer.

    Args:
        session_id: The session ID
        reply_type: Content type hint (e.g. 'thinking'), or None for default text
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:{session_id}"
    if reply_type is None:
        await redis.delete(key)
    else:
        await redis.set(key, reply_type, ex=BUFFER_TTL_SECONDS)


async def get_buffer_type(session_id: str) -> str | None:
    """Get the reply type for the current buffer.

    Args:
        session_id: The session ID

    Returns:
        The reply type string, or None if not set / default text
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:{session_id}"
    result: str | None = await redis.get(key)
    return result


async def clear_buffer_type(session_id: str) -> None:
    """Clear the reply type for a session's buffer."""
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_BUFFER_TYPE}:{session_id}"
    await redis.delete(key)


# Flush schedule operations (for centralized flush manager)


async def schedule_flush(session_id: str, flush_at: float) -> None:
    """Schedule a session for buffer flush.

    Uses a sorted set where score is the flush timestamp.

    Args:
        session_id: The session ID
        flush_at: Unix timestamp when to flush
    """
    redis = await get_redis_client()
    await redis.zadd(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, {session_id: flush_at})


async def get_due_flushes(before: float, limit: int = 100) -> list[str]:
    """Get sessions due for flush.

    Args:
        before: Unix timestamp - get all sessions scheduled before this time
        limit: Maximum number of sessions to return (default 100)

    Returns:
        List of session IDs due for flush (up to limit)
    """
    redis = await get_redis_client()
    # Get sessions with score <= before, limited to prevent unbounded results
    result: list[str] = await redis.zrangebyscore(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, "-inf", before, start=0, num=limit)
    return result


async def remove_from_flush_schedule(session_id: str) -> None:
    """Remove a session from the flush schedule.

    Args:
        session_id: The session ID
    """
    redis = await get_redis_client()
    await redis.zrem(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, session_id)


async def get_next_flush_time() -> float | None:
    """Get the next scheduled flush time.

    Returns:
        Unix timestamp of next flush, or None if no flushes scheduled
    """
    redis = await get_redis_client()
    # Get the first item (lowest score)
    result: list[tuple[str, float]] = await redis.zrange(REDIS_KEY_PREFIX_FLUSH_SCHEDULE, 0, 0, withscores=True)
    if result:
        return result[0][1]  # Return the score (timestamp)
    return None


# Status update operations (live tool-use hints, rate-limited context block)


async def try_acquire_status_ratelimit(session_id: str) -> bool:
    """Attempt to acquire the status-update rate-limit gate for a session.

    Uses Redis SET NX EX so only one caller per STATUS_RATELIMIT_SECONDS window
    succeeds.  Returns True if the caller may post to Slack immediately.

    Args:
        session_id: The session ID

    Returns:
        True if the caller may post now, False if rate-limited
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_STATUS_RATELIMIT}:{session_id}"
    result = await redis.set(key, "1", ex=STATUS_RATELIMIT_SECONDS, nx=True)
    return result is not None


async def set_pending_status(session_id: str, text: str) -> None:
    """Store the latest pending status text for a session.

    Always overwrites the previous value — the newest text wins.

    Args:
        session_id: The session ID
        text: Status text to store
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_STATUS_PENDING}:{session_id}"
    await redis.set(key, text, ex=STATUS_PENDING_TTL_SECONDS)


async def get_and_clear_pending_status(session_id: str) -> str | None:
    """Atomically read and delete the pending status text.

    Args:
        session_id: The session ID

    Returns:
        Pending status text, or None if none was stored
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_STATUS_PENDING}:{session_id}"
    result: str | None = await redis.getdel(key)
    return result


async def peek_pending_status(session_id: str) -> str | None:
    """Read the pending status text without deleting it.

    Used to check whether new pending text arrived after a flush completed,
    so the caller can reschedule a flush if needed.

    Args:
        session_id: The session ID

    Returns:
        Pending status text, or None if none is stored
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_STATUS_PENDING}:{session_id}"
    result: str | None = await redis.get(key)
    return result


async def set_tool_cluster_pending(session_id: str) -> None:
    """Signal that tool entries were updated and a cluster flush is needed.

    Mirrors set_pending_status but for the tool-cluster path, so the two
    signals use separate Redis keys and cannot collide.

    Args:
        session_id: The session ID
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING}:{session_id}"
    await redis.set(key, "1", ex=STATUS_PENDING_TTL_SECONDS)


async def get_and_clear_tool_cluster_pending(session_id: str) -> bool:
    """Atomically read and delete the tool-cluster-pending flag.

    Args:
        session_id: The session ID

    Returns:
        True if a tool-cluster flush was pending, False otherwise
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING}:{session_id}"
    result: str | None = await redis.getdel(key)
    return result is not None


async def peek_tool_cluster_pending(session_id: str) -> bool:
    """Check whether a tool-cluster flush is pending without consuming the flag.

    Used after a flush completes to detect whether a new tool event arrived
    in the window, so the caller can reschedule if needed.

    Args:
        session_id: The session ID

    Returns:
        True if a tool-cluster flush is pending, False otherwise
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_CLUSTER_PENDING}:{session_id}"
    result: str | None = await redis.get(key)
    return result is not None


async def schedule_status_flush(session_id: str, flush_at: float) -> None:
    """Schedule a deferred status-update flush.

    Uses a sorted set (score = Unix timestamp) so the flush manager can pick
    up due entries.

    Args:
        session_id: The session ID
        flush_at: Unix timestamp when to flush
    """
    redis = await get_redis_client()
    await redis.zadd(REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, {session_id: flush_at})


async def get_due_status_flushes(before: float, limit: int = 100) -> list[str]:
    """Get sessions whose deferred status flush is now due.

    Args:
        before: Unix timestamp — return entries scheduled at or before this time
        limit: Maximum number of results

    Returns:
        List of session IDs
    """
    redis = await get_redis_client()
    result: list[str] = await redis.zrangebyscore(
        REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, "-inf", before, start=0, num=limit
    )
    return result


async def remove_from_status_flush_schedule(session_id: str) -> None:
    """Remove a session from the status flush schedule.

    Args:
        session_id: The session ID
    """
    redis = await get_redis_client()
    await redis.zrem(REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, session_id)


# Tool entry operations (structured tool-use tracking for the cluster display)


# Lua script for atomic tool-result update on a Redis List.
# Scans the list for the entry whose tool_use_id matches ARGV[1], updates its
# result_status and error_msg in-place using LSET, and returns 1 on success or
# 0 when the id is not found.  Using a Lua script makes the scan-and-update
# atomic so concurrent START appends cannot interleave between our LRANGE read
# and the subsequent LSET write.
_LUA_UPDATE_TOOL_RESULT = """
local entries = redis.call('LRANGE', KEYS[1], 0, -1)
for i, raw in ipairs(entries) do
    local entry = cjson.decode(raw)
    if entry['tool_use_id'] == ARGV[1] then
        entry['result_status'] = ARGV[2]
        if ARGV[3] == '1' then
            entry['error_msg'] = ARGV[4]
        else
            entry['error_msg'] = cjson.null
        end
        if ARGV[5] == '1' then
            entry['result_content'] = ARGV[6]
        else
            entry['result_content'] = cjson.null
        end
        redis.call('LSET', KEYS[1], i - 1, cjson.encode(entry))
        return 1
    end
end
return 0
"""


async def append_tool_entry(session_id: str, entry: ToolUseEntry) -> None:
    """Append a new tool-use entry to the session's ordered list.

    Uses RPUSH for an atomic append so concurrent START events in the same batch
    cannot overwrite each other (no GET-then-SET race).  TTL is reset on every
    append so the list outlives the session's soft-expiration window.

    Args:
        session_id: The session ID
        entry: The tool-use entry to append
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:{session_id}"
    await redis.rpush(key, json.dumps(entry.model_dump()))  # type: ignore[misc]
    await redis.expire(key, TOOL_ENTRIES_TTL_SECONDS)


async def update_tool_result(
    session_id: str,
    tool_use_id: str,
    result_status: ToolResultStatus,
    error_msg: str | None = None,
    result_content: str | None = None,
) -> None:
    """Update the result status of an existing tool entry identified by tool_use_id.

    Uses a Lua script so the scan-and-update is atomic — a concurrent RPUSH
    (START event) cannot land between the LRANGE read and the LSET write.
    No-op if the session has no entries or the ID is not found.

    Args:
        session_id: The session ID
        tool_use_id: Identifier matching the original tool_use event
        result_status: New status (done / empty / failed)
        error_msg: Short error text (only meaningful for FAILED)
        result_content: First line of tool output (only meaningful for DONE)
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:{session_id}"
    await redis.eval(  # type: ignore[misc]
        _LUA_UPDATE_TOOL_RESULT,
        1,
        key,
        tool_use_id,
        str(result_status),
        "1" if error_msg is not None else "0",
        error_msg or "",
        "1" if result_content is not None else "0",
        result_content or "",
    )


async def get_tool_entries(session_id: str) -> list[ToolUseEntry]:
    """Return all tool-use entries for a session in insertion order.

    Args:
        session_id: The session ID

    Returns:
        List of ToolUseEntry (empty if none recorded yet)
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:{session_id}"
    raw_list: list[str] = await redis.lrange(key, 0, -1)  # type: ignore[misc]
    if not raw_list:
        return []
    return [ToolUseEntry.model_validate(json.loads(raw)) for raw in raw_list]


async def clear_tool_entries(session_id: str) -> None:
    """Delete all tool-use entries for a session.

    Called when a real text reply arrives and the tool cluster is finalised.

    Args:
        session_id: The session ID
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_TOOL_ENTRIES}:{session_id}"
    await redis.delete(key)


async def get_next_status_flush_time() -> float | None:
    """Get the next scheduled status-flush time.

    Returns:
        Unix timestamp of the next due entry, or None if none scheduled
    """
    redis = await get_redis_client()
    result: list[tuple[str, float]] = await redis.zrange(REDIS_KEY_PREFIX_STATUS_FLUSH_SCHEDULE, 0, 0, withscores=True)
    if result:
        return result[0][1]
    return None


# Message queue operations (for resilience when Agent Service is down)


async def queue_message(session_id: str, message: Message) -> int | None:
    """Queue a message for later delivery (at the end of the queue).

    Args:
        session_id: The session ID
        message: The message to queue

    Returns:
        Queue length after adding, or None if queue is full
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"

    # Check queue length before adding to prevent unbounded growth
    current_length: int = await redis.llen(key)  # type: ignore[misc]
    if current_length >= MAX_QUEUE_LENGTH:
        logger.warning(
            "Message queue full, rejecting message",
            session_id=session_id,
            queue_length=current_length,
            max_length=MAX_QUEUE_LENGTH,
        )
        return None

    data = message.model_dump_json()
    length: int = await redis.rpush(key, data)  # type: ignore[misc]
    await redis.expire(key, QUEUE_TTL_SECONDS)

    logger.info("Queued message for session", session_id=session_id, queue_length=length)
    return length


async def requeue_message_front(session_id: str, message: Message) -> int:
    """Re-queue a message at the front of the queue (for retry after failure).

    Used to preserve message ordering when a message fails to send during queue drain.

    Args:
        session_id: The session ID
        message: The message to re-queue

    Returns:
        Queue length after adding
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"

    data = message.model_dump_json()
    length: int = await redis.lpush(key, data)  # type: ignore[misc]
    await redis.expire(key, QUEUE_TTL_SECONDS)

    logger.debug("Re-queued message at front", session_id=session_id, queue_length=length)
    return length


async def get_queued_messages(session_id: str) -> list[Message]:
    """Get all queued messages for a session (non-destructive).

    Args:
        session_id: The session ID

    Returns:
        List of queued messages
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"

    data_list: list[str] = await redis.lrange(key, 0, -1)  # type: ignore[misc]
    messages: list[Message] = []
    for data in data_list:
        try:
            messages.append(Message.model_validate_json(data))
        except Exception as e:
            logger.warning(
                "Failed to parse queued message",
                error=str(e),
                session_id=session_id,
                data_length=len(data),
            )

    return messages


async def pop_queued_message(session_id: str) -> Message | None:
    """Pop the oldest queued message for a session.

    Args:
        session_id: The session ID

    Returns:
        The oldest message, or None if queue is empty
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"

    data: str | None = await redis.lpop(key)  # type: ignore[misc]
    if not data:
        return None

    return Message.model_validate_json(data)


async def clear_message_queue(session_id: str) -> int:
    """Clear all queued messages for a session.

    Args:
        session_id: The session ID

    Returns:
        Number of messages cleared
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"

    length: int = await redis.llen(key)  # type: ignore[misc]
    await redis.delete(key)

    return length


async def get_queue_length(session_id: str) -> int:
    """Get the number of queued messages for a session.

    Args:
        session_id: The session ID

    Returns:
        Queue length
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_QUEUE}:{session_id}"
    length: int = await redis.llen(key)  # type: ignore[misc]
    return length


# Reply-to-session mapping (for reaction-based feedback)


async def store_reply_mapping(channel_id: str, message_ts: str, session_id: str) -> None:
    """Store a mapping from an agent reply Slack message to its session.

    Used to look up the session when a user reacts to an agent reply.

    Args:
        channel_id: Slack channel ID
        message_ts: Slack timestamp of the agent's reply message
        session_id: The session this reply belongs to
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_REPLY}:{channel_id}:{message_ts}"
    await redis.set(key, session_id, ex=REPLY_MAPPING_TTL_SECONDS)


async def get_session_for_reply(channel_id: str, message_ts: str) -> str | None:
    """Look up the session ID for an agent reply message.

    Args:
        channel_id: Slack channel ID
        message_ts: Slack timestamp of the message

    Returns:
        Session ID if found, None otherwise
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_REPLY}:{channel_id}:{message_ts}"
    result: str | None = await redis.get(key)
    return result


# Thread-to-AHS-session mapping (for re-attaching agent-initiated threads)


async def store_thread_session_mapping(channel_id: str, thread_ts: str, ahs_session_id: str) -> None:
    """Store a mapping from a Slack thread to an AHS session.

    When an agent posts a top-level message (e.g., IN_REVIEW notification),
    this mapping allows SAG to route human replies back to the agent's
    existing session instead of creating a new one.

    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (the ts of the top-level message)
        ahs_session_id: The AHS agent_session_id (UUID) to route to
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_THREAD_SESSION}:{channel_id}:{thread_ts}"
    await redis.set(key, ahs_session_id, ex=THREAD_SESSION_MAPPING_TTL_SECONDS)
    logger.info(
        "Stored thread→session mapping",
        channel_id=channel_id,
        thread_ts=thread_ts,
        ahs_session_id=ahs_session_id,
    )


async def get_ahs_session_for_thread(channel_id: str, thread_ts: str) -> str | None:
    """Look up an AHS session ID for a thread.

    Used in handle_app_mention to check if a thread was initiated by an
    agent and should be routed to an existing AHS session.

    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp

    Returns:
        AHS session UUID string if found, None otherwise
    """
    redis = await get_redis_client()
    key = f"{REDIS_KEY_PREFIX_THREAD_SESSION}:{channel_id}:{thread_ts}"
    result: str | None = await redis.get(key)
    return result
