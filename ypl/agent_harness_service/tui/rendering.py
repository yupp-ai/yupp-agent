"""Event rendering helpers and label/message rendering for the chat log."""

from __future__ import annotations
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

# ---------------------------------------------------------------------------
# Tool icons
# ---------------------------------------------------------------------------

_TOOL_ICONS = {
    "command_execution": "\u26a1",
    "file_change": "\U0001f4dd",
    "mcp_tool_call": "\U0001f527",
}


def _render_tool_started(item: dict[str, Any]) -> str:
    item_type = item.get("type", "")
    icon = _TOOL_ICONS.get(item_type, "\U0001f527")

    if item_type == "command_execution":
        cmd = item.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:197] + "..."
        return f"  \u2504 {icon} Bash: {cmd}"

    if item_type == "file_change":
        changes = item.get("changes", [])
        if changes:
            kind = changes[0].get("kind", "update").capitalize()
            path = changes[0].get("path", "?")
            return f"  \u2504 {icon} {kind} {path}"
        return f"  \u2504 {icon} File change"

    tool = item.get("tool", "?")
    server = item.get("server", "")
    args = item.get("arguments", {})
    # Filter out session_id (always the same per session, not useful to display)
    display_args = {k: v for k, v in args.items() if k != "session_id"} if args else {}
    arg_summary = ""
    if display_args:
        parts = [f"{k}={' '.join(str(v).split())}" for k, v in display_args.items()]
        joined = ", ".join(parts)
        if len(joined) > 80:
            joined = joined[:77] + "..."
        arg_summary = f"({joined})"
    prefix = f"{server}.{tool}" if server and server != "ahs" else tool
    return f"  \u2504 {icon} {prefix}{arg_summary}"


def _render_tool_completed(item: dict[str, Any]) -> str:
    status = item.get("status", "completed")
    mark = "\u2713" if status == "completed" else "\u2717"
    base = _render_tool_started(item)
    suffix = f"  {mark}"
    if item.get("type") == "command_execution" and item.get("exit_code", 0) != 0:
        suffix = f"  \u2717 exit={item['exit_code']}"
    return f"{base}{suffix}"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _escape_markup(text: str) -> str:
    return text.replace("[", r"\[")


def _render_user_label() -> Panel:
    """Render the 'You' label in a box."""
    return Panel(Text("\u25b6 You", style="bold steel_blue1"), expand=False, border_style="steel_blue1", padding=(0, 1))


def _render_user_message(content: str) -> Text:
    """Render user message content in the same blue as the 'You' label."""
    return Text(content, style="steel_blue1")


def _render_assistant_label() -> Panel:
    """Render the 'Assistant' label in a box."""
    return Panel(Text("\u25c0 Assistant", style="bold green"), expand=False, border_style="green", padding=(0, 1))


def _render_assistant_markdown(content: str) -> Markdown:
    return Markdown(content)
