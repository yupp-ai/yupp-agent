import hashlib
import hmac
import io
import json
import os
import time
from enum import Enum, auto
from typing import Any

import httpx
from fastapi import HTTPException, Request
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook import WebhookClient
from slack_sdk.webhook.async_client import AsyncWebhookClient
from sqlalchemy import text
from starlette.status import HTTP_401_UNAUTHORIZED
from tenacity import retry, retry_if_exception, retry_if_exception_type, stop_after_attempt, wait_fixed

from ypl.backend.config import settings
from ypl.backend.db import get_async_session_read_replica
from ypl.backend.llm.constants import SLACK_ID_TO_EMAIL
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.json import json_dumps
from ypl.structured_logger import get_logger
from ypl.utils import async_timed_cache

logger = get_logger()


class YuppSlackApps(Enum):
    """Enum for Yupp Slack applications."""

    ABUSE_ALERT = auto()
    LIT_ACTIONS = auto()
    MODEL_MANAGEMENT = auto()
    REWARD_MANAGEMENT = auto()
    APP_FEEDBACK = auto()
    GUEST_MANAGEMENT = auto()
    SOUL_SLACKBOT = auto()
    INTERESTING_TURNS = auto()


class SlackPayloadType(str, Enum):
    """Enum for Slack webhook payload types."""

    URL_VERIFICATION = "url_verification"
    EVENT_CALLBACK = "event_callback"


class SlackEventType(str, Enum):
    """Enum for Slack event types within event_callback payloads."""

    MESSAGE = "message"
    APP_MENTION = "app_mention"
    MEMBER_JOINED_CHANNEL = "member_joined_channel"
    MEMBER_LEFT_CHANNEL = "member_left_channel"
    CHANNEL_CREATED = "channel_created"
    CHANNEL_DELETED = "channel_deleted"
    REACTION_ADDED = "reaction_added"
    REACTION_REMOVED = "reaction_removed"


class SlackCommandType(str, Enum):
    """Enum for Slack slash command types.

    These correspond to slash commands configured in the Slack app (e.g., /soul-search, /soul-status).
    The command name (without the leading slash) should match the enum value.
    """

    SOUL_CASHOUT = "soul-cashout"
    SOUL_HELP = "soul-help"
    SOUL_SEARCH = "soul-search"
    SOUL_STATUS = "soul-status"
    SOUL_TICKETS = "soul-tickets"


"""mapping from slack app enum to a tuple of (bot token env var, signing secret env var)"""
SLACK_APP_TOKEN_ENV_VARS = {
    YuppSlackApps.MODEL_MANAGEMENT: ("SLACK_MODEL_MANAGEMENT_APP_BOT_TOKEN", "SLACK_MODEL_MANAGEMENT_SIGNING_SECRET"),
    YuppSlackApps.ABUSE_ALERT: ("SLACK_ABUSE_ALERT_APP_BOT_TOKEN", "SLACK_ABUSE_ALERT_SIGNING_SECRET"),
    YuppSlackApps.REWARD_MANAGEMENT: (
        "SLACK_REWARD_MANAGEMENT_APP_BOT_TOKEN",
        "SLACK_REWARD_MANAGEMENT_SIGNING_SECRET",
    ),
    YuppSlackApps.APP_FEEDBACK: ("SLACK_APP_FEEDBACK_APP_BOT_TOKEN", "SLACK_APP_FEEDBACK_SIGNING_SECRET"),
    YuppSlackApps.GUEST_MANAGEMENT: ("SLACK_GUEST_MANAGEMENT_APP_BOT_TOKEN", "SLACK_GUEST_MANAGEMENT_SIGNING_SECRET"),
    YuppSlackApps.SOUL_SLACKBOT: ("SLACK_SOUL_SLACKBOT_APP_BOT_TOKEN", "SLACK_SOUL_SLACKBOT_SIGNING_SECRET"),
    YuppSlackApps.INTERESTING_TURNS: (
        "SLACK_INTERESTING_TURNS_APP_BOT_TOKEN",
        "SLACK_INTERESTING_TURNS_SIGNING_SECRET",
    ),
    YuppSlackApps.LIT_ACTIONS: ("SLACK_ABUSE_ALERT_APP_BOT_TOKEN", "SLACK_ABUSE_ALERT_SIGNING_SECRET"),
}


def get_abuse_alert_channel_id() -> str:
    match os.environ.get("ENVIRONMENT"):
        case "production":
            return "C08HZTSQ4GZ"
        case "staging":
            return "C0911Q988AK"
        case _:
            return "C0911Q988AK"


def get_backend_alert_channels() -> str:
    match os.environ.get("ENVIRONMENT"):
        case "production":
            return "alert-backend"
        case "staging":
            return "alert-backend-staging"
        case _:
            return "alert-test-only"


def get_slack_token_and_secret(app: YuppSlackApps) -> tuple[str, str]:
    bot_token_env_var, signing_secret_env_var = SLACK_APP_TOKEN_ENV_VARS[app]
    bot_token = os.environ.get(bot_token_env_var)
    if bot_token is None:
        raise ValueError(f"No Slack token found for app {app.name} in environment variable {bot_token_env_var}")
    signing_secret = os.environ.get(signing_secret_env_var)
    if signing_secret is None:
        raise ValueError(
            f"No Slack signing secret found for app {app.name} in environment variable {signing_secret_env_var}"
        )
    return bot_token, signing_secret


def verify_slack_signature(request: Request, body: bytes, signing_secret: str) -> None:
    """Verify Slack webhook signature using HMAC-SHA256.

    This function verifies that incoming requests from Slack are authentic and haven't been tampered with.
    It checks the X-Slack-Signature and X-Slack-Request-Timestamp headers and validates the signature
    using HMAC-SHA256 with the provided signing secret.

    Args:
        request: The incoming FastAPI request
        body: The raw request body bytes
        signing_secret: The Slack app signing secret

    Raises:
        HTTPException: If signature validation fails (missing headers, invalid timestamp, or signature mismatch)
    """
    logger = get_logger()

    if settings.ENVIRONMENT == "local":
        return

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
                {"timestamp": timestamp, "time_diff": time_diff},
            )
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Request timestamp too old")
    except ValueError as e:
        logger.warning("Invalid timestamp format", {"timestamp": timestamp, "error": str(e)})
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid timestamp format") from e

    # Construct signature base string
    sig_basestring = f"v0:{timestamp}:".encode() + body

    # Compute HMAC-SHA256
    computed_signature = hmac.new(
        signing_secret.encode(),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()
    expected_signature = f"v0={computed_signature}"

    # Compare signatures using constant-time comparison
    if not hmac.compare_digest(expected_signature, signature):
        logger.warning(
            "Invalid Slack signature",
            {"signature": signature, "expected_signature": expected_signature},
        )
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Invalid Slack signature")


async def post_to_slack_channel(
    message: str, channel: str | tuple[str, ...], app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT
) -> str | None:
    return await post_to_slack_channel_sync(message, channel, app, thread_ts=None)


def is_retryable_http_error(exc: BaseException) -> bool:
    # Retry if it's a 429 or a transient 5xx error
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(0.2),
    retry=retry_if_exception_type((TimeoutError, httpx.ConnectError, httpx.TimeoutException))
    | retry_if_exception(is_retryable_http_error),
)
async def post_to_slack_channel_sync(
    message: str,
    channel: str | tuple[str, ...],
    app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT,
    *,
    thread_ts: str | None = None,
) -> str | None:
    """
    Post a message to a Slack channel using a specific app

    Args:
        message: The message text to post
        channel: The channel ID or channel name to post to
        app: The Slack app to use for posting
        thread_ts: Optional thread timestamp to post as a reply to a thread

    Returns:
        str | None: The timestamp of the posted message, or None if posting failed
    """
    if len(message.strip()) == 0:
        logger.warning(f"Skipping empty message to Slack channel {channel}")
        return None
    if os.environ.get("ENVIRONMENT") == "local":
        # routing everything to a test channel if it's local model so we don't contaminate the main channel
        # and still get some messages.
        channels = ["#alert-test-only"]
    else:
        channels = [channel] if isinstance(channel, str) else list(channel)

    client = get_slack_httpx_client()

    for channel in channels:
        bot_token, _ = get_slack_token_and_secret(app)
        message_dict = {
            "channel": channel,
            "text": message,
        }
        if thread_ts:
            message_dict["thread_ts"] = thread_ts
        headers = {
            "Authorization": f"Bearer {bot_token}",
        }
        try:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json=message_dict,
            )
            response.raise_for_status()
            response_data = response.json()
            if response_data.get("ok"):
                return str(response_data.get("ts", ""))
            logger.error(
                {
                    "message": f"Slack API error: {response_data.get('error', 'Unknown error')}",
                    "response_data": response_data,
                    "channel": channel,
                    "app": app,
                    "slack_message": message,
                }
            )
            return None
        except Exception:
            logger.exception(f"Failed to post message to Slack channel {channel}", exc_info=True)
            return None

    return None


IGNORE_CANDIDATE_ACTION = "ignore_candidate"
REVIEW_CANDIATE_ACTION = "review_candidate"
SUBMIT_CANDIDATE_ACTION = "submit_candidate"


def is_prod_environment() -> bool:
    return os.environ.get("ENVIRONMENT") == "production"


def is_staging_environment() -> bool:
    return os.environ.get("ENVIRONMENT") == "staging"


def is_local_environment() -> bool:
    return os.environ.get("ENVIRONMENT") == "local"


async def get_slack_user_by_email(email: str, app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT) -> str | None:
    """
    Get Slack user ID by email address.

    Args:
        email: Email address to look up
        app: The Slack app to use for the API call

    Returns:
        str | None: Slack user ID if found, None otherwise
    """
    bot_token, _ = get_slack_token_and_secret(app)
    client = AsyncWebClient(token=bot_token)
    try:
        response = await client.users_lookupByEmail(email=email)
        if response.get("ok") and response.get("user"):
            user_id = response["user"].get("id")
            return user_id or None
        logger.warning(
            {
                "message": f"Failed to find Slack user by email: {email}",
                "error": response.get("error"),
                "response": response,
            }
        )
        return None
    except Exception:
        logger.exception(f"Failed to lookup Slack user by email: {email}", exc_info=True)
        return None


async def resolve_slack_recipient(recipient: str, app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT) -> str:
    """Resolve a Slack recipient from email or channel/user ID.

    If the recipient contains '@', it's treated as an email address and
    looked up to get the Slack user ID. Otherwise, it's returned as-is
    (assumed to be a channel name or user ID).

    Args:
        recipient: Email address, Slack channel name, or Slack user ID
        app: The Slack app to use for the API call

    Returns:
        Resolved Slack user ID (if email), or the original recipient
    """
    if "@" in recipient:
        resolved = await get_slack_user_by_email(recipient, app)
        # Fallback to original recipient if email lookup fails
        return resolved if resolved is not None else recipient
    return recipient


async def get_user_email_from_slack(user_id: str, app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT) -> str | None:
    """Get user email from Slack user ID.

    Args:
        user_id: Slack user ID
        app: Slack app to use for the API call. Defaults to MODEL_MANAGEMENT.
    """
    try:
        bot_token, _ = get_slack_token_and_secret(app)
        client = AsyncWebClient(token=bot_token)
        response = await client.users_info(user=user_id)
        if response.get("ok") and response.get("user"):
            user = response["user"]
            if isinstance(user, dict):
                profile = user.get("profile", {})
                if isinstance(profile, dict):
                    email = profile.get("email")
                    return email if isinstance(email, str) else None
        return None
    except Exception as e:
        logger.warning(
            json.dumps(
                {
                    "message": f"Failed to get user email from Slack for user_id {user_id}",
                    "error": str(e),
                }
            )
        )
        return None


@async_timed_cache(seconds=3600 * 12)
async def resolve_slack_user_to_yupp_user_id(slack_user_id: str) -> str | None:
    """Resolve a Slack member ID to an internal Yupp user_id.

    Resolution strategy (first match wins):
    1. Slack API via model management bot token (has users:read.email scope) → email → DB lookup
    2. Hardcoded SLACK_ID_TO_EMAIL mapping (fallback for @yupp.ai employees) → DB lookup

    Uses the model management bot token (not agent tokens) because not all
    agent apps have the users:read.email scope.

    Uses a raw SQL query to avoid triggering SQLAlchemy mapper configuration, which
    can fail in services that don't import all ORM models (e.g., MemoryEmbedding).

    DB exceptions are re-raised (not caught) so that the cache does not store a transient
    failure as a negative result. The caller is responsible for handling exceptions.

    Args:
        slack_user_id: Slack member ID (e.g., "U086VNKP095")

    Returns:
        Internal Yupp user_id if found, None otherwise
    """
    # Try Slack API first (model management token has users:read.email scope)
    email = await get_user_email_from_slack(slack_user_id, app=YuppSlackApps.MODEL_MANAGEMENT)
    # Fallback to hardcoded mapping (only covers known @yupp.ai employees)
    if not email:
        email = SLACK_ID_TO_EMAIL.get(slack_user_id)
    if not email:
        return None

    async with get_async_session_read_replica() as session:
        result = await session.execute(text("SELECT user_id FROM users WHERE email = :email"), {"email": email})
        row = result.first()
        return str(row[0]) if row else None


async def post_thread_message(channel: str, thread_ts: str, text: str, app: YuppSlackApps) -> None:
    bot_token, _ = get_slack_token_and_secret(app)
    client = AsyncWebClient(token=bot_token)
    await client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)


async def post_threaded_slack_messages(
    channel: str, messages: list[str], app: YuppSlackApps = YuppSlackApps.MODEL_MANAGEMENT
) -> None:
    """
    Post multiple messages to Slack, with all subsequent messages threaded to the first.
    If the first message fails, all remaining messages will be posted as regular messages.

    Args:
        channel: The Slack channel to post to
        messages: List of messages to post (first message starts the thread, rest are threaded)
        app: The Slack app to use for posting
    """
    if not messages:
        return

    if len(messages) == 1:
        # Single message, just post it normally
        await post_to_slack_channel(messages[0], channel, app)
        return

    # Post first message and get timestamp for threading
    thread_ts = await post_to_slack_channel(messages[0], channel, app)

    if thread_ts:
        # Post remaining messages as thread replies
        for message in messages[1:]:
            await post_thread_message(channel, thread_ts, message, app)
    else:
        # Fallback: if first message failed, try to post all remaining messages as regular messages
        logger.warning(
            "Failed to get timestamp from first Slack message, posting remaining messages as regular messages"
        )
        for message in messages[1:]:
            await post_to_slack_channel(message, channel, app)


async def upload_csv_to_slack(
    csv_content: str,
    filename: str,
    initial_comment: str,
    channel: str,
    bot_token: str,
    thread_ts: str | None = None,
) -> None:
    client = AsyncWebClient(token=bot_token)
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))
    await client.files_upload_v2(
        channel=channel,
        file=file_bytes,
        filename=filename,
        title=filename,
        initial_comment=initial_comment,
        thread_ts=thread_ts,
    )


def get_guest_management_channel_id() -> str:
    match os.environ.get("ENVIRONMENT"):
        case "production":
            return "C085UEJ6JM6"
        case "staging":
            return "C0826FU9JMB"
        case _:
            return "C0826FU9JMB"


# Mapping from YuppSlackApps to their corresponding environment variable names
def post_to_slack_bg(
    message: str | dict[str, Any] | None = None, webhook_url: str | None = None, blocks: list | None = None
) -> None:
    """Post a message to a Slack channel using a webhook URL in the background."""
    create_background_task(post_to_slack(message, webhook_url, blocks))


SLACK_CLIENTS: dict[str, AsyncWebhookClient] = {}
SLACK_SYNC_CLIENTS: dict[str, WebhookClient] = {}

_slack_httpx_client: httpx.AsyncClient | None = None


def get_slack_httpx_client() -> httpx.AsyncClient:
    """Get or create global httpx client for Slack API calls with connection pooling."""
    global _slack_httpx_client

    if _slack_httpx_client is None:
        limits = httpx.Limits(
            max_keepalive_connections=20,  # Maximum number of connections to keep alive
            max_connections=100,  # Maximum total number of connections
            keepalive_expiry=30.0,  # Keep connections alive for 30 seconds
        )

        timeout = httpx.Timeout(
            connect=10.0,  # Connection timeout
            read=30.0,  # Read timeout
            write=10.0,  # Write timeout
            pool=5.0,  # Pool timeout
        )

        _slack_httpx_client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "yupp-slack-client/1.0",
            },
        )

    return _slack_httpx_client


async def cleanup_slack_httpx_client() -> None:
    """Cleanup the global httpx client."""
    global _slack_httpx_client

    if _slack_httpx_client is not None:
        await _slack_httpx_client.aclose()
        _slack_httpx_client = None


def get_slack_sync_client(webhook_url: str) -> WebhookClient:
    if webhook_url not in SLACK_SYNC_CLIENTS:
        SLACK_SYNC_CLIENTS[webhook_url] = WebhookClient(webhook_url)
    return SLACK_SYNC_CLIENTS[webhook_url]


def get_slack_client(webhook_url: str) -> AsyncWebhookClient:
    if webhook_url not in SLACK_CLIENTS:
        SLACK_CLIENTS[webhook_url] = AsyncWebhookClient(webhook_url)
        SLACK_CLIENTS[webhook_url].retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=1))
    return SLACK_CLIENTS[webhook_url]


def extract_plain_text_from_slack_blocks(blocks: list[dict[str, Any]] | None) -> str:
    """Extract plain text from Slack rich text blocks.

    Args:
        blocks: List of Slack block objects (e.g., rich_text blocks)

    Returns:
        Plain text extracted from the blocks
    """
    if not blocks:
        return ""

    text_parts: list[str] = []

    def extract_from_element(element: dict[str, Any]) -> None:
        """Recursively extract text from a Slack block element."""
        element_type = element.get("type", "")
        if element_type == "text":
            text_parts.append(element.get("text", ""))
        elif element_type == "link":
            # Use the display text, not the URL
            text_parts.append(element.get("text", ""))
        elif element_type == "user":
            # Skip user mentions in plain text extraction
            pass
        elif "elements" in element:
            # Recursively process nested elements
            for nested_element in element.get("elements", []):
                extract_from_element(nested_element)

    for block in blocks:
        block_type = block.get("type", "")
        if block_type == "rich_text":
            for element in block.get("elements", []):
                extract_from_element(element)

    return "".join(text_parts).strip()


async def post_to_slack_response_url(response_url: str, response: dict[str, Any]) -> None:
    """Post a delayed response to Slack using the response_url from a slash command.

    Args:
        response_url: The response_url provided by Slack in the slash command payload
        response: The response dict to send (should include 'text' and optionally 'response_type')
    """
    logger = get_logger()
    client = get_slack_httpx_client()

    try:
        slack_response = await client.post(response_url, json=response)
        slack_response.raise_for_status()
        # Slack response_url returns plain text "ok" on success, not JSON
        response_text = slack_response.text.strip()
        if response_text != "ok":
            # If not "ok", try to parse as JSON to get error details
            try:
                response_data = slack_response.json()
                if not response_data.get("ok"):
                    logger.warning(
                        "Slack response_url API error",
                        error=response_data.get("error", "Unknown error"),
                        response_data=response_data,
                    )
            except Exception:
                logger.warning(
                    "Slack response_url returned unexpected response",
                    response_text=response_text,
                    status_code=slack_response.status_code,
                )
    except Exception as e:
        logger.warning("Failed to post to Slack response_url", error=str(e), exc_info=True)


async def post_to_slack(
    message: str | dict[str, Any] | None = None, webhook_url: str | None = None, blocks: list | None = None
) -> None:
    """
    Post a message to a Slack channel using a webhook URL.

    Args:
        message (str | dict): The message to post to Slack. If a dict is provided, it will be JSON serialized.
                             If JSON string, it will be formatted with proper markdown.
        webhook_url (str | None): Optional webhook URL. If not provided, uses the URL from environment variables.
        blocks: Optional blocks to post to Slack.
    """
    if os.environ.get("ENVIRONMENT") == "local":
        print(f"Skipping Slack posting in local environment: \n{message}")
        return

    url_to_use = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url_to_use:
        log_dict = {
            "message": "No webhook URL provided and SLACK_WEBHOOK_URL environment variable is not set",
        }
        logger.warning(json_dumps(log_dict))
        return

    if isinstance(message, dict):
        message = json_dumps(message)

    try:
        client = get_slack_client(url_to_use)
        response = await client.send(text=message, blocks=blocks)
        if response.status_code != 200:
            log_dict = {
                "message": f"Failed to post message to Slack. Status code: {response.status_code}",
            }
            logger.warning(json_dumps(log_dict))
    except Exception:
        logger.exception("Failed to post message to Slack", exc_info=True)


def post_to_slack_sync(
    message: str | dict[str, Any] | None = None,
    webhook_url: str | None = None,
) -> None:
    """Post a message to Slack using a webhook URL (synchronous).

    Mirrors post_to_slack() but uses the sync WebhookClient, suitable for
    cron jobs and other non-async contexts.
    """
    if os.environ.get("ENVIRONMENT") == "local":
        print(f"Skipping Slack posting in local environment: \n{message}")
        return

    url_to_use = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url_to_use:
        logger.warning("No webhook URL provided and SLACK_WEBHOOK_URL environment variable is not set")
        return

    if isinstance(message, dict):
        message = json_dumps(message)

    try:
        client = get_slack_sync_client(url_to_use)
        response = client.send(text=message)
        if response.status_code != 200:
            logger.warning(
                "Failed to post message to Slack (sync)",
                status_code=response.status_code,
                body=response.body,
            )
    except Exception:
        logger.exception("Failed to post message to Slack (sync)")


def create_slack_link(channel: str, thread_ts: str, main_thread_ts: str | None = None) -> str:
    """Create a Slack link to a thread.

    Args:
        channel: The Slack channel ID
        thread_ts: The timestamp of the thread to link to (the latest message in the thread)
        main_thread_ts: The timestamp of the main thread to link to (the first message in the thread)

    Returns:
        A Slack link to the thread
    """

    link = f"https://yuppai.slack.com/archives/{channel}/p{thread_ts.replace('.', '')}"
    if main_thread_ts:
        link += f"?thread_ts={main_thread_ts}"
    return link


@async_timed_cache(seconds=300)  # Cache for 5 minutes
async def get_channel_name_by_id(bot_token: str, channel_id: str) -> str | None:
    """Fetch channel name from Slack API.

    Cached for 5 minutes to avoid excessive API calls.

    Args:
        bot_token: Slack bot token
        channel_id: Slack channel ID

    Returns:
        Channel name or None if lookup fails
    """
    logger = get_logger()
    try:
        client = AsyncWebClient(token=bot_token)
        response = await client.conversations_info(channel=channel_id)
        channel_info: dict[str, Any] = response.get("channel", {})
        return channel_info.get("name")
    except Exception as e:
        logger.warning(
            "Failed to get channel info from Slack",
            channel_id=channel_id,
            error=str(e),
        )
        return None
