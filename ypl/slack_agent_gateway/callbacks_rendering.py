"""Slack Block Kit rendering for typed agent replies.

Extracted to avoid circular imports between callbacks.py and buffer.py.
"""

from typing import Any

# Reply types that render as muted context blocks (small gray text) in Slack.
_CONTEXT_BLOCK_REPLY_TYPES: frozenset[str] = frozenset({"thinking"})

# Slack context block mrkdwn elements have a max length of ~3000 characters.
_CONTEXT_BLOCK_MAX_TEXT_LENGTH = 3000


def render_reply_blocks(text: str, reply_type: str | None) -> list[dict[str, Any]] | None:
    """Build Slack Block Kit blocks for a reply based on its type.

    Args:
        text: The reply text content.
        reply_type: Content type hint (e.g. 'thinking'). None means regular text.

    Returns:
        List of Block Kit block dicts, or None for plain-text rendering.
    """
    if reply_type not in _CONTEXT_BLOCK_REPLY_TYPES:
        return None

    # Truncate to stay within Slack's context block text limit.
    if len(text) > _CONTEXT_BLOCK_MAX_TEXT_LENGTH:
        text = text[: _CONTEXT_BLOCK_MAX_TEXT_LENGTH - 3] + "..."

    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
