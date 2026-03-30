"""Data sanitization, content extraction, and tool-use helpers."""

import re
from typing import Any, NamedTuple

from ypl.agent_harness_service.common.types import ToolUseItem
from ypl.agent_harness_service.executors.runner import StreamEvent
from ypl.agent_harness_service.service.state import (
    _OUTLET_TOOL_NAMES,
    _TOOL_OUTPUT_MAX_CHARS,
    _TRIM_KEYS,
    _TRIM_MAX_LEN,
    _TRIM_SUFFIX,
)


def _trim_value(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _trim_field(k, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim_value(item) for item in obj]
    return obj


def _trim_field(key: str, value: Any) -> Any:
    if key in _TRIM_KEYS and isinstance(value, str) and len(value) > _TRIM_MAX_LEN:
        return value[:_TRIM_MAX_LEN] + _TRIM_SUFFIX
    return _trim_value(value)


def _scrub_null_bytes(obj: Any) -> Any:
    """Recursively strip PostgreSQL-illegal null bytes (\\x00) from strings.

    asyncpg raises ``UntranslatableCharacterError`` when a VARCHAR column
    receives a string that contains ``\\u0000`` / ``\\x00``.  LLM output can
    occasionally contain these bytes, so we sanitise before every DB write.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _scrub_null_bytes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_null_bytes(item) for item in obj]
    return obj


# Regex to strip <thinking>...</thinking> or <think>...</think> tags (case-insensitive, multiline).
# Some models (e.g., Minimax) emit reasoning tokens wrapped in these tags that shouldn't be shown to users.
# Uses alternation to ensure matching open/close tags: <think> with </think>, <thinking> with </thinking>.
# Note: Nested thinking tags are not expected from current models; the non-greedy .*? handles the common case.
_THINKING_TAG_PATTERN = re.compile(r"<thinking>(.*?)</thinking>|<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> and <think>...</think> tags from text.

    Some models emit internal reasoning wrapped in thinking tags. These should
    be filtered out before displaying to users (e.g., in Slack).

    Note: This operates on complete API responses (not streaming chunks), so
    tags won't be split across calls. The raw executor uses non-streaming API
    calls that return complete responses per step.
    """
    return _THINKING_TAG_PATTERN.sub("", text).strip()


class VisibleContent(NamedTuple):
    """User-visible text extracted from a stream event."""

    text: str
    # True if already delivered to an outlet (e.g., send_slack_message posted
    # to Slack directly via MCP tool). When True, gateway relay is skipped to
    # avoid duplicate delivery, but the text is still persisted in DB content.
    # TODO: This flag is set at tool_use time (intent), not after tool_result
    # confirms success.  If the MCP tool fails, content won't reach Slack but
    # gateway relay is still skipped.  Consider deferring until tool_result or
    # adding a fallback relay on tool failure.  (PR #11130)
    already_delivered: bool


def _extract_visible_content(
    event: StreamEvent,
    seen_outlet_tool_ids: set[str],
) -> list[VisibleContent]:
    """Single decision point for all user-visible content in a stream event.

    All text returned here is persisted in DB content and shown in the
    Streamlit console.  Text NOT marked ``already_delivered`` is also relayed
    to connected outlets (Slack gateway).

    Sources of user-visible content:
    - Agent text blocks (assistant events with text output)
    - Tool calls that post to outlets (e.g., send_slack_message)

    To add a new outlet tool, extend ``_extract_outlet_tool_content``.

    Args:
        event: The stream event to inspect.
        seen_outlet_tool_ids: Mutable set tracking tool-call IDs already
            extracted, so the same call is not counted twice when the CLI
            runner emits both a ``tool_use`` and an ``assistant`` event.
    """
    results: list[VisibleContent] = []

    # 1. Text blocks from assistant events
    if event.type == "assistant" and event.text:
        cleaned = strip_thinking_tags(event.text)
        if cleaned:
            results.append(VisibleContent(text=cleaned, already_delivered=False))

    # 2. Tool calls that deliver content directly to outlets
    _extract_outlet_tool_content(event, results, seen_outlet_tool_ids)

    return results


def _extract_outlet_tool_content(
    event: StreamEvent,
    out: list[VisibleContent],
    seen_tool_ids: set[str],
) -> None:
    """Extract text from tool calls that post to user-visible outlets.

    Currently handles:
    - ``send_slack_message``: text posted directly to Slack by the MCP tool.

    To support a new outlet tool, add extraction logic here and add the tool
    name to ``_OUTLET_TOOL_NAMES``.

    Args:
        event: The stream event to inspect.
        out: Accumulator for visible content items.
        seen_tool_ids: Set of tool-call IDs already processed; used to
            deduplicate when the same tool call appears in both a ``tool_use``
            event and the ``assistant`` event's content blocks.
    """
    tool_blocks: list[dict] = []
    if event.type == "tool_use":
        tool_blocks = [event.raw]
    elif event.type == "assistant":
        tool_blocks = [b for b in event.raw.get("message", {}).get("content", []) if b.get("type") == "tool_use"]

    for block in tool_blocks:
        tool_id: str = block.get("id", "")
        if tool_id and tool_id in seen_tool_ids:
            continue

        name: str = block.get("name", "")
        if name in _OUTLET_TOOL_NAMES:
            text: str | None = block.get("input", {}).get("text")
            if text:
                out.append(VisibleContent(text=text, already_delivered=True))
                if tool_id:
                    seen_tool_ids.add(tool_id)


def _truncate_tool_output(output: Any) -> str | None:
    """Truncate tool output to a safe length for API responses."""
    if output is None:
        return None
    s = str(output)
    if len(s) <= _TOOL_OUTPUT_MAX_CHARS:
        return s
    return s[:_TOOL_OUTPUT_MAX_CHARS] + f"... ({len(s)} chars)"


def _message_content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized message content blocks from a raw event."""
    message = event.get("message", {})
    if not isinstance(message, dict):
        return []
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _iter_tool_use_blocks(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract tool_use blocks in the same order the WS translator consumes them."""
    tool_uses: list[dict[str, Any]] = []

    for event in raw_events:
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_uses.append(event)
            continue
        if event_type != "assistant":
            continue

        tool_uses.extend(block for block in _message_content_blocks(event) if block.get("type") == "tool_use")

    return tool_uses


def _iter_tool_result_blocks(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract tool_result blocks from standalone and embedded user events."""
    tool_results: list[dict[str, Any]] = []

    for event in raw_events:
        event_type = event.get("type")
        if event_type == "tool_result":
            tool_results.append(event)
            continue
        if event_type != "user":
            continue

        tool_results.extend(block for block in _message_content_blocks(event) if block.get("type") == "tool_result")

    return tool_results


def _extract_tool_use_id(payload: dict[str, Any]) -> str:
    """Normalize the tool call identifier across runner-specific event shapes."""
    tool_use_id = payload.get("tool_use_id") or payload.get("id") or ""
    return str(tool_use_id)


def _extract_tool_name(payload: dict[str, Any]) -> str:
    """Normalize the tool name across runner-specific event shapes."""
    name = payload.get("name") or payload.get("tool_name") or ""
    return str(name)


def _extract_tool_input(payload: dict[str, Any]) -> Any | None:
    """Normalize the tool input across runner-specific event shapes."""
    if "input" in payload:
        return payload.get("input")
    if "tool_input" in payload:
        return payload.get("tool_input")
    return None


def _extract_tool_output(payload: dict[str, Any]) -> str | None:
    """Normalize tool outputs to the same textual shape WS clients receive."""
    output = payload.get("output", payload.get("content"))
    if isinstance(output, list):
        output = " ".join(
            str(block.get("text", "")) for block in output if isinstance(block, dict) and block.get("text") is not None
        )
    return _truncate_tool_output(output)


def _extract_tool_uses(raw_events: list[dict[str, Any]] | None) -> list[ToolUseItem] | None:
    """Extract tool use/result pairs from raw_events into structured ToolUseItems.

    Supports the same tool-call shapes that WebSocket streaming translates:
    standalone tool_use/tool_result events plus assistant/user content blocks.
    Returns None if no tool uses found (keeps response compact for non-tool messages).
    """
    if not raw_events:
        return None

    results_by_id: dict[str, dict[str, Any]] = {}
    tool_uses: list[ToolUseItem] = []

    for event in _iter_tool_result_blocks(raw_events):
        tool_use_id = _extract_tool_use_id(event)
        if tool_use_id:
            results_by_id[tool_use_id] = event

    for event in _iter_tool_use_blocks(raw_events):
        tool_use_id = _extract_tool_use_id(event)
        result_event = results_by_id.get(tool_use_id, {})
        tool_uses.append(
            ToolUseItem(
                tool_use_id=tool_use_id,
                name=_extract_tool_name(event),
                input=_extract_tool_input(event),
                output=_extract_tool_output(result_event),
                is_error=bool(result_event.get("is_error", False)),
                duration_ms=result_event.get("duration_ms"),
                step=event.get("step", result_event.get("step")),
            )
        )

    return tool_uses or None
