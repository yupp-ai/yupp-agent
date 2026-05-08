# Agent Harness Service (AHS) Architecture

AHS is the runtime for AI agents. It gives them identity, memory, tools, and an
isolated workspace. It is NOT a chat router or inference endpoint. It creates
persistent, stateful AI "employees."

**AHS owns:** session lifecycle, agent identity, workspace isolation, executor
abstraction, MCP harness, project/task orchestration, scheduled execution.

**AHS does NOT own:** Slack delivery (SAG), infrastructure access for engineers
(MCP server), end-user auth (main backend).

---

## Directory Map

```
ypl/agent_harness_service/
|
|  -- Root: wiring layer --
|-- service.py              Session lifecycle hub (121K)
|-- server.py               FastAPI entrypoint + DI wiring (10K)
|-- routes.py               REST API routes (24K)
|-- orchestration.py        Subagent spawn, recursive cancel (28K)
|-- task_executor.py        Task execution, dependency resolution (36K)
|-- scheduler.py            Schedule polling, cron, stale recovery (20K)
|
|-- common/               Layer 0 -- zero AHS deps, imported by everyone
|   |-- constants.py        Constants + get_session_dir()
|   |-- config.py           Settings (pydantic-settings)
|   |-- auth.py             Request auth
|   |-- models.py           AgentSpec, ExecutorConfig, ExecutorResult
|   |-- types.py            StreamEvent, SessionCreateRequest, SessionPermissions, etc.
|   |-- exceptions.py       Exception types
|   |-- agent_registry.py   Agent config loading from disk (no DB deps)
|   +-- providers.py        Model provider registry (shared by executors, title gen, guardrails, etc.)
|
|-- core/                 Layer 1 -- depends on common/ only
|   |-- streaming.py        Event bus, WebSocket, PubSub
|   |-- gcs_sync.py         GCS manifest-based sync
|   |-- session_persistence.py
|   +-- session_title.py
|
|-- gateway/              Layer 1 -- depends on common/ only
|   |-- base.py             Abstract gateway interface
|   |-- registry.py         GatewayRegistry
|   |-- slack.py            Slack gateway implementation
|   +-- slack_prefetch.py   Inbound Slack message prefetch
|
|-- executors/            Layer 1 -- depends on common/ only
|   |-- runner.py           CLI subprocess (harnessed mode)
|   |-- raw_executor.py     Direct API execution loop
|   |-- codex_runner.py     Codex CLI runner
|   |-- context.py          Context mgmt (truncation, pruning, compaction)
|   |-- system_prompt.py    System prompt assembly
|   +-- sandbox.py          Permissions, bwrap, env filtering
|
|-- tools/                Layer 1 -- depends on common/ only
|   |-- local_mcp_server.py In-process MCP server + all tool defs (uses DI for orchestration)
|   |-- workspace_tools.py  Workspace/file tools
|   |-- repo_manager.py     Git worktree management
|   |-- mcp_client.py       MCP client
|   |-- github_token_storage.py
|   +-- linear_sync/        Linear import/export
|
|-- projects/             Layer 1 -- depends on common/ only
|   |-- project_service.py  Project + task CRUD
|   |-- project_routes.py   Project API routes
|   |-- project_types.py    Project/task Pydantic models
|   |-- task_utils.py       Task helpers
|   +-- schedule_service.py Schedule CRUD
|
|-- tui/                  Terminal UI client
|-- scripts/              CLI tools (ahscli)
|-- deploy/               VM setup, agent configs, systemd
+-- docs/                 Supplementary docs
```

---

## Layering

The codebase is organized into three strict layers. This is the most important
architectural constraint in AHS.

```
                       +----------+
                       | common/  |  Layer 0
                       +----+-----+
      +----------+---------+---+----------+----------+
      v          v         v   v          v          v
   +--------+ +------+ +--------+ +--------+ +---------+
   | core/  | |gate- | |execu-  | | tools/ | |projects/|  Layer 1
   |        | |way/  | |tors/   | |        | |         |  (independent)
   +--------+ +------+ +--------+ +--------+ +---------+

        +----------------------------------------------+
        |  Root: service, server, routes, auth,        |  Wiring
        |  orchestration, task_executor, scheduler      |
        +----------------------------------------------+
```

**Layer 0 (common/):** Pure types, configuration, and constants. Zero
dependencies on any other AHS package. Everything imports from here.

**Layer 1 (core/, gateway/, executors/, tools/, projects/):** Five independent
packages that each depend only on common/. They MUST NOT import from each other
or from root wiring files. This independence is what keeps the codebase
modular -- any Layer 1 package can be understood, tested, and modified without
knowledge of the others.

**Wiring layer (root files):** The root-level `.py` files are the only place
where Layer 1 packages are composed together. `service.py` is the orchestration
hub. `server.py` handles FastAPI setup and dependency injection wiring.
`orchestration.py`, `task_executor.py`, and `scheduler.py` contain cross-cutting
logic that coordinates multiple Layer 1 packages.

---

## Dependency Injection

The most important DI boundary is between `tools/` and the wiring layer.

`tools/local_mcp_server.py` needs to spawn subagents (an orchestration concern),
but it lives in Layer 1 and MUST NOT import from `orchestration.py` or
`service.py`. This is solved with callback registration:

- `local_mcp_server.py` exposes `register_orchestration_callbacks()`.
- `server.py` calls this at startup, injecting the actual orchestration
  functions as callbacks.
- At runtime, `local_mcp_server.py` invokes the callbacks without knowing
  their implementation.

This pattern MUST be used for any future case where a Layer 1 package needs
wiring-layer functionality.

---

## Key Invariants

1. **One active turn per session.** Messages are queued in arrival order. A new
   user message does not interrupt an in-progress turn.

2. **Monotonic session state.** Session state transitions never roll back.

3. **Workspace before executor.** The workspace must be created and ready
   before any executor starts.

4. **Metadata before runner.** Session metadata is persisted to DB before the
   runner process is spawned.

5. **Gateway via callback.** Gateway callbacks are registered via a mechanism
   (not a direct import). Executors never call Slack or any delivery layer.

6. **Executors are side-effect-limited.** They may write to the workspace and
   call MCP tools. They must NOT write to the database or import `service.py`.

7. **Permission intersection, never widening.**
   `effective_permissions = agent_config ^ session.requested ^ executor_capabilities`

---

## Executor Contract

Executors are the pluggable "brains" of a session. Three implementations exist:
`runner.py` (CLI subprocess), `raw_executor.py` (direct API loop),
`codex_runner.py` (Codex CLI).

**Input:**
- `session_id`
- `system_prompt` (assembled by `system_prompt.py`)
- `messages` (conversation history)
- `agent_spec` (model, provider, permissions, etc.)

**Output:** `ExecutorResult` containing:
- `content` (text response)
- `cost` (USD)
- `tokens` (prompt + completion)
- `stop_reason`

**Allowed side effects:**
- Write to the workspace filesystem
- Call MCP tools (via `local_mcp_server`)
- Write history files (via `session_persistence`)

**Forbidden:**
- Import `service.py`
- Write to the database
- Call Slack or any external delivery system

---

## Context Management (raw_executor)

Context is managed in three phases, applied in order of increasing cost:

| Phase | What | Cost |
|-------|------|------|
| 1. Tool result truncation | Truncate individual tool call results that exceed a per-call size limit | Cheap (string slicing) |
| 2. Message pruning + LLM compaction | When approaching the context window limit, drop old messages and/or use an LLM to summarize pruned content | Expensive (LLM call) |
| 3. Cross-turn history persistence | Persist conversation history to disk between turns so it can be reloaded selectively | Medium (file I/O) |

Implementation lives in `executors/context.py`.

---

## Scheduler and Task Execution

The scheduler (`scheduler.py`) polls the database on an interval for tasks that
are ready to run (dependencies met, schedule due). It uses an **atomic claim
pattern**:

```sql
SELECT ... FOR UPDATE SKIP LOCKED
```

This prevents multiple AHS instances from claiming the same task. The claimed
task is handed to `task_executor.py`, which resolves dependencies, spawns a
session via `service.py`, and monitors completion.

`scheduler.py` does not know about project structure. `task_executor.py` does
not know about specific agent names. This separation keeps orchestration generic.

---

## Import Rules

These rules exist to prevent circular dependencies and maintain the layering
constraint. They are enforced by convention, not tooling.

**Structural rules:**

| Package | May import from | Must NOT import from |
|---------|----------------|---------------------|
| `common/` | stdlib, third-party only | Any other AHS package |
| `core/` | `common/` | `gateway/`, `executors/`, `tools/`, `projects/`, root wiring |
| `gateway/` | `common/` | `core/`, `executors/`, `tools/`, `projects/`, root wiring |
| `executors/` | `common/` | `core/`, `gateway/`, `tools/`, `projects/`, root wiring |
| `tools/` | `common/` | `core/`, `gateway/`, `executors/`, `projects/`, root wiring |
| `projects/` | `common/` | `core/`, `gateway/`, `executors/`, `tools/`, root wiring |
| Root wiring files | Any AHS package | (no restriction) |

**Specific rules:**

| Module | Must NOT import |
|--------|----------------|
| `tools/local_mcp_server.py` | `service.py`, `orchestration.py` -- use DI callbacks instead |
| `executors/runner.py` | LLM API clients directly |
| `executors/raw_executor.py` | Database session / ORM |
| `task_executor.py` | Specific agent names (must be config-driven) |
| `scheduler.py` | Project structure types |

**Wiring carve-outs in `ypl/mcp_common/`:**

`ypl/mcp_common/` is otherwise pure (Layer-0-equivalent for MCP code), but
two modules sit at the same architectural level as `ypl/mono_server/server.py`
and `ypl/agent_harness_service/tools/local_mcp_server.py` — the wiring layer
where AHS and the agcouch MCP are composed. They are documented here as
explicit carve-outs:

| Module | What it imports | Why |
|--------|-----------------|-----|
| `mcp_common/shared_tool.py` | `agent_harness_service.tools.mcp_instance.mcp` (harness FastMCP) and `mcp_server.core.mcp_server` (agcouch FastMCP) | Implements the dual-registration decorator. Path-based mount is still the auth boundary; this file is just the one place that knows about both registries so individual tool modules don't have to. Adding a future external MCP server = appending to its `_INSTANCES` list. |

The architecture tests in `tests/agent_harness_service/test_architecture.py`
scope to `ypl/agent_harness_service/`, so they do not constrain
`ypl/mcp_common/`. Future tooling that scans `ypl/mcp_common/` for
cross-package imports must allow `shared_tool.py` to reach into both AHS
and `mcp_server`. Pure-internal AHS tools never touch this decorator and
remain Layer 1.

---

## Known Architectural Debt

1. **`service.py` is oversized (~121K).** It is the session lifecycle monolith.
   Further decomposition into session creation, turn handling, and state
   management would improve maintainability.

2. **`tools/local_mcp_server.py` is the largest file (~92K).** It contains both
   the MCP server framework and all tool definitions. Splitting tool definitions
   into separate modules per category would help.

3. **`tools/local_mcp_server.py` has lazy imports** of `service.create_agent`
   and `GatewayRegistry` at runtime. These are acceptable (not import-time
   violations), but they are a sign that the DI boundary could be cleaner.

4. **Two external consumers** depend directly on AHS internals
   (`mcp_server/tools/project_tasks.py` and `slack_agent_gateway/bot_father.py`).
   These are tight couplings. A formal API boundary (or at minimum a stable
   public interface module) would reduce breakage risk.

---

## For AI Agents Working on This Codebase

**Start here:**
1. `common/types.py` and `common/config.py` -- understand the data shapes.
2. `common/models.py` -- understand AgentSpec, ExecutorConfig, ExecutorResult.
3. `service.py` -- the main orchestrator. Most session-level questions are
   answered there.

**Before modifying code:**
- Read the layering constraint above. It is the primary architectural rule.
- If you are adding a new tool, it goes in `tools/`. If the tool needs
  orchestration capabilities, use the DI callback pattern -- do not add a
  direct import from root wiring files.
- If you are adding a new executor, it goes in `executors/` and must follow
  the executor contract.
- If you are modifying cross-cutting behavior, it belongs in the root wiring
  layer, not in a Layer 1 package.

**Common mistakes to avoid:**
- Importing between Layer 1 packages (e.g., `tools/` importing from `core/`).
- Adding database writes inside executors.
- Adding Slack-specific logic outside `gateway/`.

See `docs/` for deployment, local testing, and roadmap details.
