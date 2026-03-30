"""FastAPI routes for Slack Agent Gateway.

Defines the HTTP endpoints for:
- Receiving Slack events (/slack/events)
- Callback APIs for Agent Service (/sessions/*)
"""

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ypl.slack_agent_gateway.auth import verify_api_key
from ypl.slack_agent_gateway.bot_father import (
    complete_oauth_installation,
    get_bot_status,
    handle_oauth_cancellation,
    request_bot_creation,
)
from ypl.slack_agent_gateway.bot_father_types import (
    BotCreationRequest,
    BotCreationResponse,
    BotStatusResponse,
)
from ypl.slack_agent_gateway.buffer import append_to_reply
from ypl.slack_agent_gateway.callbacks import (
    add_reply,
    request_feedback,
    send_message,
    send_status_update,
    update_reply,
)
from ypl.slack_agent_gateway.commands import process_slack_command
from ypl.slack_agent_gateway.events import process_slack_event
from ypl.slack_agent_gateway.interactions import process_slack_interaction
from ypl.slack_agent_gateway.sessions import get_session_info
from ypl.slack_agent_gateway.types import (
    AddReplyRequest,
    AddReplyResponse,
    AppendToReplyRequest,
    AppendToReplyResponse,
    GetSessionInfoRequest,
    RequestFeedbackRequest,
    RequestFeedbackResponse,
    SendMessageRequest,
    SendMessageResponse,
    SendStatusUpdateRequest,
    SendStatusUpdateResponse,
    SlackSessionInfoResponse,
    UpdateReplyRequest,
    UpdateReplyResponse,
)

# Router without prefix - will be mounted at multiple prefixes in server.py
router = APIRouter(tags=["slack-agent-gateway"])


@router.post("/slack/events")
async def slack_events(request: Request) -> JSONResponse:
    """Handle Slack bot webhook requests.

    This endpoint receives events from all configured Slack agent apps.
    It verifies signatures, deduplicates events, and processes them.

    Args:
        request: The incoming FastAPI request containing the Slack payload

    Returns:
        JSONResponse with processing result
    """
    return await process_slack_event(request)


@router.post("/sessions/info", dependencies=[Depends(verify_api_key)])
async def session_info(request_body: GetSessionInfoRequest) -> SlackSessionInfoResponse:
    """Get session info for Agent Service.

    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id

    Returns:
        SlackSessionInfoResponse with session details

    Raises:
        HTTPException: If session not found
    """
    info = await get_session_info(request_body.session_id)
    if not info:
        raise HTTPException(status_code=404, detail="Session not found")
    return info


@router.post("/sessions/reply", dependencies=[Depends(verify_api_key)])
async def post_reply(request_body: AddReplyRequest) -> AddReplyResponse:
    """Add a new reply message to Slack.

    Posts immediately to Slack as a new message in the thread.
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id and text

    Returns:
        AddReplyResponse with success status and message_ts
    """
    return await add_reply(request_body)


@router.post("/sessions/reply/append", dependencies=[Depends(verify_api_key)])
async def post_reply_append(request_body: AppendToReplyRequest) -> AppendToReplyResponse:
    """Append text to the last reply (buffered).

    Text is buffered and flushed to Slack at rate-limited intervals (~1.2s).
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id and text to append

    Returns:
        AppendToReplyResponse with success status
    """
    return await append_to_reply(request_body)


@router.post("/sessions/reply/update", dependencies=[Depends(verify_api_key)])
async def post_reply_update(request_body: UpdateReplyRequest) -> UpdateReplyResponse:
    """Replace the content of the last reply.

    Discards any pending buffer and replaces the message content.
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id and new text

    Returns:
        UpdateReplyResponse with success status
    """
    return await update_reply(request_body)


@router.post("/slack/interactions")
async def slack_interactions(request: Request) -> JSONResponse:
    """Handle Slack interactivity payloads (button clicks, form submissions).

    Slack sends these as application/x-www-form-urlencoded with a 'payload' field.
    Signature is verified against all configured signing secrets.

    Args:
        request: The incoming FastAPI request containing the Slack interaction payload

    Returns:
        JSONResponse acknowledging the interaction
    """
    return await process_slack_interaction(request)


@router.post("/slack/commands")
async def slack_commands(request: Request) -> JSONResponse:
    """Handle Slack slash commands (e.g., /create-agent).

    Slack sends commands as application/x-www-form-urlencoded.
    Signature is verified using Bot Father's signing secret.

    Args:
        request: The incoming FastAPI request containing the slash command payload

    Returns:
        JSONResponse acknowledging the command (must respond within 3 seconds)
    """
    return await process_slack_command(request)


@router.post("/sessions/request-feedback", dependencies=[Depends(verify_api_key)])
async def post_request_feedback(request_body: RequestFeedbackRequest) -> RequestFeedbackResponse:
    """Request a feedback survey be posted to the Slack thread.

    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id and optional prompt

    Returns:
        RequestFeedbackResponse with success status and message_ts
    """
    return await request_feedback(request_body)


@router.post("/sessions/status", dependencies=[Depends(verify_api_key)])
async def post_status_update(request_body: SendStatusUpdateRequest) -> SendStatusUpdateResponse:
    """Post or update a live status hint for an agent session.

    AHS calls this endpoint to push tool-use progress and other internal info
    during execution.  SAG renders the text as a muted context block and edits
    it in-place so that users see a continuously updating status line without
    new messages being created.  Rate-limited to at most one Slack API call per
    2 seconds per session.
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing session_id and status text

    Returns:
        SendStatusUpdateResponse with success status

    Raises:
        HTTPException: 404 if the session is not found (expired or never existed),
            so AHS can evict the session from its status-update queue immediately.
    """
    result = await send_status_update(request_body)
    if not result.success and result.error == "Session not found":
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@router.post("/messages/send", dependencies=[Depends(verify_api_key)])
async def post_send_message(request_body: SendMessageRequest) -> SendMessageResponse:
    """Send a message to a Slack channel proactively.

    Allows an agent to initiate a conversation by sending a message to any channel.
    Does not require an existing session.
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing agent_name, channel_id, text, and optional thread_ts

    Returns:
        SendMessageResponse with success status and message_ts
    """
    return await send_message(request_body)


# ---------------------------------------------------------------------------
# Bot Father routes
# ---------------------------------------------------------------------------


@router.post("/bot-father/request", dependencies=[Depends(verify_api_key)])
async def post_bot_father_request(request_body: BotCreationRequest) -> BotCreationResponse:
    """Request creation of a new Slack bot for an agent.

    Creates a pending bot creation request that must be approved via Slack.
    Requires X-API-Key header for authentication.

    Args:
        request_body: Request containing agent_name, slack_name, display_name, and requester info

    Returns:
        BotCreationResponse with request_id and PENDING status

    Raises:
        HTTPException: If agent already has a bot or a pending request exists
    """
    try:
        return await request_bot_creation(request_body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.get("/bot-father/status/{agent_name}", dependencies=[Depends(verify_api_key)])
async def get_bot_father_status(agent_name: str) -> BotStatusResponse:
    """Get the status of Slack bot setup for an agent.

    Returns whether the agent has a live bot and any pending creation requests.
    Requires X-API-Key header for authentication.

    Args:
        agent_name: AHS agent name to check

    Returns:
        BotStatusResponse with bot status and pending request info
    """
    return await get_bot_status(agent_name)


# ---------------------------------------------------------------------------
# OAuth callback route (no auth - Slack redirects browser here)
# ---------------------------------------------------------------------------


@router.get("/slack/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> HTMLResponse:
    """Handle OAuth callback from Slack after app installation.

    This endpoint is called by Slack when an admin completes the OAuth flow.
    Slack redirects the admin's browser here with an authorization code.

    No API key auth because this is a browser redirect from Slack.

    Args:
        code: Authorization code from Slack (exchanged for bot token).
        state: State parameter (request_id) for CSRF protection.
        error: Error code if the user denied authorization.

    Returns:
        HTML page showing success or error message.
    """
    if error:
        # Handle the cancellation in bot_father to update record status
        await handle_oauth_cancellation(state=state or "", error=error)
        return HTMLResponse(
            content=f"""
            <html>
            <head><title>Bot Installation Failed</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>❌ Installation Cancelled</h1>
                <p>The Slack app installation was cancelled or denied.</p>
                <p>Error: {escape(error)}</p>
                <p>You can close this window.</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            content="""
            <html>
            <head><title>Bot Installation Failed</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>❌ Invalid Request</h1>
                <p>Missing authorization code or state parameter.</p>
                <p>Please try the installation link again.</p>
            </body>
            </html>
            """,
            status_code=400,
        )

    try:
        record = await complete_oauth_installation(code=code, state=state)
        # Build icon instructions HTML only if we have a valid app ID
        icon_instructions_html = ""
        if record.slack_app_id:
            app_settings_url = f"https://api.slack.com/apps/{escape(record.slack_app_id)}/general"
            icon_instructions_html = f"""
                <hr style="margin: 20px 0; border: none; border-top: 1px solid #ddd;">
                <p><strong>📷 To set a custom icon:</strong></p>
                <p>Visit the <a href="{app_settings_url}" target="_blank" rel="noopener noreferrer">
                App Settings page</a> and upload your icon under "Display Information".</p>"""

        return HTMLResponse(
            content=f"""
            <html>
            <head><title>Bot Installation Complete</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>✅ Installation Complete!</h1>
                <p>The Slack bot <strong>{escape(record.display_name)}</strong> has been successfully installed.</p>
                <p>App ID: <code>{escape(record.slack_app_id or "")}</code></p>
                <p>Mention <strong>@{escape(record.slack_name)}</strong> in any channel to use the bot.</p>
                {icon_instructions_html}
                <p style="margin-top: 20px;">You can close this window.</p>
            </body>
            </html>
            """,
            status_code=200,
        )
    except ValueError as e:
        return HTMLResponse(
            content=f"""
            <html>
            <head><title>Bot Installation Failed</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>❌ Installation Failed</h1>
                <p>{escape(str(e))}</p>
                <p>Please contact the administrator.</p>
            </body>
            </html>
            """,
            status_code=400,
        )
    except Exception as e:
        return HTMLResponse(
            content=f"""
            <html>
            <head><title>Bot Installation Failed</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1>❌ Installation Failed</h1>
                <p>An error occurred during installation.</p>
                <p>Error: {escape(str(e))}</p>
                <p>Please try again or contact the administrator.</p>
            </body>
            </html>
            """,
            status_code=500,
        )
