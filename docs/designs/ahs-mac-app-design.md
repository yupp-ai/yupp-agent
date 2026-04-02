# AHS Mac App — Design Document

## Vision

A native macOS desktop client for the Agent Harness Service — blending the polished, Mac-native feel of Claude/ChatGPT desktop apps with the workspace observability of Devin. AHS manages fleets of autonomous agents, so the UI must surface agent activity, projects, and schedules alongside chat — not just be a chat wrapper.

**Design principle:** Claude's warmth + Devin's transparency, purpose-built for multi-agent orchestration.

---

## Layout Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ●  ●  ●                        AHS                                        │
├────────────┬────────────────────────────────────────────────────────────────┤
│            │  ┌─────────────────────────────────────────────────────────┐  │
│  SIDEBAR   │  │  Session Header: Agent · Model · Status · Cost         │  │
│  (240px)   │  ├─────────────────────────────────────────────────────────┤  │
│            │  │                                                         │  │
│ ┌────────┐ │  │                    CHAT AREA                            │  │
│ │⌕ Search│ │  │                                                         │  │
│ └────────┘ │  │  ┌─────────────────────────────────┐                    │  │
│            │  │  │ 🤖 Agent message with markdown   │                    │  │
│ + New      │  │  │ and streaming text...            │                    │  │
│            │  │  └─────────────────────────────────┘                    │  │
│ ─ TODAY ── │  │                                                         │  │
│  Session 1 │  │           ┌──────────────────────────────────────┐      │  │
│  Session 2 │  │           │ 👤 Your message here                 │      │  │
│ ─ EARLIER  │  │           └──────────────────────────────────────┘      │  │
│  Session 3 │  │                                                         │  │
│  Session 4 │  │  ┌─ Tool Activity ──────────────────┐                   │  │
│            │  │  │ ✓ mcp:search_code      0.8s      │                   │  │
│            │  │  │ ● mcp:edit_file     running...   │                   │  │
│            │  │  └──────────────────────────────────┘                   │  │
│            │  │                                                         │  │
│ ────────── │  ├─────────────────────────────────────────────────────────┤  │
│ 📋 Projects│  │  ┌──────────────────────────────────────────────────┐   │  │
│ 📅 Schedule│  │  │  Message input (multi-line, auto-resize)    ➤   │   │  │
│ 🤖 Agents  │  │  │  ⊕ Attach                                      │   │  │
│ ⚙ Settings│  │  └──────────────────────────────────────────────────┘   │  │
│            │  │                                                         │  │
└────────────┴──┴─────────────────────────────────────────────────────────┘
```

---

## 1. Sidebar (Left Panel — 240px, collapsible)

The sidebar is the primary navigation hub. It lives in a darker tint of the background color (think Claude's sidebar — slightly recessed).

### 1.1 Top: Search Bar
- Compact search field with `Cmd+K` shortcut (spotlight-style)
- Searches across sessions, projects, tasks, schedules, agents, PRs
- Results appear as a floating popover grouped by type (like VS Code command palette)
- Each result has an icon, title, subtitle, and keyboard shortcut to select

### 1.2 New Session Button
- Prominent `+ New Session` button below search
- Click opens an **agent picker popover**: searchable list of agents sorted by recent usage
- Each agent shows: name, model badge, executor type tag
- Selecting an agent immediately creates a session and opens it

### 1.3 Session List (Main Body)
- Sessions grouped by time: **Today**, **Yesterday**, **This Week**, **Older**
- Each session row shows:
  - **Status dot** (left edge): green=active, gray=completed, amber=stale
  - **Title** (auto-generated or session ID prefix) — bold, single line, truncated
  - **Agent name** — secondary text, muted color
  - **Trigger badge** — tiny pill: `API` `Slack` `Cron` `Webhook`
  - **Time** — relative ("3h ago") on the right
- **Hover:** shows three-dot menu (rename, copy ID, delete)
- **Right-click:** context menu with same options + "Open in Lit console"
- **Active session** has a subtle left-edge accent bar (like Slack's active channel indicator)
- Root sessions only by default; child sessions nested with indent + disclosure triangle
- Filter chips above list: `All` | `Active` | `Mine` — plus trigger filter dropdown

### 1.4 Bottom: Navigation Icons
A compact row of icon buttons at the sidebar bottom:

| Icon | Label | Shortcut | Opens |
|------|-------|----------|-------|
| 📋 | Projects | `Cmd+1` | Projects panel (replaces chat area) |
| 📅 | Schedules | `Cmd+2` | Schedules panel |
| 🤖 | Agents | `Cmd+3` | Agent browser panel |
| ⚙ | Settings | `Cmd+,` | Settings sheet |

Plus the user avatar/account at the very bottom.

---

## 2. Main Area — Chat View (Default)

### 2.1 Session Header Bar
A thin bar at the top of the chat area:

```
┌──────────────────────────────────────────────────────────────────┐
│  🤖 sre-agent  ·  claude-opus-4  ·  harnessed  │ ● Active │ $1.24 │
└──────────────────────────────────────────────────────────────────┘
```

- Agent name (clickable — opens agent detail popover)
- Model badge (styled pill)
- Executor type tag
- Status indicator (colored dot + label)
- Session cost (cumulative USD)
- Overflow menu: Copy session ID, Open in Lit, View history, End session

### 2.2 Chat Thread
The scrolling message area. Design language follows Claude desktop:

**User messages:**
- Right-aligned, with a subtle colored background (light blue tint)
- User avatar (Google profile pic, small circle) on the right
- Timestamp on hover

**Agent messages:**
- Left-aligned, no background (or very faint)
- Agent avatar (small icon based on executor type) on the left
- Full markdown rendering: headers, lists, code blocks, tables, links
- Code blocks: syntax-highlighted with copy button, language label
- Timestamp on hover

**Tool activity blocks** (collapsible, between messages):
```
┌─ Tool Activity ─────────────────────────────────────────┐
│  ✓ search_code (mcp)              "auth middleware"  0.8s│
│  ✓ read_file (mcp)                "src/auth.py"     0.3s│
│  ● edit_file (mcp)                "src/auth.py"  running │
│  ▸ command_execution               "pytest tests/"       │
└─────────────────────────────────────────────────────────┘
```

- Each tool call shows: status icon, tool name, server, key argument, duration
- Collapsed by default to just a summary line: "Used 4 tools (3 completed, 1 running)"
- Click to expand full detail
- Failed tools show in red with error preview

**Turn summary** (after each agent turn):
```
  ─── 1,240 in · 3,891 out · $0.12 · 8.3s ───
```
Thin, centered, muted — shows token counts, cost, duration.

**Streaming behavior:**
- Text appears token-by-token with a subtle cursor blink at the end
- A "Thinking..." pill appears during the initial delay before tokens arrive
- For extended thinking models, a collapsible "Reasoning" section shows the thinking chain

### 2.3 Input Area
At the bottom, a multi-line text input with:

```
┌──────────────────────────────────────────────────────────────┐
│  Type a message...                                     ➤    │
│  ⊕ Attach   ⌘↵ Send   ⌘⇧↵ New line                        │
└──────────────────────────────────────────────────────────────┘
```

- Auto-resizes up to ~6 lines, then scrolls internally
- `Cmd+Enter` to send (or Enter, configurable)
- `Shift+Enter` for newline
- Attach button for files
- Send button (arrow) appears when there's text
- When agent is running: send button becomes a **Stop** button (■ square icon)
- Slash commands work inline (`/new`, `/help`, `/clear`, etc.)

### 2.4 Empty State (New Session, No Messages)
Centered in the chat area:

```
        ┌─────────────────────────┐
        │      AHS Logo/Icon      │
        │                         │
        │   What should we work   │
        │        on today?        │
        │                         │
        │  [Fix a bug]  [Deploy]  │
        │  [Review PR]  [Write]   │
        └─────────────────────────┘
```

Suggestion chips that pre-fill the input with common prompts.

---

## 3. Main Area — Projects View (`Cmd+1`)

Replaces the chat area when navigating to Projects. Uses a master-detail layout:

```
┌──────────────────────────────────────────────────────────────────┐
│  Projects                                        + New  ⟳       │
├──────────────────────────┬───────────────────────────────────────┤
│  PROJECT LIST            │  PROJECT DETAIL                      │
│                          │                                      │
│  ● My Web App     ████░░ │  My Web App                          │
│  ✓ Data Pipeline  ██████ │  Status: Active   Budget: $24/$100   │
│  ❙ Old Thing      ███░░░ │  Creator: alice   Slack: #my-web-app │
│                          │  ─────────────────────────────────── │
│                          │  TASKS                               │
│                          │  ✓ 1. Set up project structure       │
│                          │  ✓ 2. Implement auth                 │
│                          │  ● 3. Build API endpoints            │
│                          │    ● 3.1 Users endpoint  [sre-agent] │
│                          │    ▸ 3.2 Orders endpoint             │
│                          │  · 4. Frontend                       │
│                          │                                      │
│                          │  ─── Selected Task ────────────────  │
│                          │  3.1 Users endpoint                  │
│                          │  Status: IN_PROGRESS  Agent: sre     │
│                          │  Depends on: 1, 2                    │
│                          │  PR: org/repo#42                     │
│                          │  [Open Session] [View PR]            │
└──────────────────────────┴───────────────────────────────────────┘
```

- Left: project list with mini progress bars (colored segments by task status)
- Right: selected project detail + task tree
- Task tree: hierarchical with indentation, status icons, agent badges
- Clicking a task shows its detail below the tree
- Action buttons: change status, resume failed, open assigned session
- `S` key or right-click to change status
- Toggle completed tasks visibility

---

## 4. Main Area — Schedules View (`Cmd+2`)

```
┌──────────────────────────────────────────────────────────────────┐
│  Schedules                           My Schedules ☐  ⟳          │
├────────────────────────────────┬─────────────────────────────────┤
│  SCHEDULE LIST                 │  DETAIL + RUNS                 │
│                                │                                │
│  ── Recurring ──               │  Daily SRE Check               │
│  ● Daily SRE Check    sre     │  Agent: sre-agent               │
│    Next: 9:00 AM  0 0 9 * *   │  Cron: 0 0 9 * * (PST)        │
│  ● Weekly Review      pm      │  Next run: Tomorrow 9:00 AM    │
│    Next: Mon 10AM              │  Runs: 142 total               │
│                                │                                │
│  ── One-time ──                │  ── Recent Runs ──             │
│  ✓ Migration check   dba      │  #142 ✓ 2h ago   3m 12s       │
│    Completed 2h ago            │  #141 ✓ 1d ago   2m 45s       │
│                                │  #140 ! 2d ago   error: ...    │
│                                │                                │
│                                │  [Run Now]  [Cancel]           │
│                                │  [Open Latest Session]         │
└────────────────────────────────┴─────────────────────────────────┘
```

- Left: schedule list, sectioned by Recurring / One-time
- Right: selected schedule detail + past runs table
- Runs show: number, status, time ago, duration, error preview
- Actions: Run Now, Cancel (with confirmation), Open Session

---

## 5. Main Area — Agents View (`Cmd+3`)

```
┌──────────────────────────────────────────────────────────────────┐
│  Agents                                    ⌕ Filter  ⟳          │
├────────────────────────────────┬─────────────────────────────────┤
│  AGENT LIST                    │  AGENT DETAIL                  │
│                                │                                │
│  sre-agent        opus  harness│  sre-agent                     │
│  code-reviewer    sonnet raw   │  "On-call SRE agent for..."    │
│  pm-agent         opus  harness│                                │
│  security-scanner haiku  raw   │  Executor: harnessed           │
│                                │  Model: claude-opus-4          │
│                                │  Max turns: 200                │
│                                │  Budget: $50.00                │
│                                │  Sandbox: enabled              │
│                                │                                │
│                                │  ── Tools (12 allowed) ──      │
│                                │  ✓ search_code                 │
│                                │  ✓ edit_file                   │
│                                │  ✓ run_command                 │
│                                │  ...                           │
│                                │                                │
│                                │  ── System Prompts (3) ──      │
│                                │  ▸ role.md (142 lines)         │
│                                │  ▸ tools.md (89 lines)         │
│                                │  ▸ guidelines.md (56 lines)    │
│                                │                                │
│                                │  [New Session] [View Config]   │
└────────────────────────────────┴─────────────────────────────────┘
```

- Left: agent list with model badge + executor type tag
- Right: full agent detail with tool permissions, subagents, system prompts
- System prompts expandable inline (click to expand full text in a sheet/modal)
- "New Session" button to quickly create a session with this agent

---

## 6. Right Detail Panel (Optional, Context-Sensitive)

An optional right panel that slides in from the right edge when contextually useful. This is the "Devin-inspired" element — it shows live workspace activity.

**When it appears:**
- Clicking "Details" on a session header
- When a running agent is executing commands or editing files
- Manually toggled with `Cmd+D`

**Content tabs:**

| Tab | Content |
|-----|---------|
| Activity | Live timeline of agent actions (tool calls, file changes, commands) |
| Files | List of files the agent has created/modified in this session |
| Terminal | Live terminal output from command executions |
| Plan | If the session has a structured plan/task, show progress |

```
┌── Chat Area ──────────────────┬── Detail Panel (320px) ─────────┐
│                               │  Activity │ Files │ Terminal     │
│  Agent message...             │  ──────────────────────────────  │
│                               │  9:41 ✓ search_code "auth"      │
│  User message...              │  9:41 ✓ read_file src/auth.py   │
│                               │  9:42 ● edit_file src/auth.py   │
│  Agent thinking...            │       + added import statement   │
│                               │       ~ modified authenticate()  │
│                               │  9:42 ▸ run_command pytest       │
│                               │       stdout: 12 passed, 0 fail │
│                               │                                  │
└───────────────────────────────┴──────────────────────────────────┘
```

This panel is **not always visible** — it's for power users who want Devin-level observability. The chat view is fully functional without it.

---

## 7. Visual Design System

### Color Palette

Following a warm, professional aesthetic (inspired by Claude, adapted for an engineering tool):

| Token | Light Mode | Dark Mode | Usage |
|-------|-----------|-----------|-------|
| `bg-primary` | `#FAFAF8` | `#1C1C1E` | Main background |
| `bg-sidebar` | `#F0EFED` | `#161618` | Sidebar background |
| `bg-surface` | `#FFFFFF` | `#242426` | Cards, popovers |
| `bg-user-msg` | `#E8F0FE` | `#1A2A3A` | User message bubble |
| `text-primary` | `#1D1D1F` | `#F5F5F7` | Primary text |
| `text-secondary` | `#6E6E73` | `#8E8E93` | Muted/secondary text |
| `accent` | `#4A90D9` | `#5BA0E9` | Links, active states |
| `status-active` | `#34C759` | `#30D158` | Active/running |
| `status-complete` | `#8E8E93` | `#636366` | Completed |
| `status-error` | `#FF3B30` | `#FF453A` | Failed/error |
| `status-warning` | `#FF9500` | `#FFa500` | Stale/warning |
| `border` | `#E5E5EA` | `#38383A` | Subtle borders |

### Typography

- **System font** (SF Pro on macOS) — no custom fonts needed for native feel
- Body: 14px / 1.5 line-height
- Secondary: 12px / 1.4
- Headers: 16-20px / semibold
- Code: SF Mono or Menlo, 13px
- Monospace for session IDs, token counts, costs

### Iconography

- SF Symbols for all icons (native macOS icon set)
- Status dots: filled circles with status colors
- Navigation: outline-style SF Symbols
- Agent types: distinct icons (robot for harnessed, terminal for raw)

### Spacing and Layout

- 8px grid system
- Sidebar: 240px default, collapsible to 0px (icon-only not needed)
- Detail panel: 320px when open
- Card padding: 12px
- List row height: 44px (standard macOS)
- Section spacing: 24px

---

## 8. Key Interactions

### Session Creation Flow
1. Click `+ New Session` → agent picker popover appears
2. Type to filter agents → select agent
3. Session created immediately, chat opens
4. Optional: type first message before creating (message sent on creation)

### Session Switching
- Click session in sidebar → smooth crossfade transition
- Previous session's WebSocket disconnects, new one connects
- Chat history loads with skeleton placeholders, then fills in
- Scroll position restored if returning to a previously viewed session

### Search Flow (`Cmd+K`)
1. Floating search bar appears (like Spotlight)
2. Type query → live results grouped by type
3. Arrow keys to navigate, Enter to select
4. Sessions: opens chat. Projects/tasks: opens project view. Agents: opens agent detail.

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd+K` | Global search |
| `Cmd+N` | New session |
| `Cmd+1` | Projects view |
| `Cmd+2` | Schedules view |
| `Cmd+3` | Agents view |
| `Cmd+D` | Toggle detail panel |
| `Cmd+,` | Settings |
| `Cmd+Enter` | Send message |
| `Escape` | Stop agent / close popover |
| `Cmd+[` / `Cmd+]` | Navigate back/forward between views |
| `Cmd+Shift+C` | Copy session ID |

---

## 9. Technology Considerations

### Framework Options

| Option | Pros | Cons |
|--------|------|------|
| **SwiftUI** | True native, best macOS integration, SF Symbols, system fonts | Steep learning curve if team is Python-heavy, separate codebase |
| **Tauri + React** | Web tech (easy for the team), lightweight, Rust backend | Less native feel, custom rendering |
| **Electron + React** | Maximum ecosystem, proven (Claude/ChatGPT use this) | Heavy RAM, less native feel |
| **PyQt6 / PySide6** | Python (matches backend team), decent native styling | Mediocre macOS integration, Qt look-and-feel quirks |

**Recommendation:** **Tauri + React** for the best balance of developer productivity and native feel. Use `tauri-plugin-websocket` for WebSocket, style with Tailwind CSS using the macOS design tokens above. If true-native is paramount, SwiftUI is the gold standard but requires Swift expertise.

### API Integration

Reuse all existing AHS endpoints (identical to TUI):
- REST via `fetch` / `axios` for CRUD operations
- WebSocket for real-time chat streaming
- Google OAuth via system browser redirect (Tauri deep link)
- Cache layer with 60s TTL (same as TUI)

---

## 10. MVP Scope

### Phase 1 — Core Chat (v0.1)
- Sidebar with session list (create, switch, filter)
- Chat view with streaming messages
- Tool activity display (collapsible)
- Turn cost summary
- Google OAuth login
- `Cmd+K` search

### Phase 2 — Management Views (v0.2)
- Projects view with task tree
- Schedules view with run history
- Agents browser with prompt viewer
- Status actions (change status, resume, run now)

### Phase 3 — Power Features (v0.3)
- Right detail panel (activity, files, terminal)
- Keyboard shortcut system
- Notification support (macOS native notifications for completed sessions)
- System tray / menu bar presence
- Multiple windows support

---

## Appendix: Screen Mockup Reference

### Compared to Existing TUI

| TUI Feature | Mac App Equivalent |
|---|---|
| Textual DataTable screens | Native list views with master-detail |
| Tab-cycling between panes | Mouse-first with keyboard shortcuts |
| Slash commands (`/new`, `/agents`) | Sidebar nav + `Cmd+K` palette |
| Rich terminal markdown | Native web-rendered markdown |
| Status symbols (`●✓○`) | Colored dots + SF Symbol icons |
| Three-dot overlay menus | Native context menus + popovers |
| 120-col terminal constraint | Fluid responsive layout |

The Mac app should feel like a **natural evolution** of the TUI — same capabilities, but with the polish and discoverability of a graphical interface.
