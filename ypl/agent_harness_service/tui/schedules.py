"""Schedules screen — two-panel view for browsing and managing agent schedules."""

from __future__ import annotations
import time
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, OptionList, RichLog, Static

try:
    from textual.widgets.option_list import Option
except ImportError:
    from textual.widgets._option_list import Option  # type: ignore[no-redef,unused-ignore]

from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.agent_harness_service.tui.config import _http_request
from ypl.agent_harness_service.tui.rendering import _escape_markup

# ---------------------------------------------------------------------------
# TTL cache for API responses
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
# Status helpers
# ---------------------------------------------------------------------------

SCHEDULE_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "PENDING": ("\u25cf", "cyan"),  # ●
    "IN_PROGRESS": ("\u25cf", "green"),  # ●
    "COMPLETED": ("\u2713", "bright_green"),  # ✓
    "FAILED": ("!", "bold red"),
    "CANCELLED": ("\u25cb", "dim"),  # ○
    "PAUSED": ("\u23f8", "yellow"),  # ⏸
}

# Sort priority for display (lower = shown first / more prominent)
_STATUS_SORT: dict[str, int] = {
    "IN_PROGRESS": 0,
    "PENDING": 1,
    "PAUSED": 2,
    "FAILED": 3,
    "COMPLETED": 4,
    "CANCELLED": 5,
}

# Run status display
_RUN_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "COMPLETED": ("\u2713", "bright_green"),
    "FAILED": ("!", "bold red"),
    "IN_PROGRESS": ("\u25cf", "green"),
    "PENDING": ("\u25cb", "dim"),
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _fmt_local(dt: datetime | str | None) -> str:
    """Format a datetime as mm-dd hh:mm in local time."""
    if dt is None:
        return ""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local_dt = dt.astimezone()
        return local_dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(dt)[:11]


def _time_ago(dt: datetime | str | None) -> str:
    """Return a human-readable 'X ago' string."""
    if dt is None:
        return ""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
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


def _time_until(dt: datetime | str | None) -> str:
    """Return 'in Xh Ym' for a future datetime, or 'X ago' if in the past."""
    if dt is None:
        return ""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = dt - datetime.now(tz=UTC)
        secs = int(delta.total_seconds())
        if secs < 0:
            return _time_ago(dt)
        if secs < 60:
            return f"in {secs}s"
        mins = secs // 60
        if mins < 60:
            return f"in {mins}m"
        hours = mins // 60
        rem_mins = mins % 60
        if hours < 24:
            return f"in {hours}h {rem_mins}m" if rem_mins else f"in {hours}h"
        days = hours // 24
        rem_hours = hours % 24
        return f"in {days}d {rem_hours}h" if rem_hours else f"in {days}d"
    except Exception:
        return ""


def _duration_str(started: str | None, completed: str | None) -> str:
    """Return a human-readable duration between two ISO timestamps."""
    if not started or not completed:
        return ""
    try:
        s = datetime.fromisoformat(started)
        c = datetime.fromisoformat(completed)
        secs = int((c - s).total_seconds())
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m {secs % 60}s"
        hours = mins // 60
        return f"{hours}h {mins % 60}m"
    except Exception:
        return ""


def _sort_schedules(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by status priority, then by relevant date."""

    def _key(s: dict[str, Any]) -> tuple[int, str]:
        priority = _STATUS_SORT.get(s.get("status", ""), 99)
        dt = s.get("next_run_at") or s.get("execute_at") or s.get("created_at")
        return (priority, str(dt) if dt else "")

    return sorted(schedules, key=_key)


# ---------------------------------------------------------------------------
# Screen CSS
# ---------------------------------------------------------------------------

SCHEDULES_SCREEN_CSS = """
#schedules-container {
    height: 1fr;
}
#schedules-filter-bar {
    height: auto;
    max-height: 3;
    padding: 0 1;
    background: $surface-darken-1;
    color: $text-muted;
}
#schedules-list-panel {
    height: 1fr;
}
#schedules-list {
    height: 1fr;
}
#detail-panel {
    height: 2fr;
}
#schedule-detail-pane {
    width: 1fr;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#schedule-detail-pane:focus {
    border: none;
}
#schedule-runs-pane {
    width: 1fr;
    border-left: solid $surface-lighten-2;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#schedule-runs-pane:focus {
    border-left: solid $surface-lighten-2;
}
#confirm-overlay {
    display: none;
    dock: bottom;
    height: auto;
    max-height: 6;
    background: $surface;
    border: solid $accent;
    padding: 0 1;
}
"""


# ---------------------------------------------------------------------------
# SchedulesScreen
# ---------------------------------------------------------------------------


class SchedulesScreen(Screen[str | None]):
    """Two-panel screen for browsing and managing agent schedules.

    Layout:
        Filter bar
        [Unified schedule list — recurring section then one-time section]
        [Detail pane (left)]  |  [Past runs pane (right)]

    Top:bottom ratio is 1:2.
    Tab cycles: List → Detail → Runs → List.
    ESC always dismisses back to chat.
    """

    CSS = SCHEDULES_SCREEN_CSS
    BINDINGS = [
        Binding("escape", "pop_screen", "Back", show=True),
        Binding("o", "open_latest_session", "Open session", show=True),
        Binding("r", "run_now", "Run now", show=True),
        Binding("x", "cancel_schedule", "Cancel", show=True),
        Binding("m", "toggle_mine", "My schedules", show=True),
        Binding("R", "refresh_all", "Refresh", show=True, key_display="Shift+R"),
        Binding("tab", "focus_next_pane", "Next Pane", show=True),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False),
    ]

    def __init__(self, user_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id
        self._recurring: list[dict[str, Any]] = []
        self._one_time: list[dict[str, Any]] = []
        self._all_schedules_ordered: list[dict[str, Any]] = []
        self._selected_schedule: dict[str, Any] | None = None
        self._my_schedules: bool = True
        self._confirm_pending: str | None = None  # schedule_id awaiting cancel confirmation
        self._current_runs: list[dict[str, Any]] = []  # runs for the selected schedule
        self._pane_order = ["schedules-list", "schedule-detail-pane", "schedule-runs-pane"]
        self._current_pane_idx = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="schedules-container"):
            yield Static("", id="schedules-filter-bar")
            with Vertical(id="schedules-list-panel"):
                sl: DataTable[str] = DataTable(id="schedules-list", cursor_type="row")
                sl.can_focus = True
                yield sl
            with Horizontal(id="detail-panel"):
                detail = RichLog(id="schedule-detail-pane", markup=True, wrap=True, auto_scroll=False)
                detail.can_focus = True
                yield detail
                runs = RichLog(id="schedule-runs-pane", markup=True, wrap=True, auto_scroll=False)
                runs.can_focus = True
                yield runs
        ol = OptionList(id="confirm-overlay")
        ol.can_focus = True
        yield ol
        yield Footer()

    async def on_mount(self) -> None:
        sl = self.query_one("#schedules-list", DataTable)
        sl.add_columns("", "St", "Name                                        ", "Agent", "Next / Execute", "Cron", "#")
        sl.focus()
        self._update_filter_bar()
        self._load_schedules()

    def _update_filter_bar(self) -> None:
        bar = self.query_one("#schedules-filter-bar", Static)
        mine_label = "[bold]mine[/bold]" if self._my_schedules else "all"
        parts = [
            f"[bold]Schedules[/bold]  {mine_label}",
            f"{len(self._recurring)} recurring / {len(self._one_time)} one-time",
            "[dim]o[/dim]=open [dim]m[/dim]=mine [dim]r[/dim]=run [dim]x[/dim]=cancel"
            " [dim]R[/dim]=refresh [dim]Esc[/dim]=back",
        ]
        bar.update(" | ".join(parts))

    @work(thread=False)
    async def _load_schedules(self, force: bool = False) -> None:
        cache_key = f"schedules:{self._user_id}:{self._my_schedules}"
        cached = None if force else _cache_get(cache_key)
        detail = self.query_one("#schedule-detail-pane", RichLog)

        if cached is not None:
            all_schedules: list[dict[str, Any]] = cached
        else:
            detail.clear()
            detail.write("[dim]Loading schedules...[/dim]")
            try:
                params = "limit=100"
                if self._my_schedules and self._user_id:
                    params += f"&created_by={self._user_id}"
                resp = await _http_request("GET", f"/schedules?{params}")
                all_schedules = resp.get("schedules", [])
                _cache_set(cache_key, all_schedules)
            except Exception as e:
                detail.clear()
                detail.write(f"[indian_red]Error loading schedules: {e}[/indian_red]")
                return

        self._recurring = _sort_schedules([s for s in all_schedules if s.get("schedule_type") == "RECURRING"])
        self._one_time = _sort_schedules([s for s in all_schedules if s.get("schedule_type") == "SCHEDULED"])
        self._render_list()

    def _render_list(self) -> None:
        sl = self.query_one("#schedules-list", DataTable)
        sl.clear()
        self._all_schedules_ordered = []

        # --- Recurring section ---
        if self._recurring:
            sl.add_row(
                Text("\u2500 Recurring", style="bold cyan"),
                "",
                "",
                "",
                "",
                "",
                "",
                key="__section_recurring__",
            )
            for sched in self._recurring:
                sym, color = SCHEDULE_STATUS_STYLE.get(sched.get("status", ""), ("?", ""))
                name = (sched.get("name") or sched.get("agent_schedule_id", "?")[:12])[:40]
                agent = (sched.get("agent_name") or "")[:12]
                next_run = _time_until(sched.get("next_run_at"))
                cron = (sched.get("cron_expression") or "")[:14]
                count = str(sched.get("run_count", 0))
                sl.add_row(
                    "",
                    Text(sym, style=color),
                    name,
                    agent,
                    next_run,
                    cron,
                    count,
                    key=sched.get("agent_schedule_id", ""),
                )
                self._all_schedules_ordered.append(sched)

        # --- One-time section ---
        if self._one_time:
            sl.add_row(
                Text("\u2500 One-time", style="bold yellow"),
                "",
                "",
                "",
                "",
                "",
                "",
                key="__section_onetime__",
            )
            for sched in self._one_time:
                sym, color = SCHEDULE_STATUS_STYLE.get(sched.get("status", ""), ("?", ""))
                name = (sched.get("name") or sched.get("agent_schedule_id", "?")[:12])[:40]
                agent = (sched.get("agent_name") or "")[:12]
                exec_at = _fmt_local(sched.get("execute_at")) or _fmt_local(sched.get("last_run_at"))
                sl.add_row(
                    "",
                    Text(sym, style=color),
                    name,
                    agent,
                    exec_at,
                    "",
                    "",
                    key=sched.get("agent_schedule_id", ""),
                )
                self._all_schedules_ordered.append(sched)

        self._update_filter_bar()

        # Auto-select first item
        if self._all_schedules_ordered:
            self._selected_schedule = self._all_schedules_ordered[0]
            self._show_detail(self._all_schedules_ordered[0])
            self._load_runs(self._all_schedules_ordered[0])
        else:
            self._selected_schedule = None
            self._current_runs = []
            self._confirm_pending = None
            detail = self.query_one("#schedule-detail-pane", RichLog)
            detail.clear()
            detail.write("[dim]No schedules found.[/dim]")
            runs_pane = self.query_one("#schedule-runs-pane", RichLog)
            runs_pane.clear()

    def _show_detail(self, sched: dict[str, Any]) -> None:
        detail = self.query_one("#schedule-detail-pane", RichLog)
        detail.clear()

        sid = sched.get("agent_schedule_id", "")
        status = sched.get("status", "?")
        sym, color = SCHEDULE_STATUS_STYLE.get(status, ("?", ""))
        stype = sched.get("schedule_type", "?")
        agent = sched.get("agent_name") or "-"
        name = sched.get("name") or sid[:12]
        desc = sched.get("description") or ""
        cron = sched.get("cron_expression") or ""
        tz = sched.get("cron_timezone") or "UTC"
        next_run = sched.get("next_run_at")
        last_run = sched.get("last_run_at")
        execute_at = sched.get("execute_at")
        run_count = sched.get("run_count", 0)
        max_runs = sched.get("max_runs")
        created_at = sched.get("created_at")
        created_by = sched.get("created_by_user") or sched.get("created_by_agent") or "?"
        message = sched.get("message") or ""

        detail.write("[bold]Schedule Details[/bold]")
        detail.write("")
        detail.write(f"[bold]{_escape_markup(name)}[/bold]")
        if desc:
            detail.write(f"[dim]{_escape_markup(desc)}[/dim]")

        is_mine = self._is_mine(sched)
        owner_label = "[bold white]you[/bold white]" if is_mine else _escape_markup(str(created_by)[:16])
        detail.write(
            f"[{color}]{sym} {status}[/{color}]   Type: {stype}   Agent: {_escape_markup(agent)}   Owner: {owner_label}"
        )

        if cron:
            detail.write(f"Cron: [bold]{_escape_markup(cron)}[/bold]   TZ: {_escape_markup(tz)}")
        if execute_at:
            detail.write(f"Execute at: {_fmt_local(execute_at)}")
        if next_run:
            detail.write(f"Next run: {_time_until(next_run)}")
        if last_run:
            detail.write(f"Last run: {_time_ago(last_run)}")

        runs_str = f"{run_count} / {max_runs}" if max_runs else str(run_count)
        detail.write(f"Runs: {runs_str}")

        if created_at:
            detail.write(f"[dim]Created: {_fmt_local(created_at)} ({_time_ago(created_at)})[/dim]")

        if message:
            detail.write("")
            detail.write("[dim]\u2500\u2500 Message \u2500\u2500[/dim]")
            detail.write(f"[dim]{_escape_markup(message)}[/dim]")

        # Show available actions as inline hints
        actions: list[str] = []
        can_trigger = stype == "RECURRING" and status == "PENDING" and is_mine
        can_cancel = status in ("PENDING", "PAUSED") and is_mine
        if can_trigger:
            actions.append("[bold][r][/bold] Run now")
        if can_cancel:
            actions.append("[bold][x][/bold] Cancel")
        if actions:
            detail.write("")
            detail.write("  ".join(actions))

    @work(thread=False)
    async def _load_runs(self, sched: dict[str, Any]) -> None:
        """Fetch and display past runs for the selected schedule."""
        runs_pane = self.query_one("#schedule-runs-pane", RichLog)
        runs_pane.clear()
        runs_pane.write("[bold]Past Runs[/bold]")
        runs_pane.write("")

        schedule_id = sched.get("agent_schedule_id", "")
        if not schedule_id:
            runs_pane.write("[dim]No schedule selected.[/dim]")
            return

        cache_key = f"runs:{schedule_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            runs: list[dict[str, Any]] = cached
        else:
            runs_pane.write("[dim]Loading runs...[/dim]")
            try:
                resp = await _http_request("GET", f"/schedule/{schedule_id}/runs?limit=20")
                runs = resp.get("runs", [])
                _cache_set(cache_key, runs)
            except Exception as e:
                # Guard: only update UI if this schedule is still selected
                if self._selected_schedule and self._selected_schedule.get("agent_schedule_id") == schedule_id:
                    self._current_runs = []
                    runs_pane.clear()
                    runs_pane.write("[bold]Past Runs[/bold]")
                    runs_pane.write("")
                    runs_pane.write(f"[indian_red]Error loading runs: {e}[/indian_red]")
                return

        # Guard: only apply results if this schedule is still the selected one
        if self._selected_schedule and self._selected_schedule.get("agent_schedule_id") != schedule_id:
            return

        # Re-clear and re-write after loading
        runs_pane.clear()
        runs_pane.write("[bold]Past Runs[/bold]")
        runs_pane.write("")
        self._current_runs = runs

        if not runs:
            runs_pane.write("[dim]No runs yet.[/dim]")
            return

        for run in runs:
            run_num = run.get("run_number", "?")
            status = run.get("status", "UNKNOWN")
            sym, color = _RUN_STATUS_STYLE.get(status, ("?", ""))
            started = run.get("started_at")
            completed = run.get("completed_at")
            session_id = run.get("session_id")
            error = run.get("error")
            duration = _duration_str(started, completed)

            # Run header line: #N  ● STATUS  started_time  (duration)
            time_str = _fmt_local(started) if started else ""
            dur_str = f"  ({duration})" if duration else ""
            runs_pane.write(f"[{color}]{sym}[/{color}] #{run_num}  [{color}]{status}[/{color}]  {time_str}{dur_str}")

            # Session links
            if session_id:
                console_url = f"{AHS_LIT_BASE_URL}/agent_harness_console?session_id={session_id}"
                runs_pane.write(f"  [dim][link={console_url}]Lit \u2192[/link][/dim]")

            # Error message
            if error:
                truncated = error[:120] + ("..." if len(error) > 120 else "")
                runs_pane.write(f"  [indian_red]{_escape_markup(truncated)}[/indian_red]")

            runs_pane.write("")

    # --- DataTable cursor events ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        key = str(event.row_key.value)
        # Skip section header rows
        if key.startswith("__section_"):
            return

        sched = next((s for s in self._all_schedules_ordered if s.get("agent_schedule_id") == key), None)
        if sched:
            self._selected_schedule = sched
            self._show_detail(sched)
            self._load_runs(sched)

    # --- Actions ---

    def action_open_latest_session(self) -> None:
        """Open the most recent session for the selected schedule in TUI chat."""
        # Find the first run with a session_id
        for run in self._current_runs:
            sid = run.get("session_id")
            if sid:
                self.dismiss(sid)
                return
        detail = self.query_one("#schedule-detail-pane", RichLog)
        detail.write("[dim]No session found for this schedule.[/dim]")

    def action_pop_screen(self) -> None:
        """ESC — always dismiss immediately back to chat."""
        self.dismiss(None)

    def action_focus_next_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx + 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_focus_prev_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx - 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_refresh_all(self) -> None:
        _cache_invalidate("schedules:")
        _cache_invalidate("runs:")
        self._load_schedules(force=True)

    def action_toggle_mine(self) -> None:
        self._my_schedules = not self._my_schedules
        _cache_invalidate("schedules:")
        _cache_invalidate("runs:")
        self._load_schedules(force=True)

    def _is_mine(self, sched: dict[str, Any]) -> bool:
        """Check if the current user owns this schedule."""
        return bool(self._user_id) and sched.get("created_by_user") == self._user_id

    def action_run_now(self) -> None:
        if not self._selected_schedule:
            return
        sched = self._selected_schedule
        detail = self.query_one("#schedule-detail-pane", RichLog)
        if sched.get("schedule_type") != "RECURRING":
            detail.write("[dim]Run now: only available for recurring schedules.[/dim]")
            return
        if sched.get("status") != "PENDING":
            detail.write("[dim]Run now: schedule must be in PENDING status.[/dim]")
            return
        if not self._is_mine(sched):
            detail.write("[dim]Run now: only the schedule owner can trigger a run.[/dim]")
            return
        self._do_run_now(sched["agent_schedule_id"])

    @work(thread=False)
    async def _do_run_now(self, schedule_id: str) -> None:
        detail = self.query_one("#schedule-detail-pane", RichLog)
        try:
            resp = await _http_request(
                "POST",
                f"/schedule/{schedule_id}/trigger",
                {"user_id": self._user_id},
            )
            run_num = resp.get("run_number", "?")
            session_id = resp.get("session_id", "")
            console_url = f"{AHS_LIT_BASE_URL}/agent_harness_console?session_id={session_id}"
            detail.write(
                f"[green]Triggered run #{run_num}  [link={console_url}]Lit \u2192[/link]  [dim]o=Chat[/dim][/green]"
            )
            _cache_invalidate("schedules:")
            _cache_invalidate("runs:")
            self._load_schedules(force=True)
        except Exception as e:
            detail.write(f"[indian_red]Error triggering run: {e}[/indian_red]")

    def action_cancel_schedule(self) -> None:
        if not self._selected_schedule:
            return
        sched = self._selected_schedule
        detail = self.query_one("#schedule-detail-pane", RichLog)
        if sched.get("status") not in ("PENDING", "PAUSED"):
            detail.write("[dim]Cancel: only PENDING or PAUSED schedules can be cancelled.[/dim]")
            return
        if not self._is_mine(sched):
            detail.write("[dim]Cancel: only the schedule owner can cancel.[/dim]")
            return
        # Show inline confirmation overlay
        name = sched.get("name") or sched["agent_schedule_id"][:12]
        self._confirm_pending = sched["agent_schedule_id"]
        picker = self.query_one("#confirm-overlay", OptionList)
        picker.clear_options()
        picker.add_option(Option(f'Yes \u2014 cancel "{_escape_markup(name)}"', id="yes"))
        picker.add_option(Option("No \u2014 keep it", id="no"))
        picker.display = True
        picker.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        picker = self.query_one("#confirm-overlay", OptionList)
        picker.display = False
        # Restore focus to current list pane
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

        if event.option.id == "yes" and self._confirm_pending:
            self._do_cancel(self._confirm_pending)
        self._confirm_pending = None

    @work(thread=False)
    async def _do_cancel(self, schedule_id: str) -> None:
        detail = self.query_one("#schedule-detail-pane", RichLog)
        try:
            if not self._user_id:
                detail.write("[dim]Cannot cancel: user ID not available.[/dim]")
                return
            await _http_request("DELETE", f"/schedule/{schedule_id}?user_id={self._user_id}")
            detail.write("[green]Schedule cancelled.[/green]")
            _cache_invalidate("schedules:")
            _cache_invalidate("runs:")
            self._load_schedules(force=True)
        except Exception as e:
            detail.write(f"[indian_red]Error cancelling schedule: {e}[/indian_red]")
