"""Search screen — full-screen search with grouped results."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from ypl.agent_harness_service.tui.config import _http_request
from ypl.agent_harness_service.tui.rendering import _escape_markup

# ---------------------------------------------------------------------------
# Status styling
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ACTIVE": ("\u25cf", "green"),
    "COMPLETED": ("\u2713", "bright_green"),
    "FAILED": ("!", "bold red"),
    "CANCELLED": ("\u25cb", "dim"),
    "IN_PROGRESS": ("\u25cf", "green"),
    "PENDING": ("\u25cb", "cyan"),
    "READY": ("\u25cf", "cyan"),
    "BLOCKED": ("\u25a0", "yellow"),
    "IN_REVIEW": ("\u25cf", "magenta"),
    "PAUSED": ("\u23f8", "yellow"),
    "STALE": ("\u25cb", "dim"),
}


def _time_ago(dt: datetime | str | None) -> str:
    if dt is None:
        return ""
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = datetime.now(tz=UTC) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
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


def _status_styled(status: str | None) -> Text:
    if not status:
        return Text("")
    sym, color = _STATUS_STYLE.get(status, ("", ""))
    return Text(f"{sym} {status}" if sym else status, style=color)


def _sched_type_label(stype: str | None) -> str:
    if not stype:
        return ""
    up = stype.upper()
    if up == "RECURRING":
        return "recurring"
    if up in ("SCHEDULED", "ONE_TIME"):
        return "one-time"
    return stype.lower()


# ---------------------------------------------------------------------------
# Screen CSS
# ---------------------------------------------------------------------------

SEARCH_SCREEN_CSS = """
#search-box {
    height: 3;
    border: round $accent;
    margin: 0 1;
}
#search-status-bar {
    height: 1;
    padding: 0 2;
    color: $text-muted;
}
#search-results-table {
    height: 3fr;
    scrollbar-size: 1 1;
}
#search-results-table > .datatable--header {
    display: none;
}
#search-detail-pane {
    height: 2fr;
    padding: 0 1;
    border-top: solid $surface-lighten-2;
    scrollbar-size: 1 1;
}
#search-detail-pane:focus {
    border-top: solid $surface-lighten-2;
}
"""


# ---------------------------------------------------------------------------
# SearchScreen
# ---------------------------------------------------------------------------


class SearchScreen(Screen[str | None]):
    """Full-screen search with a bordered search box and grouped results."""

    CSS = SEARCH_SCREEN_CSS
    BINDINGS = [
        Binding("escape", "dismiss_search", "Back", show=True),
        Binding("tab", "focus_next_pane", "Next Pane", show=False),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False),
    ]

    def __init__(self, user_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id
        self._all_results_ordered: list[dict[str, Any]] = []
        self._last_query: str = ""
        self._pane_order = ["search-box", "search-results-table", "search-detail-pane"]
        self._current_pane_idx = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Input(
                placeholder="Search sessions, projects, tasks, schedules, PRs...  (Enter to search)",
                id="search-box",
            )
            yield Static("", id="search-status-bar")
            tbl: DataTable = DataTable(
                id="search-results-table",
                cursor_type="row",
                show_header=False,
            )
            tbl.can_focus = True
            yield tbl
            detail = RichLog(id="search-detail-pane", markup=True, wrap=True, auto_scroll=False)
            detail.can_focus = True
            yield detail
        yield Footer()

    async def on_mount(self) -> None:
        tbl = self.query_one("#search-results-table", DataTable)
        tbl.add_column("result", width=None)
        self.query_one("#search-box", Input).focus()
        self._update_status("Type a query and press Enter to search")

    def _update_status(self, text: str) -> None:
        try:
            bar = self.query_one("#search-status-bar", Static)
        except Exception:
            return
        bar.update(f"[dim]{text}[/dim]" if text else "")

    # --- Input handling ---

    async def _on_key(self, event: Key) -> None:
        focused = self.focused
        if isinstance(focused, Input) and event.key == "enter":
            event.prevent_default()
            event.stop()
            query = focused.value.strip()
            if query:
                self._do_search(query)
            return
        if isinstance(focused, DataTable) and event.key == "enter":
            event.prevent_default()
            event.stop()
            self._select_current_row()
            return
        await super()._on_key(event)

    # --- Search execution ---

    @work(thread=False)
    async def _do_search(self, query: str) -> None:
        self._last_query = query
        self._update_status(f'Searching for "{_escape_markup(query)}"...')
        try:
            body: dict[str, Any] = {"text": query, "user_id": self._user_id or ""}
            resp = await _http_request("POST", "/search", body)
            total = resp.get("total_count", 0)
            results = resp.get("results", {})
            self._render_results(results, total, query)
        except Exception as e:
            self._update_status(f"Error: {e}")

    # --- Rendering ---

    def _render_results(self, results: dict[str, list[dict[str, Any]]], total: int, query: str) -> None:
        tbl = self.query_one("#search-results-table", DataTable)
        tbl.clear()
        self._all_results_ordered = []

        if total == 0:
            self._update_status(f'No results for "{_escape_markup(query)}"')
            try:
                detail = self.query_one("#search-detail-pane", RichLog)
                detail.clear()
                detail.write("[dim]No results found. Try a different search term.[/dim]")
            except Exception:
                pass
            return

        self._update_status(f'{total} result{"s" if total != 1 else ""} for "{_escape_markup(query)}"')

        sessions = results.get("sessions", [])
        messages = results.get("session_messages", [])
        projects = results.get("projects", [])
        tasks = results.get("tasks", [])
        schedules = results.get("schedules", [])
        prs = results.get("artifact_pr", [])
        docs = results.get("artifact_doc", [])

        groups_rendered = 0
        if sessions or messages:
            if groups_rendered:
                tbl.add_row(Text(""), key=f"__spacer_{groups_rendered}__")
            self._render_sessions_group(tbl, sessions, messages)
            groups_rendered += 1
        if projects or tasks:
            if groups_rendered:
                tbl.add_row(Text(""), key=f"__spacer_{groups_rendered}__")
            self._render_projects_group(tbl, projects, tasks)
            groups_rendered += 1
        if schedules:
            if groups_rendered:
                tbl.add_row(Text(""), key=f"__spacer_{groups_rendered}__")
            self._render_schedules_group(tbl, schedules)
            groups_rendered += 1
        if prs:
            if groups_rendered:
                tbl.add_row(Text(""), key=f"__spacer_{groups_rendered}__")
            self._render_prs_group(tbl, prs)
            groups_rendered += 1
        if docs:
            if groups_rendered:
                tbl.add_row(Text(""), key=f"__spacer_{groups_rendered}__")
            self._render_docs_group(tbl, docs)
            groups_rendered += 1

        if self._all_results_ordered:
            self._show_detail(self._all_results_ordered[0])

    def _add_section(self, tbl: DataTable, text: Text, key: str) -> None:
        tbl.add_row(text, key=key)

    def _add_row(self, tbl: DataTable, text: Text, key: str, item: dict[str, Any]) -> None:
        tbl.add_row(text, key=key)
        self._all_results_ordered.append(item)

    # --- Sessions & Messages ---

    def _render_sessions_group(
        self,
        tbl: DataTable,
        sessions: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        n = len(sessions) + len(messages)
        self._add_section(
            tbl, Text(f"\u2500 \U0001f4ac Sessions & Messages ({n})", style="bold cyan"), "__section_sessions__"
        )

        msg_by_session: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            sid = str(msg.get("session_id", ""))
            msg_by_session.setdefault(sid, []).append(msg)

        rendered_sessions: set[str] = set()

        for s in sessions:
            sid = str(s.get("id", ""))
            rendered_sessions.add(sid)
            self._add_row(tbl, self._fmt_session(s), f"sessions:{sid}", s)
            for msg in msg_by_session.pop(sid, []):
                mid = str(msg.get("id", ""))
                self._add_row(tbl, self._fmt_message(msg), f"session_messages:{mid}", msg)

        for sid, msgs_list in msg_by_session.items():
            if sid in rendered_sessions:
                continue
            session_title = str(msgs_list[0].get("session_title") or sid[:12])
            label = Text("   \u2514 ")
            label.append(session_title, style="bold")
            label.append(f"  {sid}", style="dim")
            self._add_section(tbl, label, f"__msg_session_{sid}__")
            for msg in msgs_list:
                mid = str(msg.get("id", ""))
                self._add_row(tbl, self._fmt_message(msg), f"session_messages:{mid}", msg)

    def _fmt_session(self, s: dict[str, Any]) -> Text:
        sid = str(s.get("id", ""))
        title = str(s.get("title") or sid[:12])
        agent = str(s.get("agent_name") or "")
        status = str(s.get("status") or "")
        msgs = s.get("message_count", 0)
        created = _time_ago(s.get("created_at"))

        t = Text("   ")
        t.append(f"{title[:45]:<45}  ", style="bold")
        t.append(f"{sid}  ", style="dim")
        t.append(f"{agent}  ")
        t.append_text(_status_styled(status))
        t.append(f"  {msgs} msgs" if msgs else "")
        t.append(f"  {created}", style="dim")
        return t

    def _fmt_message(self, msg: dict[str, Any]) -> Text:
        role = str(msg.get("role") or "")
        snippet = str(msg.get("snippet") or msg.get("title") or "")[:120]
        turn = msg.get("turn_number")
        created = _time_ago(msg.get("created_at"))

        t = Text("      ")
        if turn is not None:
            t.append(f"T{turn} ", style="dim")
        if role:
            t.append(f"[{role}] ", style="cyan")
        t.append(f"{snippet}  ")
        t.append(created, style="dim")
        return t

    # --- Projects & Tasks ---

    def _render_projects_group(
        self,
        tbl: DataTable,
        projects: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> None:
        n = len(projects) + len(tasks)
        self._add_section(
            tbl, Text(f"\u2500 \U0001f4c1 Projects & Tasks ({n})", style="bold green"), "__section_projects__"
        )

        tasks_by_project: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            pid = str(task.get("project_id", ""))
            tasks_by_project.setdefault(pid, []).append(task)

        rendered_projects: set[str] = set()

        for p in projects:
            pid = str(p.get("id", ""))
            rendered_projects.add(pid)
            self._add_row(tbl, self._fmt_project(p), f"projects:{pid}", p)
            for task in tasks_by_project.pop(pid, []):
                tid = str(task.get("id", ""))
                self._add_row(tbl, self._fmt_task(task), f"tasks:{tid}", task)

        for pid, task_list in tasks_by_project.items():
            if pid in rendered_projects:
                continue
            project_name = str(task_list[0].get("project_name") or pid[:12])
            label = Text("   \u2514 ")
            label.append(project_name, style="bold")
            label.append(f"  {pid}", style="dim")
            self._add_section(tbl, label, f"__task_project_{pid}__")
            for task in task_list:
                tid = str(task.get("id", ""))
                self._add_row(tbl, self._fmt_task(task), f"tasks:{tid}", task)

    def _fmt_project(self, p: dict[str, Any]) -> Text:
        pid = str(p.get("id", ""))
        name = str(p.get("name") or pid[:12])
        status = str(p.get("status") or "")
        created = _time_ago(p.get("created_at"))
        task_counts = p.get("task_counts") or {}

        t = Text("   ")
        t.append(f"{name[:45]:<45}  ", style="bold")
        t.append_text(_status_styled(status))
        if task_counts:
            parts = [f"{v}{k[0]}" for k, v in task_counts.items()]
            t.append(f"  ({'/'.join(parts)})", style="dim")
        t.append(f"  {created}", style="dim")
        return t

    def _fmt_task(self, task: dict[str, Any]) -> Text:
        title = str(task.get("title") or "")[:50]
        status = str(task.get("status") or "")
        priority = str(task.get("priority") or "")
        agent = str(task.get("agent_name") or "")
        created = _time_ago(task.get("created_at"))

        t = Text("      ")
        t.append(f"{title:<40}  ")
        t.append_text(_status_styled(status))
        if priority:
            t.append(f"  {priority}", style="yellow")
        if agent:
            t.append(f"  @{agent}", style="dim")
        t.append(f"  {created}", style="dim")
        return t

    # --- Schedules ---

    def _render_schedules_group(self, tbl: DataTable, schedules: list[dict[str, Any]]) -> None:
        self._add_section(
            tbl,
            Text(f"\u2500 \u23f0 Schedules ({len(schedules)})", style="bold magenta"),
            "__section_schedules__",
        )
        for s in schedules:
            sid = str(s.get("id", ""))
            self._add_row(tbl, self._fmt_schedule(s), f"schedules:{sid}", s)

    def _fmt_schedule(self, s: dict[str, Any]) -> Text:
        name = str(s.get("name") or str(s.get("id", "?"))[:12])
        stype = _sched_type_label(s.get("schedule_type"))
        status = str(s.get("status") or "")
        agent = str(s.get("agent_name") or "")
        cron = str(s.get("cron_expression") or "")
        runs = s.get("run_count", 0)

        t = Text("   ")
        t.append(f"{name[:40]:<40}  ", style="bold")
        t.append(f"{stype:<10}  ", style="magenta")
        t.append_text(_status_styled(status))
        if agent:
            t.append(f"  @{agent}", style="dim")
        if cron:
            t.append(f"  {cron}", style="dim")
        elif runs:
            t.append(f"  {runs} runs", style="dim")
        return t

    # --- Pull Requests ---

    def _render_prs_group(self, tbl: DataTable, prs: list[dict[str, Any]]) -> None:
        self._add_section(
            tbl,
            Text(f"\u2500 \U0001f517 Pull Requests ({len(prs)})", style="bold blue"),
            "__section_prs__",
        )
        for pr in prs:
            pid = str(pr.get("id", ""))
            self._add_row(tbl, self._fmt_pr(pr), f"artifact_pr:{pid}", pr)

    def _fmt_pr(self, pr: dict[str, Any]) -> Text:
        title = str(pr.get("title") or str(pr.get("id", "?"))[:12])
        repo = str(pr.get("repo") or "")
        pr_num = pr.get("pr_number")
        created = _time_ago(pr.get("created_at"))

        t = Text("   ")
        if repo:
            t.append(f"{repo}", style="dim")
        if pr_num:
            t.append(f"#{pr_num}", style="bold blue")
        t.append(f"  {title[:50]}  ", style="bold")
        t.append(created, style="dim")
        return t

    # --- Documents ---

    def _render_docs_group(self, tbl: DataTable, docs: list[dict[str, Any]]) -> None:
        self._add_section(
            tbl,
            Text(f"\u2500 \U0001f4c4 Documents ({len(docs)})", style="bold"),
            "__section_docs__",
        )
        for doc in docs:
            did = str(doc.get("id", ""))
            self._add_row(tbl, self._fmt_doc(doc), f"artifact_doc:{did}", doc)

    def _fmt_doc(self, doc: dict[str, Any]) -> Text:
        title = str(doc.get("title") or str(doc.get("id", "?"))[:12])
        url = str(doc.get("url") or "")
        created = _time_ago(doc.get("created_at"))

        t = Text("   ")
        t.append(f"{title[:45]}  ", style="bold")
        if url:
            t.append(f"{url[:40]}  ", style="dim")
        t.append(created, style="dim")
        return t

    # --- Detail pane ---

    def _show_detail(self, item: dict[str, Any]) -> None:
        try:
            detail = self.query_one("#search-detail-pane", RichLog)
        except Exception:
            return
        detail.clear()

        type_key = str(item.get("type", ""))
        item_id = str(item.get("id", "?"))
        score = item.get("score", 0)
        created = item.get("created_at")

        if type_key == "sessions":
            title = str(item.get("title") or item_id[:12])
            detail.write(f"[bold cyan]\U0001f4ac Session[/bold cyan]  [dim]score: {score:.1f}[/dim]")
            detail.write(f"[bold]{_escape_markup(title)}[/bold]")
            agent = str(item.get("agent_name") or "-")
            status = str(item.get("status") or "-")
            trigger = str(item.get("trigger") or "-")
            msgs = item.get("message_count", 0)
            detail.write(f"Agent: {_escape_markup(agent)}  Status: {status}  Trigger: {trigger}")
            detail.write(f"Messages: {msgs}  ID: [dim]{item_id}[/dim]")

        elif type_key == "session_messages":
            detail.write(f"[bold white]\U0001f4dd Message[/bold white]  [dim]score: {score:.1f}[/dim]")
            session_title = str(item.get("session_title") or str(item.get("session_id", "?"))[:12])
            role = str(item.get("role") or "?")
            turn = item.get("turn_number")
            snippet = str(item.get("snippet") or "")
            sid = str(item.get("session_id") or "")
            detail.write(
                f"Session: {_escape_markup(session_title)}  [dim]{sid[:8]}[/dim]  Role: {role}  Turn: {turn or '?'}"
            )
            if snippet:
                detail.write(f"[dim]{_escape_markup(snippet[:200])}[/dim]")

        elif type_key == "projects":
            detail.write(f"[bold green]\U0001f4c1 Project[/bold green]  [dim]score: {score:.1f}[/dim]")
            name = str(item.get("name") or item_id[:12])
            detail.write(f"[bold]{_escape_markup(name)}[/bold]")
            desc = str(item.get("description") or "")
            status = str(item.get("status") or "-")
            if desc:
                detail.write(f"[dim]{_escape_markup(desc[:150])}[/dim]")
            task_counts = item.get("task_counts") or {}
            if task_counts:
                counts_str = "  ".join(f"{k}: {v}" for k, v in task_counts.items())
                detail.write(f"Status: {status}  Tasks: {counts_str}")
            else:
                detail.write(f"Status: {status}")

        elif type_key == "tasks":
            detail.write(f"[bold yellow]\u2611 Task[/bold yellow]  [dim]score: {score:.1f}[/dim]")
            title = str(item.get("title") or item_id[:12])
            detail.write(f"[bold]{_escape_markup(title)}[/bold]")
            project_name = str(item.get("project_name") or "-")
            status = str(item.get("status") or "-")
            priority = str(item.get("priority") or "-")
            agent = str(item.get("agent_name") or "-")
            desc = str(item.get("description") or "")
            detail.write(f"Project: {_escape_markup(project_name)}  Status: {status}")
            detail.write(f"Priority: {priority}  Agent: {_escape_markup(agent)}")
            if desc:
                detail.write(f"[dim]{_escape_markup(desc[:150])}[/dim]")

        elif type_key == "schedules":
            detail.write(f"[bold magenta]\u23f0 Schedule[/bold magenta]  [dim]score: {score:.1f}[/dim]")
            name = str(item.get("name") or item_id[:12])
            detail.write(f"[bold]{_escape_markup(name)}[/bold]")
            stype = str(item.get("schedule_type") or "-")
            status = str(item.get("status") or "-")
            cron = str(item.get("cron_expression") or "")
            runs = item.get("run_count", 0)
            agent = str(item.get("agent_name") or "-")
            detail.write(f"Type: {stype}  Status: {status}  Agent: {_escape_markup(agent)}  Runs: {runs}")
            if cron:
                detail.write(f"Cron: {_escape_markup(cron)}")

        elif type_key == "artifact_pr":
            detail.write(f"[bold blue]\U0001f517 Pull Request[/bold blue]  [dim]score: {score:.1f}[/dim]")
            title = str(item.get("title") or item_id[:12])
            pr_num = item.get("pr_number")
            repo = str(item.get("repo") or "")
            url = str(item.get("url") or "")
            detail.write(f"[bold]{_escape_markup(title)}[/bold]")
            detail.write(f"Repo: {_escape_markup(repo)}  PR: #{pr_num or '?'}")
            if url:
                detail.write(f"[link={url}]{url}[/link]")

        elif type_key == "artifact_doc":
            detail.write(f"[bold white]\U0001f4c4 Document[/bold white]  [dim]score: {score:.1f}[/dim]")
            title = str(item.get("title") or item_id[:12])
            url = str(item.get("url") or "")
            detail.write(f"[bold]{_escape_markup(title)}[/bold]")
            if url:
                detail.write(f"[link={url}]{url}[/link]")

        if created:
            detail.write(f"[dim]Created: {_time_ago(created)}[/dim]")

    # --- DataTable cursor events ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        key = str(event.row_key.value)
        if key.startswith("__") or ":" not in key:
            return
        type_key, item_id = key.split(":", 1)
        for item in self._all_results_ordered:
            if item.get("id") == item_id and item.get("type") == type_key:
                self._show_detail(item)
                break

    def _select_current_row(self) -> None:
        tbl = self.query_one("#search-results-table", DataTable)
        if tbl.row_count == 0:
            return
        row_key = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key
        key = str(row_key.value) if row_key.value is not None else ""

        # Handle __msg_session_<sid>__ context rows — Enter goes to that session
        if key.startswith("__msg_session_") and key.endswith("__"):
            sid = key[len("__msg_session_") : -2]
            if sid:
                self.dismiss(sid)
            return

        if key.startswith("__") or ":" not in key:
            return

        type_key, item_id = key.split(":", 1)

        if type_key == "sessions":
            self.dismiss(item_id)
            return

        if type_key == "session_messages":
            for item in self._all_results_ordered:
                if item.get("id") == item_id and item.get("type") == type_key:
                    parent_sid = item.get("session_id")
                    if parent_sid:
                        self.dismiss(str(parent_sid))
                    return

        # No action for non-session types
        return

    # --- Actions ---

    def action_dismiss_search(self) -> None:
        self.dismiss(None)

    def action_focus_next_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx + 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_focus_prev_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx - 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()
