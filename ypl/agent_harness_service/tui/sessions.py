"""Sessions screen — browse and attach to agent sessions."""

from __future__ import annotations
import time
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, OptionList, RichLog, Static

try:
    from textual.widgets.option_list import Option
except ImportError:
    from textual.widgets._option_list import Option

from ypl.agent_harness_service.tui.config import _http_request
from ypl.agent_harness_service.tui.rendering import _escape_markup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRIGGER_OPTIONS = ["(all)", "slack", "webhook", "cron", "api"]

# TODO: Extract cache helpers and time formatting functions into tui/utils.py
# to share with projects.py and avoid duplication.

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def _cache_invalidate(prefix: str) -> None:
    to_delete = [k for k in _cache if k.startswith(prefix)]
    for k in to_delete:
        del _cache[k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ACTIVE": ("\u25cf", "green"),  # ●
    "COMPLETED": ("\u2713", "bright_green"),  # ✓
    "STALE": ("\u25cb", "dim"),  # ○
}

TRIGGER_STYLE: dict[str, str] = {
    "slack": "cyan",
    "cron": "yellow",
    "api": "green",
    "webhook": "magenta",
}


def _fmt_timestamp(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_str))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_str)[:16]


def _fmt_local(dt_str: str | None) -> str:
    """Format as mm-dd hh:mm in local time."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local_dt = dt.astimezone()
        return local_dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(dt_str)[:11]


def _time_ago(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = datetime.now(tz=UTC) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        return f"{days // 30}mo ago"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Screen CSS
# ---------------------------------------------------------------------------

SESSIONS_SCREEN_CSS = """
#sessions-container {
    height: 1fr;
}
#sessions-filter-bar {
    height: auto;
    max-height: 3;
    padding: 0 1;
    background: $surface-darken-1;
    color: $text-muted;
}
#session-list {
    height: 1fr;
    border-bottom: solid $surface-lighten-2;
}
#session-detail-pane {
    height: 30%;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#session-detail-pane:focus {
    border: none;
}
#trigger-picker-overlay {
    display: none;
    dock: bottom;
    height: auto;
    max-height: 10;
    background: $surface;
    border: solid $accent;
    padding: 0 1;
}
"""


# ---------------------------------------------------------------------------
# SessionsScreen
# ---------------------------------------------------------------------------


class SessionsScreen(Screen[str | None]):
    """Two-pane screen for browsing and attaching to sessions."""

    CSS = SESSIONS_SCREEN_CSS
    BINDINGS = [
        Binding("escape", "pop_screen", "Back", show=True),
        Binding("enter", "attach_session", "Attach", show=True),
        Binding("t", "trigger_filter", "Filter trigger", show=True),
        Binding("R", "refresh_all", "Refresh", show=True, key_display="Shift+R"),
        Binding("tab", "focus_next_pane", "Next Pane", show=True),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False),
    ]

    def __init__(self, user_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id
        self._sessions: list[dict[str, Any]] = []
        self._selected_session: dict[str, Any] | None = None
        self._trigger_filter: str = "(all)"
        self._pane_order = ["session-list", "session-detail-pane"]
        self._current_pane_idx = 0
        self._picking_agent: bool = False  # True when agent picker overlay is visible

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sessions-container"):
            yield Static("", id="sessions-filter-bar")
            session_table: DataTable = DataTable(id="session-list", cursor_type="row")
            session_table.can_focus = True
            yield session_table
            detail = RichLog(id="session-detail-pane", markup=True, wrap=True, auto_scroll=False)
            detail.can_focus = True
            yield detail
        ol = OptionList(id="trigger-picker-overlay")
        ol.can_focus = True
        yield ol
        yield Footer()

    async def on_mount(self) -> None:
        st = self.query_one("#session-list", DataTable)
        st.add_columns("Status", "Trigger", "Agent", "Title", "Msgs", "Created", "Ago")
        st.focus()
        self._update_filter_bar()
        self._load_sessions()

    def _update_filter_bar(self) -> None:
        bar = self.query_one("#sessions-filter-bar", Static)
        parts = ["[bold]Sessions[/bold]"]
        if self._user_id:
            parts.append(f"user: {self._user_id[:8]}...")
        if self._trigger_filter != "(all)":
            parts.append(f"trigger: [bold]{self._trigger_filter}[/bold]")
        parts.append(f"{len(self._sessions)} sessions")
        parts.append("[dim]t[/dim]=filter trigger  [dim]R[/dim]=refresh  [dim]Enter[/dim]=attach  [dim]Esc[/dim]=back")
        bar.update(" | ".join(parts))

    @work(thread=False)
    async def _load_sessions(self, force: bool = False) -> None:
        cache_key = f"sessions:{self._user_id}:{self._trigger_filter}"
        cached = None if force else _cache_get(cache_key)
        if cached is not None:
            self._sessions = cached
        else:
            detail = self.query_one("#session-detail-pane", RichLog)
            detail.clear()
            detail.write("[dim]Loading sessions...[/dim]")
            try:
                params = "limit=100&root_sessions_only=true"
                if self._user_id:
                    params += f"&user_id={self._user_id}"
                if self._trigger_filter != "(all)":
                    params += f"&trigger={self._trigger_filter}"
                resp = await _http_request("GET", f"/sessions?{params}")
                self._sessions = resp.get("sessions", [])
                _cache_set(cache_key, self._sessions)
            except Exception as e:
                detail = self.query_one("#session-detail-pane", RichLog)
                detail.clear()
                detail.write(f"[indian_red]Error loading sessions: {e}[/indian_red]")
                return

        self._render_session_list()

    def _render_session_list(self) -> None:
        st = self.query_one("#session-list", DataTable)
        st.clear()
        detail = self.query_one("#session-detail-pane", RichLog)
        detail.clear()

        # Always show "New Session" as first row
        st.add_row(
            Text("+ NEW", style="bold bright_cyan"),
            Text("", style=""),
            Text("", style=""),
            Text("Create new session...", style="bold bright_cyan"),
            "",
            "",
            "",
            key="__new__",
        )

        if not self._sessions:
            detail.write("[dim]No other sessions found. Select 'New Session' to create one.[/dim]")
            self._update_filter_bar()
            return

        for sess in self._sessions:
            status = sess.get("status", "")
            sym, color = STATUS_STYLE.get(status, (" ", ""))
            trigger = sess.get("trigger", "")
            trigger_color = TRIGGER_STYLE.get(trigger, "")
            agent = sess.get("agent_name") or ""
            title = sess.get("title") or ""
            if not title:
                title = sess.get("session_id", "")[:12] + "..."
            msg_count = sess.get("message_count", 0)
            created_local = _fmt_local(sess.get("created_at"))
            created_ago = _time_ago(sess.get("created_at"))

            st.add_row(
                Text(f"{sym} {status}", style=color),
                Text(trigger, style=trigger_color) if trigger_color else trigger,
                agent,
                title[:50],
                str(msg_count),
                created_local,
                created_ago,
                key=sess.get("session_id", ""),
            )

        self._update_filter_bar()

        # Auto-select first
        if self._sessions:
            self._selected_session = self._sessions[0]
            self._show_session_detail(self._sessions[0])

    def _show_session_detail(self, sess: dict[str, Any]) -> None:
        detail = self.query_one("#session-detail-pane", RichLog)
        detail.clear()

        sid = sess.get("session_id", "?")
        status = sess.get("status", "?")
        sym, color = STATUS_STYLE.get(status, (" ", ""))
        trigger = sess.get("trigger", "?")
        agent = sess.get("agent_name") or "-"
        title = sess.get("title") or ""
        model = sess.get("model") or ""
        msg_count = sess.get("message_count", 0)
        created = sess.get("created_at") or ""
        parent = sess.get("parent_session_id") or ""
        slack_channel = sess.get("slack_channel_name") or ""

        # Title
        if title:
            detail.write(f"[bold]{_escape_markup(title)}[/bold]")
        detail.write(f"[{color}]{sym} {status}[/{color}]   Trigger: {trigger}   Agent: {_escape_markup(agent)}")

        # Links
        console_url = f"http://lit.yupp.ai/agent_harness_console?session_id={sid}"
        detail.write(f"Session: [link={console_url}]{sid[:12]}...[/link]")

        # Metadata
        meta: list[str] = []
        if model:
            meta.append(f"model: {model}")
        meta.append(f"{msg_count} messages")
        if created:
            meta.append(f"created {_fmt_timestamp(str(created))} ({_time_ago(str(created))})")
        if meta:
            detail.write(f"[dim]{' | '.join(meta)}[/dim]")

        if slack_channel:
            detail.write(f"[dim]Slack: #{_escape_markup(slack_channel)}[/dim]")
        if parent:
            detail.write(f"[dim]Parent: {parent[:12]}...[/dim]")

    # --- DataTable cursor events ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        key = event.row_key.value
        if key == "__new__":
            self._selected_session = None
            detail = self.query_one("#session-detail-pane", RichLog)
            detail.clear()
            detail.write("[bold bright_cyan]Create a new session[/bold bright_cyan]")
            detail.write("Press [bold]Enter[/bold] to choose an agent")
            return
        sess = next((s for s in self._sessions if s.get("session_id") == key), None)
        if sess:
            self._selected_session = sess
            self._show_session_detail(sess)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter key on a row — attach to session or show agent picker."""
        if event.row_key is None or event.row_key.value is None:
            return
        if event.row_key.value == "__new__":
            self._show_agent_picker()
            return
        self.dismiss(event.row_key.value)

    # --- Actions ---

    def action_pop_screen(self) -> None:
        """ESC — always dismiss immediately back to chat."""
        self.dismiss(None)

    def action_attach_session(self) -> None:
        if self._selected_session:
            self.dismiss(self._selected_session.get("session_id"))

    def action_focus_next_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx + 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_focus_prev_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx - 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_refresh_all(self) -> None:
        _cache_invalidate("sessions:")
        self._load_sessions(force=True)

    def action_trigger_filter(self) -> None:
        """Show trigger filter picker."""
        self._picking_agent = False
        picker = self.query_one("#trigger-picker-overlay", OptionList)
        picker.clear_options()
        for t in TRIGGER_OPTIONS:
            picker.add_option(Option(t, id=t))
        picker.display = True
        picker.focus()

    def _show_agent_picker(self) -> None:
        """Show agent picker overlay, sorted by frequency in sessions."""
        self._picking_agent = True
        # Count agent frequency from loaded sessions
        agent_counts: dict[str, int] = {}
        for sess in self._sessions:
            name = sess.get("agent_name") or ""
            if name:
                agent_counts[name] = agent_counts.get(name, 0) + 1

        # Also fetch all agents from API (cached)
        self._load_agents_for_picker(agent_counts)

    @work(thread=False)
    async def _load_agents_for_picker(self, agent_counts: dict[str, int]) -> None:
        try:
            resp = await _http_request("GET", "/agents?include_all=true")
            agents = resp.get("agents", [])
        except Exception as e:
            agents = []
            detail = self.query_one("#session-detail-pane", RichLog)
            detail.write(f"[indian_red]Error loading agents: {e}[/indian_red]")

        # Build sorted list: by frequency desc, then alphabetically
        agent_names: list[tuple[str, str, int]] = []
        for a in agents:
            name = a.get("name", "")
            display = a.get("display_name") or name
            count = agent_counts.get(name, 0)
            agent_names.append((name, display, count))
        agent_names.sort(key=lambda x: (-x[2], x[0]))

        picker = self.query_one("#trigger-picker-overlay", OptionList)
        picker.clear_options()
        for name, display, count in agent_names:
            label = f"{display} ({name})"
            if count > 0:
                label += f"  [{count} sessions]"
            picker.add_option(Option(label, id=name))
        picker.display = True
        picker.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        picker = self.query_one("#trigger-picker-overlay", OptionList)
        picker.display = False
        selected = event.option.id
        if not selected:
            self.query_one("#session-list").focus()
            return

        if self._picking_agent:
            # Dismiss with "new:agent_name" to signal new session creation
            self._picking_agent = False
            self.dismiss(f"new:{selected}")
        else:
            # Trigger filter selection
            self._trigger_filter = selected
            _cache_invalidate("sessions:")
            self._load_sessions(force=True)
            self.query_one("#session-list").focus()
