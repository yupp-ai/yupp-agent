"""Proactive Slack thread fetching for AHS sessions.

Fetches Slack thread content at session creation time so it can be injected
directly into the system prompt, eliminating the need for agents to call
read_slack_thread on their first turn.

This removes:
  - ToolSearch round-trip      (~0.63s)
  - MCP read_slack_thread call (~0.57–2.95s)
  - One extra LLM turn

Net saving: ~1.2–3.6s off first-message latency.
"""

import asyncio
import os
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from ypl.structured_logger import get_logger

logger = get_logger()

# Singleton bot client (lazy-initialised, same token as mcp_tools.py).
_slack_bot_client: AsyncWebClient | None = None


def _get_slack_bot_client() -> AsyncWebClient:
    """Return (or create) the singleton Slack bot client for prefetching."""
    global _slack_bot_client
    if _slack_bot_client is None:
        token = os.environ.get("SLACK_MCP_SERVER_APP_BOT_TOKEN")
        if not token:
            raise ValueError("SLACK_MCP_SERVER_APP_BOT_TOKEN is not set")
        _slack_bot_client = AsyncWebClient(
            token=token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=2)],
        )
    return _slack_bot_client


async def _resolve_user(client: AsyncWebClient, user_id: str) -> str:
    """Resolve a Slack user ID to a human-readable display name.

    Returns the raw user_id if resolution fails so callers always get a string.
    """
    try:
        response = await client.users_info(user=user_id)
        if response.get("ok") and response.get("user"):
            user = response["user"]
            profile = user.get("profile", {})
            return (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
    except SlackApiError as exc:
        logger.warning("Slack user resolution failed during prefetch", user_id=user_id, error=str(exc))
    return user_id


def _extract_attachment_text(attachments: list[dict[str, Any]]) -> str | None:
    """Extract readable text from Slack message attachments (email-forwarded messages, etc.)."""
    attachment_parts: list[str] = []
    for att in attachments:
        parts: list[str] = []
        if att.get("pretext"):
            parts.append(att["pretext"])
        if att.get("title"):
            parts.append(att["title"])
        if att.get("text"):
            parts.append(att["text"])
        # Only use fallback when no richer content was found
        if not parts and att.get("fallback"):
            parts.append(att["fallback"])
        for field in att.get("fields", []):
            title = field.get("title", "")
            value = field.get("value", "")
            if title and value:
                parts.append(f"{title}: {value}")
        if parts:
            attachment_parts.append("\n".join(parts))
    return "\n\n".join(attachment_parts) if attachment_parts else None


def _extract_block_text(blocks: list[dict[str, Any]]) -> str | None:
    """Extract readable text from Slack rich_text blocks.

    Slack bot and system messages often carry their payload only in ``blocks``
    while ``text`` is empty.  This extracts plain-text elements from
    ``rich_text`` block types so the prefetch doesn't silently drop content.
    """
    parts: list[str] = []
    for block in blocks:
        block_type = block.get("type", "")
        # rich_text blocks contain elements → sub-elements with text
        if block_type == "rich_text":
            for element in block.get("elements", []):
                for sub in element.get("elements", []):
                    if sub.get("type") == "text" and sub.get("text"):
                        parts.append(sub["text"])
                    elif sub.get("type") == "link" and sub.get("url"):
                        parts.append(sub.get("text") or sub["url"])
                    elif sub.get("type") == "user" and sub.get("user_id"):
                        parts.append(f"@{sub['user_id']}")
        # section and header blocks have a text field
        elif block_type in ("section", "header"):
            block_text_val = block.get("text", {}).get("text")
            if block_text_val:
                parts.append(block_text_val)
    return "\n".join(parts) if parts else None


def _format_messages(messages: list[dict[str, Any]], name_map: dict[str, str]) -> str:
    """Format raw Slack message dicts into a readable chat-log string.

    Mirrors the extraction logic in ``mcp_server/tools/slack.py`` so bot messages,
    email-forwarded messages, attachments, and rich-layout blocks are not silently
    dropped.
    """
    lines: list[str] = []
    for msg in messages:
        user_id = msg.get("user", "")
        # Bot and email-forwarded messages have no `user` field — fall back to `username`
        if user_id:
            name = name_map.get(user_id, user_id)
        else:
            name = msg.get("username") or "unknown"
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        line = f"[{name}] ({ts}): {text}"

        # Append attachment text (email bodies, etc.)
        raw_attachments: list[dict[str, Any]] = msg.get("attachments", [])
        if raw_attachments:
            att_text = _extract_attachment_text(raw_attachments)
            if att_text:
                line += f"\n  [attachments]: {att_text}"

        # Extract text from rich-layout blocks when `text` field is empty
        raw_blocks: list[dict[str, Any]] = msg.get("blocks", [])
        if raw_blocks and not text:
            block_text = _extract_block_text(raw_blocks)
            if block_text:
                line += f"\n  [blocks]: {block_text}"

        lines.append(line)
    return "\n".join(lines)


async def fetch_slack_thread_content(
    channel: str,
    thread_ts: str,
    limit: int = 100,
) -> str | None:
    """Fetch a Slack thread and return it as a formatted string.

    Args:
        channel:   Slack channel ID (e.g. ``C01ABCDEF``).
        thread_ts: Thread root timestamp (e.g. ``1234567890.123456``).
        limit:     Maximum number of messages to fetch.

    Returns:
        A formatted multi-line string of thread messages, or ``None`` if the
        fetch fails (network error, missing token, etc.).  Callers should fall
        back to the "please call read_slack_thread" instruction on ``None``.
    """
    try:
        client = _get_slack_bot_client()
        response = await client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )
    except Exception as exc:
        logger.warning(
            "Slack thread prefetch request failed",
            channel=channel,
            thread_ts=thread_ts,
            error=str(exc),
        )
        return None

    if not response.get("ok"):
        logger.warning(
            "Slack thread prefetch returned error",
            channel=channel,
            thread_ts=thread_ts,
            slack_error=response.get("error", "unknown"),
        )
        return None

    raw_messages: list[dict[str, Any]] = response.get("messages", [])
    has_more: bool = response.get("has_more", False)
    response_metadata: dict[str, Any] = response.get("response_metadata", {})
    next_cursor: str = response_metadata.get("next_cursor", "") if has_more else ""

    # Resolve all user IDs concurrently.
    # Convert set → list to guarantee stable ordering for zip().
    unique_ids_list = list({msg.get("user", "") for msg in raw_messages} - {""})
    resolved = await asyncio.gather(*(_resolve_user(client, uid) for uid in unique_ids_list), return_exceptions=True)
    name_map: dict[str, str] = {
        uid: (name if isinstance(name, str) else uid) for uid, name in zip(unique_ids_list, resolved, strict=True)
    }

    content = _format_messages(raw_messages, name_map)

    # Cap total size to avoid blowing up the system prompt token budget.
    max_chars = 15_000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n(Content truncated. Use `read_slack_thread` to fetch the full thread.)"

    if has_more:
        cursor_hint = f', cursor="{next_cursor}"' if next_cursor else ""
        content += (
            "\n\n(Thread has additional messages. "
            f'Use `read_slack_thread(channel="{channel}", thread_ts="{thread_ts}"{cursor_hint})` '
            "to fetch more.)"
        )

    logger.info(
        "Slack thread prefetched for session",
        channel=channel,
        thread_ts=thread_ts,
        message_count=len(raw_messages),
        has_more=has_more,
    )
    return content
