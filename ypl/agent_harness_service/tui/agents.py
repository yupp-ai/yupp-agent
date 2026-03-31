"""Agents screen — split-pane browser for agent configs and system prompts."""

from __future__ import annotations
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, OptionList, RichLog, Static

try:
    from textual.widgets.option_list import Option
except ImportError:
    from textual.widgets._option_list import Option

from ypl.agent_harness_service.tui.config import _http_request

# ---------------------------------------------------------------------------
# Color coding by executor type
# ---------------------------------------------------------------------------

_TYPE_STYLE: dict[str, str] = {
    "harnessed": "green",
    "raw": "cyan",
}


def _styled_type(exec_type: str) -> Text:
    style = _TYPE_STYLE.get(exec_type, "white")
    return Text(exec_type, style=style)


def _styled_name(name: str, exec_type: str) -> Text:
    style = _TYPE_STYLE.get(exec_type, "white")
    return Text(name, style=f"bold {style}")


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

AGENTS_SCREEN_CSS = """
#agents-container {
    height: 1fr;
}
#agent-table {
    height: 50%;
    border-bottom: solid $surface-lighten-2;
}
#detail-row {
    height: 50%;
}
#agent-info {
    width: 2fr;
    padding: 0 1;
    scrollbar-size: 1 1;
}
#agent-info:focus {
    border: none;
}
#prompt-list {
    width: 1fr;
    border-left: solid $surface-lighten-2;
}
#prompt-list:focus {
    border-left: solid $accent;
}
"""

PROMPT_MODAL_CSS = """
#prompt-modal-content {
    width: 90%;
    height: 80%;
    background: $surface;
    border: solid $accent;
    padding: 1 2;
}
#prompt-text {
    height: 1fr;
    scrollbar-size: 1 1;
}
"""


# ---------------------------------------------------------------------------
# Prompt viewer modal
# ---------------------------------------------------------------------------


class PromptModal(ModalScreen[None]):
    """Scrollable modal to view a system prompt section."""

    CSS = PROMPT_MODAL_CSS
    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=True),
        Binding("q", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, title: str, content: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-modal-content"):
            yield Static(f"[bold white]{self._title}[/bold white]")
            log = RichLog(id="prompt-text", markup=True, wrap=True, auto_scroll=False)
            log.can_focus = True
            yield log
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#prompt-text", RichLog)
        for line in self._content.splitlines():
            log.write(line)
        log.focus()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# AgentsScreen
# ---------------------------------------------------------------------------


class AgentsScreen(Screen[str | None]):
    """Split-pane screen: agent list (top) + info & prompt list (bottom)."""

    CSS = AGENTS_SCREEN_CSS
    BINDINGS = [
        Binding("escape", "pop_screen", "Back", show=True),
        Binding("enter", "open_prompt", "View Prompt", show=True),
        Binding("tab", "focus_next_pane", "Next Pane", show=True),
        Binding("shift+tab", "focus_prev_pane", "Prev Pane", show=False),
        Binding("r", "refresh", "Refresh", show=True, key_display="R"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agents: list[dict[str, Any]] = []
        self._selected_agent: dict[str, Any] | None = None
        self._load_counter = 0  # unique prefix to avoid OptionList duplicate ID errors
        self._prompt_titles: list[str] = []
        self._prompts: dict[str, str] = {}
        self._pane_order = ["agent-table", "agent-info", "prompt-list"]
        self._current_pane_idx = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="agents-container"):
            table: DataTable[str] = DataTable(id="agent-table", cursor_type="row")
            table.can_focus = True
            yield table
            with Horizontal(id="detail-row"):
                info = RichLog(id="agent-info", markup=True, wrap=True, auto_scroll=False)
                info.can_focus = True
                yield info
                prompts = OptionList(id="prompt-list")
                prompts.can_focus = True
                yield prompts
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.add_columns("Name", "Type", "Model", "Tools", "Subagents", "Turns", "Budget", "Creator")
        table.focus()
        self._load_agents()

    # ----- Data loading -----

    @work(thread=False)
    async def _load_agents(self) -> None:
        info = self.query_one("#agent-info", RichLog)
        info.clear()
        info.write("[dim]Loading agents...[/dim]")
        try:
            resp = await _http_request("GET", "/agents?include_all=true")
            self._agents = resp.get("agents", [])
        except Exception as e:
            info.clear()
            info.write(f"[indian_red]Error loading agents: {e}[/indian_red]")
            return
        self._populate_table()

    def _summarize_tools(self, perms: dict[str, str]) -> str:
        if perms.get("*") == "allow":
            return "all"
        if perms.get("*") == "deny":
            allowed = [k for k, v in perms.items() if k != "*" and v == "allow"]
            return str(len(allowed)) if allowed else "none"
        return "-"

    def _summarize_subagents(self, subs: list[str]) -> str:
        if subs == ["*"]:
            return "all"
        if subs:
            return str(len(subs))
        return "none"

    def _populate_table(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.clear()
        for agent in self._agents:
            model = agent.get("executor_model") or agent.get("llm_model") or "-"
            exec_type = agent.get("executor_type", "-")
            perms = agent.get("tool_permissions", {})
            subs = agent.get("allowed_subagents", [])
            creator = agent.get("creator_user_id")
            creator_str = creator[:8] + "…" if creator and len(creator) > 8 else (creator or "built-in")

            table.add_row(
                _styled_name(agent["name"], exec_type),
                _styled_type(exec_type),
                model,
                self._summarize_tools(perms),
                self._summarize_subagents(subs),
                str(agent.get("max_turns", "-")),
                f"${agent.get('max_budget_usd', 0):.1f}",
                Text(creator_str, style="dim"),
                key=agent["name"],
            )
        if self._agents:
            table.move_cursor(row=0)

    # ----- Row selection -----

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "agent-table":
            return
        key = event.row_key.value if event.row_key else None
        if not key:
            return
        agent = next((a for a in self._agents if a["name"] == key), None)
        if agent and agent != self._selected_agent:
            self._selected_agent = agent
            self._load_agent_detail(agent["name"])

    @work(thread=False)
    async def _load_agent_detail(self, agent_name: str) -> None:
        self._load_counter += 1
        prefix = self._load_counter
        info = self.query_one("#agent-info", RichLog)
        prompt_list = self.query_one("#prompt-list", OptionList)
        # clear_options() doesn't reset the internal _id_to_option map,
        # so we reset it manually to avoid DuplicateID on shared prompt names.
        prompt_list.clear_options()
        prompt_list._id_to_option.clear()
        info.clear()
        info.write(f"[dim]Loading {agent_name}...[/dim]")

        try:
            resp = await _http_request("GET", f"/agent/{agent_name}?include_system_prompts=true")
        except Exception as e:
            info.clear()
            info.write(f"[indian_red]Error: {e}[/indian_red]")
            return

        agent = resp.get("agent", {})
        prompts = resp.get("system_prompts") or {}
        additional = resp.get("additional_system_prompt")
        if additional:
            prompts["additional_system_prompt"] = additional

        self._prompts = prompts
        self._prompt_titles = list(prompts.keys())

        info.clear()

        # Header
        display_name = agent.get("display_name", agent_name)
        exec_type = agent.get("executor_type", "-")
        style = _TYPE_STYLE.get(exec_type, "white")
        info.write(Text(display_name, style=f"bold {style}"))
        if agent.get("description"):
            info.write(f"[dim]{agent['description']}[/dim]")
        info.write("")

        # Key-value pairs with aligned columns
        model = agent.get("executor_model") or agent.get("llm_model") or "-"
        perms = agent.get("tool_permissions", {})
        subs = agent.get("allowed_subagents", [])
        gateways = agent.get("allowed_gateways", [])
        creator = agent.get("creator_user_id") or "built-in"

        if perms.get("*") == "allow":
            tools_detail = "all allowed"
        elif perms.get("*") == "deny":
            allowed = [k for k, v in perms.items() if k != "*" and v == "allow"]
            tools_detail = ", ".join(allowed) if allowed else "none"
        else:
            tools_detail = "-"

        if subs == ["*"]:
            subs_detail = "all allowed"
        elif subs:
            subs_detail = ", ".join(subs)
        else:
            subs_detail = "none"

        rows: list[tuple[str, str]] = [
            ("Executor", exec_type),
            ("Model", model),
            ("Repo", agent.get("default_repo", "-")),
            ("Max turns", str(agent.get("max_turns", "-"))),
            ("Budget", f"${agent.get('max_budget_usd', 0):.1f}"),
            ("Timeout", f"{agent.get('timeout_s', '-')}s"),
            ("Sandbox", "yes" if agent.get("sandbox_enabled") else "no"),
            ("Tools", tools_detail),
            ("Subagents", subs_detail),
            ("Gateways", ", ".join(gateways) if gateways else "-"),
            ("Creator", creator),
        ]

        for label, value in rows:
            info.write(f"  [bold]{label:<12}[/bold] {value}")

        # Populate prompt list
        if self._prompt_titles:
            for title in self._prompt_titles:
                content = prompts[title]
                lines = len(content.splitlines())
                prompt_label = Text.assemble(
                    (title, "cyan"),
                    (f"  ({lines}L)", "dim"),
                )
                prompt_list.add_option(Option(prompt_label, id=f"{prefix}:{title}"))

    # ----- Actions -----

    def action_open_prompt(self) -> None:
        """Open a prompt viewer for the highlighted prompt in the OptionList."""
        # Only act when prompt-list is focused
        prompt_list = self.query_one("#prompt-list", OptionList)
        if not self._prompt_titles or not self._prompts:
            return
        idx = prompt_list.highlighted
        if idx is None:
            return
        title = self._prompt_titles[idx]
        self.app.push_screen(PromptModal(title, self._prompts[title]))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Double-click / Enter on an option in the prompt list."""
        if event.option_list.id != "prompt-list":
            return
        raw_id = event.option.id
        if raw_id and ":" in raw_id:
            title = raw_id.split(":", 1)[1]
            if title in self._prompts:
                self.app.push_screen(PromptModal(title, self._prompts[title]))

    def action_focus_next_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx + 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_focus_prev_pane(self) -> None:
        self._current_pane_idx = (self._current_pane_idx - 1) % len(self._pane_order)
        self.query_one(f"#{self._pane_order[self._current_pane_idx]}").focus()

    def action_refresh(self) -> None:
        self._selected_agent = None
        self._load_agents()

    def action_pop_screen(self) -> None:
        self.dismiss(None)
