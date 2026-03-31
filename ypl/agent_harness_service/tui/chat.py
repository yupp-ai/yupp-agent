"""AHSTui — main chat app and ChatInput widget."""

from __future__ import annotations
import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import aiohttp
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.events import Key
from textual.message import Message
from textual.widgets import Footer, Header, LoadingIndicator, RichLog, TextArea

from ypl.agent_harness_service.tui.agents import AgentsScreen
from ypl.agent_harness_service.tui.config import (
    _http_request,
    _save_session_id,
    _ws_url,
    get_http_base,
)
from ypl.agent_harness_service.tui.projects import ProjectsScreen
from ypl.agent_harness_service.tui.rendering import (
    _escape_markup,
    _format_tokens,
    _render_assistant_label,
    _render_assistant_markdown,
    _render_status_line,
    _render_tool_completed,
    _render_tool_started,
    _render_user_label,
    _render_user_message,
)
from ypl.agent_harness_service.tui.schedules import SchedulesScreen
from ypl.agent_harness_service.tui.search import SearchScreen
from ypl.agent_harness_service.tui.sessions import SessionsScreen

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show this help",
    "/stop": "Stop the current agent turn",
    "/clear": "Clear the chat log",
    "/history": "Reload message history",
    "/agents": "List available agents",
    "/new": "Start new session: /new [agent]",
    "/sessions": "List your sessions",
    "/allsessions": "List all sessions",
    "/attach": "Attach to session: /attach <id>",
    "/projects": "Browse projects and tasks",
    "/schedules": "Browse agent schedules",
    "/search": "Search everything",
    "/exit": "Quit the TUI",
}

HELP_TEXT = (
    "[bold white]Commands:[/bold white]\n"
    + "\n".join(f"  {cmd:<12}{desc}" for cmd, desc in SLASH_COMMANDS.items())
    + "\n\n[bold white]Keys:[/bold white]\n"
    + "  Enter     Send message\n"
    + "  Ctrl+J    New line\n"
    + "  Escape    Stop current turn\n"
    + "  Ctrl+O    Projects\n"
    + "  Ctrl+S    Sessions\n"
    + "  Ctrl+H    Schedules\n"
    + "  Ctrl+/    Search\n"
    + "  Ctrl+X    Quit\n"
)


# ---------------------------------------------------------------------------
# ChatInput widget
# ---------------------------------------------------------------------------


class ChatInput(TextArea):
    """Multi-line input: Enter to submit, Ctrl+J for newline."""

    BINDINGS = [
        Binding("ctrl+j", "newline", "New line", show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, placeholder: str = "", **kwargs: Any) -> None:
        super().__init__("", language=None, **kwargs)
        self._placeholder = placeholder

    def action_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = str(self.text).strip()  # type: ignore[has-type,unused-ignore]
            if text:
                self.post_message(self.Submitted(text))
                self.text = ""
            return
        await super()._on_key(event)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
#chat-log {
    height: 1fr;
    scrollbar-size: 1 1;
    padding-bottom: 2;
}
#chat-log:focus {
    border: none;
}
#thinking-indicator {
    height: 1;
    display: none;
    color: $text-muted;
    margin: 0 1;
}
#message-input {
    height: 3;
    min-height: 3;
    max-height: 7;
    border: round $surface-lighten-2;
    border-subtitle-align: right;
    border-subtitle-color: $text-muted;
}
"""


# ---------------------------------------------------------------------------
# AHSTui App
# ---------------------------------------------------------------------------


class AHSTui(App[None]):
    TITLE = "ahstui"
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+x", "quit", "Quit", show=True),
        Binding("escape", "stop_turn", "Stop", show=False),
        Binding("ctrl+a", "open_agents", "Agents", show=True),
        Binding("ctrl+o", "open_projects", "Projects", show=True),
        Binding("ctrl+s", "open_sessions", "Sessions", show=True),
        Binding("ctrl+h", "open_schedules", "Schedules", show=True),
        Binding("ctrl+slash", "open_search", "Search", show=True),
    ]

    def __init__(
        self,
        agent: str | None = None,
        session_id: str | None = None,
        initial_message: str | None = None,
        user_id: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._session_id = session_id
        self._initial_message = initial_message
        self._user_id = user_id
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._agent_name: str = agent or "?"
        self._connected = False
        self._delta_buffer: str = ""
        self._delta_line_count: int = 0
        self._thinking: bool = False
        self._pending_tool: bool = False
        self._slash_hints_shown: bool = False
        # Avoid repeating "◀ assistant:" header for consecutive assistant items
        self._showing_assistant: bool = False
        self._in_tool_cluster: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Container():
            log = RichLog(id="chat-log", markup=True, wrap=True, auto_scroll=True)
            log.can_focus = False
            yield log
        yield LoadingIndicator(id="thinking-indicator")
        inp = ChatInput(id="message-input")
        inp.border_subtitle = "Ctrl+J: new line"
        yield inp
        yield Footer()

    async def on_mount(self) -> None:
        self._http_session = aiohttp.ClientSession()
        log = self.query_one("#chat-log", RichLog)
        msg_count: int | None = None

        if self._session_id:
            pass
        elif self._agent:
            # --agent provided: create a new session immediately
            try:
                body: dict[str, Any] = {"agent_id": self._agent, "trigger": "api", "source": "tui"}
                if self._initial_message:
                    body["message"] = self._initial_message
                if self._user_id:
                    body["user_id"] = self._user_id
                resp = await _http_request("POST", "/session/create", body)
                self._session_id = resp.get("session_id", "")
                self._agent_name = self._agent
                _save_session_id(self._session_id)
            except Exception as e:
                log.write(f"[indian_red]ERROR creating session: {e}[/indian_red]")
                return
        else:
            # No --agent, no --session-id: open sessions screen to pick or create
            self.query_one("#message-input", ChatInput).focus()
            self._open_sessions_screen()
            return

        if not self._session_id:
            log.write("[indian_red]No session ID. Use --agent to create or --session-id to resume.[/indian_red]")
            return

        # Load history for resumed sessions
        if not self._initial_message or not self._agent:
            msg_count = await self._load_history()

        # Fetch agent detail for executor info
        executor_label = ""
        try:
            agent_resp = await _http_request("GET", f"/agent/{self._agent_name}")
            agent_info = agent_resp.get("agent", {})
            exec_type = agent_info.get("executor_type", "")
            exec_model = agent_info.get("executor_model") or agent_info.get("llm_model") or ""
            if exec_type:
                executor_label = f" | Executor [bold white]\\[{exec_type}] {exec_model}[/bold white]"
        except Exception:
            pass

        # Show clean session header (right-aligned)
        host_display = get_http_base()
        log.write(
            _render_status_line(
                f"Session [bold white]{self._session_id}[/bold white]"
                f" | Agent [bold white]{self._agent_name}[/bold white]"
                f" | Host [bold white]{host_display}[/bold white]"
                f"{executor_label}"
            )
        )
        if msg_count is not None:
            msg_label = f"{msg_count} previous messages" if msg_count else "No previous messages"
            log.write(_render_status_line(msg_label))
        log.write("")

        has_no_history = not self._initial_message and (msg_count is None or msg_count == 0)
        if has_no_history:
            log.write(_render_assistant_label())
            log.write("How can I help you?")

        if self._initial_message and self._agent:
            log.write("")
            log.write(_render_user_label())
            log.write(_render_user_message(self._initial_message))

        self._update_subtitle()
        self.query_one("#message-input", ChatInput).focus()
        self._start_ws_listener()

    def action_stop_turn(self) -> None:
        """ESC handler — send /stop to the current session."""
        if self._ws and not self._ws.closed:
            log = self.query_one("#chat-log", RichLog)
            log.write("[dim]Stopping current turn...[/dim]")
            asyncio.ensure_future(self._ws.send_json({"type": "stop"}))

    async def on_unmount(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # --- History loading ---

    async def _load_history(self) -> int:
        """Load message history. Returns message count."""
        log = self.query_one("#chat-log", RichLog)
        try:
            resp = await _http_request("GET", f"/session/{self._session_id}/history?limit=500&offset=0")
            self._agent_name = resp.get("agent_id", self._agent_name)
            messages = resp.get("messages", [])
            if not messages:
                return 0
            last_role = ""
            for msg in messages:
                role = (msg.get("role") or "?").lower()
                content = msg.get("content") or ""
                tool_uses = msg.get("tool_uses") or []
                # Normalize: API returns "AGENT", WS returns "assistant"
                if role in ("agent", "assistant"):
                    role = "assistant"

                if role == "user":
                    if content:
                        log.write("")
                        log.write(_render_user_label())
                        log.write(_render_user_message(content))
                    last_role = "user"
                elif role == "assistant":
                    if not content and not tool_uses:
                        continue
                    if last_role != "assistant":
                        log.write("")
                        log.write(_render_assistant_label())
                    if tool_uses:
                        n = len(tool_uses)
                        if n <= 10:
                            for tu in tool_uses:
                                name = tu.get("name", "?")
                                log.write(f"[dim]  \u2504 \U0001f527 {_escape_markup(name)}  \u2713[/dim]")
                        else:
                            # Show first 3 + last 3 + summary
                            for tu in tool_uses[:3]:
                                name = tu.get("name", "?")
                                log.write(f"[dim]  \u2504 \U0001f527 {_escape_markup(name)}  \u2713[/dim]")
                            log.write(f"[dim]  \u2504 ... {n - 6} more tool calls ...[/dim]")
                            for tu in tool_uses[-3:]:
                                name = tu.get("name", "?")
                                log.write(f"[dim]  \u2504 \U0001f527 {_escape_markup(name)}  \u2713[/dim]")
                    if content:
                        log.write(_render_assistant_markdown(content))
                    last_role = "assistant"
                elif role == "system":
                    if content:
                        log.write("")
                        log.write(f"[dim italic]System: {_escape_markup(content[:200])}[/dim italic]")
                    last_role = "system"
            log.write("")
            return len(messages)
        except Exception as e:
            log.write(f"[red]ERROR loading history: {e}[/red]")
            return 0

    # --- WebSocket listener ---

    @work(thread=False, exclusive=True)
    async def _start_ws_listener(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        url = _ws_url(self._session_id or "")
        retry_delay = 1.0

        while True:
            try:
                # Always create a fresh session for each connection attempt
                # to avoid stale connection pool issues after server restart.
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                if self._http_session and not self._http_session.closed:
                    await self._http_session.close()
                self._http_session = aiohttp.ClientSession()
                self._ws = await self._http_session.ws_connect(url, heartbeat=30)
                self._connected = True
                self._update_subtitle()
                retry_delay = 1.0

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                            self._handle_event(event, log)
                        except json.JSONDecodeError:
                            log.write("[dim]<invalid JSON>[/dim]")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.write(f"[indian_red]WebSocket error: {self._ws.exception()}[/indian_red]")
                        break

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.write(f"[indian_red]Connection error: {e}[/indian_red]")

            self._connected = False
            self._update_subtitle("\u25cb disconnected")
            log.write(_render_status_line(f"Reconnecting in {retry_delay:.0f}s..."))
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    def _set_thinking(self, on: bool) -> None:
        self._thinking = on
        indicator = self.query_one("#thinking-indicator", LoadingIndicator)
        indicator.display = on

    def _handle_event(self, event: dict[str, Any], log: RichLog) -> None:
        etype = event.get("type", "")

        if etype == "thread/started":
            pass  # session info already shown on mount

        elif etype == "turn/started":
            self._set_thinking(True)

        elif etype == "item/started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                self._set_thinking(False)
                self._flush_delta_buffer(log)
                # End tool cluster if one was active
                if self._in_tool_cluster:
                    log.write("")
                    self._in_tool_cluster = False
                if not self._showing_assistant:
                    log.write("")
                    log.write(_render_assistant_label())
                    self._showing_assistant = True
            elif item_type in ("command_execution", "file_change", "mcp_tool_call"):
                self._flush_delta_buffer(log)
                if not self._showing_assistant:
                    log.write("")
                    log.write(_render_assistant_label())
                    self._showing_assistant = True
                # Start a new tool cluster with a line break before
                if not self._in_tool_cluster:
                    log.write("")
                    self._in_tool_cluster = True
                log.write(f"[dim]{_escape_markup(_render_tool_started(item))}  ...[/dim]")

        elif etype == "item/agentMessage/delta":
            self._set_thinking(False)
            delta = event.get("delta", "")
            if delta:
                self._delta_buffer += delta

        elif etype == "item/completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                if self._in_tool_cluster:
                    log.write("")
                    self._in_tool_cluster = False
                full_text = item.get("text", "") or self._delta_buffer
                self._flush_delta_buffer(log)
                if full_text:
                    log.write(_render_assistant_markdown(full_text))
            elif item_type in ("command_execution", "file_change", "mcp_tool_call"):
                log.write(f"[dim]{_escape_markup(_render_tool_completed(item))}[/dim]")

        elif etype == "turn/completed":
            self._set_thinking(False)
            self._showing_assistant = False
            self._in_tool_cluster = False
            self._flush_delta_buffer(log)
            status = event.get("status", "completed")
            if status == "failed":
                error = event.get("error", {})
                err_msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                fail_msg = f"\u2500\u2500 turn FAILED: {_escape_markup(err_msg)} \u2500\u2500"
                log.write(f"\n[indian_red]{fail_msg}[/indian_red]")
            else:
                usage = event.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cost = usage.get("cost_usd")
                duration = usage.get("duration_ms")
                parts = [f"{_format_tokens(in_tok)} in", f"{_format_tokens(out_tok)} out"]
                if cost is not None:
                    parts.append(f"${cost:.3f}")
                if duration is not None:
                    parts.append(f"{duration / 1000:.1f}s")
                summary = " / ".join(parts)
                log.write(f"\n[dim]\u2500\u2500\u2500 {summary} \u2500\u2500\u2500[/dim]")

        elif etype == "error":
            error_msg = event.get("message", str(event))
            log.write(f"[indian_red]Error: {_escape_markup(error_msg)}[/indian_red]")

        elif etype == "stop_ack":
            self._set_thinking(False)
            status = event.get("status", "")
            if status == "stopped":
                log.write("[dim]✓ Turn stopped.[/dim]")
            elif status == "no_inflight_turn":
                log.write("[dim]No active turn to stop.[/dim]")
            else:
                log.write(f"[dim]Stop result: {status}[/dim]")

        elif etype in ("heartbeat", "pong"):
            pass

    # --- Subtitle / helpers ---

    def _update_subtitle(self, extra: str = "") -> None:
        sid = (self._session_id or "")[:8]
        host_url = get_http_base()
        parsed = urlparse(host_url)
        hostname = parsed.hostname or ""
        is_local = hostname in ("localhost", "127.0.0.1", "0.0.0.0")
        display_host = host_url if is_local else hostname
        parts = [display_host, f"{sid}...", self._agent_name]
        if self._connected:
            parts.append("\u25cf")
        if extra:
            parts.append(extra)
        self.sub_title = " \u2502 ".join(parts)

    def _flush_delta_buffer(self, log: RichLog) -> None:
        self._delta_buffer = ""
        self._delta_line_count = 0

    # --- Input handling ---

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Auto-resize input and show slash command hints once."""
        ta = event.text_area
        # Auto-resize: grow with content, respect min/max from CSS
        line_count = ta.document.line_count
        new_height = max(3, min(7, line_count + 2))  # +2 for border, max 5 text rows
        ta.styles.height = new_height

        value = ta.text
        if value != "/":
            self._slash_hints_shown = False
            return
        if self._slash_hints_shown:
            return
        self._slash_hints_shown = True
        log = self.query_one("#chat-log", RichLog)
        hints = "  ".join(f"[bold white]{cmd}[/bold white]" for cmd in SLASH_COMMANDS)
        log.write(f"\n[dim italic]{hints}[/dim italic]\n")

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value
        log = self.query_one("#chat-log", RichLog)

        # Reset input height after submit
        inp = self.query_one("#message-input", ChatInput)
        inp.styles.height = 3

        # Handle commands — with or without leading /
        cmd_text = text if text.startswith("/") else None
        if text.lower() in ("exit", "quit"):
            cmd_text = "/exit"
        if cmd_text:
            await self._handle_command(cmd_text, log)
            return

        # Send user message — resets assistant header tracking
        self._showing_assistant = False
        log.write("")
        log.write(_render_user_label())
        log.write(_render_user_message(text))
        log.write("")

        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_json(
                    {"type": "user_message", "content": text, "user_id": self._user_id, "source": "tui"}
                )
            except Exception as e:
                log.write(f"[indian_red]Send error: {e}[/indian_red]")
        else:
            try:
                body: dict[str, Any] = {"session_id": self._session_id, "message": text, "source": "tui"}
                if self._user_id:
                    body["user_id"] = self._user_id
                await _http_request("POST", "/session/message", body)
            except Exception as e:
                log.write(f"[indian_red]Send error: {e}[/indian_red]")

    async def _handle_command(self, text: str, log: RichLog) -> None:
        cmd = text.lower().split()[0]

        if cmd == "/exit":
            self.exit()
            return

        if cmd == "/stop":
            log.write("[dim]Stopping current turn...[/dim]")
            if self._ws and not self._ws.closed:
                try:
                    await self._ws.send_json({"type": "stop"})
                except Exception as e:
                    log.write(f"[indian_red]Stop error: {e}[/indian_red]")

        elif cmd == "/clear":
            log.clear()

        elif cmd == "/history":
            log.clear()
            log.write(f"[dim]Loading history for session {self._session_id}...[/dim]")
            count = await self._load_history()
            log.write(f"[dim]{count} messages loaded[/dim]" if count else "[dim]No messages in this session[/dim]")

        elif cmd == "/help":
            log.write(HELP_TEXT)

        elif cmd == "/agents":
            await self._cmd_agents(log)

        elif cmd == "/new":
            parts = text.split(maxsplit=1)
            agent = parts[1].strip() if len(parts) > 1 else self._agent_name
            await self._cmd_new(agent, log)

        elif cmd in ("/sessions", "/allsessions"):
            self._open_sessions_screen()

        elif cmd == "/attach":
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                log.write("[dim]Usage: /attach <session-id>[/dim]")
                return
            await self._cmd_attach(parts[1].strip(), log)

        elif cmd == "/projects":
            await self._cmd_projects(log)

        elif cmd == "/schedules":
            self._open_schedules_screen()

        elif cmd == "/search":
            self._open_search_screen()

        else:
            log.write(f"[dim]Unknown command: {cmd}. Type /help for commands.[/dim]")

    async def _cmd_agents(self, log: RichLog) -> None:
        try:
            resp = await _http_request("GET", "/agents?include_all=true")
            agents = resp.get("agents", [])
            if not agents:
                log.write("[dim]No agents found.[/dim]")
                return
            log.write("")
            header = (
                f"{'NAME':<20} {'TYPE':<10} {'MODEL':<25} {'TOOLS':<8} {'SUBAGENTS':<10} {'TURNS':<6} {'BUDGET':<7}"
            )
            log.write(f"[bold white]{header}[/bold white]")
            log.write(f"[dim]{'-' * 90}[/dim]")
            for a in agents:
                model = a.get("executor_model") or a.get("llm_model") or "-"
                exec_type = a.get("executor_type", "-")
                # Summarize tool permissions
                perms = a.get("tool_permissions", {})
                if perms.get("*") == "allow":
                    tools_str = "all"
                elif perms.get("*") == "deny":
                    allowed = [k for k, v in perms.items() if k != "*" and v == "allow"]
                    tools_str = str(len(allowed)) if allowed else "none"
                else:
                    tools_str = "-"
                # Summarize allowed subagents
                subs = a.get("allowed_subagents", [])
                if subs == ["*"]:
                    subs_str = "all"
                elif subs:
                    subs_str = str(len(subs))
                else:
                    subs_str = "none"
                line = (
                    f"{a['name'][:20]:<20} "
                    f"{exec_type[:10]:<10} "
                    f"{model[:25]:<25} "
                    f"{tools_str:<8} "
                    f"{subs_str:<10} "
                    f"{str(a.get('max_turns', '-')):<6} "
                    f"${a.get('max_budget_usd', 0):.1f}"
                )
                log.write(f"[dim]{line}[/dim]")
            log.write("")
        except Exception as e:
            log.write(f"[indian_red]ERROR: {e}[/indian_red]")

    async def _cmd_new(self, agent: str, log: RichLog) -> None:
        """Start a new session with the given agent."""
        try:
            body: dict[str, Any] = {"agent_id": agent, "trigger": "api", "source": "tui"}
            if self._user_id:
                body["user_id"] = self._user_id
            resp = await _http_request("POST", "/session/create", body)
            new_sid = resp.get("session_id", "")
            if not new_sid:
                log.write("[indian_red]ERROR: No session_id in response[/indian_red]")
                return

            old_sid = self._session_id
            log.write("")
            log.write(_render_status_line("\u2500\u2500\u2500 New session \u2500\u2500\u2500"))
            log.write(_render_status_line(f"Previous: {old_sid}"))
            log.write(_render_status_line(f"New: {new_sid} (agent: {agent})"))

            # Switch to new session
            self._session_id = new_sid
            self._agent_name = agent
            _save_session_id(new_sid)
            self._showing_assistant = False
            self._in_tool_cluster = False
            self._delta_buffer = ""

            # Reconnect WS
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._update_subtitle()
            self._start_ws_listener()

            log.write("")
            log.write(_render_assistant_label())
            log.write("How can I help you?")
        except Exception as e:
            log.write(f"[indian_red]ERROR creating session: {e}[/indian_red]")

    async def _cmd_attach(self, target_sid: str, log: RichLog) -> None:
        """Stop current session and attach to another one."""
        old_sid = self._session_id
        log.write(f"[dim]Current session: {old_sid}[/dim]")

        # Validate target session exists before mutating state
        try:
            resp = await _http_request("GET", f"/session/{target_sid}")
            session_info = resp.get("session", {})
        except Exception as e:
            log.write(f"[indian_red]ERROR: session {target_sid} not found or unreachable: {e}[/indian_red]")
            return

        # Stop current session
        if old_sid and self._ws and not self._ws.closed:
            try:
                await self._ws.send_json({"type": "stop"})
            except Exception:
                pass

        # Close existing WS
        if self._ws and not self._ws.closed:
            await self._ws.close()

        # Switch to new session
        self._session_id = target_sid
        _save_session_id(target_sid)
        self._showing_assistant = False
        self._in_tool_cluster = False
        self._delta_buffer = ""
        self._agent_name = session_info.get("agent_name", "?")

        # Load history of the new session
        log.clear()
        msg_count = await self._load_history()

        log.write(
            _render_status_line(
                f"Attached to session [bold white]{target_sid}[/bold white]"
                f" | Agent [bold white]{self._agent_name}[/bold white]"
            )
        )
        if msg_count is not None:
            msg_label = f"{msg_count} previous messages" if msg_count else "No previous messages"
            log.write(_render_status_line(msg_label))
        log.write("")

        self._update_subtitle()
        self._start_ws_listener()

    async def _cmd_projects(self, log: RichLog) -> None:
        """Open the projects screen."""
        self._open_projects_screen()

    def action_open_agents(self) -> None:
        """Ctrl+A handler to open agents screen."""
        self._open_agents_screen()

    @work(thread=False)
    async def _open_agents_screen(self) -> None:
        await self.push_screen_wait(AgentsScreen())

    def action_open_projects(self) -> None:
        """Ctrl+O handler to open projects screen."""
        self._open_projects_screen()

    @work(thread=False)
    async def _open_projects_screen(self) -> None:
        result = await self.push_screen_wait(ProjectsScreen())
        if result:
            log = self.query_one("#chat-log", RichLog)
            await self._cmd_attach(result, log)

    def action_open_sessions(self) -> None:
        """Ctrl+S handler to open sessions screen."""
        self._open_sessions_screen()

    def action_open_schedules(self) -> None:
        """Ctrl+H handler to open schedules screen."""
        self._open_schedules_screen()

    def action_open_search(self) -> None:
        """Ctrl+/ handler to open search screen."""
        self._open_search_screen()

    @work(thread=False)
    async def _open_schedules_screen(self) -> None:
        result = await self.push_screen_wait(SchedulesScreen(user_id=self._user_id))
        if result:
            log = self.query_one("#chat-log", RichLog)
            await self._cmd_attach(result, log)

    @work(thread=False)
    async def _open_search_screen(self) -> None:
        if not hasattr(self, "_search_screen") or self._search_screen is None:
            self._search_screen: SearchScreen = SearchScreen(user_id=self._user_id)
        result = await self.push_screen_wait(self._search_screen)
        if result:
            log = self.query_one("#chat-log", RichLog)
            await self._cmd_attach(result, log)

    @work(thread=False)
    async def _open_sessions_screen(self) -> None:
        result = await self.push_screen_wait(SessionsScreen(user_id=self._user_id))
        if not result:
            # User dismissed without selecting — show prompt if no session active
            if not self._session_id:
                log = self.query_one("#chat-log", RichLog)
                log.write(
                    "[dim]No session selected. Use /sessions or Ctrl+S to browse, or /new <agent> to create.[/dim]"
                )
            return
        log = self.query_one("#chat-log", RichLog)
        if result.startswith("new:"):
            # Create a new session with the chosen agent
            agent = result[4:]
            await self._cmd_new(agent, log)
        else:
            # Attach to existing session
            await self._cmd_attach(result, log)
