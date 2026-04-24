"""Append buffer management for streaming replies.

Handles:
- Buffering appended text
- Scheduling flushes at rate-limited intervals
- Force flush when buffer exceeds max size
- Centralized flush manager using Redis sorted set (append buffer + status updates)
"""

import asyncio
import time
from typing import Any

from ypl.slack_agent_gateway.callbacks_rendering import render_reply_blocks
from ypl.slack_agent_gateway.constants import (
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    MAX_BUFFER_SIZE_CHARS,
    SLACK_MAX_MESSAGE_LENGTH,
    SLACK_RATELIMIT_INTERVAL_SECONDS,
    STATUS_RATELIMIT_SECONDS,
    get_agent_config_by_app_id,
)
from ypl.slack_agent_gateway.redis_client import (
    append_to_buffer,
    clear_buffer,
    clear_buffer_type,
    get_buffer_size,
    get_buffer_type,
    get_due_flushes,
    get_due_status_flushes,
    get_next_flush_time,
    get_next_status_flush_time,
    get_session,
    remove_from_flush_schedule,
    remove_from_status_flush_schedule,
    schedule_flush,
    schedule_status_flush,
    set_buffer_type,
    try_acquire_slack_ratelimit,
    try_acquire_status_ratelimit,
)
from ypl.slack_agent_gateway.sessions import record_reply
from ypl.slack_agent_gateway.slack_client import RateLimitDeferred, build_slack_client
from ypl.slack_agent_gateway.types import AppendToReplyRequest, AppendToReplyResponse
from ypl.structured_logger import get_logger

logger = get_logger()


def _slack_len(s: str) -> int:
    """Count characters as Slack/JS does — UTF-16 code units.

    Python's len() counts Unicode code points (1 per character), but Slack uses
    JavaScript's string length which counts UTF-16 code units. Characters outside
    the Basic Multilingual Plane (U+10000+), such as most emoji, count as 1 in
    Python but 2 in Slack. This mismatch causes msg_too_long errors when content
    appears to be under the limit in Python but exceeds it in Slack's view.
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _find_slack_truncation_point(s: str, max_slack_len: int) -> int:
    """Return the largest Python char index i such that _slack_len(s[:i]) <= max_slack_len.

    Walks forward through the string accumulating UTF-16 code units and stops
    when the next character would exceed the limit. This gives a safe byte-exact
    truncation point that respects Slack's character counting rules.
    """
    count = 0
    for i, c in enumerate(s):
        unit_count = 2 if ord(c) > 0xFFFF else 1
        if count + unit_count > max_slack_len:
            return i
        count += unit_count
    return len(s)


async def append_to_reply(request: AppendToReplyRequest) -> AppendToReplyResponse:
    """Append text to the last reply (buffered).

    Text is buffered and flushed to Slack at rate-limited intervals.
    If the incoming reply_type differs from the buffered type, the existing
    buffer is flushed first and a new message is posted with the new type.

    Args:
        request: Request containing session_id, text, and optional reply_type

    Returns:
        AppendToReplyResponse with success status
    """
    session = await get_session(request.session_id)
    if not session:
        return AppendToReplyResponse(success=False, buffered=False, error="Session not found")

    if not session.last_reply_ts:
        return AppendToReplyResponse(success=False, buffered=False, error="No previous reply to append to")

    # Check if the reply type changed — if so, flush the old buffer and start a new message.
    current_buffer_type = await get_buffer_type(request.session_id)
    incoming_type = request.reply_type
    # Also check against the last reply's type on the session (for the case where
    # the buffer is empty but we're appending to an existing message of a different type).
    effective_current_type = (
        current_buffer_type if await get_buffer_size(request.session_id) > 0 else session.last_reply_type
    )

    if effective_current_type != incoming_type:
        # Type mismatch — flush whatever is buffered, then post as a new message.
        flushed = await flush_buffer(request.session_id)
        if not flushed:
            # Pre-switch flush failed. Most common cause: the Slack rate-limit
            # gate was held (see ``try_acquire_slack_ratelimit`` in
            # ``flush_buffer``), so the pending old-type content was left in
            # the buffer with a retry scheduled.
            #
            # We cannot simply "retry later and return success=False" the way
            # the same-type buffered path does, because ``add_reply`` below
            # will mutate ``session.last_reply_ts`` to point at the NEW-type
            # message. A later flush of the still-pending OLD-type content
            # would then chat_update the NEW-type message with stale old
            # content, corrupting it.
            #
            # We also must not silently drop the incoming new-type text — that
            # was the pre-fix behavior and it caused the agent's final summary
            # to disappear from Slack after long turns where the "thinking"
            # buffer was non-empty at the moment the summary arrived.
            #
            # Resolution: rescue the pending old-type content out of the
            # buffer and post it as its own new message of the OLD type. Then
            # fall through and post the incoming new-type message as usual.
            # Ordering cosmetics slip a little (the rescued chunk lands in a
            # new message rather than as a trailing edit of the previous
            # message) but nothing is lost.
            rescued = await discard_buffer(request.session_id)
            if rescued:
                # Import here to avoid circular import at module level.
                from ypl.slack_agent_gateway.callbacks import add_reply as _rescue_add_reply
                from ypl.slack_agent_gateway.types import AddReplyRequest as _RescueAddReplyRequest

                rescue_result = await _rescue_add_reply(
                    _RescueAddReplyRequest(
                        session_id=request.session_id,
                        text=rescued,
                        reply_type=effective_current_type,
                    )
                )
                if rescue_result.success:
                    logger.info(
                        "Rescued pending buffer content after type-switch flush failure",
                        session_id=request.session_id,
                        rescued_chars=len(rescued),
                        old_type=effective_current_type,
                    )
                else:
                    logger.error(
                        "Failed to rescue pending buffer content after type-switch flush failure",
                        session_id=request.session_id,
                        rescued_chars=len(rescued),
                        error=rescue_result.error,
                    )
                    # Fall through anyway — delivering the incoming new-type
                    # text is still more valuable than dropping it outright.

        # Import here to avoid circular import at module level.
        from ypl.slack_agent_gateway.callbacks import add_reply
        from ypl.slack_agent_gateway.types import AddReplyRequest

        result = await add_reply(
            AddReplyRequest(
                session_id=request.session_id,
                text=request.text,
                reply_type=incoming_type,
            )
        )
        return AppendToReplyResponse(
            success=result.success,
            buffered=False,
            error=result.error,
        )

    # Same type — append to buffer as usual.
    new_size = await append_to_buffer(request.session_id, request.text)
    await set_buffer_type(request.session_id, incoming_type)

    # Schedule flush if not already scheduled
    flush_time = time.time() + DEFAULT_FLUSH_INTERVAL_SECONDS
    await schedule_flush(request.session_id, flush_time)

    # Check if we need to force flush due to buffer size
    if new_size >= MAX_BUFFER_SIZE_CHARS:
        logger.info(
            "Buffer size exceeded, force flushing",
            session_id=request.session_id,
            buffer_size=new_size,
        )
        flush_success = await flush_buffer(request.session_id)
        return AppendToReplyResponse(
            success=flush_success,
            buffered=not flush_success,  # Still buffered if flush failed
            error="Flush failed" if not flush_success else None,
        )

    return AppendToReplyResponse(success=True, buffered=True)


async def flush_buffer(session_id: str) -> bool:
    """Flush the buffer for a session to Slack.

    Args:
        session_id: The session ID

    Returns:
        True if flush succeeded, False otherwise
    """
    session = await get_session(session_id)
    if not session:
        logger.warning("Session not found for flush", session_id=session_id)
        await remove_from_flush_schedule(session_id)
        return False

    if not session.last_reply_ts:
        logger.warning("No reply to update for flush", session_id=session_id)
        await remove_from_flush_schedule(session_id)
        return False

    # Fast-path: if nothing buffered, drop from the schedule and return.
    # Done before acquiring the rate-limit gate so empty flushes don't burn a slot.
    if await get_buffer_size(session_id) == 0:
        await remove_from_flush_schedule(session_id)
        await clear_buffer_type(session_id)
        return True

    # Get Slack app config up-front — needed for the gate key and the client.
    app_config = await get_agent_config_by_app_id(session.app_id)
    if not app_config:
        logger.error("No app config found for flush", session_id=session_id, app_id=session.app_id)
        return False

    # Acquire the rate-limit gate BEFORE clearing the buffer.
    # If denied, reschedule and return — the buffer keeps accumulating, and the
    # next flush will grab OLD + NEW content in a single chat.update instead of
    # producing two back-to-back updates.
    if not await try_acquire_slack_ratelimit(session.app_id, "chat_update"):
        next_flush_at = time.time() + SLACK_RATELIMIT_INTERVAL_SECONDS + 0.1
        await schedule_flush(session_id, next_flush_at)
        # Logged at info (not debug) because this is the most common reason a
        # force-flush returns False and surfaces as "Gateway rejected append /
        # Flush failed" on the AHS side. Keeping it visible helps operators
        # distinguish benign backpressure from real flush failures.
        logger.info(
            "Slack rate limit gate denied, deferring flush to accumulate more content",
            session_id=session_id,
            next_flush_in=round(SLACK_RATELIMIT_INTERVAL_SECONDS + 0.1, 2),
        )
        return False

    # Read buffer type before clearing content — avoids a race where new content
    # with a different type arrives between clear_buffer and get_buffer_type.
    buffer_type = await get_buffer_type(session_id)

    # Get and clear buffer atomically. Anything appended between the gate grab
    # above and this call is included here — that's the intended behavior.
    buffer_content = await clear_buffer(session_id)
    if not buffer_content:
        # Racer beat us to the flush; release the schedule.
        await remove_from_flush_schedule(session_id)
        await clear_buffer_type(session_id)
        return True

    # Use reenqueue mode so a 429 that slipped past the preventive gate defers
    # instead of retrying with stale content. The overflow loop uses add_reply()
    # which builds its own client (inflight mode — each chunk is a fixed payload).
    client = build_slack_client(app_config.app_id, app_config.bot_token, retry_mode="reenqueue")

    # Build new content by appending buffer to existing content
    existing_content = session.last_reply_content or ""
    new_content = existing_content + buffer_content

    # Leave margin to avoid edge cases (Slack limit is 40000, use 39000)
    max_length = SLACK_MAX_MESSAGE_LENGTH - 1000

    # Check for overflow — if content exceeds Slack's limit, spill to new message(s).
    # Use _slack_len() (UTF-16 code units) rather than Python len() because Slack counts
    # characters as JavaScript does: astral-plane chars (emoji, etc.) count as 2.
    overflow_content: str | None = None
    if _slack_len(new_content) > max_length:
        # Find the safe Python char index where _slack_len(s[:i]) <= max_length, then
        # prefer a clean break at the last newline or space before that boundary.
        safe_cutoff = _find_slack_truncation_point(new_content, max_length)
        truncate_at = safe_cutoff
        last_newline = new_content.rfind("\n", 0, safe_cutoff)
        last_space = new_content.rfind(" ", 0, safe_cutoff)

        if last_newline > safe_cutoff // 2:
            truncate_at = last_newline
        elif last_space > safe_cutoff // 2:
            truncate_at = last_space

        overflow_content = new_content[truncate_at:].lstrip()
        new_content = new_content[:truncate_at]

        logger.info(
            "Message overflow detected, will spill to new message(s)",
            session_id=session_id,
            truncated_at=truncate_at,
            overflow_chars=len(overflow_content),
            overflow_slack_len=_slack_len(overflow_content),
        )

    # Render blocks for typed content (e.g. thinking → context block).
    blocks = render_reply_blocks(new_content, buffer_type)
    update_kwargs: dict[str, Any] = {
        "channel": session.channel_id,
        "ts": session.last_reply_ts,
        "text": new_content,
    }
    if blocks:
        update_kwargs["blocks"] = blocks

    try:
        await client.chat_update(**update_kwargs)

        # Update session with new content (preserve reply_type)
        await record_reply(session_id, session.last_reply_ts, new_content, reply_type=buffer_type)

        logger.debug(
            "Flushed buffer to Slack",
            session_id=session_id,
            appended_chars=len(buffer_content),
            reply_type=buffer_type,
        )

        # Handle overflow by posting new message(s) - chunk if overflow itself is too large
        overflow_failed = False
        if overflow_content:
            # Import here to avoid circular import at module level.
            from ypl.slack_agent_gateway.callbacks import add_reply
            from ypl.slack_agent_gateway.types import AddReplyRequest

            remaining = overflow_content
            while remaining:
                # Determine chunk size - if remaining fits, use it all; otherwise truncate.
                # Use _slack_len() so the check matches Slack's UTF-16 character counting.
                if _slack_len(remaining) <= max_length:
                    chunk = remaining
                    remaining = ""
                else:
                    # Find safe truncation point using UTF-16-aware cutoff, then prefer
                    # a clean break at the last newline or space before that boundary.
                    chunk_safe_cutoff = _find_slack_truncation_point(remaining, max_length)
                    chunk_truncate = chunk_safe_cutoff
                    chunk_newline = remaining.rfind("\n", 0, chunk_safe_cutoff)
                    chunk_space = remaining.rfind(" ", 0, chunk_safe_cutoff)

                    if chunk_newline > chunk_safe_cutoff // 2:
                        chunk_truncate = chunk_newline
                    elif chunk_space > chunk_safe_cutoff // 2:
                        chunk_truncate = chunk_space

                    chunk = remaining[:chunk_truncate]
                    remaining = remaining[chunk_truncate:].lstrip()

                overflow_result = await add_reply(
                    AddReplyRequest(
                        session_id=session_id,
                        text=chunk,
                        reply_type=buffer_type,
                    )
                )
                if overflow_result.success:
                    logger.info(
                        "Posted overflow chunk as new message",
                        session_id=session_id,
                        chunk_chars=len(chunk),
                        remaining_chars=len(remaining),
                        new_message_ts=overflow_result.message_ts,
                    )
                else:
                    logger.error(
                        "Failed to post overflow chunk",
                        session_id=session_id,
                        error=overflow_result.error,
                    )
                    # Re-buffer failed chunk + remaining content, then stop.
                    # Note: This appends to end of buffer. If new content arrived
                    # during flush, ordering may be imperfect, but we preserve data.
                    failed_content = chunk + ("\n\n" + remaining if remaining else "")
                    await append_to_buffer(session_id, failed_content)
                    overflow_failed = True
                    break

        # Only remove from flush schedule if no new content arrived during flush.
        # This prevents a race where new content is buffered but its scheduled flush is removed.
        if await get_buffer_size(session_id) == 0:
            await remove_from_flush_schedule(session_id)
            await clear_buffer_type(session_id)

        # Return False if overflow posting failed - this prevents type switches from
        # proceeding while overflow content is still buffered.
        return not overflow_failed

    except RateLimitDeferred as rl:
        # Slack 429'd despite the preventive gate — respect Retry-After and
        # re-queue the content so it accumulates with anything that arrived
        # during the wait. The next flush sends one merged chat.update.
        logger.warning(
            "Slack rate limit hit on chat.update, re-queuing buffer",
            session_id=session_id,
            retry_after=rl.retry_after,
            buffer_chars=len(buffer_content),
        )
        await append_to_buffer(session_id, buffer_content)
        await schedule_flush(session_id, time.time() + rl.retry_after + 0.1)
        return False

    except Exception as e:
        logger.error(
            "Failed to flush buffer to Slack",
            session_id=session_id,
            error=str(e),
            exc_info=True,
            # Log both Python len and Slack len to surface any remaining counting mismatches.
            new_content_python_len=len(new_content),
            new_content_slack_len=_slack_len(new_content),
        )
        # Re-add buffer content on failure (best effort)
        await append_to_buffer(session_id, buffer_content)
        return False


async def discard_buffer(session_id: str) -> str:
    """Discard the buffer for a session (used before update_reply).

    Args:
        session_id: The session ID

    Returns:
        The discarded buffer content
    """
    content = await clear_buffer(session_id)
    await clear_buffer_type(session_id)
    await remove_from_flush_schedule(session_id)
    if content:
        logger.info(
            "Discarded buffer",
            session_id=session_id,
            discarded_chars=len(content),
        )
    return content


async def process_due_flushes() -> int:
    """Process all sessions due for buffer flush.

    This should be called by a background task periodically.

    Returns:
        Number of sessions flushed
    """
    now = time.time()
    due_sessions = await get_due_flushes(now)

    if not due_sessions:
        return 0

    flushed = 0
    for session_id in due_sessions:
        try:
            if await flush_buffer(session_id):
                flushed += 1
        except Exception as e:
            logger.error(
                "Error flushing buffer",
                session_id=session_id,
                error=str(e),
                exc_info=True,
            )

    if flushed > 0:
        logger.info("Processed due flushes", flushed_count=flushed, total_due=len(due_sessions))

    return flushed


async def process_due_status_flushes() -> int:
    """Process all deferred status-update flushes that are now due.

    For each session, tries to acquire the rate-limit gate before flushing.
    Sessions where the gate is still held are left in the schedule for the
    next cycle.

    Returns:
        Number of sessions whose status was flushed
    """
    now = time.time()
    due_sessions = await get_due_status_flushes(now)

    if not due_sessions:
        return 0

    # Import here to avoid a circular import at module level
    # (callbacks.py imports from buffer.py for discard_buffer).
    from ypl.slack_agent_gateway.callbacks import flush_status_update

    flushed = 0
    for session_id in due_sessions:
        try:
            if not await try_acquire_status_ratelimit(session_id):
                # Still rate-limited — reschedule past the cooldown window to
                # avoid a tight 0-second retry loop in run_flush_manager.
                flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
                await schedule_status_flush(session_id, flush_at)
                continue
            result = await flush_status_update(session_id)
            if result.success:
                flushed += 1
            elif result.error == "Session not found":
                # Session expired — remove the stale entry to prevent
                # unbounded accumulation in the sorted set.
                await remove_from_status_flush_schedule(session_id)
            else:
                # Other transient failure (e.g. Slack 5xx, missing client).
                # Reschedule with backoff so run_flush_manager doesn't spin
                # on a past-due score and hammer Redis/Slack.
                flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
                await schedule_status_flush(session_id, flush_at)
        except Exception as e:
            logger.error(
                "Error flushing status update",
                session_id=session_id,
                error=str(e),
                exc_info=True,
            )
            # Reschedule with backoff so run_flush_manager doesn't spin on a
            # past-due score (e.g. after a Redis or network error).
            flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
            await schedule_status_flush(session_id, flush_at)

    if flushed > 0:
        logger.debug("Processed due status flushes", flushed_count=flushed, total_due=len(due_sessions))

    return flushed


async def run_flush_manager() -> None:
    """Run the centralized flush manager.

    This is a background task that continuously processes due flushes for both
    the append buffer and deferred status updates.
    Should be started when the application starts.
    """
    logger.info("Starting flush manager")

    while True:
        try:
            await process_due_flushes()
            await process_due_status_flushes()

            # Sleep until the next flush (buffer or status) is due, or for a default interval
            next_buf_at = await get_next_flush_time()
            next_sts_at = await get_next_status_flush_time()
            now = time.time()

            candidates = [t for t in (next_buf_at, next_sts_at) if t is not None]
            if candidates:
                next_flush_at = min(candidates)
                # Sleep until the next item is due, capped at 5 seconds
                sleep_duration = max(0, next_flush_at - now)
                sleep_duration = min(sleep_duration, 5.0)
            else:
                # No items scheduled, use default sleep
                sleep_duration = 1.0

            await asyncio.sleep(sleep_duration)
        except Exception as e:
            logger.error("Flush manager error", error=str(e), exc_info=True)
            # On error, sleep longer to avoid tight error loops
            await asyncio.sleep(5.0)
