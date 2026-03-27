# Agent Harness Service

The Agent Harness Service (AHS) is a standalone FastAPI server that hosts AI agents,
manages project/task lifecycles, and integrates with external tools via MCP.

For full deployment and operations guidance see [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Contents

- [Overview](#overview)
- [Project & Task Model](#project--task-model)
- [Linear Sync Integration](#linear-sync-integration)
  - [Architecture](#architecture)
  - [Status Mapping](#status-mapping)
  - [Priority Mapping](#priority-mapping)
  - [Importing from Linear](#importing-from-linear)
  - [Exporting to Linear](#exporting-to-linear)
  - [Bidirectional Sync](#bidirectional-sync)
  - [Conflict Resolution Strategies](#conflict-resolution-strategies)
  - [MCP Tools](#mcp-tools)
  - [Data Persistence](#data-persistence)
  - [Limitations & Known Caveats](#limitations--known-caveats)

---

## Overview

AHS orchestrates AI agent sessions, scheduling, and project work.  It exposes:

- **Session API** — create/resume agent sessions, send messages, collect feedback
- **Scheduler** — claim and execute tasks from a shared PostgreSQL task queue
- **MCP Server** — tools available to agents running inside sessions (write access, PR creation, Slack, etc.)
- **Gateway Registry** — route outbound replies back to Slack / other surfaces

---

## Project & Task Model

AHS uses a lightweight project/task hierarchy:

| Concept | Description |
|---------|-------------|
| `AgentProject` | A collection of related tasks with shared state |
| `AgentTask` | A unit of work with a status, priority, and optional parent/dependency links |
| `project_data` | JSON blob on the project — arbitrary metadata (including Linear refs) |
| `task_data` | JSON blob on each task — arbitrary metadata (including Linear issue refs) |

Task statuses follow this lifecycle:

```
PENDING → READY → IN_PROGRESS → COMPLETED
                             ↘ FAILED
         ↑                   ↘ CANCELLED
         BLOCKED
```

---

## Linear Sync Integration

The `linear_sync` sub-package (`ypl/agent_harness_service/tools/linear_sync/`) provides
bidirectional synchronisation between [Linear](https://linear.app) and AHS.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   AHS Project/Tasks                      │
│  AgentProject                    AgentTask               │
│  ├─ project_data                 ├─ task_data            │
│  │   └─ LinearProjectRef         │   └─ LinearIssueRef   │
│  └─ ...                          └─ ...                  │
└──────────────┬──────────────────────────┬────────────────┘
               │                          │
     import_project_from_linear    export_project_to_linear
     sync_tasks_from_linear        sync_tasks_to_linear
               │                          │
               └──────────┬───────────────┘
                           │
                   sync_bidirectional
                           │
                           ▼
              ┌────────────────────────┐
              │  Linear MCP Tools      │
              │  (mcp__claude_ai_Linear │
              │   __list_issues etc.)  │
              └────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │     Linear API         │
              │  (project + issues)    │
              └────────────────────────┘
```

The package does **not** make direct HTTP calls to the Linear API.  All
communication goes through the `mcp__claude_ai_Linear__*` MCP tool suite.

---

### Status Mapping

AHS task statuses are mapped to Linear workflow-state **types** (not specific
state names, since teams customise their states).  The mapping functions
search a team's workflow states for the first state of the required type.

**AHS → Linear**

| AHS Status | → Linear State Type |
|------------|---------------------|
| `READY` | `unstarted` |
| `IN_PROGRESS` | `started` |
| `COMPLETED` | `completed` |
| `FAILED` | `cancelled` |
| `CANCELLED` | `cancelled` |
| `BLOCKED` | `started` |

> **Note:** `BLOCKED` maps to `started` because Linear has no native "blocked"
> state type.  The `PENDING` status has no Linear equivalent and is not
> exported.

**Linear → AHS**

| Linear State Type | → AHS Status |
|-------------------|--------------|
| `triage` | `READY` |
| `backlog` | `READY` |
| `unstarted` | `READY` |
| `started` | `IN_PROGRESS` |
| `completed` | `COMPLETED` |
| `cancelled` | `CANCELLED` |

**Mapping helper functions:**

```python
from ypl.agent_harness_service.tools.linear_sync import (
    map_ahs_status_to_linear,   # AHS status + team states → Linear state ID
    map_linear_status_to_ahs,   # Linear state dict → AHS status string
)

# Convert AHS → Linear (requires the team's workflow state list)
state_id = map_ahs_status_to_linear("IN_PROGRESS", team_states)

# Convert Linear → AHS (from an issue's state dict)
ahs_status = map_linear_status_to_ahs({"type": "completed", "name": "Done"})
# → "COMPLETED"
```

---

### Priority Mapping

Linear uses integers 0–4; AHS uses string labels.

**AHS → Linear**

| AHS Priority | → Linear Priority |
|--------------|-------------------|
| `URGENT` | `1` (Urgent) |
| `HIGH` | `2` (High) |
| `MEDIUM` | `3` (Medium) |
| `LOW` | `4` (Low) |
| `NO_PRIORITY` | `0` (No Priority) |

**Linear → AHS**

| Linear Priority | → AHS Priority |
|-----------------|----------------|
| `0` | `NO_PRIORITY` |
| `1` | `URGENT` |
| `2` | `HIGH` |
| `3` | `MEDIUM` |
| `4` | `LOW` |

**Mapping helper functions:**

```python
from ypl.agent_harness_service.tools.linear_sync import (
    map_ahs_priority_to_linear,   # AHS priority string → Linear integer
    map_linear_priority_to_ahs,   # Linear integer → AHS priority string
)

linear_priority = map_ahs_priority_to_linear("HIGH")   # → 2
ahs_priority    = map_linear_priority_to_ahs(3)        # → "MEDIUM"
```

---

### Importing from Linear

Use `import_project_from_linear` to create a new AHS project from an existing
Linear project.

```python
from ypl.agent_harness_service.tools.linear_sync import import_project_from_linear

project, result = await import_project_from_linear(
    linear_project_id="proj_abc123",   # Linear project UUID
    linear_team_id="team_xyz",         # Linear team UUID
    creator_user_id="usr_me",          # AHS user who will own the project
    include_completed=False,           # skip completed/cancelled issues
)

print(f"Created AHS project: {project.name}")
print(f"Tasks: {result.created} created, {result.errors} errors")
```

**What it does:**

1. Fetches all open issues in the Linear project via `mcp__claude_ai_Linear__list_issues`
2. Sorts them topologically (parents before children, blockers before blocked)
3. Creates an `AgentProject` with `project_data` containing a `LinearProjectRef`
4. Creates an `AgentTask` for each issue, storing a `LinearIssueRef` in `task_data`
5. Preserves subtask hierarchy (`parentId` → `parent_task_id`)
6. Preserves dependency links (`blockedBy` relations → `depends_on`)

For incremental updates after the initial import:

```python
from ypl.agent_harness_service.tools.linear_sync import sync_tasks_from_linear
from datetime import datetime, timezone

result = await sync_tasks_from_linear(
    project_id="ahs-proj-abc",
    since=datetime(2026, 3, 1, tzinfo=timezone.utc),  # or None to use stored sync time
)
```

---

### Exporting to Linear

Use `export_project_to_linear` to push an AHS project to Linear.

```python
from ypl.agent_harness_service.tools.linear_sync import export_project_to_linear

linear_project_id, result = await export_project_to_linear(
    project_id="ahs-proj-abc",              # AHS project UUID
    linear_team_id="team_xyz",              # Linear team to own the project
    linear_project_name="Q3 Backend Work",  # optional override (default: AHS title)
)

print(f"Linear project: {linear_project_id}")
print(f"Issues: {result.created} created, {result.updated} updated, {result.errors} errors")
```

**What it does:**

1. Fetches the AHS project and all its tasks
2. Creates (or updates) the Linear project via `mcp__claude_ai_Linear__save_project`
3. Creates issues in topological order, setting `parentId` and `blockedBy`
4. Writes `LinearProjectRef` / `LinearIssueRef` back to AHS for future syncs
5. Is idempotent — re-exporting updates existing issues rather than creating duplicates

For pushing incremental updates:

```python
from ypl.agent_harness_service.tools.linear_sync import sync_tasks_to_linear

result = await sync_tasks_to_linear(project_id="ahs-proj-abc")
```

---

### Bidirectional Sync

Use `sync_bidirectional` to reconcile both sides after changes have been made
in either system.

```python
from ypl.agent_harness_service.tools.linear_sync import ConflictResolution, sync_bidirectional

result = await sync_bidirectional(
    project_id="ahs-proj-abc",
    conflict_resolution=ConflictResolution.LATEST_WINS,
)

print(f"Updated: {result.updated}, Created: {result.created}, Errors: {result.errors}")
```

**Prerequisites:** the project must have already been linked via either
`import_project_from_linear` or `export_project_to_linear`.

---

### Conflict Resolution Strategies

A *conflict* occurs when both the AHS task and its linked Linear issue have been
modified since the last sync.

| Strategy | Enum Value | Behaviour |
|----------|------------|-----------|
| **Latest wins** | `ConflictResolution.LATEST_WINS` | Compares `updated_at` (AHS) vs `updatedAt` (Linear); most recently modified side wins.  **Default.** |
| **Linear wins** | `ConflictResolution.LINEAR_WINS` | Always applies the Linear state.  Use when Linear is the system of record (e.g. a PM-driven project). |
| **AHS wins** | `ConflictResolution.AHS_WINS` | Always applies the AHS state.  Use when AHS agents are the authoritative source (e.g. fully automated pipelines). |
| **Skip** | `ConflictResolution.SKIP` | Leaves both sides unchanged.  Useful for dry runs or when manual resolution is preferred. |

**Choosing a strategy:**

- **Collaborative workflows** (humans in Linear, agents in AHS) → `LATEST_WINS`
- **Linear as source of truth** (PM-managed, agents just execute) → `LINEAR_WINS`
- **Fully automated pipeline** (agents drive everything, Linear is read-only display) → `AHS_WINS`
- **Audit / dry run** (want to see conflicts without resolving them) → `SKIP`

---

### MCP Tools

The sync operations are exposed as MCP tools via
`ypl/mcp_server/tools/linear_sync.py`.  These tools are available to agents
running in AHS sessions.

| Tool | Description |
|------|-------------|
| `import_project_from_linear` | Create an AHS project by importing from Linear |
| `link_project_to_linear` | Link an existing AHS project to an existing Linear project without syncing data |
| `export_project_to_linear` | Push an AHS project + tasks to Linear |
| `push_task_status_to_linear` | Push a single AHS task status update to Linear immediately |
| `sync_project_with_linear` | Full bidirectional sync (wraps `sync_bidirectional`) |

**Example tool call (from within an agent session):**

```python
# Import a Linear project into AHS
result = await mcp.call_tool(
    "import_project_from_linear",
    {
        "linear_project_id": "proj_abc123",
        "linear_team_id": "team_xyz",
        "include_completed": False,
    },
)

# Or push a quick status update
await mcp.call_tool(
    "push_task_status_to_linear",
    {"task_id": "task-uuid-here"},
)
```

---

### Data Persistence

The sync layer persists its state entirely within AHS's existing data model —
no additional tables are needed.

Linear sync metadata is stored under a nested `linear_ref` key:

| AHS Field | Stored Value | Description |
|-----------|-------------|-------------|
| `AgentProject.project_data["linear_ref"]["linear_project_id"]` | Linear project UUID | Links AHS project to Linear |
| `AgentProject.project_data["linear_ref"]["linear_team_id"]` | Linear team UUID | Used for state lookup during export |
| `AgentProject.project_data["linear_ref"]["last_synced_at"]` | ISO-8601 timestamp | Last successful project sync time |
| `AgentTask.task_data["linear_ref"]["linear_issue_id"]` | Linear issue UUID | Links AHS task to Linear issue |
| `AgentTask.task_data["linear_ref"]["linear_identifier"]` | e.g. `"ENG-123"` | Human-readable Linear issue ID |
| `AgentTask.task_data["linear_ref"]["last_synced_at"]` | ISO-8601 timestamp | Per-task sync timestamp for conflict detection |

**Pydantic models** for these fields live in `linear_sync/types.py`:

```python
from ypl.agent_harness_service.tools.linear_sync import LinearProjectRef, LinearIssueRef, SyncResult

ref = LinearProjectRef(
    linear_project_id="proj_abc123",
    linear_team_id="team_xyz",
    last_synced_at=datetime.now(timezone.utc),
)

issue_ref = LinearIssueRef(
    linear_issue_id="issue-uuid",
    linear_identifier="ENG-123",
    last_synced_at=datetime.now(timezone.utc),
)
```

---

### Limitations & Known Caveats

| Limitation | Detail |
|------------|--------|
| **No direct HTTP calls** | All Linear operations go through the `mcp__claude_ai_Linear__*` MCP tools.  The integration requires an active MCP session with Linear credentials configured. |
| **Workflow state matching** | Status mapping finds the *first* Linear state of the required type; teams with multiple states of the same type (e.g. two "started" states) will always get the first one. |
| **`BLOCKED` status** | There is no direct Linear equivalent; `BLOCKED` maps to `started`.  The block reason is not preserved in Linear. |
| **`PENDING` status** | AHS `PENDING` tasks are not exported to Linear.  Transition them to `READY` before exporting. |
| **Issue deletion** | If a Linear issue is deleted between syncs, the corresponding AHS task is *skipped* during bidirectional sync (logged as a warning). |
| **Attachment / comments** | Issue comments and file attachments are not synced — only title, description, status, priority, parent, and dependency relations. |
| **Rate limits** | Bulk imports of large projects (>100 issues) may hit Linear API rate limits.  The MCP layer handles this transparently via retries, but imports may be slow. |
