"""Projects screen — three-pane master-detail for browsing projects and tasks."""

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

from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.agent_harness_service.tui.config import _http_request
from ypl.agent_harness_service.tui.rendering import _escape_markup

# ---------------------------------------------------------------------------
# TTL cache for API responses
# ---------------------------------------------------------------------------

_CACHE_TTL = 60.0  # seconds

_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    """Return cached value if not expired, else None."""
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
    """Invalidate all cache entries whose key starts with prefix."""
    to_delete = [k for k in _cache if k.startswith(prefix)]
    for k in to_delete:
        del _cache[k]


# ---------------------------------------------------------------------------
# Status symbols, colors, and labels
# ---------------------------------------------------------------------------


def _fmt_timestamp(dt_str: str | None) -> str:
    """Return yyyy-mm-dd hh:mm."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_str))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_str)[:16]


def _time_ago(dt_str: str | None) -> str:
    """Return a human-readable relative time like '3h ago', '2d ago'."""
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


# (symbol, rich color) — symbols are all single-cell-width for alignment
PROJECT_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "ACTIVE": ("\u25cf", "green"),  # ●
    "PAUSED": ("\u2759", "yellow"),  # ❙
    "COMPLETED": ("\u2713", "bright_green"),  # ✓
    "ARCHIVED": ("\u2610", "dim"),  # ☐
}
TASK_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "PENDING": ("\u00b7", "dim"),  # ·
    "BLOCKED": ("\u2717", "red"),  # ✗
    "READY": ("\u25b8", "cyan"),  # ▸
    "IN_PROGRESS": ("\u25cb", "yellow"),  # ○
    "COMPLETED": ("\u2713", "bright_green"),  # ✓
    "FAILED": ("!", "bold red"),  # !
    "CANCELLED": ("-", "dim"),  # -
    "IN_REVIEW": ("?", "magenta"),  # ?
}
TASK_PRIORITY_STYLE: dict[str, tuple[str, str]] = {
    "URGENT": ("!!", "bold red"),
    "HIGH": ("!", "yellow"),
    "NORMAL": ("", ""),
    "LOW": ("~", "dim"),
}

# Plain-text symbols for use in the detail pane (markup context)
PROJECT_STATUS_EMOJI: dict[str, str] = {k: v[0] for k, v in PROJECT_STATUS_STYLE.items()}
TASK_STATUS_EMOJI: dict[str, str] = {k: v[0] for k, v in TASK_STATUS_STYLE.items()}
TASK_PRIORITY_BADGE: dict[str, str] = {k: v[0] for k, v in TASK_PRIORITY_STYLE.items()}

PROJECT_VALID_STATUSES = ["ACTIVE", "PAUSED", "COMPLETED", "ARCHIVED"]
TASK_VALID_STATUSES = ["READY", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED", "PENDING", "BLOCKED", "IN_REVIEW"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# (char, style) for each status segment in the progress bar
_BAR_SEGMENT: dict[str, tuple[str, str]] = {
    "COMPLETED": ("C", "bold bright_green"),
    "IN_PROGRESS": ("P", "bold green"),
    "IN_REVIEW": ("R", "cyan"),
    "READY": (".", "green"),
    "BLOCKED": ("b", "bright_black"),
    "FAILED": ("F", "bold red"),
    "PENDING": (".", "dim"),
    "CANCELLED": ("-", "dim"),
}

# Order in which segments appear in the bar (left to right)
_BAR_ORDER = ["COMPLETED", "IN_PROGRESS", "IN_REVIEW", "READY", "BLOCKED", "FAILED", "PENDING", "CANCELLED"]


_BAR_WIDTH = 18


def _progress_bar(task_summary: dict[str, Any], total: int) -> Text:
    """Render a fixed-width color-coded progress bar as a Rich Text object.

    Bar is always _BAR_WIDTH chars wide, segments are proportional to task counts.
    Example: [CCCCCCCPP...BBFF..] 4/12
    """
    completed = int(task_summary.get("COMPLETED") or 0)
    bar = Text("[")
    if total <= 0:
        bar.append("." * _BAR_WIDTH, style="dim")
    else:
        # Greedy proportional allocation that guarantees exactly _BAR_WIDTH
        raw: list[tuple[int, str, str]] = []
        for status in _BAR_ORDER:
            count = int(task_summary.get(status) or 0)
            if count > 0:
                char, style = _BAR_SEGMENT.get(status, (".", "dim"))
                raw.append((count, char, style))
        # Allocate widths using largest-remainder method
        segments: list[tuple[int, str, str]] = []
        if raw:
            raw_total = sum(c for c, _, _ in raw)
            remainders: list[tuple[float, int]] = []
            allocated = 0
            for i, (count, char, style) in enumerate(raw):
                exact = _BAR_WIDTH * count / raw_total
                floored = max(1, int(exact))  # at least 1 char per segment
                segments.append((floored, char, style))
                remainders.append((exact - floored, i))
                allocated += floored
            # Distribute remaining slots by largest remainder
            remainders.sort(key=lambda x: x[0], reverse=True)
            for _remainder, idx in remainders:
                if allocated >= _BAR_WIDTH:
                    break
                w, c, s = segments[idx]
                segments[idx] = (w + 1, c, s)
                allocated += 1
            # If over-allocated (due to min-1 guarantees), trim largest segments
            while allocated > _BAR_WIDTH:
                max_idx = max(range(len(segments)), key=lambda i: segments[i][0])
                w, c, s = segments[max_idx]
                if w <= 1:
                    break
                segments[max_idx] = (w - 1, c, s)
                allocated -= 1
        else:
            segments.append((_BAR_WIDTH, ".", "dim"))
        for width, char, style in segments:
            bar.append(char * width, style=style)
    bar.append(f"] {completed}/{total}")
    return bar


def _build_task_tree(
    tasks: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str, str]]:
    """Build a display-ordered list of (task, rank_str, prefix_str).

    Groups tasks by parent, topologically sorts siblings by depends_on,
    and computes rank strings (1, 2, 3, 3.1, 3.2, etc.) with tree prefixes.
    """
    # Index tasks
    by_id: dict[str, dict[str, Any]] = {t["agent_task_id"]: t for t in tasks}
    children_map: dict[str | None, list[dict[str, Any]]] = {}
    for t in tasks:
        parent = t.get("parent_task_id")
        if parent and parent not in by_id:
            parent = None  # orphan parent reference — treat as root
        children_map.setdefault(parent, []).append(t)

    def _topo_sort(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Topological sort within a sibling group by depends_on."""
        sibling_ids = {s["agent_task_id"] for s in siblings}
        in_degree: dict[str, int] = {s["agent_task_id"]: 0 for s in siblings}
        adj: dict[str, list[str]] = {s["agent_task_id"]: [] for s in siblings}
        for s in siblings:
            for dep in s.get("depends_on") or []:
                if dep in sibling_ids:
                    in_degree[s["agent_task_id"]] += 1
                    adj[dep].append(s["agent_task_id"])
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        result_ids: list[str] = []
        while queue:
            queue.sort()  # stable ordering by id
            node = queue.pop(0)
            result_ids.append(node)
            for nxt in adj[node]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)
        # Append any remaining (cycle) in original order
        seen = set(result_ids)
        result_ids.extend(s["agent_task_id"] for s in siblings if s["agent_task_id"] not in seen)
        return [by_id[sid] for sid in result_ids]

    result: list[tuple[dict[str, Any], str, str]] = []

    def _walk(parent_id: str | None, rank_prefix: str, depth: int) -> None:
        siblings = children_map.get(parent_id, [])
        if not siblings:
            return
        sorted_siblings = _topo_sort(siblings)
        for idx, task in enumerate(sorted_siblings):
            tid = task["agent_task_id"]
            rank = f"{rank_prefix}{idx + 1}" if rank_prefix else str(idx + 1)
            is_last = idx == len(sorted_siblings) - 1

            if depth == 0:
                prefix = ""
            else:
                connector = "\u2514\u2500 " if is_last else "\u251c\u2500 "
                indent = "    " * (depth - 1)
                prefix = f"{indent}{connector}"

            result.append((task, rank, prefix))
            # Recurse into children with dot notation
            _walk(tid, f"{rank}.", depth + 1)

    _walk(None, "", 0)
    return result


def _dep_refs(task: dict[str, Any], task_ranks: dict[str, str]) -> str:
    """Build dependency reference string like [->1,2]."""
    deps = task.get("depends_on") or []
    if not deps:
        return ""
    refs = [task_ranks.get(d, d[:6]) for d in deps]
    return f" [\u2192{','.join(refs)}]"


# ---------------------------------------------------------------------------
# Screen CSS
# ---------------------------------------------------------------------------

PROJECTS_SCREEN_CSS = """
#projects-container {
    height: 1fr;
}
#project-list {
    height: 30%;
    border-bottom: solid $surface-lighten-2;
}
#project-info-bar {
    height: auto;
    max-height: 3;
    padding: 0 1;
    background: $surface-darken-1;
    color: $text-muted;
}
#task-list {
    height: 1fr;
    border-bottom: solid $surface-lighten-2;
}
#detail-pane {
    height: 30%;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#detail-pane:focus {
    border: none;
}
#status-picker-overlay {
    display: none;
    dock: bottom;
    height: auto;
    max-height: 12;
    background: $surface;
    border: solid $accent;
    padding: 0 1;
}
"""


# ---------------------------------------------------------------------------
# ProjectsScreen
# ---------------------------------------------------------------------------


class ProjectsScreen(Screen[str | None]):
    """Three-pane master-detail screen for browsing projects and tasks."""

    CSS = PROJECTS_SCREEN_CSS
    BINDINGS = [
        Binding("escape", "pop_screen", "Back", show=True),
        Binding("r", "resume_task", "Resume", show=True),
        Binding("s", "status_picker", "Status", show=True),
        Binding("c", "toggle_completed", "Completed", show=True),
        Binding("R", "refresh_all", "Refresh", show=True, key_display="Shift+R"),
        Binding("tab", "focus_next_pane", "Next Pane", show=True),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._projects: list[dict[str, Any]] = []
        self._tasks: list[dict[str, Any]] = []
        self._task_tree: list[tuple[dict[str, Any], str, str]] = []
        self._task_ranks: dict[str, str] = {}  # task_id -> rank string
        self._selected_project: dict[str, Any] | None = None
        self._selected_task: dict[str, Any] | None = None
        self._pane_order = ["project-list", "task-list", "detail-pane"]
        self._current_pane_idx = 0
        self._status_picker_target: str = ""  # "project" or "task"
        self._show_completed: bool = False  # hide completed items by default

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="projects-container"):
            pt: DataTable[str] = DataTable(id="project-list", cursor_type="row")
            pt.can_focus = True
            yield pt
            yield Static("", id="project-info-bar")
            tt: DataTable[str] = DataTable(id="task-list", cursor_type="row")
            tt.can_focus = True
            yield tt
            detail = RichLog(id="detail-pane", markup=True, wrap=True, auto_scroll=False)
            detail.can_focus = True
            yield detail
        ol = OptionList(id="status-picker-overlay")
        ol.can_focus = True
        yield ol
        yield Footer()

    async def on_mount(self) -> None:
        # Set up project table columns
        pt = self.query_one("#project-list", DataTable)
        pt.add_columns("Status", "Name", "Progress", "Creator", "Created", "Budget")

        # Set up task table columns
        tt = self.query_one("#task-list", DataTable)
        tt.add_columns("Status", "#", "Title", "Priority", "Agent")

        pt.focus()
        self._load_projects()

    @work(thread=False)
    async def _load_projects(self, force: bool = False, restore_project_id: str | None = None) -> None:
        detail = self.query_one("#detail-pane", RichLog)

        cached = None if force else _cache_get("projects")
        if cached is not None:
            self._projects = cached
        else:
            detail.clear()
            detail.write("[dim]Loading projects...[/dim]")
            try:
                resp = await _http_request("GET", "/projects?limit=50")
                self._projects = resp.get("items", [])
                _cache_set("projects", self._projects)
            except Exception as e:
                detail.clear()
                detail.write(f"[indian_red]Error loading projects: {e}[/indian_red]")
                return

        pt = self.query_one("#project-list", DataTable)
        pt.clear()

        display_projects = self._projects
        if not self._show_completed:
            display_projects = [p for p in self._projects if p.get("status") != "COMPLETED"]

        if not display_projects:
            detail.clear()
            detail.write("[dim]No projects found.[/dim]")
            tt = self.query_one("#task-list", DataTable)
            tt.clear()
            self._selected_project = None
            self._selected_task = None
            return

        for proj in display_projects:
            status = proj.get("status", "")
            sym, color = PROJECT_STATUS_STYLE.get(status, (" ", ""))
            name = proj.get("name", "?")
            ts = proj.get("task_summary") or {}
            total = int(ts.get("total") or 0)
            # If total is missing but we have per-status counts, sum them
            if total == 0 and ts:
                total = sum(int(ts.get(s) or 0) for s in TASK_STATUS_STYLE)
            progress = _progress_bar(ts, total)
            budget_limit = proj.get("budget_usd")
            spent = proj.get("budget_spent_usd", 0)
            if budget_limit is not None:
                budget = f"${float(spent):.2f}/${float(budget_limit):.2f}"
            else:
                budget = f"${float(spent):.2f}" if spent else ""
            creator = proj.get("creator_user_name") or ""
            created = _time_ago(proj.get("created_at"))
            pt.add_row(
                Text(f"{sym} {status}", style=color),
                name,
                progress,
                creator,
                created,
                budget,
                key=proj["agent_project_id"],
            )

        detail.clear()
        # Restore cursor to the previously selected project, or auto-select first
        target_proj = None
        if restore_project_id:
            target_proj = next((p for p in display_projects if p["agent_project_id"] == restore_project_id), None)
        if target_proj is None and display_projects:
            target_proj = display_projects[0]

        if target_proj:
            self._selected_project = target_proj
            self._show_project_detail(target_proj)
            self._load_tasks(target_proj["agent_project_id"])
            # Move cursor to the restored project row
            try:
                pt.move_cursor(row=pt.get_row_index(target_proj["agent_project_id"]))
            except Exception:
                pass

    @work(thread=False)
    async def _load_tasks(self, project_id: str, force: bool = False, restore_task_id: str | None = None) -> None:
        cache_key = f"tasks:{project_id}"
        cached = None if force else _cache_get(cache_key)
        if cached is not None:
            self._tasks = cached
        else:
            try:
                resp = await _http_request("GET", f"/projects/{project_id}/tasks")
                self._tasks = resp.get("items", [])
                _cache_set(cache_key, self._tasks)
            except Exception as e:
                detail = self.query_one("#detail-pane", RichLog)
                detail.clear()
                detail.write(f"[indian_red]Error loading tasks: {e}[/indian_red]")
                return

        self._task_tree = _build_task_tree(self._tasks)
        self._task_ranks = {t["agent_task_id"]: rank for t, rank, _ in self._task_tree}

        display_tree = self._task_tree
        if not self._show_completed:
            display_tree = [(t, r, p) for t, r, p in self._task_tree if t.get("status") != "COMPLETED"]

        tt = self.query_one("#task-list", DataTable)
        tt.clear()

        for task, rank, prefix in display_tree:
            status = task.get("status", "")
            sym, color = TASK_STATUS_STYLE.get(status, (" ", ""))
            priority = task.get("priority", "")
            pri_sym, pri_color = TASK_PRIORITY_STYLE.get(priority, ("", ""))
            agent = task.get("agent_name") or ""
            title = task.get("title", "?")
            dep_str = _dep_refs(task, self._task_ranks)
            display_title = f"{prefix}{title}{dep_str}"
            pri_display = f"{pri_sym} {priority}" if pri_sym else priority
            tt.add_row(
                Text(f"{sym} {status}", style=color),
                rank,
                display_title,
                Text(pri_display, style=pri_color) if pri_color else pri_display,
                agent,
                key=task["agent_task_id"],
            )

        # Restore cursor to the previously selected task
        if restore_task_id:
            try:
                tt.move_cursor(row=tt.get_row_index(restore_task_id))
            except Exception:
                pass

    def _show_project_detail(self, proj: dict[str, Any]) -> None:
        """Update the project info bar between project list and task list."""
        bar = self.query_one("#project-info-bar", Static)
        name = proj.get("name", "?")
        status = proj.get("status", "?")
        sym, color = PROJECT_STATUS_STYLE.get(status, (" ", ""))
        creator = proj.get("creator_user_name") or ""
        slack = proj.get("slack_channel") or ""
        desc = proj.get("description") or ""
        budget_limit = proj.get("budget_usd")
        spent = proj.get("budget_spent_usd", 0)
        created = _time_ago(proj.get("created_at"))
        ts = proj.get("task_summary") or {}

        parts: list[str] = [f"[bold]{_escape_markup(name)}[/bold]", f"[{color}]{sym} {status}[/{color}]"]
        if creator:
            parts.append(f"by {_escape_markup(creator)}")
        if created:
            parts.append(created)
        if slack:
            parts.append(f"#{_escape_markup(slack)}")
        if budget_limit is not None:
            parts.append(f"${float(spent):.2f}/${float(budget_limit):.2f}")
        if ts:
            counts = " ".join(
                f"{TASK_STATUS_EMOJI.get(s, '')}{ts.get(s, 0)}"
                for s in ["COMPLETED", "IN_PROGRESS", "READY", "BLOCKED", "FAILED"]
                if ts.get(s, 0) > 0
            )
            if counts:
                parts.append(counts)
        bar.update(" | ".join(parts))

        # Also show description in detail pane when no task selected
        detail = self.query_one("#detail-pane", RichLog)
        detail.clear()
        if desc:
            detail.write(f"[dim]{_escape_markup(desc)}[/dim]")

    def _show_task_detail(self, task: dict[str, Any]) -> None:
        detail = self.query_one("#detail-pane", RichLog)
        detail.clear()
        title = _escape_markup(task.get("title", "?"))
        status = task.get("status", "?")
        sym, color = TASK_STATUS_STYLE.get(status, (" ", ""))
        priority = task.get("priority", "?")
        pri_badge = TASK_PRIORITY_BADGE.get(priority, "")
        agent = task.get("agent_name") or "-"
        desc = task.get("description") or ""
        deps = task.get("depends_on") or []
        sessions = task.get("assigned_session_ids") or []
        result = task.get("result") or {}
        spending = task.get("actual_spending_usd")
        created = task.get("created_at") or ""
        completed = task.get("completed_at") or ""
        effort = task.get("estimated_effort") or ""
        tid = task["agent_task_id"]

        # Title + status line
        detail.write(f"[bold]{title}[/bold]")
        detail.write(
            f"[{color}]{sym} {status}[/{color}]   Priority: {pri_badge} {priority}   Agent: {_escape_markup(agent)}"
        )

        # Links section — PR, sessions, slack thread
        links: list[str] = []
        pr_url = result.get("pr_url") if isinstance(result, dict) else None
        if isinstance(pr_url, str) and pr_url.startswith("https://"):
            pr_num = ""
            if "/pull/" in pr_url:
                pr_num = pr_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
            pr_label = f"PR #{pr_num}" if pr_num.isdigit() else "PR"
            links.append(f"[link={pr_url}]{pr_label}[/link]")
        if sessions:
            for sid in sessions:
                console_url = f"{AHS_LIT_BASE_URL}/agent_harness_console?session_id={sid}"
                links.append(f"[link={console_url}]Lit {sid[:8]} \u2192[/link]")
            links.append("[dim]Enter=Chat[/dim]")
        slack_thread = result.get("slack_thread_url") if isinstance(result, dict) else None
        if isinstance(slack_thread, str) and slack_thread.startswith("https://"):
            links.append(f"[link={slack_thread}]Slack thread[/link]")
        if links:
            detail.write(f"Links: {' | '.join(links)}")

        # Dependencies + reverse deps (compact, on same area)
        if deps:
            dep_parts: list[str] = []
            for d in deps:
                rank = self._task_ranks.get(d, d[:8])
                dep_task = next((t for t in self._tasks if t["agent_task_id"] == d), None)
                if dep_task:
                    dep_emoji = TASK_STATUS_EMOJI.get(dep_task.get("status", ""), "")
                    dep_title = dep_task.get("title", "")[:30]
                    dep_parts.append(f"{dep_emoji} {rank} {dep_title}")
                else:
                    dep_parts.append(rank)
            detail.write(f"[dim]Deps: {', '.join(dep_parts)}[/dim]")
        rdeps = [t for t in self._tasks if tid in (t.get("depends_on") or [])]
        if rdeps:
            rdep_parts = [
                f"{TASK_STATUS_EMOJI.get(t.get('status', ''), '')}"
                f" {self._task_ranks.get(t['agent_task_id'], '?')}"
                f" {t.get('title', '')[:30]}"
                for t in rdeps
            ]
            detail.write(f"[dim]Dependents: {', '.join(rdep_parts)}[/dim]")

        # Metadata line (dates, spending, effort)
        meta: list[str] = []
        if spending is not None:
            meta.append(f"${float(spending):.2f}")
        if effort:
            meta.append(effort)
        if created:
            meta.append(f"created {_fmt_timestamp(str(created))} ({_time_ago(str(created))})")
        if completed:
            meta.append(f"completed {_fmt_timestamp(str(completed))} ({_time_ago(str(completed))})")
        if meta:
            detail.write(f"[dim]{' | '.join(meta)}[/dim]")

        # Full description / system prompt
        if desc:
            detail.write("")
            detail.write("[dim]\u2500\u2500\u2500 Description \u2500\u2500\u2500[/dim]")
            detail.write(_escape_markup(desc))

    # --- DataTable cursor events ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = event.data_table.id
        if event.row_key is None or event.row_key.value is None:
            return

        if table_id == "project-list":
            key = event.row_key.value
            proj = next((p for p in self._projects if p["agent_project_id"] == key), None)
            if proj and proj != self._selected_project:
                self._selected_project = proj
                self._selected_task = None
                self._show_project_detail(proj)
                self._load_tasks(key)

        elif table_id == "task-list":
            key = event.row_key.value
            task = next((t for t in self._tasks if t["agent_task_id"] == key), None)
            if task:
                self._selected_task = task
                self._show_task_detail(task)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter key pressed on a row."""
        table_id = event.data_table.id
        if event.row_key is None or event.row_key.value is None:
            return

        if table_id == "task-list":
            key = event.row_key.value
            task = next((t for t in self._tasks if t["agent_task_id"] == key), None)
            if task:
                sessions = task.get("assigned_session_ids") or []
                if sessions:
                    # Dismiss screen, return session id to attach to
                    self.dismiss(sessions[-1])

    # --- Actions ---

    def action_pop_screen(self) -> None:
        """ESC — always dismiss immediately back to chat."""
        self.dismiss(None)

    def action_focus_next_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx + 1) % len(self._pane_order)
        pane_id = self._pane_order[self._current_pane_idx]
        self.query_one(f"#{pane_id}").focus()

    def action_focus_prev_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx - 1) % len(self._pane_order)
        pane_id = self._pane_order[self._current_pane_idx]
        self.query_one(f"#{pane_id}").focus()

    def action_refresh_all(self) -> None:
        self._selected_task = None
        _cache_invalidate("")  # clear all
        self._load_projects(force=True)

    def action_toggle_completed(self) -> None:
        """Toggle visibility of completed projects and tasks."""
        self._show_completed = not self._show_completed
        # Re-render without re-fetching
        self._load_projects(force=False)

    def action_resume_task(self) -> None:
        if not self._selected_task:
            return
        if self._selected_task.get("status") != "FAILED":
            detail = self.query_one("#detail-pane", RichLog)
            detail.write("[dim]Only FAILED tasks can be resumed.[/dim]")
            return
        self._do_resume_task()

    @work(thread=False)
    async def _do_resume_task(self) -> None:
        if not self._selected_project or not self._selected_task:
            return
        pid = self._selected_project["agent_project_id"]
        tid = self._selected_task["agent_task_id"]
        detail = self.query_one("#detail-pane", RichLog)
        try:
            await _http_request("POST", f"/projects/{pid}/tasks/{tid}/resume")
            detail.write("[green]Task resumed successfully.[/green]")
            _cache_invalidate(f"tasks:{pid}")
            _cache_invalidate("projects")
            self._load_tasks(pid, force=True)
        except Exception as e:
            detail.write(f"[indian_red]Error resuming task: {e}[/indian_red]")

    def action_status_picker(self) -> None:
        """Show status picker for the focused pane's selected item."""
        picker = self.query_one("#status-picker-overlay", OptionList)
        picker.clear_options()

        focused = self.focused
        if focused and focused.id == "project-list" and self._selected_project:
            self._status_picker_target = "project"
            for s in PROJECT_VALID_STATUSES:
                emoji = PROJECT_STATUS_EMOJI.get(s, "")
                picker.add_option(Option(f"{emoji} {s}", id=s))
        elif self._selected_task:
            self._status_picker_target = "task"
            for s in TASK_VALID_STATUSES:
                emoji = TASK_STATUS_EMOJI.get(s, "")
                picker.add_option(Option(f"{emoji} {s}", id=s))
        else:
            return

        picker.display = True
        picker.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """User selected a status from the picker."""
        picker = self.query_one("#status-picker-overlay", OptionList)
        picker.display = False

        status = event.option.id
        if not status:
            return

        if self._status_picker_target == "project" and self._selected_project:
            self._do_set_project_status(self._selected_project["agent_project_id"], status)
        elif self._status_picker_target == "task" and self._selected_project and self._selected_task:
            self._do_set_task_status(
                self._selected_project["agent_project_id"],
                self._selected_task["agent_task_id"],
                status,
            )

        # Refocus pane
        pane_id = self._pane_order[self._current_pane_idx]
        self.query_one(f"#{pane_id}").focus()

    @work(thread=False)
    async def _do_set_project_status(self, project_id: str, status: str) -> None:
        detail = self.query_one("#detail-pane", RichLog)
        try:
            await _http_request("POST", f"/projects/{project_id}/status", {"status": status})
            detail.write(f"[green]Project status set to {status}.[/green]")
            _cache_invalidate("projects")
            self._load_projects(force=True, restore_project_id=project_id)
        except Exception as e:
            detail.write(f"[indian_red]Error setting status: {e}[/indian_red]")

    @work(thread=False)
    async def _do_set_task_status(self, project_id: str, task_id: str, status: str) -> None:
        detail = self.query_one("#detail-pane", RichLog)
        try:
            await _http_request("POST", f"/projects/{project_id}/tasks/{task_id}/status", {"status": status})
            detail.write(f"[green]Task status set to {status}.[/green]")
            _cache_invalidate(f"tasks:{project_id}")
            _cache_invalidate("projects")
            self._load_tasks(project_id, force=True, restore_task_id=task_id)
        except Exception as e:
            detail.write(f"[indian_red]Error setting status: {e}[/indian_red]")
