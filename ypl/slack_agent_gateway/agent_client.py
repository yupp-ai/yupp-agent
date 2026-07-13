"""Client for calling the Agent Harness Service (AHS).

Handles outbound calls to forward Slack messages to agents:
- Create new sessions via POST /ahs/session/create
- Send messages to existing sessions via POST /ahs/session/message

AHS processes messages asynchronously and calls back to the gateway's
callback endpoints to post replies to Slack.
"""

from typing import Any

import httpx

from ypl.backend.config import settings
from ypl.backend.utils.slack_utils import resolve_slack_user_to_yupp_user_id
from ypl.slack_agent_gateway.constants import get_agent_service_url
from ypl.slack_agent_gateway.types import Message
from ypl.structured_logger import get_logger

logger = get_logger()

# HTTP client timeout in seconds
DEFAULT_TIMEOUT = 30.0

_MSG_TRUNCATE_LEN = 50


def _log_outbound_payload(endpoint: str, payload: dict[str, Any]) -> None:
    """Log an outbound AHS request payload, truncating long message fields."""
    logged = dict(payload)
    val = logged.get("message")
    if isinstance(val, str) and len(val) > _MSG_TRUNCATE_LEN:
        logged["message"] = val[:_MSG_TRUNCATE_LEN] + f"... ({len(val)} chars)"
    logger.info("SAG -> AHS request", endpoint=endpoint, **logged)


def _get_ahs_headers() -> dict[str, str]:
    """Build HTTP headers for AHS requests."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    api_key = settings.AGENT_HARNESS_SERVICE_API_KEY
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


async def _resolve_user_name(user_id: str) -> str | None:
    """Look up the user's name from the users table by user_id."""
    from sqlalchemy import text

    from ypl.backend.db import get_async_session_read_replica

    try:
        async with get_async_session_read_replica() as session:
            result = await session.execute(text("SELECT name FROM users WHERE user_id = :uid"), {"uid": user_id})
            row = result.first()
            return str(row[0]) if row and row[0] else None
    except Exception:
        logger.warning("Failed to resolve user name", user_id=user_id)
        return None


async def _build_slack_context(
    session_id: str,
    message: Message,
    channel_id: str,
    thread_ts: str,
    channel_name: str | None = None,
    slack_name: str | None = None,
    bot_token: str | None = None,
) -> dict:
    """Build the context dict with Slack metadata for AHS session creation."""
    slack_user_id = message.sender.slack_user_id
    yupp_user_id: str | None = None
    if slack_user_id:
        try:
            yupp_user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id, bot_token=bot_token)
        except Exception:
            logger.warning("Failed to resolve Slack user to Yupp user_id", slack_user_id=slack_user_id)

    # Resolve the user's name from the users table
    user_name: str | None = None
    if yupp_user_id:
        user_name = await _resolve_user_name(yupp_user_id)

    return {
        "slack_session_id": session_id,
        "slack_channel_id": channel_id,
        "slack_channel_name": channel_name,
        "slack_thread_ts": thread_ts,
        "slack_user_id": slack_user_id,
        "slack_username": message.sender.username,
        "slack_display_name": message.sender.display_name,
        "slack_message_ts": message.ts,
        "slack_agent_name": slack_name,
        "user_id": yupp_user_id,
        "user_name": user_name,
    }


async def get_available_models() -> dict | None:
    """Fetch the list of available models from AHS GET /ahs/models.

    Returns a dict with ``harnessed`` and ``raw`` lists, or None on failure.
    Harnessed values are CLI wrapper names (e.g. 'claude-code-cli').
    Raw values are ``provider/model_id`` strings (e.g. 'anthropic/claude-sonnet-4-6').
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    url = f"{base_url}/ahs/models"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=_get_ahs_headers())
            response.raise_for_status()
            result: dict = response.json()
            return result
    except Exception as e:
        logger.error("Failed to fetch available models from AHS", error=str(e))
        return None


async def get_available_agents() -> list[dict] | None:
    """Fetch the list of available agents from AHS GET /ahs/agents.

    Returns a list of agent summary dicts (see ``AgentInfo``), or None on failure.

    Each agent dict includes ``name``, ``display_name``, ``description``, plus
    executor / model / capability metadata.  Used by the ``/agents`` and
    ``/agent NAME`` slash commands to enumerate and validate agent names.
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    # include_all=true so we see all agents defined on the filesystem as well as
    # any DB-registered ones — `/agents` is a discovery command, not user-scoped.
    url = f"{base_url}/ahs/agents"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=_get_ahs_headers(), params={"include_all": "true"})
            response.raise_for_status()
            payload: dict = response.json()
            agents: list[dict] = payload.get("agents", [])
            return agents
    except Exception as e:
        logger.error("Failed to fetch available agents from AHS", error=str(e))
        return None


async def create_agent_session(
    agent_name: str,
    session_id: str,
    message: Message,
    channel_id: str,
    thread_ts: str,
    channel_name: str | None = None,
    slack_name: str | None = None,
    bot_token: str | None = None,
    force_model: str | None = None,
) -> dict | None:
    """Create a new session with the Agent Harness Service.

    Sends POST /ahs/session/create with:
    - agent_id: The AHS agent name (maps to AHS agent config)
    - trigger: "slack"
    - message: The initial message text
    - session_id: The Slack composite session ID (channel:thread_ts:app_id)
    - context: Slack metadata (user info, timestamps, slack_agent_name)
    - force_model: Optional model override (harness name or provider/model_id)

    Args:
        agent_name: AHS agent name (e.g., 'sre', 'code-reviewer')
        session_id: Slack composite session identifier
        message: Initial message that created the session
        channel_id: Slack channel ID
        thread_ts: Slack thread timestamp
        channel_name: Optional human-readable channel name
        slack_name: Slack bot name (e.g., 'giladovski') for agent persona context
        force_model: Override the agent's default model for this session.

    Returns:
        Response dict from AHS, or None on failure
    """
    if settings.SLACK_AGENT_GATEWAY_USE_MOCK_AGENT:
        from ypl.slack_agent_gateway.mock_agent_service import mock_create_agent_session

        return await mock_create_agent_session(agent_name, session_id, message)

    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    context = await _build_slack_context(
        session_id,
        message,
        channel_id=channel_id,
        thread_ts=thread_ts,
        channel_name=channel_name,
        slack_name=slack_name,
        bot_token=bot_token,
    )
    payload: dict[str, Any] = {
        "agent_id": agent_name,
        "trigger": "slack",
        "message": message.text,
        "session_id": session_id,
        "user_id": context.get("user_id"),
        "context": context,
        "source": "slack_gateway",
    }
    if message.attachments:
        payload["attachments"] = [a.model_dump() for a in message.attachments]
    if force_model:
        payload["force_model"] = force_model

    url = f"{base_url}/ahs/session/create"
    _log_outbound_payload("/ahs/session/create", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Created AHS agent session",
                agent_name=agent_name,
                session_id=session_id,
                status_code=response.status_code,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout creating AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error creating AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception as e:
        logger.error(
            "Error creating AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )
        return None


async def send_message_to_agent(
    agent_name: str,
    session_id: str,
    message: Message,
    bot_token: str | None = None,
) -> dict | None:
    """Send a message to an existing AHS agent session.

    Sends POST /ahs/session/message with:
    - session_id: The Slack composite session ID
    - message: The message text
    - slack_ts: The Slack message timestamp

    Args:
        agent_name: Internal agent name
        session_id: Slack composite session identifier
        message: Message to send

    Returns:
        Response dict from AHS, or None on failure
    """
    if settings.SLACK_AGENT_GATEWAY_USE_MOCK_AGENT:
        from ypl.slack_agent_gateway.mock_agent_service import mock_send_message_to_agent

        return await mock_send_message_to_agent(agent_name, session_id, message)

    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    slack_user_id = message.sender.slack_user_id
    yupp_user_id: str | None = None
    if slack_user_id:
        try:
            yupp_user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id, bot_token=bot_token)
        except Exception:
            logger.warning("Failed to resolve Slack user to Yupp user_id", slack_user_id=slack_user_id)

    payload: dict[str, Any] = {
        "session_id": session_id,
        "message": message.text,
        "slack_ts": message.ts,
        "slack_user_id": slack_user_id,
        "user_id": yupp_user_id,
        "source": "slack_gateway",
    }
    if message.attachments:
        payload["attachments"] = [a.model_dump() for a in message.attachments]

    url = f"{base_url}/ahs/session/message"
    _log_outbound_payload("/ahs/session/message", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Sent message to AHS agent",
                agent_name=agent_name,
                session_id=session_id,
                status_code=response.status_code,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout sending message to AHS agent",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error sending message to AHS agent",
            agent_name=agent_name,
            session_id=session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception as e:
        logger.error(
            "Error sending message to AHS agent",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )
        return None


async def stop_agent_session(
    agent_name: str,
    session_id: str,
) -> dict | None:
    """Stop a running agent task on AHS.

    Sends POST /ahs/session/stop to cancel the inflight turn,
    kill the CLI subprocess, and unblock the session for new messages.

    Args:
        agent_name: Internal agent name (for logging)
        session_id: Slack composite session identifier

    Returns:
        Response dict from AHS, or None on failure
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    payload = {"session_id": session_id}
    url = f"{base_url}/ahs/session/stop"
    _log_outbound_payload("/ahs/session/stop", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Stopped AHS agent session",
                agent_name=agent_name,
                session_id=session_id,
                status_code=response.status_code,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout stopping AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error stopping AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception as e:
        logger.error(
            "Error stopping AHS agent session",
            agent_name=agent_name,
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )
        return None


async def send_feedback(
    session_id: str,
    user_id: str | None,
    slack_ts: str,
    rating: str | None = None,
    comment: str | None = None,
    structured: dict | None = None,
) -> dict | None:
    """Send feedback to the Agent Harness Service.

    Sends POST /ahs/session/feedback with the reaction data.

    Args:
        session_id: Slack composite session identifier
        user_id: User ID for the feedback (FK to users table), or None to default to "SYSTEM"
        slack_ts: Slack timestamp of the message being reacted to
        rating: Optional rating ("POSITIVE" or "NEGATIVE")
        comment: Optional free-text feedback (e.g., emoji name)
        structured: Optional structured eval payload

    Returns:
        Response dict from AHS, or None on failure
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    payload: dict = {
        "session_id": session_id,
        "slack_ts": slack_ts,
    }
    if user_id is not None:
        payload["user_id"] = user_id
    if rating is not None:
        payload["rating"] = rating
    if comment is not None:
        payload["comment"] = comment
    if structured is not None:
        payload["structured"] = structured

    url = f"{base_url}/ahs/session/feedback"
    _log_outbound_payload("/ahs/session/feedback", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Sent feedback to Agent Harness Service (AHS)",
                session_id=session_id,
                rating=rating,
                status_code=response.status_code,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout sending feedback to Agent Harness Service (AHS)",
            session_id=session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error sending feedback to Agent Harness Service (AHS)",
            session_id=session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception as e:
        logger.error(
            "Error sending feedback to Agent Harness Service (AHS)",
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )
        return None


async def get_session_info(session_id: str) -> dict | None:
    """Fetch session info from AHS.

    Calls GET /ahs/session/{session_id} to get session details
    including message_count and created_at.

    Args:
        session_id: AHS session UUID

    Returns:
        Response dict from AHS, or None on failure
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    url = f"{base_url}/ahs/session/{session_id}"

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=_get_ahs_headers())
            response.raise_for_status()
            result: dict = response.json()
            return result

    except Exception as e:
        logger.warning("Failed to fetch AHS session info", session_id=session_id, error=str(e))
        return None


async def attach_slack_to_session(
    ahs_session_id: str,
    slack_session_id: str,
    channel_id: str,
    thread_ts: str,
    channel_name: str | None = None,
) -> dict | None:
    """Attach Slack thread context to an existing headless AHS session.

    Called when a human replies to an agent-initiated thread (e.g., IN_REVIEW notification).
    Updates the AHS session's slack_session_id and context so that:
    1. AHS can resolve the session via the SAG session_id format
    2. AHS callbacks (add_reply, etc.) route back to the correct Slack thread

    Args:
        ahs_session_id: The AHS session UUID to attach to
        slack_session_id: SAG composite session ID ({channel_id}:{thread_ts}:{app_id})
        channel_id: Slack channel ID
        thread_ts: Slack thread timestamp
        channel_name: Optional human-readable channel name

    Returns:
        Response dict from AHS, or None on failure
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    payload = {
        "session_id": ahs_session_id,
        "slack_session_id": slack_session_id,
        "context": {
            "slack_session_id": slack_session_id,
            "slack_channel_id": channel_id,
            "slack_channel_name": channel_name,
            "slack_thread_ts": thread_ts,
        },
    }

    url = f"{base_url}/ahs/session/attach-slack"
    _log_outbound_payload("/ahs/session/attach-slack", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Attached Slack context to AHS session",
                ahs_session_id=ahs_session_id,
                slack_session_id=slack_session_id,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout attaching Slack to AHS session",
            ahs_session_id=ahs_session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error attaching Slack to AHS session",
            ahs_session_id=ahs_session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception:
        logger.error(
            "Error attaching Slack to AHS session",
            ahs_session_id=ahs_session_id,
            exc_info=True,
        )
        return None


async def send_questionnaire_answer_to_agent(
    session_id: str,
    answer_text: str,
    slack_user_id: str,
    slack_ts: str,
) -> dict | None:
    """Forward a questionnaire button-click answer to AHS as a session message.

    Called when a user clicks a questionnaire choice button in Slack.
    The selected label is sent to AHS as a regular session message so the
    agent can process it and continue the conversation.

    Args:
        session_id: SAG composite session identifier (channel:thread_ts:app_id)
        answer_text: The choice label the user selected
        slack_user_id: Slack user ID who clicked the button
        slack_ts: Slack timestamp of the questionnaire message

    Returns:
        Response dict from AHS, or None on failure
    """
    base_url = get_agent_service_url()
    if not base_url:
        logger.error("Agent Harness Service URL not configured")
        return None

    yupp_user_id: str | None = None
    if slack_user_id:
        try:
            yupp_user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id)
        except Exception:
            logger.warning("Failed to resolve Slack user to Yupp user_id", slack_user_id=slack_user_id)

    payload: dict[str, Any] = {
        "session_id": session_id,
        "message": answer_text,
        "slack_ts": slack_ts,
        "slack_user_id": slack_user_id,
        "source": "slack_gateway",
    }
    if yupp_user_id is not None:
        payload["user_id"] = yupp_user_id

    url = f"{base_url}/ahs/session/message"
    _log_outbound_payload("/ahs/session/message (questionnaire answer)", payload)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=_get_ahs_headers())
            response.raise_for_status()

            logger.info(
                "Forwarded questionnaire answer to AHS",
                session_id=session_id,
                answer_length=len(answer_text),
                status_code=response.status_code,
            )
            result: dict = response.json()
            return result

    except httpx.TimeoutException as e:
        logger.error(
            "Timeout forwarding questionnaire answer to AHS",
            session_id=session_id,
            error=str(e),
        )
        return None

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error forwarding questionnaire answer to AHS",
            session_id=session_id,
            status_code=e.response.status_code,
            error=str(e),
        )
        return None

    except Exception as e:
        logger.error(
            "Error forwarding questionnaire answer to AHS",
            session_id=session_id,
            error=str(e),
            exc_info=True,
        )
        return None


async def is_agent_service_healthy() -> bool:
    """Check if the Agent Harness Service is healthy.

    Returns:
        True if healthy, False otherwise
    """
    base_url = get_agent_service_url()
    if not base_url:
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}/health")
            return response.status_code == 200
    except Exception:
        return False
