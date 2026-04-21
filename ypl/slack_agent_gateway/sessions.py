"""Session management for Slack Agent Gateway.

Handles session lifecycle:
- Create new sessions
- Get session info
- Update session activity
- Session expiration (soft expiration model)
- File attachment processing (download from Slack, upload to the blob store)
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ypl.backend.utils.blob_store import BlobStore, get_blob_store
from ypl.slack_agent_gateway.constants import DEFAULT_SESSION_EXPIRATION_SECONDS
from ypl.slack_agent_gateway.redis_client import (
    get_session,
    save_session,
    update_session_activity,
    update_session_reply,
)
from ypl.slack_agent_gateway.types import (
    AgentSession,
    Attachment,
    Message,
    MessageSender,
    SessionStatus,
    SlackSessionInfoResponse,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# Blob-store path prefix for file attachments. Resolves to
# /data/ahs/attachments/... on local storage or gs://{bucket}/attachments/...
# on GCS, depending on BLOB_STORE_ENGINE.
_BLOB_ATTACHMENT_PREFIX = "attachments"

# Max file size we'll download from Slack (20 MB)
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

# Regex for sanitizing filenames: keep alphanumeric, dash, underscore, dot
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


async def create_session(
    channel_id: str,
    thread_ts: str,
    app_id: str,
    agent_name: str,
    creator_slack_user_id: str,
    creator_slack_username: str | None = None,
    channel_name: str | None = None,
) -> AgentSession:
    """Create a new agent session.

    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (normalized)
        app_id: Slack app ID
        agent_name: Internal agent name
        creator_slack_user_id: Slack user ID who created the session
        creator_slack_username: Optional username of creator
        channel_name: Optional human-readable channel name

    Returns:
        The created AgentSession
    """
    session_id = AgentSession.build_session_id(channel_id, thread_ts, app_id)
    now = datetime.now(UTC)

    session = AgentSession(
        session_id=session_id,
        channel_id=channel_id,
        channel_name=channel_name,
        thread_ts=thread_ts,
        creator_slack_user_id=creator_slack_user_id,
        creator_slack_username=creator_slack_username,
        app_id=app_id,
        agent_name=agent_name,
        status=SessionStatus.ACTIVE,
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(seconds=DEFAULT_SESSION_EXPIRATION_SECONDS),
    )

    await save_session(session)
    logger.info(
        "Created new session",
        session_id=session_id,
        agent_name=agent_name,
        channel_id=channel_id,
    )

    return session


async def get_or_create_session(
    channel_id: str,
    thread_ts: str,
    app_id: str,
    agent_name: str,
    user_id: str,
    username: str | None = None,
    channel_name: str | None = None,
) -> tuple[AgentSession, bool]:
    """Get existing session or create a new one.

    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (normalized)
        app_id: Slack app ID
        agent_name: Internal agent name
        user_id: Slack user ID sending the message
        username: Optional username
        channel_name: Optional channel name

    Returns:
        Tuple of (session, is_new) where is_new is True if a new session was created
    """
    session_id = AgentSession.build_session_id(channel_id, thread_ts, app_id)

    # Try to get existing session
    session = await get_session(session_id)

    if session:
        was_expired = session.status == SessionStatus.EXPIRED
        # Session exists - update activity and reactivate if expired
        session = await update_session_activity(session_id)
        if session:
            logger.info(
                "Reusing existing session",
                session_id=session_id,
                was_expired=was_expired,
            )
            return session, False

    # Create new session
    session = await create_session(
        channel_id=channel_id,
        thread_ts=thread_ts,
        app_id=app_id,
        agent_name=agent_name,
        creator_slack_user_id=user_id,
        creator_slack_username=username,
        channel_name=channel_name,
    )
    return session, True


async def get_session_info(session_id: str) -> SlackSessionInfoResponse | None:
    """Get session info for Agent Service.

    Args:
        session_id: The session ID

    Returns:
        SlackSessionInfoResponse if session exists, None otherwise
    """
    session = await get_session(session_id)
    if not session:
        return None

    return SlackSessionInfoResponse(
        session_id=session.session_id,
        channel_id=session.channel_id,
        channel_name=session.channel_name,
        thread_ts=session.thread_ts,
        creator_slack_user_id=session.creator_slack_user_id,
        creator_slack_username=session.creator_slack_username,
        agent_name=session.agent_name,
        status=session.status,
        created_at=session.created_at,
        last_activity_at=session.last_activity_at,
    )


async def record_reply(
    session_id: str,
    reply_ts: str,
    reply_content: str,
    reply_type: str | None = None,
) -> AgentSession | None:
    """Record a reply sent to Slack.

    Updates the session with the latest reply info.

    Args:
        session_id: The session ID
        reply_ts: Slack timestamp of the reply
        reply_content: Content of the reply
        reply_type: Content type hint (e.g. 'thinking')

    Returns:
        Updated AgentSession if found, None otherwise
    """
    return await update_session_reply(session_id, reply_ts, reply_content, reply_type=reply_type)


def _sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitize a filename for GCS storage.

    Replaces non-alphanumeric chars (except -, _, .) with underscore,
    truncates to max_length, and preserves the file extension.
    """
    sanitized = _SAFE_FILENAME_RE.sub("_", name)
    if len(sanitized) > max_length:
        # Preserve extension when truncating, unless the extension itself is too long
        dot_idx = sanitized.rfind(".")
        if dot_idx > 0:
            ext = sanitized[dot_idx:]
            if len(ext) < max_length:
                sanitized = sanitized[: max_length - len(ext)] + ext
            else:
                sanitized = sanitized[:max_length]
        else:
            sanitized = sanitized[:max_length]
    return sanitized


_SLACK_OWNED_SUFFIXES = (".slack.com", ".slack-edge.com", ".slack-msgs.com")


def _is_slack_host(url: str) -> bool:
    """Return True if the URL host is a Slack-owned domain."""
    hostname = urlparse(url).hostname or ""
    return any(hostname == suffix.lstrip(".") or hostname.endswith(suffix) for suffix in _SLACK_OWNED_SUFFIXES)


async def _download_slack_file(url: str, bot_token: str) -> bytes:
    """Download a file from Slack using the bot token for auth.

    httpx drops the Authorization header on cross-domain redirects by default
    (standard security behaviour). Slack's url_private_download can redirect to
    a CDN, so we follow redirects manually. The bot token is only forwarded to
    Slack-owned domains; CDN redirect URLs are pre-signed and must not receive
    our token.

    Args:
        url: Slack's url_private_download URL.
        bot_token: Bot token for Authorization header.

    Returns:
        Raw file bytes.

    Raises:
        ValueError: If the file exceeds the size limit, the redirect chain is
            too long, or a redirect is missing its Location header.
        httpx.HTTPStatusError: If the download fails.
    """
    headers: dict[str, str] = {"Authorization": f"Bearer {bot_token}"}
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for _ in range(10):
            response = await client.get(url, headers=headers)
            if not response.is_redirect:
                break
            location = response.headers.get("location", "")
            if not location:
                raise ValueError("Redirect response missing Location header")
            # Resolve relative redirects against the current request URL.
            url = urljoin(str(response.url), location)
            # Only forward the bot token to Slack-owned hosts. CDN redirect
            # URLs are pre-signed via query parameters and must not receive
            # our credential (forward-secrecy / least-privilege principle).
            headers = {"Authorization": f"Bearer {bot_token}"} if _is_slack_host(url) else {}
        else:
            raise ValueError("Too many redirects downloading Slack file")

        response.raise_for_status()
        data = response.content
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise ValueError(f"File too large ({len(data)} bytes, max {_MAX_ATTACHMENT_BYTES})")
        return data


async def process_slack_attachments(
    files: list[dict[str, Any]],
    session_id: str,
    bot_token: str,
    blob_store: BlobStore | None = None,
) -> list[Attachment]:
    """Download files from Slack and upload to the configured blob store.

    For each file in the Slack event:
    1. Download from Slack using url_private_download.
    2. Sanitize the filename.
    3. Upload to ``attachments/{session_id}/{filename}`` — resolves to
       ``/data/ahs/attachments/...`` on local storage or
       ``gs://{GCS_BUCKET_NAME}/attachments/...`` on GCS, depending on
       ``settings.BLOB_STORE_ENGINE``.
    4. Return :class:`Attachment` metadata with the logical ``blob_path``.

    Args:
        files: List of Slack file dicts from the event payload.
        session_id: Session ID for path scoping.
        bot_token: Slack bot token for downloading files.
        blob_store: Optional override for the blob store (tests).

    Returns:
        List of Attachment objects for successfully processed files.
    """
    attachments: list[Attachment] = []
    used_names: set[str] = set()
    store = blob_store or get_blob_store()

    for file_info in files:
        download_url = file_info.get("url_private_download")
        if not download_url:
            logger.warning("Slack file missing url_private_download, skipping", file_id=file_info.get("id"))
            continue

        original_name = file_info.get("name", "unknown")
        content_type = file_info.get("mimetype", "application/octet-stream")
        file_size = file_info.get("size", 0)

        if file_size > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Slack file too large, skipping",
                filename=original_name,
                size=file_size,
                max_size=_MAX_ATTACHMENT_BYTES,
            )
            continue

        sanitized_name = _sanitize_filename(original_name)

        # Deduplicate: append _1, _2, etc. if the name was already used.
        if sanitized_name in used_names:
            dot_idx = sanitized_name.rfind(".")
            if dot_idx > 0:
                stem, ext = sanitized_name[:dot_idx], sanitized_name[dot_idx:]
            else:
                stem, ext = sanitized_name, ""
            counter = 1
            while f"{stem}_{counter}{ext}" in used_names:
                counter += 1
            sanitized_name = f"{stem}_{counter}{ext}"
        used_names.add(sanitized_name)

        blob_path = f"{_BLOB_ATTACHMENT_PREFIX}/{session_id}/{sanitized_name}"

        try:
            data = await _download_slack_file(download_url, bot_token)
            await store.upload(blob_path, data, content_type=content_type)

            attachments.append(
                Attachment(
                    filename=sanitized_name,
                    content_type=content_type,
                    size=len(data),
                    blob_path=blob_path,
                )
            )
            logger.info(
                "Uploaded Slack attachment to blob store",
                filename=sanitized_name,
                size=len(data),
                blob_path=blob_path,
                session_id=session_id,
            )
        except ValueError as e:
            logger.warning(
                "Skipping Slack attachment due to download error",
                filename=original_name,
                content_type=content_type,
                size=file_info.get("size"),
                error=str(e),
            )
        except Exception:
            logger.error(
                "Failed to process Slack attachment",
                filename=original_name,
                session_id=session_id,
                exc_info=True,
            )

    return attachments


def build_message_from_event(event: dict[str, Any], attachments: list[Attachment] | None = None) -> Message:
    """Build a Message from a Slack event.

    Args:
        event: The Slack event payload
        attachments: Optional list of processed file attachments

    Returns:
        Message object ready to send to Agent Service
    """
    return Message(
        text=event.get("text", ""),
        sender=MessageSender(
            slack_user_id=event.get("user", ""),
            username=None,  # Would need to fetch from Slack API
            display_name=None,
        ),
        ts=event.get("ts", ""),
        attachments=attachments or [],
    )
