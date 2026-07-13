"""Slack slash command handler for Bot Father.

Handles the /create-agent slash command to create new Slack bots via a modal.
"""

from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ypl.slack_agent_gateway.constants import get_bot_father_config
from ypl.slack_agent_gateway.events import verify_slack_signature_multi
from ypl.slack_agent_gateway.slack_client import build_slack_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Modal callback ID for create-agent form submission
CREATE_AGENT_MODAL_CALLBACK_ID = "bot_father_create_agent_modal"


def _build_create_agent_modal(trigger_id: str) -> dict[str, Any]:
    """Build the Slack modal for creating a new agent bot.

    Args:
        trigger_id: Slack trigger_id from the slash command.

    Returns:
        Modal view payload for views.open.
    """
    return {
        "trigger_id": trigger_id,
        "view": {
            "type": "modal",
            "callback_id": CREATE_AGENT_MODAL_CALLBACK_ID,
            "title": {"type": "plain_text", "text": "Create Agent Bot"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "agent_name_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "agent_name_input",
                        "placeholder": {"type": "plain_text", "text": "e.g., sre, code-reviewer"},
                    },
                    "label": {"type": "plain_text", "text": "Agent Name (internal slug)"},
                    "hint": {"type": "plain_text", "text": "Lowercase letters, numbers, and hyphens only."},
                },
                {
                    "type": "input",
                    "block_id": "slack_name_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "slack_name_input",
                        "placeholder": {"type": "plain_text", "text": "e.g., giladovski"},
                    },
                    "label": {"type": "plain_text", "text": "Slack Bot Username"},
                    "hint": {"type": "plain_text", "text": "The @username for the bot in Slack."},
                },
                {
                    "type": "input",
                    "block_id": "display_name_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "display_name_input",
                        "placeholder": {"type": "plain_text", "text": "e.g., Giladovski"},
                    },
                    "label": {"type": "plain_text", "text": "Display Name"},
                    "hint": {"type": "plain_text", "text": "Human-readable name shown in Slack."},
                },
                {
                    "type": "input",
                    "block_id": "description_block",
                    "optional": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "description_input",
                        "placeholder": {"type": "plain_text", "text": "e.g., SRE assistant for incident response"},
                    },
                    "label": {"type": "plain_text", "text": "Description (optional)"},
                    "hint": {"type": "plain_text", "text": "Brief description of what this agent does."},
                },
                {
                    "type": "input",
                    "block_id": "system_prompt_block",
                    "optional": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "system_prompt_input",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "e.g., You are a helpful SRE assistant..."},
                    },
                    "label": {"type": "plain_text", "text": "System Prompt (optional)"},
                    "hint": {"type": "plain_text", "text": "Additional instructions or persona for the agent."},
                },
            ],
        },
    }


async def process_slack_command(request: Request) -> JSONResponse:
    """Process incoming Slack slash command.

    Main entry point for /slack/commands endpoint.
    Slack sends commands as application/x-www-form-urlencoded.

    Args:
        request: The incoming FastAPI request.

    Returns:
        JSONResponse with immediate acknowledgment (Slack requires <3s response).
    """
    # Read raw body for signature verification
    body = await request.body()

    # Verify signature using Bot Father's signing secret
    bot_father_config = get_bot_father_config()
    signing_secret = bot_father_config.get("signing_secret", "")
    if not signing_secret:
        logger.error("Bot Father signing secret not configured")
        raise HTTPException(status_code=500, detail="Bot Father not configured")

    verify_slack_signature_multi(request, body, [signing_secret])

    # Parse form data
    try:
        body_str = body.decode("utf-8")
        form_data = parse_qs(body_str)
    except UnicodeDecodeError as e:
        logger.warning("Error decoding slash command body", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid request body") from e

    # Extract command info
    command = form_data.get("command", [""])[0]
    trigger_id = form_data.get("trigger_id", [""])[0]
    user_id = form_data.get("user_id", [""])[0]
    channel_id = form_data.get("channel_id", [""])[0]

    logger.info(
        "Received slash command",
        command=command,
        user_id=user_id,
        channel_id=channel_id,
    )

    # Handle /create-agent command
    if command == "/create-agent":
        return await _handle_create_agent_command(trigger_id, user_id)

    # Unknown command
    logger.warning("Unknown slash command", command=command)
    return JSONResponse(
        status_code=200,
        content={"response_type": "ephemeral", "text": f"Unknown command: {command}"},
    )


async def _handle_create_agent_command(trigger_id: str, user_id: str) -> JSONResponse:
    """Handle the /create-agent slash command by opening a modal.

    Args:
        trigger_id: Slack trigger_id for opening the modal.
        user_id: Slack user ID of the requester.

    Returns:
        JSONResponse acknowledging the command.
    """
    bot_father_config = get_bot_father_config()
    bot_token = bot_father_config.get("bot_token", "")

    if not bot_token:
        logger.error("Bot Father bot token not configured")
        return JSONResponse(
            status_code=200,
            content={"response_type": "ephemeral", "text": "Bot Father is not configured."},
        )

    try:
        # Bot Father is a singleton app — a stable pseudo-id keys the gate correctly.
        client = build_slack_client("bot_father", bot_token)
        modal_payload = _build_create_agent_modal(trigger_id)

        await client.views_open(
            trigger_id=trigger_id,
            view=modal_payload["view"],
        )

        logger.info("Opened create-agent modal", user_id=user_id)

        # Return empty 200 to acknowledge the command (modal handles the rest)
        return JSONResponse(status_code=200, content={})

    except Exception as e:
        logger.exception("Failed to open create-agent modal", error=str(e))
        return JSONResponse(
            status_code=200,
            content={"response_type": "ephemeral", "text": "Failed to open the form. Please try again."},
        )
