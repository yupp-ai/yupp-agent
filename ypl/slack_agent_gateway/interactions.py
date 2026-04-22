"""Slack interactivity handler for Slack Agent Gateway.

Handles incoming Slack interaction payloads (button clicks, form submissions)
from the /slack/interactions endpoint. Currently handles:
- survey_* button clicks for feedback surveys
"""

import json
from typing import Any
from urllib.parse import unquote_plus

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from slack_sdk.errors import SlackApiError

from ypl.backend.utils.slack_utils import resolve_slack_user_to_yupp_user_id
from ypl.slack_agent_gateway.agent_client import send_feedback, send_questionnaire_answer_to_agent
from ypl.slack_agent_gateway.bot_father import handle_approval, handle_denial, request_bot_creation
from ypl.slack_agent_gateway.bot_father_types import BotCreationRequest
from ypl.slack_agent_gateway.commands import CREATE_AGENT_MODAL_CALLBACK_ID
from ypl.slack_agent_gateway.constants import get_agent_config_by_app_id, get_all_signing_secrets
from ypl.slack_agent_gateway.events import verify_slack_signature_multi
from ypl.slack_agent_gateway.redis_client import release_survey_response_claim, try_claim_survey_response
from ypl.slack_agent_gateway.slack_client import build_slack_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Map survey action_id suffix to feedback rating
_SURVEY_ACTION_TO_RATING: dict[str, str | None] = {
    "survey_bad": "NEGATIVE",
    "survey_ok": None,
    "survey_good": "POSITIVE",
}

# Bot Father action IDs
_BOT_FATHER_ACTIONS = frozenset({"bot_father_approve", "bot_father_deny"})

# Prefix for questionnaire choice action IDs (format: "questionnaire_{question_id}")
_QUESTIONNAIRE_ACTION_PREFIX = "questionnaire_"


async def _handle_survey_action(payload: dict[str, Any]) -> JSONResponse:
    """Handle a survey button click from a feedback survey.

    Extracts rating and optional comment, sends feedback to AHS,
    and replaces the survey message with a thank-you message.

    Args:
        payload: The parsed Slack interaction payload.

    Returns:
        JSONResponse acknowledging the action.
    """
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(status_code=200, content={"status": "no_actions"})

    action = actions[0]
    action_id = action.get("action_id", "")
    action_value = action.get("value", "")

    # Extract rating from action_id
    rating = _SURVEY_ACTION_TO_RATING.get(action_id)

    # Extract session_id from button value (format: "{rating_label}:{session_id}")
    parts = action_value.split(":", 1)
    if len(parts) != 2:
        logger.warning("Invalid survey button value format", value=action_value)
        return JSONResponse(status_code=200, content={"status": "invalid_value"})
    session_id = parts[1]

    # Extract optional comment from state.values
    comment: str | None = None
    try:
        state_values = payload.get("state", {}).get("values", {})
        comment_block = state_values.get("survey_comment", {})
        comment_input = comment_block.get("comment_input", {})
        comment = comment_input.get("value")
    except (AttributeError, TypeError):
        pass

    # Extract Slack user info
    slack_user_id = payload.get("user", {}).get("id", "")

    # Resolve Slack user to Yupp user_id
    yupp_user_id: str | None = None
    if slack_user_id:
        try:
            yupp_user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id)
        except Exception:
            logger.exception(
                "DB error resolving Slack user for survey feedback",
                slack_user_id=slack_user_id,
                session_id=session_id,
            )
    if not yupp_user_id:
        logger.warning(
            "Could not resolve Slack user for survey feedback, falling back to SYSTEM",
            slack_user_id=slack_user_id,
            session_id=session_id,
        )

    # Get the message ts and channel for updating the survey message
    message_channel = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")

    logger.info(
        "Recording survey feedback",
        session_id=session_id,
        action_id=action_id,
        rating=rating,
        has_comment=comment is not None and len(comment) > 0 if comment else False,
        slack_user_id=slack_user_id,
        yupp_user_id=yupp_user_id,
    )

    # Dedup: prevent duplicate submissions from double-clicks or Slack retries
    if not await try_claim_survey_response(session_id, slack_user_id):
        logger.info(
            "Duplicate survey response, skipping",
            session_id=session_id,
            slack_user_id=slack_user_id,
            action_id=action_id,
        )
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # Send feedback to AHS
    result = await send_feedback(
        session_id=session_id,
        user_id=yupp_user_id,
        slack_ts=message_ts,
        rating=rating,
        comment=comment,
        structured={"type": "survey", "slack_user_id": slack_user_id},
    )

    if result is None:
        logger.error(
            "Failed to send survey feedback to AHS",
            session_id=session_id,
            action_id=action_id,
        )
        # Release dedup claim so the user can retry
        await release_survey_response_claim(session_id, slack_user_id)
        # Keep the survey visible so the user can retry
        return JSONResponse(status_code=200, content={"status": "feedback_persistence_failed"})

    # Replace the survey message with a thank-you message only on successful persistence
    if message_channel and message_ts:
        try:
            api_app_id = payload.get("api_app_id", "")
            app_config = await get_agent_config_by_app_id(api_app_id)
            if app_config:
                client = build_slack_client(app_config.app_id, app_config.bot_token)
                rating_label = action_id.replace("survey_", "").capitalize()
                thank_you_text = f"_Thank you for your feedback! ({rating_label})_"
                await client.chat_update(
                    channel=message_channel,
                    ts=message_ts,
                    text=thank_you_text,
                    blocks=[],
                )
        except SlackApiError as e:
            logger.warning(
                "Failed to update survey message with thank-you",
                session_id=session_id,
                error=str(e),
            )

    return JSONResponse(status_code=200, content={"status": "feedback_recorded"})


async def _handle_bot_father_action(payload: dict[str, Any]) -> JSONResponse:
    """Handle a Bot Father approval/denial button click.

    Args:
        payload: The parsed Slack interaction payload.

    Returns:
        JSONResponse acknowledging the action.
    """
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(status_code=200, content={"status": "no_actions"})

    action = actions[0]
    action_id = action.get("action_id", "")
    request_id = action.get("value", "")
    slack_user_id = payload.get("user", {}).get("id", "")

    if not request_id:
        logger.warning("Bot Father action missing request_id", action_id=action_id)
        return JSONResponse(status_code=200, content={"status": "missing_request_id"})

    logger.info(
        "Processing Bot Father action",
        action_id=action_id,
        request_id=request_id,
        slack_user_id=slack_user_id,
    )

    try:
        if action_id == "bot_father_approve":
            await handle_approval(request_id, slack_user_id)
            return JSONResponse(status_code=200, content={"status": "approved"})
        if action_id == "bot_father_deny":
            await handle_denial(request_id, slack_user_id)
            return JSONResponse(status_code=200, content={"status": "denied"})
        return JSONResponse(status_code=200, content={"status": "unknown_bot_father_action"})
    except ValueError as e:
        logger.warning("Bot Father action failed", action_id=action_id, request_id=request_id, error=str(e))
        return JSONResponse(status_code=200, content={"status": "error", "message": str(e)})
    except Exception:
        logger.exception("Unexpected error in Bot Father action", action_id=action_id, request_id=request_id)
        return JSONResponse(status_code=200, content={"status": "error"})


async def _handle_create_agent_submission(payload: dict[str, Any]) -> JSONResponse:
    """Handle the create-agent modal form submission.

    Extracts form values and calls request_bot_creation().

    Args:
        payload: The parsed Slack view_submission payload.

    Returns:
        JSONResponse (empty for successful modal close, or errors object for validation).
    """
    # Extract user info
    user = payload.get("user", {})
    slack_user_id = user.get("id", "")

    # Extract form values from state
    state_values = payload.get("view", {}).get("state", {}).get("values", {})

    agent_name = state_values.get("agent_name_block", {}).get("agent_name_input", {}).get("value", "")
    slack_name = state_values.get("slack_name_block", {}).get("slack_name_input", {}).get("value", "")
    display_name = state_values.get("display_name_block", {}).get("display_name_input", {}).get("value", "")
    description = state_values.get("description_block", {}).get("description_input", {}).get("value")
    system_prompt = state_values.get("system_prompt_block", {}).get("system_prompt_input", {}).get("value")

    logger.info(
        "Processing create-agent modal submission",
        slack_user_id=slack_user_id,
        agent_name=agent_name,
        slack_name=slack_name,
        display_name=display_name,
        has_description=description is not None,
        has_system_prompt=system_prompt is not None,
    )

    # Validate inputs
    errors: dict[str, str] = {}
    if not agent_name or not agent_name.replace("-", "").isalnum() or agent_name != agent_name.lower():
        errors["agent_name_block"] = "Agent name must be lowercase letters, numbers, and hyphens only."
    if not slack_name or not slack_name.replace("-", "").isalnum() or slack_name != slack_name.lower():
        errors["slack_name_block"] = "Slack name must be lowercase letters, numbers, and hyphens only."
    if not display_name:
        errors["display_name_block"] = "Display name is required."

    if errors:
        return JSONResponse(
            status_code=200,
            content={"response_action": "errors", "errors": errors},
        )

    # Create the bot creation request
    try:
        bot_request = BotCreationRequest(
            agent_name=agent_name.lower(),
            slack_name=slack_name.lower(),
            display_name=display_name,
            requested_by=slack_user_id,
            requested_by_email=None,  # Resolved from Slack user ID in request_bot_creation
            description=description,
            system_prompt=system_prompt,
        )
        result = await request_bot_creation(bot_request)

        logger.info(
            "Bot creation request submitted from modal",
            request_id=result.request_id,
            agent_name=agent_name,
        )

        # Close the modal - empty 200 response closes it
        # The user will know the request succeeded because the modal closes without error
        return JSONResponse(status_code=200, content={})

    except ValueError as e:
        # Return error to the modal
        logger.warning("Bot creation request failed", error=str(e))
        return JSONResponse(
            status_code=200,
            content={
                "response_action": "errors",
                "errors": {"agent_name_block": str(e)},
            },
        )
    except PermissionError as e:
        logger.warning("Bot creation permission denied", error=str(e))
        return JSONResponse(
            status_code=200,
            content={
                "response_action": "errors",
                "errors": {"agent_name_block": str(e)},
            },
        )
    except Exception:
        logger.exception("Unexpected error in create-agent submission")
        return JSONResponse(
            status_code=200,
            content={
                "response_action": "errors",
                "errors": {"agent_name_block": "An unexpected error occurred. Please try again."},
            },
        )


async def _handle_questionnaire_action(payload: dict[str, Any]) -> JSONResponse:
    """Handle a questionnaire choice button click.

    Extracts the selected choice label from the button text and the session_id
    from the button value, then forwards the answer to AHS as a session message
    so the agent can continue processing.

    Args:
        payload: The parsed Slack interaction payload.

    Returns:
        JSONResponse acknowledging the action.
    """
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(status_code=200, content={"status": "no_actions"})

    action = actions[0]
    action_id = action.get("action_id", "")
    # The session_id is stored directly as the button value
    session_id = action.get("value", "")
    # The displayed choice label is the button text
    choice_label = action.get("text", {}).get("text", "")

    if not session_id:
        logger.warning("Questionnaire action missing session_id in value", action_id=action_id)
        return JSONResponse(status_code=200, content={"status": "missing_session_id"})

    if not choice_label:
        logger.warning("Questionnaire action missing choice label in button text", action_id=action_id)
        return JSONResponse(status_code=200, content={"status": "missing_choice_label"})

    # Extract the question_id from the action_id suffix (after "questionnaire_")
    question_id = action_id[len(_QUESTIONNAIRE_ACTION_PREFIX) :]

    slack_user_id = payload.get("user", {}).get("id", "")
    message_channel = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")

    logger.info(
        "Processing questionnaire answer",
        session_id=session_id,
        question_id=question_id,
        choice_label=choice_label,
        slack_user_id=slack_user_id,
    )

    # Forward the selected choice to AHS as a session message
    result = await send_questionnaire_answer_to_agent(
        session_id=session_id,
        answer_text=choice_label,
        slack_user_id=slack_user_id,
        slack_ts=message_ts,
    )

    if result is None:
        logger.error(
            "Failed to forward questionnaire answer to AHS",
            session_id=session_id,
            question_id=question_id,
        )
        # Keep the buttons visible so the user can retry
        return JSONResponse(status_code=200, content={"status": "forwarding_failed"})

    # Replace the questionnaire message with a confirmation showing the selection
    if message_channel and message_ts:
        try:
            api_app_id = payload.get("api_app_id", "")
            app_config = await get_agent_config_by_app_id(api_app_id)
            if app_config:
                client = build_slack_client(app_config.app_id, app_config.bot_token)
                confirmation_text = f"_You selected: {choice_label}_"
                await client.chat_update(
                    channel=message_channel,
                    ts=message_ts,
                    text=confirmation_text,
                    blocks=[],
                )
        except SlackApiError as e:
            logger.warning(
                "Failed to update questionnaire message with confirmation",
                session_id=session_id,
                error=str(e),
            )

    return JSONResponse(status_code=200, content={"status": "answer_forwarded"})


async def handle_interaction(payload: dict[str, Any]) -> JSONResponse:
    """Handle a Slack interaction payload.

    Routes to the appropriate handler based on payload type and action.

    Args:
        payload: The parsed Slack interaction payload.

    Returns:
        JSONResponse acknowledging the interaction.
    """
    payload_type = payload.get("type", "")

    # Handle modal submissions
    if payload_type == "view_submission":
        callback_id = payload.get("view", {}).get("callback_id", "")
        if callback_id == CREATE_AGENT_MODAL_CALLBACK_ID:
            return await _handle_create_agent_submission(payload)
        logger.info("Unhandled view_submission", callback_id=callback_id)
        return JSONResponse(status_code=200, content={})

    if payload_type != "block_actions":
        logger.info("Unhandled interaction type", interaction_type=payload_type)
        return JSONResponse(status_code=200, content={"status": "unhandled_type"})

    # Route based on action type
    actions = payload.get("actions", [])
    for action in actions:
        action_id = action.get("action_id", "")
        if action_id in _SURVEY_ACTION_TO_RATING:
            return await _handle_survey_action(payload)
        if action_id in _BOT_FATHER_ACTIONS:
            return await _handle_bot_father_action(payload)
        if action_id.startswith(_QUESTIONNAIRE_ACTION_PREFIX):
            return await _handle_questionnaire_action(payload)

    logger.info("Unhandled block_actions", action_ids=[a.get("action_id") for a in actions])
    return JSONResponse(status_code=200, content={"status": "unhandled_action"})


async def process_slack_interaction(request: Request) -> JSONResponse:
    """Process incoming Slack interaction webhook.

    Main entry point for /slack/interactions endpoint.
    Slack sends interactions as application/x-www-form-urlencoded with a 'payload' field.

    Args:
        request: The incoming FastAPI request.

    Returns:
        JSONResponse with processing result.
    """
    # Read raw body for signature verification
    body = await request.body()

    # Verify signature against all configured signing secrets
    signing_secrets = await get_all_signing_secrets()
    if not signing_secrets:
        logger.error("No Slack agent apps configured")
        raise HTTPException(status_code=500, detail="No Slack agent apps configured")

    verify_slack_signature_multi(request, body, signing_secrets)

    # Parse form data — Slack sends application/x-www-form-urlencoded
    try:
        body_str = body.decode("utf-8")
        # Extract the 'payload' field from form data
        payload_str: str | None = None
        for part in body_str.split("&"):
            if part.startswith("payload="):
                payload_str = unquote_plus(part[len("payload=") :])
                break

        if not payload_str:
            raise HTTPException(status_code=400, detail="Missing payload in interaction request")

        payload = json.loads(payload_str)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Error parsing Slack interaction payload", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid interaction payload") from e

    return await handle_interaction(payload)
