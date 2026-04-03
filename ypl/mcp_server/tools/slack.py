"""MCP tools for Slack integration.

Provides tools for reading Slack threads and searching Slack messages
with user ID resolution and caching.
"""

import asyncio
import os
from typing import Any

from cachetools import TTLCache
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from ypl.mcp_server.core import mcp_server
from ypl.structured_logger import get_logger

logger = get_logger()


# ============================================================================
# Singletons and Cache
# ============================================================================

# TTL cache for Slack user ID -> display name resolution (1 hour, max 2k entries).
# Only successful lookups are cached; transient failures are retried on next call.
_slack_user_cache: TTLCache[str, str] = TTLCache(maxsize=2000, ttl=3600)

# Singleton Slack clients (lazy-initialized) with rate-limit retry.
_slack_bot_client: AsyncWebClient | None = None
_slack_user_client: AsyncWebClient | None = None


# ============================================================================
# Helpers
# ============================================================================


def _get_slack_bot_client() -> AsyncWebClient:
    """Get or create the singleton Slack bot client."""
    global _slack_bot_client
    if _slack_bot_client is None:
        token = os.environ.get("SLACK_MCP_SERVER_APP_BOT_TOKEN")
        if not token:
            raise ValueError("SLACK_MCP_SERVER_APP_BOT_TOKEN environment variable is not set")
        _slack_bot_client = AsyncWebClient(
            token=token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=2)],
        )
    return _slack_bot_client


def _get_slack_user_client() -> AsyncWebClient:
    """Get or create the singleton Slack user-token client.

    The user token (xoxp-...) is required for search.messages, which does not
    support bot tokens.
    """
    global _slack_user_client
    if _slack_user_client is None:
        token = os.environ.get("SLACK_MCP_SERVER_APP_USER_TOKEN")
        if not token:
            raise ValueError("SLACK_MCP_SERVER_APP_USER_TOKEN environment variable is not set")
        _slack_user_client = AsyncWebClient(
            token=token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=2)],
        )
    return _slack_user_client


async def _resolve_slack_user(client: AsyncWebClient, user_id: str) -> str:
    """Resolve a Slack user ID to a display name, with caching.

    Only successful resolutions are cached. Transient failures (rate limits,
    network errors) return the raw user_id without caching so the next call
    can retry.
    """
    if user_id in _slack_user_cache:
        return str(_slack_user_cache[user_id])

    try:
        response = await client.users_info(user=user_id)
        if response.get("ok") and response.get("user"):
            user = response["user"]
            profile = user.get("profile", {})
            # display_name can be "" when unset — the or-chain relies on empty
            # string being falsy to fall through to real_name / name.
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            _slack_user_cache[user_id] = display_name
            return display_name
    except SlackApiError as e:
        logger.warning("Failed to resolve Slack user", user_id=user_id, error=str(e))

    # Don't cache failures — return raw ID so next call can retry
    return user_id


# ============================================================================
# MCP Tool Functions
# ============================================================================


@mcp_server.tool(
    name="read_slack_thread",
    description=(
        "Read messages from a Slack thread. Use this to get the full conversation context "
        "from a Slack thread for investigation or summarization. Requires a channel ID and "
        "thread timestamp (thread_ts). Resolves user IDs to display names. "
        "For long threads, use the returned `next_cursor` to paginate. "
        "Bot and email-forwarded messages are identified by their `username` field. "
        "Email content appears in `attachments_text`; rich layout content appears in `blocks`."
    ),
)
async def read_slack_thread(
    channel: str,
    thread_ts: str,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Read messages from a Slack thread.

    Args:
        channel: Slack channel ID (e.g., 'C01ABCDEF')
        thread_ts: Thread timestamp (e.g., '1234567890.123456')
        limit: Maximum number of messages to return (default: 100)
        cursor: Pagination cursor from a previous response's `next_cursor`

    Returns:
        Dictionary containing thread messages with resolved user names.
        Bot/email messages include `attachments_text` and/or `blocks` with their content.
    """
    try:
        client = _get_slack_bot_client()

        kwargs: dict[str, Any] = {
            "channel": channel,
            "ts": thread_ts,
            "limit": limit,
        }
        if cursor:
            kwargs["cursor"] = cursor

        response = await client.conversations_replies(**kwargs)

        if not response.get("ok"):
            return {
                "success": False,
                "error": response.get("error", "Unknown Slack API error"),
            }

        raw_messages: list[dict[str, Any]] = response.get("messages", [])

        # Resolve unique user IDs concurrently and build a lookup map.
        # Using a dict avoids serial retries inside the loop when a transient
        # API failure prevents caching during the gather phase.
        unique_user_ids = list({msg.get("user", "") for msg in raw_messages} - {""})
        resolved_names = await asyncio.gather(*(_resolve_slack_user(client, uid) for uid in unique_user_ids))
        user_map: dict[str, Any] = dict(zip(unique_user_ids, resolved_names, strict=True))

        messages = []
        for msg in raw_messages:
            user_id = msg.get("user", "")
            # Bot and email-forwarded messages have no `user` field — fall back to `username`
            if user_id:
                display_name = user_map.get(user_id, user_id)
            else:
                display_name = msg.get("username") or "unknown"

            entry: dict[str, Any] = {
                "user": display_name,
                "user_id": user_id,
                "text": msg.get("text", ""),
                "ts": msg.get("ts", ""),
                "thread_ts": msg.get("thread_ts", ""),
            }

            # Extract readable text from attachments (email-forwarded messages store
            # their body here instead of in `text`).
            raw_attachments: list[dict[str, Any]] = msg.get("attachments", [])
            if raw_attachments:
                attachment_parts: list[str] = []
                for att in raw_attachments:
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
                if attachment_parts:
                    entry["attachments_text"] = "\n\n".join(attachment_parts)

            # Include rich-layout blocks (present on newer Slack messages)
            if msg.get("blocks"):
                entry["blocks"] = msg["blocks"]

            messages.append(entry)

        logger.info(
            "Slack thread read",
            channel=channel,
            thread_ts=thread_ts,
            message_count=len(messages),
        )

        has_more = response.get("has_more", False)
        response_metadata: dict[str, Any] = response.get("response_metadata", {})
        next_cursor = response_metadata.get("next_cursor", "") if has_more else None

        result: dict[str, Any] = {
            "success": True,
            "channel": channel,
            "thread_ts": thread_ts,
            "message_count": len(messages),
            "has_more": has_more,
            "messages": messages,
        }
        if next_cursor:
            result["next_cursor"] = next_cursor

        return result

    except SlackApiError as e:
        slack_error = e.response.get("error", "unknown_slack_error")
        if slack_error == "not_in_channel":
            friendly = (
                f"Bot is not a member of channel {channel} — "
                "please add the Yupp MCP bot to the channel first "
                "(open the channel in Slack → Integrations → Add apps)."
            )
            logger.warning(
                "Slack bot not in channel",
                channel=channel,
                thread_ts=thread_ts,
                slack_error=slack_error,
            )
            return {"success": False, "error": friendly, "slack_error": slack_error}
        logger.warning(
            "Slack API error reading thread",
            error=slack_error,
            channel=channel,
            thread_ts=thread_ts,
        )
        return {"success": False, "error": str(e), "slack_error": slack_error}
    except Exception as e:
        logger.warning("Error reading Slack thread", error=str(e), channel=channel, thread_ts=thread_ts)
        return {"success": False, "error": str(e)}


@mcp_server.tool(
    name="search_slack",
    description=(
        "Search Slack messages across channels. Supports Slack search operators like "
        "'from:', 'in:', 'before:', 'after:', 'has:', etc. "
        "Use this to find relevant conversations, incidents, or discussions. "
        "NOTE: Requires the Slack app to have the search:read scope on a user token; "
        "if the bot token lacks this scope, the API will return a token-type error."
    ),
)
async def search_slack(
    query: str,
    max_results: int = 20,
) -> dict[str, Any]:
    """Search Slack messages.

    Args:
        query: Search query (supports Slack search operators like from:, in:, before:, after:)
        max_results: Maximum number of results to return (default: 20)

    Returns:
        Dictionary containing matching messages with channel, timestamp, and text.
    """
    try:
        client = _get_slack_user_client()

        response = await client.search_messages(
            query=query,
            count=max_results,
            sort="timestamp",
            sort_dir="desc",
        )

        if not response.get("ok"):
            return {
                "success": False,
                "error": response.get("error", "Unknown Slack API error"),
            }

        matches_data: dict[str, Any] = response.get("messages", {})
        raw_matches = matches_data.get("matches", [])
        total = matches_data.get("total", 0)

        results = []
        for match in raw_matches:
            channel_info = match.get("channel", {})
            results.append(
                {
                    "text": match.get("text", ""),
                    "username": match.get("username", ""),
                    "ts": match.get("ts", ""),
                    "channel_id": channel_info.get("id", ""),
                    "channel_name": channel_info.get("name", ""),
                    "permalink": match.get("permalink", ""),
                    "thread_ts": match.get("thread_ts"),
                }
            )

        logger.info(
            "Slack search completed",
            query=query,
            result_count=len(results),
            total_matches=total,
        )

        return {
            "success": True,
            "query": query,
            "total_matches": total,
            "result_count": len(results),
            "results": results,
        }

    except Exception as e:
        logger.warning("Error searching Slack", error=str(e), query=query)
        return {"success": False, "error": str(e)}
