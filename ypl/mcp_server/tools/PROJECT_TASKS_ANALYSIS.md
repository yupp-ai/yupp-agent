# Project & Task MCP Tools — Agent Recipe Book

Reference for AI agents using project/task tools to plan, execute, and coordinate
long-running work across multiple sessions. Written as concrete recipes with
exact tool call sequences.

---

## Tool Inventory (15 tools)

### Project Lifecycle

| Tool | Auth | DB | Purpose |
|------|------|----|---------|
| `add_project(name, description?, slack_channel?)` | write | primary | Create a project |
| `update_project(project_id, name?, description?, slack_channel?)` | write | primary | Modify project metadata |
| `list_projects(status?, limit?)` | read | replica | Discover projects (newest first) |
| `get_project(project_id?, name?)` | read | replica | Lookup by ID or exact name |
| `set_project_status(project_id, status)` | write | primary | ACTIVE / PAUSED / COMPLETED / ARCHIVED |

### Task Lifecycle

| Tool | Auth | DB | Purpose |
|------|------|----|---------|
| `add_task_sequence(project_id, tasks_json, parent_task_id?)` | write | primary | Linear chain: A→B→C |
| `add_tasks(project_id, tasks_json)` | write | primary | Arbitrary DAG with named deps |
| `update_task(task_id, title?, description?, priority?, agent_name?, task_data?, estimated_effort?)` | write | primary | Modify task fields (not status) |
| `get_task(task_id?, title?, project_id?)` | read | replica | Lookup by ID or title |
| `get_project_tasks(project_id, status?)` | read | replica | All tasks, optional status filter |
| `get_ready_tasks(project_id)` | — | primary | Ready tasks + auto-promote eligible blocked tasks |
| `claim_task(project_id, task_id?)` | write | primary | Atomic READY→IN_PROGRESS (row lock) |
| `set_task_status(task_id, status, result?)` | write | primary | Status transition + cascade |

### Cross-Session State

| Tool | Auth | DB | Purpose |
|------|------|----|---------|
| `get_project_state(project_id, key?)` | read | replica | Read shared_state (full or single key) |
| `set_project_state(project_id, key, value_json)` | write | primary | Write one key to shared_state |

---

## Status Machines

### Project Status

```
ACTIVE ──→ PAUSED ──→ ACTIVE    (pause/resume)
ACTIVE ──→ COMPLETED            (all done)
ACTIVE ──→ ARCHIVED             (abandoned)
PAUSED ──→ ARCHIVED
COMPLETED ──→ ARCHIVED
```

### Task Status

```
        ┌──────────────────────────────────────┐
        │              CANCELLED ←─────────────┤
        │                 │                     │
        │                 ↓                     │
        │              PENDING ←── FAILED       │
        │              ↗    ↘        ↑         │
        │         BLOCKED   READY    │         │
        │                     ↓      │         │
        │               IN_PROGRESS ─┤         │
        │                ↓    ↓      ↓         │
        │            COMPLETED  FAILED  ───────┘
        │                ↑
        │          (terminal — no outbound transitions)
        └──────────────────────────────────────┘
```

Allowed transitions:
```
PENDING      → BLOCKED, READY, CANCELLED
BLOCKED      → READY, PENDING, CANCELLED
READY        → IN_PROGRESS, CANCELLED, BLOCKED
IN_PROGRESS  → COMPLETED, FAILED, CANCELLED, READY (stale recovery)
COMPLETED    → (none — terminal)
FAILED       → PENDING, READY
CANCELLED    → PENDING
```

Key behaviors:
- **COMPLETED cascade**: When a task completes, all dependents whose deps are fully met auto-promote to READY.
- **`completed_at`**: Set on COMPLETED/FAILED/CANCELLED, cleared on reopen.
- **IN_PROGRESS→READY**: Allowed for stale task recovery (crashed session).

---

## Recipe 1: Session Bootstrap (Every New Session)

Every agent session working on a project should start with this sequence.

```
# Step 1: Find the project
list_projects(status="ACTIVE")
# — or if you know the name —
get_project(name="My Project")

# Step 2: Get full picture
get_project_tasks(project_id=P)
# → See task_summary: {PENDING: 2, BLOCKED: 3, READY: 1, IN_PROGRESS: 0, ...}

# Step 3: Check for stale tasks (crashed previous session)
get_project_tasks(project_id=P, status="IN_PROGRESS")
# → If any found and look stale, recover:
set_task_status(task_id=T_stale, status="READY")

# Step 4: Read any shared state from prior sessions
get_project_state(project_id=P)

# Step 5: Claim work
claim_task(project_id=P)
# → Returns the highest-priority READY task, atomically set to IN_PROGRESS
```

---

## Recipe 2: Linear Pipeline (Sequential Tasks)

Use `add_task_sequence` when tasks must run one after another.

**Scenario**: Deploy a service — build, test, stage, production.

```
# Create project
add_project(
  name="Deploy auth-service v2.1",
  description="Build, test, deploy auth-service v2.1. Done when production is healthy.",
  slack_channel="C-deploys"
)
# → {project_id: "P"}

# Add sequential pipeline
add_task_sequence(
  project_id="P",
  tasks='[
    {"title": "Build and run unit tests", "priority": "HIGH"},
    {"title": "Deploy to staging", "priority": "HIGH"},
    {"title": "Run integration tests on staging"},
    {"title": "Deploy to production", "priority": "URGENT"},
    {"title": "Verify production health", "priority": "URGENT"}
  ]'
)
# → Task 1 is READY, tasks 2-5 are BLOCKED

# Session claims and executes
claim_task(project_id="P")
# → "Build and run unit tests" (IN_PROGRESS)
# ... do work ...
set_task_status(task_id="T1", status="COMPLETED", result='{"tests_passed": 142, "coverage": "87%"}')
# → "Deploy to staging" auto-promoted to READY

# Next session (or same session if has capacity):
claim_task(project_id="P")
# → "Deploy to staging" (IN_PROGRESS)
```

---

## Recipe 3: Fan-Out / Fan-In (Parallel Investigation)

Use `add_tasks` with named dependencies for DAGs.

**Scenario**: Investigate a production incident from multiple angles, then synthesize.

```
add_project(
  name="Incident-2026-03-08 /api/chat 500s",
  description="Root-cause 500 errors on /api/chat. Done when fix PR is merged.",
  slack_channel="C-oncall"
)

add_tasks(
  project_id="P",
  tasks='[
    {"name": "logs",    "title": "Search GCP logs for error pattern",     "agent_name": "sre"},
    {"name": "sentry",  "title": "Analyze Sentry breadcrumbs and traces", "agent_name": "sre"},
    {"name": "deploys", "title": "Check recent deploys for regression",   "agent_name": "sre"},
    {"name": "synth",   "title": "Synthesize findings and propose fix",   "agent_name": "sre",
     "depends_on": ["logs", "sentry", "deploys"]}
  ]'
)
# → logs, sentry, deploys are READY (no deps)
# → synth is BLOCKED (3 deps)

# Three agents can claim concurrently (row-level locking prevents double-claim):
claim_task(project_id="P")  # Agent A → "logs"
claim_task(project_id="P")  # Agent B → "sentry"
claim_task(project_id="P")  # Agent C → "deploys"

# Each completes with structured results:
set_task_status(task_id="T_logs", status="COMPLETED",
  result='{"error_pattern": "NullPointerException in ChatRouter.route()", "first_seen": "14:30 UTC", "count": 342}')

# When all 3 complete, "synth" auto-promotes to READY
# Synth agent reads completed task results:
get_task(task_id="T_logs")     # → includes result field
get_task(task_id="T_sentry")
get_task(task_id="T_deploys")
```

---

## Recipe 4: Self-Decomposition (Agent Discovers Sub-Tasks)

Agent picks up a broad task, investigates, then breaks it into sub-tasks.

```
# Agent claims broad task
claim_task(project_id="P")
# → "Optimize slow database queries" (IN_PROGRESS)

# Agent investigates, finds 3 specific issues, creates sub-tasks:
add_tasks(
  project_id="P",
  tasks='[
    {"name": "fix-n-plus-1",   "title": "Fix N+1 in user_feed query",    "parent_task_id": "T_parent"},
    {"name": "add-index",      "title": "Add composite index on turns",  "parent_task_id": "T_parent"},
    {"name": "cache-rankings", "title": "Cache leaderboard rankings",    "parent_task_id": "T_parent",
     "depends_on": ["add-index"]}
  ]'
)

# Agent records what it discovered in shared state
set_project_state(project_id="P", key="db_investigation",
  value='{"slow_queries_found": 3, "estimated_latency_improvement": "40%"}')

# Agent works on sub-tasks, then closes parent when all children complete:
get_project_tasks(project_id="P")
# → check all sub-tasks of T_parent are COMPLETED
set_task_status(task_id="T_parent", status="COMPLETED",
  result='{"sub_tasks_completed": 3, "total_improvement": "40% latency reduction"}')
```

---

## Recipe 5: Multi-Session State Coordination

Use `shared_state` when sessions need to share structured data beyond task results.

**Scenario**: Weekly PR review sweep — each session reviews a batch, tracks which PRs are done.

```
# Session 1: Create project and seed state
add_project(name="Weekly PR Review Sweep 2026-W10")
set_project_state(project_id="P", key="reviewed_prs", value="[]")
set_project_state(project_id="P", key="total_prs_at_start", value="15")

# Session 1: Create initial tasks
add_tasks(project_id="P", tasks='[
  {"name": "pr-100", "title": "Review PR #100"},
  {"name": "pr-101", "title": "Review PR #101"},
  {"name": "pr-102", "title": "Review PR #102"}
]')

# Session 1: Claims and reviews PR #100
claim_task(project_id="P")
# ... reviews ...
set_task_status(task_id="T_100", status="COMPLETED", result='{"verdict": "approved", "comments": 2}')
set_project_state(project_id="P", key="reviewed_prs", value="[100]")

# Session 2 (next day): Resume
get_project_state(project_id="P")
# → {"reviewed_prs": [100], "total_prs_at_start": 15}
# Agent sees PR #100 is done, adds new PRs that appeared:
add_tasks(project_id="P", tasks='[
  {"name": "pr-105", "title": "Review PR #105"},
  {"name": "pr-106", "title": "Review PR #106"}
]')

# Session 2: Claims next task
claim_task(project_id="P")
# → "Review PR #101"
# ... reviews ...
set_task_status(task_id="T_101", status="COMPLETED", result='{"verdict": "changes_requested", "comments": 5}')
set_project_state(project_id="P", key="reviewed_prs", value="[100, 101]")
```

---

## Recipe 6: Retry with Updated Instructions

When a task fails, update its description before retrying.

```
# Task failed
set_task_status(task_id="T", status="FAILED",
  result='{"error": "tailwind config conflict", "attempted": "modified tailwind.config.js directly"}')

# Update instructions for next attempt
update_task(task_id="T",
  description="Use CSS custom properties instead of Tailwind classes. Previous attempt failed due to tailwind config conflict — do NOT modify tailwind.config.js.",
  task_data='{"retry_count": 1, "previous_error": "tailwind config conflict"}'
)

# Reopen for retry
set_task_status(task_id="T", status="READY")
# → completed_at is cleared, task is back in the READY queue
```

---

## Recipe 7: Cron-Driven Project Executor

Use `create_recurring_agent_schedule` to periodically resume a project.

```
# Create project
add_project(name="Nightly data pipeline validation")
add_task_sequence(project_id="P", tasks='[...]')

# Schedule recurring execution
create_recurring_agent_schedule(
  agent_name="sre",
  message="Resume project 'Nightly data pipeline validation'. Call list_projects(status='ACTIVE') to find it, then claim_task and execute the next ready task. If all tasks are done, set_project_status to COMPLETED and cancel this schedule.",
  cron_expression="0 */4 * * *",
  timezone="America/Los_Angeles",
  name="pipeline-validator",
  description="Every 4 hours, check for ready tasks in the pipeline validation project"
)
```

Agent session (invoked by cron):
```
# 1. Find project
list_projects(status="ACTIVE")

# 2. Check for work
get_ready_tasks(project_id="P")

# 3a. If ready tasks exist:
claim_task(project_id="P")
# ... execute ...
set_task_status(task_id="T", status="COMPLETED", result='...')

# 3b. If no ready tasks, check if all done:
get_project(project_id="P")
# → task_summary: {COMPLETED: 10, total: 10}
set_project_status(project_id="P", status="COMPLETED")
cancel_agent_schedule(agent_schedule_id="SCHED_ID")
```

---

## Recipe 8: Human-AI Collaboration

Human creates/reviews via REST (future), AI executes via MCP.

```
# Human creates project and tasks in REST UI
# AI agent starts session:

list_projects(status="ACTIVE")
# → finds "Add dark mode" project

get_project_tasks(project_id="P")
# → sees tasks with human-written descriptions

claim_task(project_id="P")
# → "Update CSS variables" (IN_PROGRESS)

# Agent works... but gets stuck
set_task_status(task_id="T1", status="FAILED",
  result='{"blocker": "Need design tokens from Figma — cannot proceed without them", "question": "What are the dark mode color values?"}')

# Human sees failure in REST UI, updates task with answers:
# (REST) PATCH /tasks/T1 {description: "Use these tokens: --bg-dark: #1a1a2e, --text-dark: #eee ..."}
# (REST) PATCH /tasks/T1 {status: "READY"}

# Next AI session picks it up:
claim_task(project_id="P")
# → "Update CSS variables" again, now with design tokens in description
```

---

## Recipe 9: Stale Task Recovery

A previous session crashed, leaving a task stuck IN_PROGRESS.

```
# New session starts
list_projects(status="ACTIVE")
get_project_tasks(project_id="P", status="IN_PROGRESS")
# → T_stale: "Migrate OpenAI provider", IN_PROGRESS since 6 hours ago

# Check if it looks stale (no recent activity, old created_at)
# Decision: reclaim it
set_task_status(task_id="T_stale", status="READY")
# → completed_at cleared, task back in queue

# Now claim it fresh
claim_task(project_id="P")
# → T_stale is now yours
```

---

## Recipe 10: Dynamic Priority Escalation

Mid-project, a task becomes urgent.

```
# Original task was NORMAL priority
update_task(task_id="T5", priority="URGENT")

# Now when any agent calls claim_task, T5 will be returned first
# (claim_task orders by priority ASC: URGENT=1, HIGH=2, NORMAL=3, LOW=4)
```

---

## Recipe 11: Agent Reassignment

Reassign a task to a different agent mid-project.

```
# Task was assigned to "sre" but needs "code-reviewer" instead
update_task(task_id="T", agent_name="code-reviewer")

# Or unassign (let any agent claim it):
update_task(task_id="T", agent_name="")
```

---

## Anti-Patterns (Don't Do This)

### 1. Don't use `set_task_status(IN_PROGRESS)` — use `claim_task`

```
# BAD: race condition if two agents call simultaneously
get_ready_tasks(project_id="P")
set_task_status(task_id="T", status="IN_PROGRESS")

# GOOD: atomic with row-level lock
claim_task(project_id="P", task_id="T")
```

### 2. Don't store coordination data in agent memory — use `shared_state`

```
# BAD: save_memory defaults to scope="agent" (per-agent), not project-scoped
save_memory(topic="migration-progress", content="...")

# GOOD: project-scoped, visible to all sessions
set_project_state(project_id="P", key="migration_progress", value='{"completed": ["openai", "anthropic"]}')
```

### 3. Don't poll `get_project_tasks` in a loop — use `get_ready_tasks`

```
# BAD: manual check for promotable tasks
while True:
  tasks = get_project_tasks(project_id="P", status="BLOCKED")
  for t in tasks: check_deps_manually(t)

# GOOD: auto-promotion built in
get_ready_tasks(project_id="P")
# → Automatically promotes eligible BLOCKED/PENDING → READY
```

### 4. Don't create tasks with external dep UUIDs you haven't verified

```
# BAD: will fail at creation time if dep doesn't exist in project
add_tasks(project_id="P", tasks='[{"name": "x", "title": "X", "depends_on": ["nonexistent-uuid"]}]')

# GOOD: add_tasks validates external UUIDs exist in the project
# Just let it validate — the error message is clear
```

### 5. Don't forget to close the parent task

```
# BAD: parent stays IN_PROGRESS forever after sub-tasks complete
add_tasks(project_id="P", tasks='[{"name":"sub1", "title":"...", "parent_task_id":"T_parent"}]')
# ... sub-tasks complete ... parent forgotten

# GOOD: check and close parent explicitly
get_project_tasks(project_id="P")
# → verify all children of T_parent are COMPLETED
set_task_status(task_id="T_parent", status="COMPLETED")
```

---

## Response Shapes

### Task object (from claim_task, get_task, update_task, get_ready_tasks, get_project_tasks)

```json
{
  "agent_task_id": "uuid",
  "agent_project_id": "uuid",
  "title": "string",
  "description": "string | null",
  "status": "PENDING | BLOCKED | READY | IN_PROGRESS | COMPLETED | FAILED | CANCELLED",
  "priority": "URGENT | HIGH | NORMAL | LOW",
  "parent_task_id": "uuid | null",
  "depends_on": ["uuid", "..."] | null,
  "agent_name": "string | null",
  "result": {} | null,
  "task_data": {} | null,
  "estimated_effort": "string | null",
  "completed_at": "ISO datetime | null",
  "created_at": "ISO datetime"
}
```

### Project object (from add_project, get_project, update_project, list_projects)

```json
{
  "agent_project_id": "uuid",
  "name": "string",
  "description": "string | null",
  "status": "ACTIVE | PAUSED | COMPLETED | ARCHIVED",
  "slack_channel": "string | null",
  "created_at": "ISO datetime",
  "task_summary": {
    "PENDING": 0, "BLOCKED": 3, "READY": 1,
    "IN_PROGRESS": 1, "COMPLETED": 5, "FAILED": 0, "CANCELLED": 0,
    "total": 10
  }
}
```

---

## MCP Parameter Constraints

MCP tools only accept flat/primitive parameters (str, int, bool). Complex objects must be passed as JSON strings:

- `tasks` parameter → JSON array string: `'[{"title": "...", "depends_on": ["..."]}]'`
- `task_data` parameter → JSON string: `'{"model_id": "gpt-4", "config": {...}}'`
- `result` parameter → JSON string: `'{"output": "...", "metrics": {...}}'`
- `value` in `set_project_state` → JSON string: `'"simple string"'` or `'{"complex": "object"}'`

---

## Integration with Other MCP Tools

| Tool | How it complements projects/tasks |
|------|----------------------------------|
| `create_agent_schedule` | Schedule a future session to resume a project |
| `create_recurring_agent_schedule` | Periodic project executor (cron-driven) |
| `cancel_agent_schedule` | Stop recurring execution when project completes |
| `save_memory` / `search_memory` | Per-agent learnings (use `shared_state` for project-scoped data) |
| `add_artifact` | Store large investigation results as TEXT artifacts, link URL in task `result` |
| `search_slack` / `read_slack_thread` | Reference Slack context in task descriptions |
| `query_bigquery` / `query_appdb` | Data tasks, store query results in task `result` |
| `search_gcp_logs` / `get_sentry_issue_details` | Investigation tasks, store findings in task `result` |

---

## Remaining Gaps (Future Work)

These are known limitations that don't block current usage but could be added later:

| Gap | Workaround |
|-----|-----------|
| No auto-complete parent when all children finish | Agent checks manually and closes parent |
| No `get_subtasks(parent_task_id)` filter | Use `get_project_tasks` and filter client-side by `parent_task_id` |
| No pagination on `get_project_tasks` | Fine for projects with <200 tasks; large projects need future pagination |
| No budget tracking via tools | `budget_usd`/`budget_spent_usd` exist in DB but aren't exposed |
| No bulk status update | Agent loops `set_task_status` for each task |
| No task deletion (only CANCELLED) | CANCELLED is soft-delete semantics; hard delete not needed |
| No project-level default_agent_id via tools | Set agent per-task instead |

---

*Generated 2026-03-08 for PR #10887. 15 tools, 11 recipes, 5 anti-patterns.*
