"""Callback handlers for Agent Service replies.

Handles:
- add_new_reply: Posts a new message to Slack
- append_to_last_reply: Buffers text for streaming (PR 6)
- update_last_reply: Replaces the last reply content
- handle_tool_event: Accumulates tool-use entries and edits a live cluster block
- send_status_update: Posts/edits a rate-limited status context block (legacy)
"""

import time
from collections import Counter
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
    append_tool_entry,
    clear_tool_entries,
    get_and_clear_pending_status,
    get_and_clear_tool_cluster_pending,
    get_session,
    get_tool_entries,
    peek_pending_status,
    peek_tool_cluster_pending,
    release_feedback_claim,
    remove_from_status_flush_schedule,
    save_session,
    schedule_status_flush,
    set_pending_status,
    set_tool_cluster_pending,
    store_reply_mapping,
    store_thread_session_mapping,
    try_acquire_status_ratelimit,
    try_claim_feedback_request,
    update_tool_result,
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
    SendQuestionnaireRequest,
    SendQuestionnaireResponse,
    SendStatusUpdateRequest,
    SendStatusUpdateResponse,
    SendToolEventRequest,
    SendToolEventResponse,
    ToolEventKind,
    ToolResultStatus,
    ToolUseEntry,
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
        # 1. If a tool cluster is active, edit it to show a compact summary so
        #    the live display is replaced by a clean "Bash*5, Grep*2 ..." line.
        # 2. Clear tool entries so the next cluster starts fresh.
        # 3. Reset status_message_ts so future tool events post a new message.
        # 4. Clear any pending status text and scheduled flush.
        if request.reply_type != "thinking":
            try:
                # Re-fetch a fresh session — record_reply already updated it
                # and we must not overwrite those changes with our stale copy.
                fresh_session = await get_session(request.session_id)
                # 1. Snapshot tool entries before clearing so we can render the summary.
                entries = await get_tool_entries(request.session_id)
                # 2. Clear all pending state first to prevent a concurrent flush from
                #    racing against our summary write below.
                await remove_from_status_flush_schedule(request.session_id)
                await get_and_clear_pending_status(request.session_id)
                await get_and_clear_tool_cluster_pending(request.session_id)
                await clear_tool_entries(request.session_id)
                # 3. Write summary to the cluster message (best-effort).
                if fresh_session and fresh_session.status_message_ts:
                    if entries:
                        summary = _render_tool_summary(entries)
                        cluster_client = await _get_slack_client(fresh_session)
                        if cluster_client:
                            try:
                                await cluster_client.chat_update(
                                    channel=fresh_session.channel_id,
                                    ts=fresh_session.status_message_ts,
                                    text=summary,
                                    blocks=[
                                        {
                                            "type": "context",
                                            "elements": [{"type": "mrkdwn", "text": summary}],
                                        }
                                    ],
                                )
                            except SlackApiError as slack_err:
                                logger.warning(
                                    "Failed to post tool cluster summary",
                                    session_id=request.session_id,
                                    error=str(slack_err),
                                )
                    # 4. Nullify status_message_ts so the next tool cluster posts fresh.
                    fresh_session.status_message_ts = None
                    await save_session(fresh_session)
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


def _build_questionnaire_blocks(request: SendQuestionnaireRequest) -> list[dict]:
    """Build Slack Block Kit blocks for a multiple-choice questionnaire.

    Renders the question as a section block and each choice as an action button.
    Buttons carry the session_id as their value; the question_id is encoded in
    the action_id so the interaction handler can route the response.

    Args:
        request: The questionnaire request.

    Returns:
        List of Slack block dicts.
    """
    # Sanitize question_id: keep only alphanumeric and underscores so it's safe
    # to embed in a Slack action_id (which must be <= 255 chars, no special chars).
    safe_qid = "".join(c if c.isalnum() or c == "_" else "_" for c in request.question_id)[:50]

    elements: list[dict] = [
        {
            "type": "button",
            "action_id": f"questionnaire_{safe_qid}_{i}",
            "text": {"type": "plain_text", "text": choice.label},
            # Session ID in value so the interaction handler can look up the session
            "value": request.session_id,
        }
        for i, choice in enumerate(request.choices)
    ]

    blocks: list[dict] = [
        {
            "type": "section",
            "block_id": "questionnaire_question",
            "text": {"type": "mrkdwn", "text": request.text},
        },
        {
            "type": "actions",
            "block_id": "questionnaire_buttons",
            "elements": elements,
        },
    ]

    if request.allow_free_text:
        blocks.append(
            {
                "type": "context",
                "block_id": "questionnaire_hint",
                "elements": [
                    {"type": "mrkdwn", "text": "_or type your answer in the thread_"},
                ],
            }
        )

    return blocks


async def send_questionnaire(request: SendQuestionnaireRequest) -> SendQuestionnaireResponse:
    """Post a multiple-choice questionnaire to the Slack thread for a session.

    Renders the question with action buttons. When the user clicks a button,
    SAG forwards the selected label to AHS as a new session message so the
    agent can continue its turn.

    Args:
        request: Request containing session_id, question_id, text, and choices.

    Returns:
        SendQuestionnaireResponse with success status and message_ts.
    """
    session = await get_session(request.session_id)
    if not session:
        return SendQuestionnaireResponse(success=False, error="Session not found")

    client = await _get_slack_client(session)
    if not client:
        return SendQuestionnaireResponse(success=False, error="Failed to get Slack client")

    blocks = _build_questionnaire_blocks(request)

    try:
        response = await client.chat_postMessage(
            channel=session.channel_id,
            thread_ts=session.thread_ts,
            blocks=blocks,
            text=request.text,
        )

        message_ts = str(response.get("ts", ""))
        if not message_ts:
            return SendQuestionnaireResponse(success=False, error="Slack response missing message timestamp")

        logger.info(
            "Posted questionnaire to Slack",
            session_id=request.session_id,
            question_id=request.question_id,
            message_ts=message_ts,
            num_choices=len(request.choices),
        )

        # Store reply mapping so the interaction handler can resolve the session
        # when a button is clicked.
        try:
            await store_reply_mapping(session.channel_id, message_ts, request.session_id)
        except Exception as e:
            logger.error(
                "Failed to store reply mapping for questionnaire",
                session_id=request.session_id,
                message_ts=message_ts,
                error=str(e),
            )

        return SendQuestionnaireResponse(success=True, message_ts=message_ts)

    except Exception as e:
        logger.error(
            "Failed to post questionnaire to Slack",
            session_id=request.session_id,
            error=str(e),
            exc_info=True,
        )
        return SendQuestionnaireResponse(success=False, error=str(e))


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
# Tool-use cluster display helpers
# ---------------------------------------------------------------------------

# How many tool entries to show in the live cluster block
_TOOL_CLUSTER_DISPLAY_COUNT = 3

# Max characters for a command string in the cluster display
_TOOL_COMMAND_MAX_LEN = 100


def _render_tool_cluster(entries: list[ToolUseEntry]) -> str:
    """Render the last up-to-3 tool entries as a Slack mrkdwn code block.

    Format per entry:
        toolname (command)
        ⎿  DONE | EMPTY | FAILED: error | ...

    A ``(N tools used)`` footer is appended when total > 3.

    Args:
        entries: All tool entries for the session (ordered by insertion).

    Returns:
        Slack mrkdwn string using a fenced code block.
    """
    total = len(entries)
    visible = entries[-_TOOL_CLUSTER_DISPLAY_COUNT:]
    lines: list[str] = []
    for entry in visible:
        cmd = entry.command or ""
        if len(cmd) > _TOOL_COMMAND_MAX_LEN:
            cmd = cmd[: _TOOL_COMMAND_MAX_LEN - 3] + "..."
        header = f"{entry.name} ({cmd})" if cmd else entry.name
        lines.append(header)
        if entry.result_status == ToolResultStatus.RUNNING:
            lines.append("⎿  ...")
        elif entry.result_status == ToolResultStatus.DONE:
            lines.append("⎿  DONE")
        elif entry.result_status == ToolResultStatus.EMPTY:
            lines.append("⎿  EMPTY")
        else:  # FAILED
            err = (entry.error_msg or "")[:50]
            if entry.error_msg and len(entry.error_msg) > 50:
                err += "..."
            lines.append(f"⎿  FAILED: {err}" if err else "⎿  FAILED")

    content = "\n".join(lines)
    if total > _TOOL_CLUSTER_DISPLAY_COUNT:
        content += f"\n({total} tools used)"
    # Escape any triple backticks in tool output so they don't break the code block.
    content = content.replace("```", "'''")
    return f"```\n{content}\n```"


def _render_tool_summary(entries: list[ToolUseEntry]) -> str:
    """Render a compact summary of all tool calls after a cluster finishes.

    Format: ``Bash*5, Grep*2, Read*1 (8 tools used)``
    Shows at most 5 distinct tool types.

    Args:
        entries: All tool entries accumulated for this cluster.

    Returns:
        Single-line summary string (no code block wrapper).
    """
    total = len(entries)
    counts = Counter(e.name for e in entries)
    top = counts.most_common(5)
    parts = [f"{name}*{n}" for name, n in top]
    if len(counts) > 5:
        parts.append("...")
    return f"{', '.join(parts)} ({total} tool{'s' if total != 1 else ''} used)"


# ---------------------------------------------------------------------------
# Tool event handler (new structured path)
# ---------------------------------------------------------------------------


async def handle_tool_event(request: SendToolEventRequest) -> SendToolEventResponse:
    """Accept a structured tool-start or tool-result event from AHS.

    START events append a new ToolUseEntry (status=RUNNING) to the session's
    list.  RESULT events update the matching entry's result_status.

    After updating the list, a rate-limited flush is triggered so Slack sees
    the last 3 tool entries as a live code-block message.

    Args:
        request: SendToolEventRequest from AHS

    Returns:
        SendToolEventResponse
    """
    session = await get_session(request.session_id)
    if not session:
        return SendToolEventResponse(success=False, error="Session not found")

    if request.kind == ToolEventKind.START:
        entry = ToolUseEntry(
            tool_use_id=request.tool_use_id,
            name=request.name or "unknown",
            command=request.command or "",
            result_status=ToolResultStatus.RUNNING,
        )
        await append_tool_entry(request.session_id, entry)
    else:  # RESULT
        await update_tool_result(
            request.session_id,
            request.tool_use_id,
            ToolResultStatus(request.result_status or "done"),
            request.error_msg,
        )

    # Signal that tool entries were updated so flush_status_update renders the cluster.
    await set_tool_cluster_pending(request.session_id)

    if not await try_acquire_status_ratelimit(request.session_id):
        flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
        await schedule_status_flush(request.session_id, flush_at)
        logger.debug(
            "Tool event deferred (rate-limited)",
            session_id=request.session_id,
            kind=request.kind,
            tool_use_id=request.tool_use_id,
        )
        return SendToolEventResponse(success=True, message_ts=session.status_message_ts)

    # Gate acquired — flush immediately. Pass None to force a fresh session fetch
    # so we don't use a stale status_message_ts from when this handler started.
    flush_resp = await flush_status_update(request.session_id)
    return SendToolEventResponse(
        success=flush_resp.success,
        message_ts=flush_resp.message_ts,
        error=flush_resp.error,
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

    # Read both pending signals atomically before any rendering decision.
    is_tool_cluster_flush = await get_and_clear_tool_cluster_pending(session_id)
    raw_pending = await get_and_clear_pending_status(session_id)
    if not is_tool_cluster_flush and not raw_pending:
        # Nothing pending — remove from flush schedule and return success.
        await remove_from_status_flush_schedule(session_id)
        return SendStatusUpdateResponse(success=True, message_ts=session.status_message_ts)

    client = await _get_slack_client(session)
    if not client:
        # Restore pending flags so the next scheduled flush can retry.
        if is_tool_cluster_flush:
            await set_tool_cluster_pending(session_id)
        if raw_pending:
            await set_pending_status(session_id, raw_pending)
        flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
        await schedule_status_flush(session_id, flush_at)
        return SendStatusUpdateResponse(success=False, error="Failed to get Slack client")

    # Determine rendering mode: structured tool cluster vs legacy plain-text.
    if is_tool_cluster_flush:
        entries = await get_tool_entries(session_id)
        if not entries:
            # Entries were cleared between the pending flag being set and the flush —
            # nothing to render; just clean up the schedule.
            await remove_from_status_flush_schedule(session_id)
            return SendStatusUpdateResponse(success=True, message_ts=session.status_message_ts)
        text = _render_tool_cluster(entries)
        if len(text) > _STATUS_MAX_TEXT_LENGTH:
            text = text[: _STATUS_MAX_TEXT_LENGTH - 3] + "..."
        # Tool cluster: standalone section block (not muted context)
        blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    else:
        # Legacy plain-text path: context block (muted gray).
        text = raw_pending or ""
        if len(text) > _STATUS_MAX_TEXT_LENGTH:
            text = text[: _STATUS_MAX_TEXT_LENGTH - 3] + "..."
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]

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
        new_tool_cluster = await peek_tool_cluster_pending(session_id)
        if new_pending or new_tool_cluster:
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
            # Restore pending flags so the next flush retries correctly.
            if is_tool_cluster_flush:
                await set_tool_cluster_pending(session_id)
            if raw_pending:
                await set_pending_status(session_id, raw_pending)
            # Schedule a deferred flush so the re-stored content is eventually delivered.
            flush_at = time.time() + STATUS_RATELIMIT_SECONDS + 0.1
            await schedule_status_flush(session_id, flush_at)
        else:
            logger.error(
                "Failed to post/update status context block",
                session_id=session_id,
                error=error_str,
                exc_info=True,
            )
            # Restore pending flags so retryable transient errors don't silently
            # drop content (e.g. max-turn / context-overflow messages).
            if is_tool_cluster_flush:
                await set_tool_cluster_pending(session_id)
            if raw_pending:
                await set_pending_status(session_id, raw_pending)
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
