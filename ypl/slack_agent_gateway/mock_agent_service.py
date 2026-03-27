"""In-memory mock agent for testing the Slack Agent Gateway.

This mock simulates an AI agent that:
1. Receives messages via the agent_client functions
2. Posts "Let me think about it..." immediately
3. Updates to "I think Gilad will know!" after 1 second

Enable by setting SLACK_AGENT_GATEWAY_USE_MOCK_AGENT=true in environment.
"""

import asyncio

from ypl.slack_agent_gateway.callbacks import add_reply, update_reply
from ypl.slack_agent_gateway.types import (
    AddReplyRequest,
    Message,
    UpdateReplyRequest,
)
from ypl.structured_logger import get_logger

logger = get_logger()


async def _mock_agent_response(session_id: str, message: Message) -> None:
    """Simulate agent processing and response.

    Args:
        session_id: The session to respond to
        message: The message that triggered this response
    """
    try:
        # Step 1: Post "Let me think about it..."
        add_response = await add_reply(
            AddReplyRequest(
                session_id=session_id,
                text="Let me think about it...",
            )
        )

        if not add_response.success:
            logger.error(
                "Mock agent failed to post initial reply",
                session_id=session_id,
                error=add_response.error,
            )
            return

        logger.info(
            "Mock agent posted thinking message",
            session_id=session_id,
            message_ts=add_response.message_ts,
        )

        # Step 2: Wait 1 second
        await asyncio.sleep(1.0)

        # Step 3: Update to "I think Gilad will know!"
        update_response = await update_reply(
            UpdateReplyRequest(
                session_id=session_id,
                text="I think Gilad will know!",
            )
        )

        if not update_response.success:
            logger.error(
                "Mock agent failed to update reply",
                session_id=session_id,
                error=update_response.error,
            )
            return

        logger.info(
            "Mock agent updated message",
            session_id=session_id,
            message_ts=update_response.message_ts,
        )

    except Exception as e:
        logger.error(
            "Mock agent error",
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )


async def mock_create_agent_session(
    agent_name: str,
    session_id: str,
    message: Message,
) -> dict | None:
    """Mock implementation of create_agent_session.

    Starts a background task to simulate agent response.
    """
    logger.info(
        "Mock agent received new session",
        agent_name=agent_name,
        session_id=session_id,
        message_text=message.text[:100],
    )

    # Start background task (don't await)
    asyncio.create_task(_mock_agent_response(session_id, message))

    return {"status": "accepted", "session_id": session_id}


async def mock_send_message_to_agent(
    agent_name: str,
    session_id: str,
    message: Message,
) -> dict | None:
    """Mock implementation of send_message_to_agent.

    Starts a background task to simulate agent response.
    """
    logger.info(
        "Mock agent received message",
        agent_name=agent_name,
        session_id=session_id,
        message_text=message.text[:100],
    )

    # Start background task (don't await)
    asyncio.create_task(_mock_agent_response(session_id, message))

    return {"status": "accepted", "session_id": session_id}
