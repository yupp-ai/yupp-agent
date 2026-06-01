"""Message queue for resilience when Agent Service is unavailable.

Handles:
- Queueing messages when Agent Service is down
- Draining queue when service recovers
- Status notifications to Slack
"""

from slack_sdk.errors import SlackApiError

from ypl.slack_agent_gateway.agent_client import (
    create_agent_session,
    is_agent_service_healthy,
    send_message_to_agent,
)
from ypl.slack_agent_gateway.constants import get_agent_config_by_app_id
from ypl.slack_agent_gateway.redis_client import (
    clear_message_queue,
    get_queue_length,
    get_session,
    pop_queued_message,
    queue_message,
    requeue_message_front,
)
from ypl.slack_agent_gateway.slack_client import build_slack_client
from ypl.slack_agent_gateway.types import AgentSession, Message
from ypl.structured_logger import get_logger

logger = get_logger()


async def _post_status_to_slack(session: AgentSession, text: str) -> str | None:
    """Post a status message to Slack.

    Args:
        session: The agent session
        text: Status message text

    Returns:
        Message timestamp if successful, None otherwise
    """
    app_config = await get_agent_config_by_app_id(session.app_id)
    if not app_config:
        return None

    client = build_slack_client(app_config.app_id, app_config.bot_token)

    try:
        response = await client.chat_postMessage(
            channel=session.channel_id,
            thread_ts=session.thread_ts,
            text=text,
        )
        ts: str | None = response.get("ts")
        return ts
    except SlackApiError as e:
        logger.error(
            "Failed to post status to Slack",
            session_id=session.session_id,
            error=str(e),
        )
        return None


async def queue_message_for_later(
    session_id: str,
    message: Message,
    notify_user: bool = True,
) -> bool:
    """Queue a message for later delivery when Agent Service is unavailable.

    Args:
        session_id: The session ID
        message: The message to queue
        notify_user: Whether to post a status message to Slack

    Returns:
        True if queued successfully
    """
    session = await get_session(session_id)
    if not session:
        logger.error("Session not found for queueing", session_id=session_id)
        return False

    # Queue the message
    queue_length = await queue_message(session_id, message)

    logger.info(
        "Queued message for later delivery",
        session_id=session_id,
        queue_length=queue_length,
    )

    # Notify user (only on first queued message)
    if notify_user and queue_length == 1:
        await _post_status_to_slack(
            session,
            "⏳ Message queued - the agent is temporarily unavailable. Your message will be delivered shortly.",
        )

    return True


async def drain_queue_for_session(session_id: str) -> int:
    """Drain the message queue for a session.

    Sends all queued messages to the Agent Service.

    Args:
        session_id: The session ID

    Returns:
        Number of messages successfully sent
    """
    session = await get_session(session_id)
    if not session:
        logger.warning("Session not found for queue drain", session_id=session_id)
        await clear_message_queue(session_id)
        return 0

    sent_count = 0

    while True:
        message = await pop_queued_message(session_id)
        if not message:
            break

        app_config = await get_agent_config_by_app_id(session.app_id)
        bot_token = app_config.bot_token if app_config else None

        # Try to send to Agent Service
        result = await send_message_to_agent(
            agent_name=session.agent_name,
            session_id=session_id,
            message=message,
            bot_token=bot_token,
        )

        if result:
            sent_count += 1
        else:
            # Failed to send - re-queue at front to preserve message order
            await requeue_message_front(session_id, message)
            logger.warning(
                "Failed to send queued message, stopping drain",
                session_id=session_id,
                sent_count=sent_count,
            )
            break

    if sent_count > 0:
        logger.info(
            "Drained message queue",
            session_id=session_id,
            sent_count=sent_count,
        )

    return sent_count


async def forward_or_queue_message(
    agent_name: str,
    session_id: str,
    message: Message,
    is_new_session: bool,
    channel_id: str = "",
    thread_ts: str = "",
    channel_name: str | None = None,
    slack_name: str | None = None,
) -> bool:
    """Forward a message to Agent Service, or queue if unavailable.

    Args:
        agent_name: AHS agent name (e.g., 'sre', 'data-scientist')
        session_id: Session identifier
        message: Message to forward
        is_new_session: Whether this is a new session
        channel_id: Slack channel ID (required for new sessions)
        thread_ts: Slack thread timestamp (required for new sessions)
        channel_name: Optional human-readable channel name
        slack_name: Slack bot name (e.g., 'giladovski') for agent persona context

    Returns:
        True if forwarded immediately, False if queued
    """
    # Check if Agent Service is healthy
    if not await is_agent_service_healthy():
        logger.warning(
            "Agent Service unavailable, queueing message",
            agent_name=agent_name,
            session_id=session_id,
        )
        await queue_message_for_later(session_id, message)
        return False

    session = await get_session(session_id)
    bot_token: str | None = None
    if session:
        app_config = await get_agent_config_by_app_id(session.app_id)
        bot_token = app_config.bot_token if app_config else None

    # Try to forward
    if is_new_session:
        result = await create_agent_session(
            agent_name,
            session_id,
            message,
            channel_id=channel_id,
            thread_ts=thread_ts,
            channel_name=channel_name,
            slack_name=slack_name,
            bot_token=bot_token,
        )
    else:
        result = await send_message_to_agent(agent_name, session_id, message, bot_token=bot_token)

    if result:
        # Also check if there are queued messages to drain
        queued_count = await get_queue_length(session_id)
        if queued_count > 0:
            logger.info(
                "Agent Service recovered, draining queue",
                session_id=session_id,
                queued_count=queued_count,
            )
            await drain_queue_for_session(session_id)
        return True

    # Forward failed - queue the message
    await queue_message_for_later(session_id, message)
    return False
