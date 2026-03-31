"""Event rendering helpers and label/message rendering for the chat log."""

from __future__ import annotations
from typing import Any

from rich.align import Align
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


def _render_user_label() -> Text:
    """Render an empty placeholder (user label is now inline with message)."""
    return Text("")


def _render_user_message(content: str) -> Text:
    """Render user message with a subtle background highlight, like Claude Code."""
    return Text(f" {content} ", style="on grey23")


def _render_assistant_label() -> Text:
    """No-op — assistant messages have no label, they just appear."""
    return Text("")


def _render_assistant_markdown(content: str) -> Markdown:
    return Markdown(content)


def _render_status_line(markup: str) -> Align:
    """Render a dim status/meta line right-aligned."""
    return Align(Text.from_markup(f"[dim]{markup}[/dim]"), align="right")


def _render_session_box(
    old_sid: str | None,
    new_sid: str,
    agent: str,
    *,
    title: str = "New session",
    msg_count: int | None = None,
) -> Panel:
    """Render a session-transition notice in a left-aligned bordered box.

    Displays the full session IDs (never truncated) along with the agent name
    and, optionally, the number of pre-existing messages in the session.
    """
    body = Text(overflow="fold", no_wrap=False)
    body.append("Previous: ", style="dim")
    body.append(str(old_sid) if old_sid else "None", style="dim italic")
    body.append("\n")
    body.append("New:      ", style="dim")
    body.append(new_sid, style="dim bold")
    body.append(f"  (agent: {agent})", style="dim")
    if msg_count is not None:
        label = f"{msg_count} previous message{'s' if msg_count != 1 else ''}" if msg_count else "No previous messages"
        body.append(f"\n{label}", style="dim italic")
    return Panel(
        body,
        title=f"[dim]{title}[/dim]",
        title_align="left",
        border_style="dim",
        expand=False,
        padding=(0, 1),
    )
