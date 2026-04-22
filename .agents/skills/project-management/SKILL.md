---
name: project-management
description: Create and manage agent projects and tasks. Helps decompose work from discussions or design docs into structured projects with dependency-aware tasks, track progress, and manage lifecycle transitions.
allowed-tools:
---

# Project Management

Help users create, decompose, track, and manage agent projects and tasks using the project/task MCP tools.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `add_project` | Create a new project |
| `update_project` | Update project name, description, slack_channel |
| `set_project_status` | Change project status (ACTIVE/PAUSED/COMPLETED/ARCHIVED) |
| `get_project` | Look up project by ID or name |
| `list_projects` | List projects with optional status filter |
| `add_tasks` | Add tasks with arbitrary dependency graph |
| `add_task_sequence` | Add a linear chain of dependent tasks |
| `update_task` | Update task fields (title, description, priority, agent, task_data) |
| `set_task_status` | Transition task status + store result on completion |
| `restart_task` | Reset a task for re-execution (clears result, spending, sessions) |
| `get_task` | Look up task by ID or title |
| `get_project_tasks` | List all tasks in a project (with optional status filter) |
| `get_ready_tasks` | Get tasks ready for execution (auto-promotes eligible blocked tasks) |
| `claim_task` | Atomically claim a READY task (used by scheduler, not this skill) |
| `get_project_state` | Read shared project key-value state |
| `set_project_state` | Write to shared project key-value state |
| `list_ahs_agents` | List available agents (for agent assignment) |

---

## Confirm Before Writing — Hard Rule

**NEVER call a write/mutate MCP tool without explicit user approval.** Read-only tools (`get_project`, `get_task`, `get_project_tasks`, `list_projects`, `get_ready_tasks`, `get_project_state`, `list_ahs_agents`) can be called freely. Any tool that creates or modifies data requires presenting a plan first.

Write/mutate tools that require approval:
`add_project`, `add_tasks`, `add_task_sequence`, `update_project`, `update_task`, `set_project_status`, `set_task_status`, `set_project_state`

Before creating a project, adding tasks, or modifying existing data — present exactly what will be written and ask for confirmation. See each scenario below for the specific format. This rule applies because MCP writes go directly to the production database with no undo.

---

## Scenario 1: Create a Project from a Discussion or Document

When the user shares a discussion (Slack thread, meeting notes) or a design doc and asks to create a project, follow these steps.

### Step 1: Extract Project Identity

Read the source material and identify:
- **Project name**: Short, descriptive (e.g. "Q3 model migration", "Fix routing latency")
- **Description**: 1-3 sentences covering the goal and what "done" looks like
- **Slack channel**: Used for progress updates and human confirmations. Defaults to `agentic-projects` — you usually don't need to specify this. If the user wants a different channel, prefer a plain channel name (channel IDs are also accepted). The corresponding Slack bot must already be a member of the chosen channel — remind the user to verify this.
- **Default agent**: Required for task execution. The scheduler uses this agent for any task that doesn't have its own `agent_name` set. Always ask: "Which agent should execute tasks in this project? (default: sre)". If the user is unsure, call `list_ahs_agents` to show available agents. If the user doesn't specify, use `sre`.

Before creating, check for duplicates using `list_projects` or `get_project(name=...)`.

Present the project identity to the user for confirmation:
```
I'd like to create the following project:
  Name: <project name>
  Description: <description>
  Slack channel: <channel> (default: agentic-projects)
  Default agent: <agent> (default: sre)

Note: The project will be created in PAUSED status. Set it to ACTIVE when you're ready to start execution.
Please verify the Slack bot is a member of the channel above.

Shall I go ahead?
```

### Step 2: Decompose into Tasks

Break the work into tasks following these principles:

**Structure rules:**
- Aim for a flat list with dependencies, or at most 2 levels (parent tasks as organizational groups only)
- Target 3-12 tasks per project. Fewer than 3 means the project is too simple for this system. More than 12 means you should split into multiple projects or use parent tasks to group phases.
- If a task feels too big, split it. If it feels trivial, merge it with its neighbor.

**Naming and stage markers:**
- Every task title must include a stage marker indicating its position in the project: `[1]`, `[2]`, `[3]`, etc.
- For subtasks within a stage, use dot notation: `[1.1]`, `[1.2]`, etc.
- The user may request a different marker format — follow their preference
- The marker goes at the start of the title: `[1] Research current routing config`, `[2.1] Implement read path`
- These markers must be used consistently across all artifacts the task produces:
  - **PR titles**: `[Q3 migration][2] Implement migration script`
  - **PR descriptions**: Include the project name, task name, and stage marker
  - **Branch names**: Include the marker where practical (e.g. `tw/q3-migration-2-impl-script`)
  - **Documents/artifact**: Include the marker in the title (e.g. `[Q3 migration][1] Routing analysis findings`)
  - **Linear tickets**: Include the marker in the title if a ticket is created for the task
- The purpose is traceability — anyone seeing a PR or document should immediately know which project and stage it belongs to

**Task sizing rules:**
- Every task is executed in a single agent session — one session, one task, start to finish
- If a task fails, it may be retried in a new session (the new session starts fresh with no prior context)
- If you find yourself thinking "this task needs to do X, then Y, then Z" and each is substantial — those are three tasks, not one
- Each task should produce **one recognizable deliverable** that a human can review or verify:
  - **Coding task** → one PR (at most). If it needs multiple PRs, split into multiple tasks.
  - **Investigation/research task** → one analysis document or summary (uploaded to online storage for anything non-trivial)
  - **Data task** → one table, dataset, or query result
  - **Config/infra task** → one config change, one deployment, one migration
  - **Design task** → one design doc or schema proposal
- The deliverable should be human-consumable at a glance — if a human can't tell what the task produced in under a minute, it's too big or too vague
- For large outputs (detailed logs, full analysis reports, big datasets), upload to online storage (currently artifact) and reference the URL in the task result

**Task self-containment rules:**
- A task must be self-contained: an agent picking it up should be able to complete it using ONLY:
  1. The task's own `description` and `task_data`
  2. The `result` of dependency tasks it needs to read (retrieved via `get_task`) — only if the task description says to read them
  3. The project's `shared_state` (retrieved via `get_project_state`)
- Never assume an agent has context from previous tasks unless it's explicitly stored in results or shared state
- If two tasks need to share information, define what goes into `shared_state` or task `result`
- Not all dependencies produce outputs that downstream tasks consume — see Dependency rules below

**Dependency rules:**
- Dependencies must form a DAG (no cycles)
- A dependency means "this task must finish before mine starts." It does NOT necessarily mean the downstream task reads the upstream task's result. There are two kinds:
  - **Data dependency**: Task B needs Task A's output to do its work. The task description should say which dependency results to read.
  - **Ordering dependency**: Task B just needs Task A to be done first (e.g. "deploy" depends on "merge PR" — it doesn't read the merge result, it just needs the PR merged before deploying)
- Only add a dependency when there is a true ordering or data requirement
- Prefer independent parallel tasks over unnecessary sequential chains
- Use `add_tasks` (DAG) over `add_task_sequence` (linear chain) when tasks can run in parallel
- When creating tasks, order them by priority (URGENT → HIGH → NORMAL → LOW) within each dependency level, so that parallel tasks with higher priority appear first in the task list and their natural rank reflects execution importance

**Task description as system prompt:**
- The task `description` field is injected as an additional system prompt into the executor agent's session
- The executor agent also loads its own prompts (SOUL.md, shared prompts, etc.), so the task description should not duplicate general instructions
- Focus the description on: what specifically to do, what inputs to read, what output to produce, and any constraints specific to this task
- If the task has data dependencies, reference them explicitly: "Read the result of task 'Research phase' for the list of affected models"
- Include the artifact naming convention in the description so the executor names PRs, branches, and documents correctly: "Use prefix `[Project Name][stage marker]` for PR titles and document names"
- Don't copy-paste shared background into every task description — put shared context in the project `description` or `shared_state` and reference it
- See `ypl/agent_harness_service/deploy/shared/tasks/TASK_EXECUTION.md` for the full executor contract

**Output rules:**
- Every task description must specify what its expected output/result is
- Use this format in the description: `**Expected output:** <what the result JSON should contain>`
- Results should be lean (under 500 chars). For larger outputs, upload to online storage and store the URL.

**Priority rules:**
- URGENT — blocking other teams or time-critical
- HIGH — on the critical path of the project
- NORMAL — default for most tasks
- LOW — nice-to-have, not blocking anything

**Human checkpoint rules:**
- Tasks run automatically via the scheduler/runner without human prompting by default
- For high-risk or irreversible tasks (deploys, data migrations, major architectural decisions, external-facing changes), propose a **human checkpoint** at the start of the task
- A human checkpoint pauses execution and sends a notification to a prearranged channel (e.g. Slack) asking a human to review and approve before the task proceeds
- Mark checkpoints in the task description: `**Human checkpoint:** <what needs review and why>`
- The user has final say on which tasks get checkpoints — always ask during planning

**Agent assignment rules:**
- The project's `default_agent_name` is used for any task without an explicit `agent_name`. This is set during project creation (default: `sre`).
- Individual tasks can override the project default by setting `agent_name` explicitly.
- If the user wants different agents for different tasks, set `agent_name` per task. Otherwise, the project default covers everything.
- Different agents have different capabilities (e.g. harnessed executors with code access vs. raw executors for investigations)
- **IMPORTANT:** Tasks without an `agent_name` AND without a project `default_agent_name` will FAIL immediately when the scheduler picks them up. Always ensure at least the project default is set.

### Step 3: Present the Plan

Before creating anything, present the decomposition to the user as a table:

```
I'd like to add the following tasks to project "Q3 model migration":

| # | Task Title | Depends On | Priority | Agent | Checkpoint? | Expected Output |
|---|---|---|---|---|---|---|
| [1] | [1] Research current routing config | — | HIGH | — | no | List of models and their routing rules |
| [2] | [2] Design new routing schema | [1] | NORMAL | — | no | Schema definition + migration plan |
| [3] | [3] Implement migration script | [2] | NORMAL | agent-x | no | PR link + test results |
| [4] | [4] Deploy migration to production | [3] | HIGH | — | YES | Deployment confirmation |
| [5] | [5] Update monitoring dashboards | — | LOW | — | no | Dashboard URLs |

Tasks [1] and [5] can run in parallel (no dependency between them).
Task [4] has a human checkpoint because it's an irreversible production deploy.

PRs created by these tasks will be titled like: [Q3 model migration][3] Implement migration script

Shall I create these?
```

### Step 4: Create the Project and Tasks

After user approval:
1. Call `add_project` with the agreed name, description, and `default_agent_name` (default `sre`). The `slack_channel` defaults to `agentic-projects` — only pass it if the user wants a different channel. Prefer a plain channel name; channel IDs are also accepted.
2. Call `add_tasks` with the full task list, using `name` fields for local dependency references
3. Seed project shared state with creator context:
   - `set_project_state(project_id, "creator_slack_user_id", '"<slack_user_id>"')` — the Slack user ID of the project owner, used by executor agents for @mentions in human checkpoints
4. Report back the created project ID and task IDs
5. Remind the user: "The project is PAUSED. Run `set_project_status(project_id, 'ACTIVE')` when you're ready to start execution."

Note: Projects are created in **PAUSED** status by default. Tasks in PAUSED projects are not picked up by the scheduler. The user must explicitly set the project to **ACTIVE** to begin execution. This gives the user time to review the plan, adjust tasks, and confirm before any work starts.

Note: the `tasks` parameter to `add_tasks` is a JSON string containing an array of task objects. Each object has: `name` (local ref for deps), `title`, `description`, `depends_on` (list of names or existing UUIDs), `priority`, `agent_name`, `parent_task_id`, `task_data`.

---

## Scenario 2: Track Progress and Get Status Updates

When the user asks "where are we?" or wants a status update on a project:

### Step 1: Fetch Current State

1. Call `get_project` to get the project details and task summary counts
2. Call `get_project_tasks` to get all tasks with their statuses (no need to call `get_task` individually after this)
3. Optionally call `get_project_state` if shared state is relevant

### Step 2: Present a Status Report

Format the update clearly:

```
**Project: <name>** (status: ACTIVE)

Progress: 4/10 tasks completed

| Status | Count |
|---|---|
| COMPLETED | 4 |
| IN_PROGRESS | 1 |
| READY | 2 |
| BLOCKED | 2 |
| FAILED | 1 |
| CANCELLED | 0 |

**Failed (needs attention):**
- <task title> — error: <brief error from result>

**Currently in progress:**
- <task title> (assigned to <agent>)

**Up next (READY):**
- <task title> — <one-line description>
- <task title> — <one-line description>

**Blocked on:**
- <task title> — waiting on: <dependency task titles>
```

Omit sections with zero items. Surface FAILED tasks first — they need the most attention.

### Step 3: Suggest Next Actions

Based on the current state, suggest what to do:
- If there are FAILED tasks: "These tasks failed and need investigation. Check their result for error details."
- If there are READY tasks with no one working on them: "These tasks are ready to be claimed by the scheduler."
- If all non-blocked tasks are done: "All available work is complete. Remaining tasks are blocked on: ..."
- If there are CANCELLED tasks: mention them and ask if they should be reopened or are intentionally cancelled
- If all tasks are COMPLETED: "All tasks are done. Project is ready to be marked COMPLETED."

---

## Scenario 3: Manage Task Lifecycle (Planning Agent)

This scenario covers what the **planning agent** (this skill's user) can do to manage tasks. Actual task execution (claiming, running, completing) is handled by the scheduler/runner and executor agents — see `ypl/agent_harness_service/deploy/shared/tasks/TASK_EXECUTION.md` for executor instructions.

### Monitoring Execution

- Call `get_ready_tasks` to see what's available for the scheduler to pick up
- Call `get_project_tasks(status="IN_PROGRESS")` to see what's currently being worked on
- Call `get_project_tasks(status="FAILED")` to find tasks that need attention

### Handling Failures

When a task has FAILED status:
1. Read the task's `result` field to understand what went wrong (it should contain `error_message`, `diagnosis`, and `suggestions` — see TASK_EXECUTION.md)
2. Present findings to the user and decide on next action:
   - **Retry as-is**: Transition FAILED → READY (the scheduler will pick it up again in a new session)
   - **Fix and retry**: Update the task description/task_data to address the failure, then transition to READY
   - **Cancel**: If the task is no longer needed, transition to CANCELLED
   - **Investigate**: If the failure is unclear, investigate before taking action
3. Tasks are NOT automatically retried unless the task description explicitly instructs retry behavior

### Cancelling Tasks

- `set_task_status(task_id, status="CANCELLED")` — can be done from PENDING, BLOCKED, READY, or IN_PROGRESS
- Cancelled tasks can be reopened: CANCELLED → PENDING
- Cancelling a task does NOT cancel its dependents — they remain BLOCKED (and will never become READY unless the cancelled task is reopened and completed, or the dependency is removed)

---

## Scenario 4: Modify an Existing Project

All modifications require user approval per the "Confirm Before Writing" rule above.

### Adding More Tasks

When scope changes or new work is discovered mid-project:
1. Call `get_project_tasks` to understand the current task structure and statuses
2. Present the new tasks to the user (same table format as Scenario 1 Step 3), showing how they connect to existing tasks
3. After approval, use `add_tasks` to add them, referencing existing task UUIDs in `depends_on` if needed

### Updating Tasks

Present each change explicitly (old → new) before calling `update_task`:
- `title` or `description` — clarify scope
- `priority` — reprioritize (URGENT > HIGH > NORMAL > LOW)
- `agent_name` — reassign to a different agent (empty string to unassign)
- `task_data` — add/update input data (merged with existing)
- `estimated_effort` — set effort estimate (small/medium/large)

Cannot update `status` via `update_task` — use `set_task_status` instead.

### Closing Out a Project

When all tasks are done:
1. Verify via `get_project_tasks` that all tasks are COMPLETED (or intentionally CANCELLED)
2. Present a final summary to the user
3. After confirmation, call `set_project_status(project_id, status="COMPLETED")`

---

## Data Flow Between Tasks

Tasks communicate through two mechanisms. The planning agent needs to understand these when writing task descriptions, so that each task knows where to find its inputs and where to put its outputs.

### 1. Task Results (point-to-point)
- Producer: executor calls `set_task_status(task_id, status="COMPLETED", result='{"output": "..."}')`
- Consumer: executor calls `get_task(task_id)` on a dependency to read its `.result`
- Use for: outputs specific to one task that its direct dependents need
- Not all tasks produce results that downstream tasks consume — ordering-only dependencies don't need this

### 2. Project Shared State (broadcast)
- Writer: `set_project_state(project_id, key, value_json)`
- Reader: `get_project_state(project_id, key)`
- Use for: cross-cutting information any task might need (e.g. "selected_approach", "pr_url", "environment")

**When to use which:**
- If only the next task in the chain needs it → task result
- If multiple unrelated tasks need it → shared state
- When in doubt → shared state (more discoverable)

### Standard Artifact Keys

When tasks produce common artifacts, use these standard keys in `task_data` and `result` for consistency:

| Key | Where | Description |
|---|---|---|
| `pr_url` | result, task_data | GitHub PR URL |
| `pr_number` | result | PR number as integer |
| `branch_name` | result, task_data | Git branch name |
| `linear_issue_id` | task_data | Linear issue ID (e.g. `YPP-1234`) |
| `artifact_url` | result | Link to detailed findings on artifact |
| `commit_sha` | result | Git commit SHA |
| `deploy_url` | result | URL of deployed service/preview |
| `error_message` | result (on failure) | Brief error description |
| `creator_slack_user_id` | shared_state | Slack user ID of the project owner (for @mentions) |
| `updates_thread_ts` | shared_state | Slack thread timestamp for general project updates |

These are conventions, not enforced — add more as needed.

---

## Project Status State Machine

Valid statuses: PAUSED, ACTIVE, COMPLETED, ARCHIVED

- PAUSED → ACTIVE (start/resume execution)
- ACTIVE → PAUSED, COMPLETED, ARCHIVED
- PAUSED → ARCHIVED
- COMPLETED → ARCHIVED
- ARCHIVED → (terminal)

New projects always start as PAUSED. The scheduler only executes tasks in ACTIVE projects.

---

## Task Status State Machine

Valid transitions:

- PENDING → READY, BLOCKED, CANCELLED
- BLOCKED → READY, PENDING, CANCELLED
- READY → IN_PROGRESS, BLOCKED, CANCELLED
- IN_PROGRESS → COMPLETED, FAILED, CANCELLED, READY
- FAILED → PENDING, READY
- CANCELLED → PENDING
- COMPLETED → (terminal, no transitions out)

Automatic behavior:
- Tasks with no dependencies start as READY
- Tasks with dependencies start as BLOCKED
- BLOCKED → READY happens automatically when all dependencies complete
- Only IN_PROGRESS tasks can be COMPLETED or FAILED

---

## Budget Awareness and Token Efficiency

When writing task descriptions and planning output formats, keep token costs in mind. This applies both to this planning skill and to the executor agents that will run the tasks.

- **Task descriptions**: Don't copy-paste shared background into every task. Put shared context in the project `description` or `shared_state` and reference it.
- **Task results**: Should be lean (under 500 chars). Large outputs go to online storage (currently artifact); the result stores a summary + URL.
- **Status reports**: Summarize for the user rather than echoing raw tool output.
- **Avoid redundant fetches**: `get_project_tasks` returns all task details — don't follow up with individual `get_task` calls for the same tasks.
