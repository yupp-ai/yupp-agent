"""Callback handlers for Agent Service replies.

Handles:
- add_new_reply: Posts a new message to Slack
- append_to_last_reply: Buffers text for streaming (PR 6)
- update_last_reply: Replaces the last reply content
- send_status_update: Posts/edits a rate-limited status context block
"""

import time
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from ypl.slack_agent_gateway.buffer import discard_buffer
from ypl.slack_agent_gateway.callbacks_rendering import render_reply_blocks
from ypl.slack_agent_gateway.constants import (
    STATUS_RATELIMIT_SECONDS,
    get_agent_config_by_app_id,
    get_agent_config_by_name,
)
from ypl.slack_agent_gateway.redis_client import (
    get_and_clear_pending_status,
    get_session,
    peek_pending_status,
    release_feedback_claim,
    remove_from_status_flush_schedule,
    save_session,
    schedule_status_flush,
    set_pending_status,
    store_reply_mapping,
    store_thread_session_mapping,
    try_acquire_status_ratelimit,
    try_claim_feedback_request,
)
from ypl.slack_agent_gateway.sessions import record_reply
from ypl.slack_agent_gateway.types import (
    AddReplyRequest,
    AddReplyResponse,
    AgentSession,
    RequestFeedbackRequest,
    RequestFeedbackResponse,
    SendMessageRequest,
    SendMessageResponse,
    SendStatusUpdateRequest,
    SendStatusUpdateResponse,
    UpdateReplyRequest,
    UpdateReplyResponse,
)
from ypl.structured_logger import get_logger

logger = get_logger()


async def _get_slack_client(session: AgentSession) -> AsyncWebClient | None:
    """Get Slack client for the session's app.

    Args:
        session: The agent session

    Returns:
        AsyncWebClient configured with the app's bot token, or None if not found
    """
    app_config = await get_agent_config_by_app_id(session.app_id)
    if not app_config:
        logger.error("No app config found for session", session_id=session.session_id, app_id=session.app_id)
        return None

    return AsyncWebClient(token=app_config.bot_token)


async def add_reply(request: AddReplyRequest) -> AddReplyResponse:
    """Add a new reply message to Slack.

    Posts immediately to Slack as a new message in the thread.

    Args:
        request: Request containing session_id and text

    Returns:
        AddReplyResponse with success status and message_ts
    """
    session = await get_session(request.session_id)
    if not session:
        return AddReplyResponse(success=False, error="Session not found")

    client = await _get_slack_client(session)
    if not client:
        return AddReplyResponse(success=False, error="Failed to get Slack client")

    # Build Block Kit blocks for typed replies (e.g. thinking → context block).
    blocks = render_reply_blocks(request.text, request.reply_type)
    post_kwargs: dict[str, Any] = {
        "channel": session.channel_id,
        "thread_ts": session.thread_ts,
        "text": request.text,
    }
    if blocks:
        post_kwargs["blocks"] = blocks
    if request.username:
        post_kwargs["username"] = request.username

    try:
        # If there's a placeholder message, update it in-place instead of posting a new one.
        if session.placeholder_ts:
            try:
                update_kwargs: dict[str, Any] = {
                    "channel": session.channel_id,
                    "ts": session.placeholder_ts,
                    "text": request.text,
                }
                if blocks:
                    update_kwargs["blocks"] = blocks
                response = await client.chat_update(**update_kwargs)
                message_ts = session.placeholder_ts
                logger.info(
                    "Replaced placeholder with reply",
                    session_id=request.session_id,
                    message_ts=message_ts,
                    reply_type=request.reply_type,
                )
            except SlackApiError as e:
                logger.warning(
                    "Failed to update placeholder, falling back to new message",
                    session_id=request.session_id,
                    placeholder_ts=session.placeholder_ts,
                    error=str(e),
                )
                # Fall through to post as new message
                session.placeholder_ts = None
                response = await client.chat_postMessage(**post_kwargs)
                message_ts = str(response.get("ts", ""))
                if not message_ts:
                    logger.error("Slack response missing ts field", session_id=request.session_id)
                    return AddReplyResponse(success=False, error="Slack response missing message timestamp")
            finally:
                # Clear the placeholder flag so subsequent replies post as new messages.
                # Best-effort: don't let Redis failures affect Slack delivery.
                try:
                    session.placeholder_ts = None
                    await save_session(session)
                except Exception as e:
                    logger.warning(
                        "Failed to clear placeholder_ts",
                        session_id=request.session_id,
                        error=str(e),
                    )
        else:
            response = await client.chat_postMessage(**post_kwargs)

            message_ts = str(response.get("ts", ""))
            if not message_ts:
                logger.error(
                    "Slack response missing ts field",
                    session_id=request.session_id,
                )
                return AddReplyResponse(success=False, error="Slack response missing message timestamp")

            logger.info(
                "Posted reply to Slack",
                session_id=request.session_id,
                message_ts=message_ts,
                reply_type=request.reply_type,
            )

        # Record the reply in the session (non-critical - log errors but still return success)
        try:
            await record_reply(request.session_id, message_ts, request.text, reply_type=request.reply_type)
        except Exception as e:
            logger.warning(
                "Failed to record reply in session (message was posted successfully)",
                session_id=request.session_id,
                message_ts=message_ts,
                error=str(e),
            )

        # When a real (non-status) reply is posted:
        # 1. Reset status_message_ts so future status updates get a fresh block.
        # 2. Clear any pending status text and scheduled flush so stale
        #    tool-use hints don't appear after the real reply is visible.
        if request.reply_type != "thinking":
            try:
                # Re-fetch a fresh session — record_reply already updated it
                # and we must not overwrite those changes with our stale copy.
                fresh_session = await get_session(request.session_id)
                if fresh_session and fresh_session.status_message_ts:
                    fresh_session.status_message_ts = None
                    await save_session(fresh_session)
                # Always clear pending status + flush schedule regardless of
                # whether status_message_ts was set, to prevent a deferred
                # flush from posting stale tool hints after the real reply.
                await get_and_clear_pending_status(request.session_id)
                await remove_from_status_flush_schedule(request.session_id)
            except Exception as e:
                logger.warning(
                    "Failed to clear status state after real reply",
                    session_id=request.session_id,
                    error=str(e),
                )

        # Store reply-to-session mapping for reaction-based feedback
        try:
            await store_reply_mapping(session.channel_id, message_ts, request.session_id)
        except Exception as e:
            logger.error(
                "Failed to store reply mapping (message was posted successfully)",
                session_id=request.session_id,
                message_ts=message_ts,
                error=str(e),
                exc_info=True,
            )

        return AddReplyResponse(success=True, message_ts=message_ts)

    except SlackApiError as e:
        logger.error(
            "Failed to post reply to Slack",
            session_id=request.session_id,
            error=str(e),
            exc_info=True,
        )
        return AddReplyResponse(success=False, error=str(e))


def _build_survey_blocks(session_id: str, prompt: str | None = None) -> list[dict]:
    """Build Slack Block Kit blocks for a feedback survey.

    Args:
        session_id: Session ID to embed in button values.
        prompt: Optional custom prompt text.

    Returns:
        List of Slack block dicts.
    """
    prompt_text = prompt or "How would you rate your experience with the agent in this session?"
    return [
        {
            "type": "section",
            "block_id": "survey_intro",
            "text": {"type": "mrkdwn", "text": f"*Quick survey* - {prompt_text}"},
        },
        {
            "type": "input",
            "block_id": "survey_comment",
            "optional": True,
            "label": {"type": "plain_text", "text": "Comments (optional)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "comment_input",
                "multiline": True,
                "placeholder": {"type": "plain_text", "text": "Share any feedback..."},
            },
        },
        {
            "type": "actions",
            "block_id": "survey_buttons",
            "elements": [
                {
                    "type": "button",
                    "action_id": "survey_bad",
                    "text": {"type": "plain_text", "text": "Bad"},
                    "style": "danger",
                    "value": f"bad:{session_id}",
                },
                {
                    "type": "button",
                    "action_id": "survey_ok",
                    "text": {"type": "plain_text", "text": "OK"},
                    "value": f"ok:{session_id}",
                },
                {
                    "type": "button",
                    "action_id": "survey_good",
                    "text": {"type": "plain_text", "text": "Good"},
                    "style": "primary",
                    "value": f"good:{session_id}",
                },
            ],
        },
    ]


async def request_feedback(request: RequestFeedbackRequest) -> RequestFeedbackResponse:
    """Post a feedback survey to the Slack thread for a session.

    Args:
        request: Request containing session_id and optional prompt.

    Returns:
        RequestFeedbackResponse with success status and message_ts.
    """
    # Ensure we only request feedback once per session (atomic claim).
    # Claim early to prevent concurrent requests, but release on failure.
    if not await try_claim_feedback_request(request.session_id):
        logger.info("Feedback already requested for session, skipping", session_id=request.session_id)
        return RequestFeedbackResponse(success=True, error="Feedback already requested for this session")

    session = await get_session(request.session_id)
    if not session:
        await release_feedback_claim(request.session_id)
        return RequestFeedbackResponse(success=False, error="Session not found")

    client = await _get_slack_client(session)
    if not client:
        await release_feedback_claim(request.session_id)
        return RequestFeedbackResponse(success=False, error="Failed to get Slack client")

    blocks = _build_survey_blocks(request.session_id, request.prompt)

    try:
        response = await client.chat_postMessage(
            channel=session.channel_id,
            thread_ts=session.thread_ts,
            blocks=blocks,
            text="Feedback survey",
        )

        message_ts = str(response.get("ts", ""))
        if not message_ts:
            await release_feedback_claim(request.session_id)
            return RequestFeedbackResponse(success=False, error="Slack response missing message timestamp")

        logger.info(
            "Posted feedback survey to Slack",
            session_id=request.session_id,
            message_ts=message_ts,
        )

        # Store reply mapping so we can look up the session from interactions
        try:
            await store_reply_mapping(session.channel_id, message_ts, request.session_id)
        except Exception as e:
            logger.error(
                "Failed to store reply mapping for survey",
                session_id=request.session_id,
                message_ts=message_ts,
                error=str(e),
            )

        return RequestFeedbackResponse(success=True, message_ts=message_ts)

    except Exception as e:
        logger.error(
            "Failed to post feedback survey to Slack",
            session_id=request.session_id,
            error=str(e),
            exc_info=True,
        )
        await release_feedback_claim(request.session_id)
        return RequestFeedbackResponse(success=False, error=str(e))


async def update_reply(request: UpdateReplyRequest) -> UpdateReplyResponse:
    """Replace the content of the last reply.

    Discards any pending buffer and replaces the message content.

    Args:
        request: Request containing session_id and new text

    Returns:
        UpdateReplyResponse with success status
    """
    session = await get_session(request.session_id)
    if not session:
        return UpdateReplyResponse(success=False, error="Session not found")

    if not session.last_reply_ts:
        return UpdateReplyResponse(success=False, error="No previous reply to update")

    client = await _get_slack_client(session)
    if not client:
        return UpdateReplyResponse(success=False, error="Failed to get Slack client")

    # Discard any pending buffer before updating
    await discard_buffer(request.session_id)

    try:
        response = await client.chat_update(
            channel=session.channel_id,
            ts=session.last_reply_ts,
            text=request.text,
        )

        message_ts = response.get("ts")

        # Update the session with new content
        await record_reply(request.session_id, session.last_reply_ts, request.text)

        logger.info(
            "Updated reply in Slack",
            session_id=request.session_id,
            message_ts=message_ts,
        )

        return UpdateReplyResponse(success=True, message_ts=message_ts)

    except SlackApiError as e:
        logger.error(
            "Failed to update reply in Slack",
            session_id=request.session_id,
            error=str(e),
            exc_info=True,
        )
        return UpdateReplyResponse(success=False, error=str(e))


async def send_message(request: SendMessageRequest) -> SendMessageResponse:
    """Send a message to a Slack channel proactively.

    This does not require an existing session - it allows an agent to
    initiate a conversation by sending a message to any channel.

    Args:
        request: Request containing agent_name, channel_id, text, and optional thread_ts

    Returns:
        SendMessageResponse with success status and message_ts
    """
    # Look up the agent config by name to get the bot token.
    # Personal agent names (e.g. "yuppclaw-alice") won't match any config,
    # so fall back to the base prefix (e.g. "yuppclaw") for token lookup.
    agent_config = await get_agent_config_by_name(request.agent_name)
    if not agent_config:
        base_name = request.agent_name.rsplit("-", 1)[0] if "-" in request.agent_name else None
        if base_name:
            agent_config = await get_agent_config_by_name(base_name)
    if not agent_config:
        logger.warning(
            "Agent does not have a Slack presence",
            agent_name=request.agent_name,
        )
        return SendMessageResponse(
            success=False,
            error=f"Agent '{request.agent_name}' does not have a Slack presence",
        )

    client = AsyncWebClient(token=agent_config.bot_token)

    # Normalize thread_ts: treat empty string as None
    thread_ts = request.thread_ts or None
    in_thread = thread_ts is not None

    try:
        # Build post kwargs with optional username override
        post_msg_kwargs: dict[str, Any] = {
            "channel": request.channel,
            "text": request.text,
            "thread_ts": thread_ts,
        }
        if request.username:
            post_msg_kwargs["username"] = request.username

        # Post the message (thread_ts=None is ignored by Slack SDK)
        response = await client.chat_postMessage(**post_msg_kwargs)

        message_ts = str(response.get("ts", ""))
        if not message_ts:
            logger.error(
                "Slack response missing ts field",
                agent_name=request.agent_name,
                channel=request.channel,
            )
            return SendMessageResponse(
                success=False,
                channel=request.channel,
                error="Slack response missing message timestamp",
            )

        logger.info(
            "Sent proactive message to Slack",
            agent_name=request.agent_name,
            channel=request.channel,
            message_ts=message_ts,
            in_thread=in_thread,
        )

        # If this is a top-level message with an AHS session ID, register the
        # thread so human replies route to the existing agent session.
        if request.ahs_session_id and not in_thread:
            try:
                # response["channel"] gives the resolved channel ID
                resolved_channel_id = str(response.get("channel", request.channel))
                await store_thread_session_mapping(resolved_channel_id, message_ts, request.ahs_session_id)
            except Exception:
                logger.warning(
                    "Failed to store thread→session mapping (message was sent successfully)",
                    ahs_session_id=request.ahs_session_id,
                    channel=request.channel,
                    message_ts=message_ts,
                    exc_info=True,
                )

        return SendMessageResponse(
            success=True,
            message_ts=message_ts,
            channel=request.channel,
        )

    except SlackApiError as e:
        logger.error(
            "Failed to send message to Slack",
            agent_name=request.agent_name,
            channel=request.channel,
            error=str(e),
            exc_info=True,
        )
        return SendMessageResponse(
            success=False,
            channel=request.channel,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Status update (live tool-use hints rendered as a muted context block)
# ---------------------------------------------------------------------------

# Slack context block mrkdwn max length
_STATUS_MAX_TEXT_LENGTH = 3000


async def flush_status_update(session_id: str, session: AgentSession | None = None) -> SendStatusUpdateResponse:
    """Post or edit the status context block for a session immediately.

    Reads and clears the pending status text from Redis, then either edits the
    existing status message in-place or posts a new one.  Caller must have
    already acquired the rate-limit gate (or be the flush manager).

    Args:
        session_id: The session ID
        session: Pre-fetched AgentSession (fetched here if None)

    Returns:
        SendStatusUpdateResponse
    """
    if session is None:
        session = await get_session(session_id)
    if not session:
        return SendStatusUpdateResponse(success=False, error="Session not found")

    text = await get_and_clear_pending_status(session_id)
    if not text:
        # Nothing pending — remove from flush schedule and return success.
        await remove_from_status_flush_schedule(session_id)
        return SendStatusUpdateResponse(success=True, message_ts=session.status_message_ts)

    client = await _get_slack_client(session)
    if not client:
        # Restore pending text so the next scheduled flush can retry.
        await set_pending_status(session_id, text)
        flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
        await schedule_status_flush(session_id, flush_at)
        return SendStatusUpdateResponse(success=False, error="Failed to get Slack client")

    # Truncate to stay within Slack's context block text limit.
    if len(text) > _STATUS_MAX_TEXT_LENGTH:
        text = text[: _STATUS_MAX_TEXT_LENGTH - 3] + "..."

    blocks: list[dict] = [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]

    try:
        if session.status_message_ts:
            # Edit the existing status message in-place — no new message created.
            await client.chat_update(
                channel=session.channel_id,
                ts=session.status_message_ts,
                text=text,
                blocks=blocks,
            )
            message_ts = session.status_message_ts
            logger.debug(
                "Updated status context block",
                session_id=session_id,
                message_ts=message_ts,
            )
        else:
            # Post a new status message.
            response = await client.chat_postMessage(
                channel=session.channel_id,
                thread_ts=session.thread_ts,
                text=text,
                blocks=blocks,
            )
            message_ts = str(response.get("ts", ""))
            if not message_ts:
                return SendStatusUpdateResponse(success=False, error="Slack response missing message timestamp")
            session.status_message_ts = message_ts
            try:
                # Re-fetch a fresh session to avoid overwriting concurrent
                # changes (e.g. from record_reply/add_reply) with our stale copy.
                fresh = await get_session(session_id)
                if fresh:
                    fresh.status_message_ts = message_ts
                    await save_session(fresh)
                else:
                    await save_session(session)
            except Exception as e:
                logger.warning(
                    "Failed to save session after posting status message",
                    session_id=session_id,
                    error=str(e),
                )
            logger.info(
                "Posted status context block",
                session_id=session_id,
                message_ts=message_ts,
            )

        # Remove from flush schedule now that we've posted.  Then check if a
        # concurrent send_status_update enqueued new text while we were
        # posting (it would have called schedule_status_flush because the
        # rate-limit gate was held by us).  If so, reschedule a flush so the
        # new text is eventually delivered.
        await remove_from_status_flush_schedule(session_id)
        new_pending = await peek_pending_status(session_id)
        if new_pending:
            flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
            await schedule_status_flush(session_id, flush_at)

        return SendStatusUpdateResponse(success=True, message_ts=message_ts)

    except SlackApiError as e:
        error_str = str(e)
        if session.status_message_ts and "message_not_found" in error_str:
            # The status message was deleted by a user or admin.  Reset so
            # the next flush creates a fresh one.  Re-store the text so it
            # isn't silently dropped.
            logger.warning(
                "Status message deleted externally, resetting status_message_ts",
                session_id=session_id,
                message_ts=session.status_message_ts,
            )
            session.status_message_ts = None
            try:
                await save_session(session)
            except Exception:
                pass
            await set_pending_status(session_id, text)
            # Schedule a deferred flush so the re-stored text is eventually
            # delivered even if no further status updates arrive.
            flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
            await schedule_status_flush(session_id, flush_at)
        else:
            logger.error(
                "Failed to post/update status context block",
                session_id=session_id,
                error=error_str,
                exc_info=True,
            )
            # Restore pending text so retryable transient errors don't silently
            # drop one-off notices (e.g. max-turn / context-overflow messages).
            await set_pending_status(session_id, text)
            flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
            await schedule_status_flush(session_id, flush_at)
        return SendStatusUpdateResponse(success=False, error=error_str)


async def send_status_update(request: SendStatusUpdateRequest) -> SendStatusUpdateResponse:
    """Accept a status update from AHS and post it to Slack (rate-limited).

    The status is rendered as a small muted context block that is always edited
    in-place — no new message is created.  Updates are rate-limited to at most
    one Slack API call per STATUS_RATELIMIT_SECONDS.  When rate-limited the text
    is stored as pending and a deferred flush is scheduled so the latest status
    is eventually visible.

    Args:
        request: SendStatusUpdateRequest with session_id and status text

    Returns:
        SendStatusUpdateResponse
    """
    session = await get_session(request.session_id)
    if not session:
        return SendStatusUpdateResponse(success=False, error="Session not found")

    # Always overwrite pending text — latest status wins.
    await set_pending_status(request.session_id, request.text)

    # Try to acquire the rate-limit gate.
    if not await try_acquire_status_ratelimit(request.session_id):
        # Rate-limited: schedule a deferred flush just past the cooldown window.
        flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
        await schedule_status_flush(request.session_id, flush_at)
        logger.debug(
            "Status update deferred (rate-limited)",
            session_id=request.session_id,
            flush_at=flush_at,
        )
        return SendStatusUpdateResponse(success=True, message_ts=session.status_message_ts)

    # Gate acquired — flush immediately.
    return await flush_status_update(request.session_id, session)
