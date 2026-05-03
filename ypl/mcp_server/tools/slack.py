"""MCP tools for Slack integration.

Provides tools for reading Slack threads and searching Slack messages
with user ID resolution and caching. All reads use OpsBot (the shared
workspace reader app); see :mod:`ypl.slack_common.ops_bot` for the policy.
"""

import asyncio
from typing import Any

from slack_sdk.errors import SlackApiError

from ypl.mcp_common.shared_tool import shared_tool
from ypl.slack_common import get_ops_bot_user_client, resolve_display_name
from ypl.structured_logger import get_logger

logger = get_logger()


# ============================================================================
# MCP Tool Functions
# ============================================================================


@shared_tool(
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
        # Reads go through OpsBot's user token so we don't need to invite a bot
        # to every channel. ``resolve_display_name`` uses OpsBot's bot token
        # (``users:read``) internally; see :mod:`ypl.slack_common.ops_bot`.
        read_client = get_ops_bot_user_client()

        kwargs: dict[str, Any] = {
            "channel": channel,
            "ts": thread_ts,
            "limit": limit,
        }
        if cursor:
            kwargs["cursor"] = cursor

        response = await read_client.conversations_replies(**kwargs)

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
        resolved_names = await asyncio.gather(*(resolve_display_name(uid) for uid in unique_user_ids))
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
            # Reads use the user token, so this means the installing user
            # (not the bot) isn't in the channel — usually a private channel.
            friendly = (
                f"Channel {channel} is not accessible with the current reader — "
                "add the reader user to the private channel, or verify the channel ID."
            )
            logger.warning(
                "Slack reader not in channel",
                channel=channel,
                thread_ts=thread_ts,
                slack_error=slack_error,
            )
            return {"success": False, "error": friendly, "slack_error": slack_error}
        if slack_error == "channel_not_found":
            return {
                "success": False,
                "error": f"Channel '{channel}' was not found — verify the channel ID is correct.",
                "slack_error": slack_error,
            }
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


@shared_tool(
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
        client = get_ops_bot_user_client()

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
