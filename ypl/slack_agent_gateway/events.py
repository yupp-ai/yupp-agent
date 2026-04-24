"""Slack event handling for Slack Agent Gateway.

Handles incoming Slack events from multiple agent apps:
- URL verification challenge
- app_mention events
- reaction_added events (emoji feedback on agent replies)
- Event deduplication
- Signature verification against multiple signing secrets
"""

import hashlib
import hmac
import json
import random
import re
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from slack_sdk.errors import SlackApiError
from starlette.status import HTTP_401_UNAUTHORIZED

from ypl.backend.config import settings
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.dynamic_app_settings import get_slack_agent_gateway_settings
from ypl.backend.utils.slack_utils import get_channel_name_by_id, resolve_slack_user_to_yupp_user_id
from ypl.slack_agent_gateway.agent_client import (
    attach_slack_to_session,
    create_agent_session,
    get_available_agents,
    get_available_models,
    send_feedback,
    send_message_to_agent,
)
from ypl.slack_agent_gateway.constants import (
    get_agent_config_by_app_id,
    get_all_signing_secrets,
)
from ypl.slack_agent_gateway.mention_commands import (
    dispatch_bare_command,
    extract_bare_command,
    extract_leading_directives,
    format_agents_list,
    format_models_list,
)
from ypl.slack_agent_gateway.redis_client import (
    get_ahs_session_for_thread,
    get_session_for_reply,
    save_session,
    try_claim_event,
)
from ypl.slack_agent_gateway.sessions import (
    build_message_from_event,
    get_or_create_session,
    process_slack_attachments,
)
from ypl.slack_agent_gateway.slack_client import build_slack_client
from ypl.slack_agent_gateway.types import AgentAppConfig, AgentSession
from ypl.structured_logger import get_logger
from ypl.utils import async_timed_cache

logger = get_logger()


def verify_slack_signature_multi(request: Request, body: bytes, signing_secrets: list[str]) -> str | None:
    """Verify Slack webhook signature against multiple signing secrets.

    Since we can't read api_app_id until after verification, we try verification
    against all configured signing secrets and return the one that matches.

    Args:
        request: The incoming FastAPI request
        body: The raw request body bytes
        signing_secrets: List of signing secrets to try

    Returns:
        The signing secret that matched, or None if verification should be skipped (local env)

    Raises:
        HTTPException: If signature validation fails against all secrets
    """
    if settings.ENVIRONMENT == "local":
        return None

    signature = request.headers.get("X-Slack-Signature")
    timestamp = request.headers.get("X-Slack-Request-Timestamp")

    if not signature or not timestamp:
        logger.warning("Missing Slack signature headers")
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Missing Slack signature headers")

    # Check if timestamp is too old (replay attack protection)
    try:
        timestamp_int = int(timestamp)
        current_time = int(time.time())
        time_diff = current_time - timestamp_int

        if abs(time_diff) > 300:  # 5 minutes
            logger.warning(
                "Slack request timestamp too old",
                timestamp=timestamp,
                time_diff=time_diff,
            )
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Request timestamp too old")
    except ValueError as e:
        logger.warning("Invalid timestamp format", timestamp=timestamp, error=str(e))
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid timestamp format") from e

    # Construct signature base string
    sig_basestring = f"v0:{timestamp}:".encode() + body

    # Try each signing secret
    for signing_secret in signing_secrets:
        computed_signature = hmac.new(
            signing_secret.encode(),
            sig_basestring,
            hashlib.sha256,
        ).hexdigest()
        expected_signature = f"v0={computed_signature}"

        if hmac.compare_digest(expected_signature, signature):
            return signing_secret

    # No secret matched
    logger.warning("Invalid Slack signature - no signing secret matched")
    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid Slack signature")


async def handle_url_verification(payload: dict[str, Any]) -> JSONResponse:
    """Handle Slack URL verification challenge.

    Args:
        payload: The Slack payload containing challenge value

    Returns:
        JSONResponse with the challenge value
    """
    challenge = payload.get("challenge")
    if challenge is None:
        logger.warning("URL verification challenge missing challenge value", payload=payload)
        raise HTTPException(status_code=400, detail="Missing challenge in url_verification event")
    logger.info("Responding to Slack URL verification challenge")
    return JSONResponse(status_code=200, content={"challenge": challenge})


# Vanilla Slack emojis that exist in every workspace.
_ACK_REACTIONS = [
    "eyes",
    "thinking_face",
    "face_with_rolling_eyes",
    "ok_hand",
    "rainbow",
]


async def _add_ack_reaction(app_config: AgentAppConfig, channel_id: str, ts: str) -> None:
    """Add a random ack reaction to the original message for immediate user feedback.

    This is best-effort — failures are logged but never block the main flow.
    """
    try:
        client = build_slack_client(app_config.app_id, app_config.bot_token)
        emoji = random.choice(_ACK_REACTIONS)
        await client.reactions_add(channel=channel_id, name=emoji, timestamp=ts)
    except SlackApiError as e:
        if e.response.get("error") == "already_reacted":
            logger.debug("Ack reaction already exists", channel_id=channel_id, ts=ts)
        else:
            logger.warning(
                "Failed to add ack reaction",
                agent_name=app_config.agent_name,
                channel_id=channel_id,
                ts=ts,
                emoji=emoji,
                error=str(e),
            )
    except Exception as e:
        logger.error(
            "Unexpected error adding ack reaction",
            agent_name=app_config.agent_name,
            channel_id=channel_id,
            ts=ts,
            error=str(e),
            exc_info=True,
        )


_PLACEHOLDER_MESSAGES = [
    "_Thinking..._",
    "_Looking into it..._",
    "_On it..._",
    "_Let me cook..._",
    "_Bribing the GPU..._",
    "_Warming up the tensor cores..._",
    "_GPU go brrr..._",
    "_Downloading more VRAM..._",
    "_Asking the weights nicely..._",
    "_Reticulating splines..._",
    "_Spinning rust off the neurons..._",
    "_Convincing logits to behave..._",
    "_One sec — attention heads aligning..._",
    "_Teaching sand to do math..._",
    "_Consulting the training data oracle..._",
    "_Shuffling tensors with intent..._",
    "_Almost there — entropy decreasing..._",
    "_Routing through the thought vector..._",
]


async def _post_placeholder(
    app_config: AgentAppConfig,
    session: AgentSession,
    channel_id: str,
    thread_ts: str,
) -> None:
    """Post a random placeholder message and save its ts on the session.

    The first add_reply callback will update this message in-place and clear the flag.
    Best-effort — failures are logged but never block the main flow.
    """
    try:
        client = build_slack_client(app_config.app_id, app_config.bot_token)
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=random.choice(_PLACEHOLDER_MESSAGES),
        )
        placeholder_ts = response.get("ts")
        if placeholder_ts:
            session.placeholder_ts = placeholder_ts
            await save_session(session)
    except Exception as e:
        logger.warning(
            "Failed to post placeholder message",
            agent_name=app_config.agent_name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            error=str(e),
        )


def _matches_any_pattern(channel_name: str, patterns: list[str]) -> bool:
    """Check if channel_name matches any of the regex patterns.

    Args:
        channel_name: Slack channel name (e.g., 'general', 'alert-backend')
        patterns: List of regex patterns

    Returns:
        True if channel name matches any pattern
    """
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, channel_name):
                return True
        except re.error as e:
            logger.warning("Invalid regex pattern in channel filter", pattern=pattern, error=str(e))
    return False


@async_timed_cache(seconds=300)  # Cache for 5 minutes
async def _check_channel_allowed(bot_token: str, channel_id: str) -> tuple[bool, list[str], str | None]:
    """Check if a channel is allowed based on denylist/allowlist settings.

    Denylist is checked first - matching channels are blocked immediately.
    Allowlist is checked second - channel must match at least one pattern to be allowed.
    If allowlist is empty, all channels are denied by default.
    Patterns are matched against channel names, not IDs.

    Args:
        bot_token: Slack bot token for API calls
        channel_id: Slack channel ID

    Returns:
        Tuple of (is_allowed, allowlist_patterns, channel_name).
        allowlist_patterns is returned for constructing the denial message.
    """
    gateway_settings = await get_slack_agent_gateway_settings()

    # Fetch channel name for pattern matching (cached in get_channel_name_by_id)
    channel_name = await get_channel_name_by_id(bot_token, channel_id)
    if not channel_name:
        # If we can't get channel name, deny by default for safety
        logger.warning("Could not resolve channel name, denying by default", channel_id=channel_id)
        return False, gateway_settings.channel_allowlist, None

    # Check denylist first - deny early
    if gateway_settings.channel_denylist and _matches_any_pattern(channel_name, gateway_settings.channel_denylist):
        logger.info("Channel denied by denylist", channel_id=channel_id, channel_name=channel_name)
        return False, gateway_settings.channel_allowlist, channel_name

    # Check allowlist - channel must match at least one pattern
    # Empty allowlist means all channels are denied
    if not gateway_settings.channel_allowlist:
        logger.info("Channel denied - no allowlist configured", channel_id=channel_id, channel_name=channel_name)
        return False, [], channel_name

    if _matches_any_pattern(channel_name, gateway_settings.channel_allowlist):
        return True, gateway_settings.channel_allowlist, channel_name

    logger.info("Channel not in allowlist", channel_id=channel_id, channel_name=channel_name)
    return False, gateway_settings.channel_allowlist, channel_name


async def _post_channel_denied_message(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    allowlist_patterns: list[str],
) -> None:
    """Post a message explaining that the channel is not allowed.

    Args:
        app_config: Agent configuration
        channel_id: Slack channel ID
        thread_ts: Thread timestamp for the reply
        allowlist_patterns: List of allowed channel patterns to show the user
    """
    # TODO: Improve denial message UX - differentiate between denylist vs not-on-allowlist,
    # and only show patterns if they're human-readable (e.g., exact names, not regex like ".*")
    # See PR #10744 for discussion.
    if allowlist_patterns:
        patterns_text = ", ".join(f"`{p}`" for p in allowlist_patterns)
        message = f"I'm not allowed to chat in this channel. Please move to channels matching: {patterns_text}"
    else:
        message = "I'm not allowed to chat in this channel. Please move to other channels I'm allowed to talk."

    try:
        client = build_slack_client(app_config.app_id, app_config.bot_token)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=message,
        )
    except Exception as e:
        logger.error(
            "Failed to post channel denied message",
            channel_id=channel_id,
            thread_ts=thread_ts,
            error=str(e),
        )


async def handle_app_mention(
    event: dict[str, Any],
    app_config: AgentAppConfig,
) -> dict[str, Any]:
    """Handle app_mention event.

    Creates or updates a session and prepares to forward to Agent Service.

    Args:
        event: The Slack event payload
        app_config: Configuration for the agent app that received the mention

    Returns:
        Result dict with session info
    """
    # Extract event data
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts  # Normalize: top-level mentions have no thread_ts

    # Validate required fields to prevent malformed session IDs
    if not channel_id or not user_id or not ts:
        logger.warning(
            "Invalid app_mention event: missing required fields",
            agent_name=app_config.agent_name,
            has_channel_id=bool(channel_id),
            has_user_id=bool(user_id),
            has_ts=bool(ts),
        )
        return {"status": "invalid_event", "reason": "missing required fields"}

    # Check channel allowlist/denylist BEFORE any processing
    is_allowed, allowlist_patterns, channel_name = await _check_channel_allowed(app_config.bot_token, channel_id)
    if not is_allowed:
        logger.info(
            "Channel filtered - denying request",
            agent_name=app_config.agent_name,
            channel_id=channel_id,
            channel_name=channel_name,
        )
        await _post_channel_denied_message(app_config, channel_id, thread_ts, allowlist_patterns)
        return {"status": "channel_denied", "channel_id": channel_id, "channel_name": channel_name}

    # Check for slash commands BEFORE forwarding to AHS
    raw_text = event.get("text", "")
    parsed_command = extract_bare_command(raw_text)
    if parsed_command is not None:
        cmd_name, cmd_args = parsed_command
        # Every bare command gets the ack emoji so the user sees their message was seen.
        create_background_task(_add_ack_reaction(app_config, channel_id, ts))

        dispatched = await dispatch_bare_command(
            cmd_name,
            cmd_args,
            app_config=app_config,
            channel_id=channel_id,
            thread_ts=thread_ts,
            ts=ts,
            user_id=user_id,
            channel_name=channel_name,
        )
        if dispatched is not None:
            return dispatched
        # dispatch_bare_command returned None for an unknown command. Parsing
        # guarantees this is unreachable; if it happens, fall through and treat
        # the mention as a regular message rather than dropping it.

    # Check for leading directives — each must be the first word of whatever's
    # left after stripping previously-parsed directives. Only applies to NEW
    # sessions (existing threads silently ignore the directive and forward the
    # rest of the message as-is).
    #
    # We parse in a loop so either ordering works, e.g.:
    #     @raccoon /agent sre /model anthropic/claude-sonnet-4-6 alerts?
    #     @raccoon /model anthropic/claude-sonnet-4-6 /agent sre alerts?
    agent_directive, model_directive, cleaned_text = extract_leading_directives(raw_text)
    force_model: str | None = None
    force_agent: str | None = None

    # Fire-and-forget: ack emoji should never block the main flow
    create_background_task(_add_ack_reaction(app_config, channel_id, ts))

    logger.info(
        "Received app_mention event",
        agent_name=app_config.agent_name,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
    )

    # Check if this thread was initiated by an agent and has an existing AHS session.
    # If so, route the message to that session instead of creating a new one.
    existing_ahs_session_id = await get_ahs_session_for_thread(channel_id, thread_ts)

    # Validate the /agent directive only for new sessions.
    if agent_directive and not existing_ahs_session_id:
        agents = await get_available_agents()
        if agents is None:
            # AHS unreachable — fall back to the mentioned bot's default agent.
            logger.warning("Could not fetch agent list from AHS; ignoring /agent directive")
        else:
            valid_agent_names = {a.get("name") for a in agents if a.get("name")}
            if agent_directive not in valid_agent_names:
                client = build_slack_client(app_config.app_id, app_config.bot_token)
                agents_text = format_agents_list(agents)
                error_msg = f":x: Agent `{agent_directive}` not found.\n\n{agents_text}"
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=error_msg,
                    )
                except Exception as post_err:
                    logger.warning("Failed to post agent-not-found message", error=str(post_err))
                return {
                    "status": "agent_not_found",
                    "agent": agent_directive,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                }
            force_agent = agent_directive
            logger.info(
                "Force agent directive parsed",
                forced_agent=force_agent,
                mentioned_bot_agent=app_config.agent_name,
                channel_id=channel_id,
            )

    # Validate the /model directive only for new sessions. For existing sessions the
    # directive is silently ignored — the message is forwarded as-is (using cleaned_text).
    if model_directive and not existing_ahs_session_id:
        available = await get_available_models()
        if available is None:
            # AHS unreachable — let the normal flow continue without override
            logger.warning("Could not fetch model list from AHS; ignoring /model directive")
        else:
            all_valid = set(available.get("harnessed", [])) | set(available.get("raw", []))
            if model_directive not in all_valid:
                # Invalid model — post an error listing valid choices and bail out.
                client = build_slack_client(app_config.app_id, app_config.bot_token)
                models_text = format_models_list(available)
                error_msg = f":x: Model `{model_directive}` not found.\n\n{models_text}"
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=error_msg,
                    )
                except Exception as post_err:
                    logger.warning("Failed to post model-not-found message", error=str(post_err))
                return {
                    "status": "model_not_found",
                    "model": model_directive,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                }
            # Valid model — remember it; will be passed to create_agent_session.
            force_model = model_directive
            logger.info(
                "Force model directive parsed",
                model=force_model,
                agent_name=app_config.agent_name,
                channel_id=channel_id,
            )

    # Resolve the effective agent for this session. ``force_agent`` (from
    # ``/agent NAME``) wins when set; otherwise we fall back to whichever agent
    # the mentioned Slack bot is bound to. The mentioned bot still owns reply
    # formatting (bot token / display name) — only AHS routing changes.
    effective_agent_name = force_agent or app_config.agent_name

    # Get or create SAG session (for Slack tracking: placeholder, buffer, reply mapping)
    session, is_new = await get_or_create_session(
        channel_id=channel_id,
        thread_ts=thread_ts,
        app_id=app_config.app_id,
        agent_name=effective_agent_name,
        user_id=user_id,
    )

    # Post directive confirmations for new sessions so the user knows overrides took effect.
    if is_new and not existing_ahs_session_id and (force_agent or force_model):
        confirm_parts: list[str] = []
        if force_agent:
            confirm_parts.append(f"agent `{force_agent}`")
        if force_model:
            confirm_parts.append(f"model `{force_model}`")
        confirm_text = ":white_check_mark: Using " + " + ".join(confirm_parts) + " for this session."
        try:
            client = build_slack_client(app_config.app_id, app_config.bot_token)
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=confirm_text,
            )
        except Exception as confirm_err:
            logger.warning("Failed to post directive confirmation", error=str(confirm_err))

    # Post a placeholder "Thinking..." message for immediate user feedback.
    # The first add_reply callback will update this message in-place instead of posting a new one.
    await _post_placeholder(app_config, session, channel_id, thread_ts)

    # Process file attachments (download from Slack, upload to GCS)
    slack_files = event.get("files", [])
    attachments = []
    if slack_files:
        attachments = await process_slack_attachments(
            files=slack_files,
            session_id=session.session_id,
            bot_token=app_config.bot_token,
        )

    # Build message from cleaned text (directive stripped) or raw event for non-new sessions.
    has_directive = bool(force_agent or force_model)
    if has_directive and is_new and not existing_ahs_session_id:
        # Use a shallow copy of the event with the directive stripped from text
        event_for_message = dict(event)
        event_for_message["text"] = cleaned_text
        message = build_message_from_event(event_for_message, attachments=attachments)
    else:
        message = build_message_from_event(event, attachments=attachments)

    # Forward to Agent Service
    if existing_ahs_session_id:
        # Thread was initiated by an agent — re-attach to the existing AHS session.
        # On first interaction, tell AHS to associate the Slack thread with the
        # existing session so that callbacks (add_reply, etc.) route correctly.
        if is_new:
            attach_result = await attach_slack_to_session(
                ahs_session_id=existing_ahs_session_id,
                slack_session_id=session.session_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                channel_name=channel_name,
            )
            if attach_result is None:
                logger.warning(
                    "Failed to attach Slack context to AHS session — replies may not route back",
                    ahs_session_id=existing_ahs_session_id,
                    sag_session_id=session.session_id,
                )
        # Send the human's message to the existing session
        result = await send_message_to_agent(
            agent_name=effective_agent_name,
            session_id=existing_ahs_session_id,
            message=message,
        )
        logger.info(
            "Re-attached thread to existing AHS session",
            ahs_session_id=existing_ahs_session_id,
            sag_session_id=session.session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
    elif is_new:
        # New session - create session on Agent Service
        result = await create_agent_session(
            agent_name=effective_agent_name,
            session_id=session.session_id,
            message=message,
            channel_id=channel_id,
            thread_ts=thread_ts,
            slack_name=app_config.slack_name,
            force_model=force_model,
        )
    else:
        # Existing session - send message. Use the session's stored agent_name
        # (which may be a ``/agent NAME`` override applied earlier) to keep
        # routing consistent with how the session was originally created.
        result = await send_message_to_agent(
            agent_name=session.agent_name,
            session_id=session.session_id,
            message=message,
        )

    # If forwarding failed, clean up stale placeholder so it doesn't linger.
    if result is None and session.placeholder_ts:
        try:
            client = build_slack_client(app_config.app_id, app_config.bot_token)
            await client.chat_delete(channel=channel_id, ts=session.placeholder_ts)
        except Exception:
            pass
        session.placeholder_ts = None
        await save_session(session)

    # If message was queued (agent busy), replace the placeholder with a brief
    # notice so the user knows their message was received but not yet processed.
    if result and result.get("status") == "queued" and session.placeholder_ts:
        try:
            client = build_slack_client(app_config.app_id, app_config.bot_token)
            await client.chat_update(
                channel=channel_id,
                ts=session.placeholder_ts,
                text=":hourglass: Message queued — I'll respond when the current task finishes.",
            )
        except Exception:
            pass
        session.placeholder_ts = None
        await save_session(session)

    return {
        "status": "session_created" if is_new else "session_updated",
        "session_id": session.session_id,
        "agent_name": effective_agent_name,
        "mentioned_bot_agent": app_config.agent_name,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "forwarded": result is not None,
    }


# Emoji-to-rating mapping for feedback (only whitelisted emojis are recorded)
_EMOJI_TO_RATING: dict[str, str] = {
    "+1": "POSITIVE",
    "thumbsup": "POSITIVE",
    "-1": "NEGATIVE",
    "thumbsdown": "NEGATIVE",
    "ai-thumbsdown": "NEGATIVE",
}


async def handle_reaction_added(
    event: dict[str, Any],
    app_config: AgentAppConfig,
) -> dict[str, Any]:
    """Handle reaction_added event to record feedback on agent replies.

    Only whitelisted emojis are recorded as feedback:
    - :thumbsup: / :+1: → POSITIVE rating
    - :thumbsdown: / :-1: / :ai-thumbsdown: → NEGATIVE rating
    All other emojis are ignored.
    Each message can have multiple feedback rows; the latest one is the effective feedback.

    Args:
        event: The Slack reaction_added event payload
        app_config: Configuration for the agent app

    Returns:
        Result dict with feedback status
    """
    reaction = event.get("reaction", "")
    user_id = event.get("user", "")
    item = event.get("item", {})
    item_channel = item.get("channel", "")
    item_ts = item.get("ts", "")

    if not reaction or not user_id or not item_channel or not item_ts:
        logger.debug(
            "Incomplete reaction_added event, skipping",
            has_reaction=bool(reaction),
            has_user=bool(user_id),
            has_channel=bool(item_channel),
            has_ts=bool(item_ts),
        )
        return {"status": "invalid_event", "reason": "missing required fields"}

    # Ignore reactions on non-message items (e.g., files)
    if item.get("type") != "message":
        return {"status": "skipped", "reason": "not a message reaction"}

    # Normalize skin-tone variants (e.g., "+1::skin-tone-2" → "+1")
    base_emoji = reaction.split("::")[0] if "::" in reaction else reaction

    # Only whitelisted emojis are recorded as feedback
    rating = _EMOJI_TO_RATING.get(base_emoji)
    if rating is None:
        return {"status": "skipped", "reason": "not a whitelisted feedback emoji"}

    # Look up whether this message is an agent reply (only agent reply timestamps
    # are stored in the reply mapping, so reactions on user messages are skipped)
    session_id = await get_session_for_reply(item_channel, item_ts)
    if not session_id:
        logger.debug(
            "Reaction on non-agent message, skipping",
            channel_id=item_channel,
            message_ts=item_ts,
        )
        return {"status": "skipped", "reason": "not an agent reply"}

    # Resolve Slack user ID to internal user_id (None if not a known allowed-domain employee)
    try:
        yupp_user_id = await resolve_slack_user_to_yupp_user_id(user_id, bot_token=app_config.bot_token)
    except Exception:
        logger.exception(
            "DB error resolving Slack user to Yupp user_id, falling back to SYSTEM",
            slack_user_id=user_id,
            session_id=session_id,
        )
        yupp_user_id = None
    if not yupp_user_id:
        logger.warning(
            "Could not resolve Slack user to Yupp user_id, falling back to SYSTEM",
            slack_user_id=user_id,
            session_id=session_id,
        )

    logger.info(
        "Recording emoji feedback",
        session_id=session_id,
        reaction=reaction,
        rating=rating,
        slack_user_id=user_id,
        yupp_user_id=yupp_user_id,
        message_ts=item_ts,
    )

    result = await send_feedback(
        session_id=session_id,
        user_id=yupp_user_id,
        slack_ts=item_ts,
        rating=rating,
        comment=f":{reaction}:",
        structured={"type": "emoji", "slack_user_id": user_id},
    )

    if result is None:
        logger.error(
            "Failed to send feedback to Agent Harness Service (AHS)",
            session_id=session_id,
            message_ts=item_ts,
            reaction=reaction,
        )
        return {"status": "error", "reason": "failed to send feedback to Agent Harness Service (AHS)"}

    return {
        "status": "feedback_recorded",
        "session_id": session_id,
        "reaction": reaction,
        "rating": rating,
    }


async def handle_event_callback(payload: dict[str, Any]) -> JSONResponse:
    """Handle Slack event callback.

    Args:
        payload: The Slack event callback payload

    Returns:
        JSONResponse acknowledging the event
    """
    # Extract event and app info
    event = payload.get("event", {})
    event_id = payload.get("event_id", "")
    api_app_id = payload.get("api_app_id", "")
    event_type = event.get("type", "")

    logger.info(
        "Received Slack event callback",
        event_type=event_type,
        event_id=event_id,
        api_app_id=api_app_id,
    )

    # Atomically claim event for processing (deduplication)
    if event_id and not await try_claim_event(event_id):
        logger.info("Duplicate event, skipping", event_id=event_id)
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # Get agent config for this app
    app_config = await get_agent_config_by_app_id(api_app_id)
    if not app_config:
        logger.warning("Unknown app_id in event", api_app_id=api_app_id)
        return JSONResponse(status_code=200, content={"status": "unknown_app"})

    # Handle different event types
    if event_type == "app_mention":
        result = await handle_app_mention(event, app_config)
        return JSONResponse(status_code=200, content=result)

    if event_type == "reaction_added":
        result = await handle_reaction_added(event, app_config)
        return JSONResponse(status_code=200, content=result)

    # Unhandled event type
    logger.info("Unhandled event type", event_type=event_type)
    return JSONResponse(status_code=200, content={"status": "unhandled_event_type"})


async def process_slack_event(request: Request) -> JSONResponse:
    """Process incoming Slack event webhook.

    Main entry point for /slack/events endpoint.

    Args:
        request: The incoming FastAPI request

    Returns:
        JSONResponse with processing result
    """
    # Read raw body for signature verification
    body = await request.body()

    # Get all signing secrets and verify signature
    signing_secrets = await get_all_signing_secrets()
    if not signing_secrets:
        logger.error("No Slack agent apps configured")
        raise HTTPException(status_code=500, detail="No Slack agent apps configured")

    # Verify signature against all configured secrets
    verify_slack_signature_multi(request, body, signing_secrets)

    # Parse JSON payload
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Error parsing Slack event request body", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid request body") from e

    # Handle different payload types
    payload_type = payload.get("type", "")

    if payload_type == "url_verification":
        return await handle_url_verification(payload)

    if payload_type == "event_callback":
        return await handle_event_callback(payload)

    # Unknown payload type
    logger.warning("Unknown Slack payload type", payload_type=payload_type)
    return JSONResponse(status_code=200, content={"status": "unknown_payload_type"})
