# yupp-agent

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

A multi-agent platform for building, deploying, and operating autonomous AI agents at scale.

The platform lets teams scale AI usage from a handful of ad-hoc prompts to a fleet of specialized agents working in parallel. Each agent gets a persistent identity, long-term shared memory, sandboxed code execution, cron-based scheduling, and the ability to spawn sub-agents — running as a first-class service with full observability, not a script bolted onto an LLM. New agents are added through configuration, not code, so the system grows organically as teams discover new workflows worth automating.

yupp-agent is designed for orchestration at scale: manage dozens of agents from a single control plane, coordinate multi-step projects with dependency-aware task execution, and evolve agent capabilities over time through shared memory and iterative feedback. The architecture treats agents as long-lived collaborators that learn and improve, not disposable one-shot tools.

Today these agents investigate production incidents, review pull requests, ship code, run daily standups, manage multi-week projects with Linear sync, and collaborate in Slack threads alongside the engineering team.

## Architecture

```
                            Slack               REST API
                              |                     |
┌───────────────────────────────────────────────────────────────────┐
│  Access Points                                                    │
│    Slack Agent Gateway (SAG)          REST Endpoints              │
│                                       (streaming / non-streaming) │
├───────────────────────────────────────────────────────────────────┤
│  Orchestration                                                    │
│    Project & Task Management            Scheduled Jobs            │
│    (Linear sync, dependency DAG)        (cron, health tracking)   │
├───────────────────────────────────────────────────────────────────┤
│  Agent Runtime (AHS)                                              │
│    Harnessed Executors (Claude CLI)     Sub-agent Orchestration   │
│    Raw Executors (direct API)           Session Management        │
├───────────────────────────────────────────────────────────────────┤
│  Skills & Tools                     Memories                      │
│    Skills, CLAUDE.md instructions     Shared (object storage)     │
│    MCP Server (tool gateway)          + private per-agent         │
│    External + Local MCPs              Long-term, cross-session    │
├───────────────────────────────────────────────────────────────────┤
│  Workspace                          Permissions & Auth            │
│    Sandbox (bubblewrap)               GitHub App OAuth            │
│    Git worktrees, session history     API keys, RBAC              │
├───────────────────────────────────────────────────────────────────┤
│  Infrastructure                                                   │
│    One VM (monolith) + PostgreSQL + Redis                         │
└───────────────────────────────────────────────────────────────────┘
```

Requests enter through Slack (via SAG) or direct REST API calls. The orchestration layer coordinates multi-step work — scheduling jobs, managing projects, and routing tasks with dependency awareness. Each agent runs inside the AHS runtime with its own sandbox, memory, and tool access. The MCP server sits in the tools layer, exposing platform capabilities (databases, logs, Slack, GitHub) to agents and engineers' IDEs alike. The platform is layered so that adding a new agent, tool, or access point doesn't require changes to the layers above or below it.

## Services

### Agent Harness Service (AHS)

The core runtime for AI agents. AHS gives each agent a persistent identity, long-term memory, sandboxed repo access, and the ability to spawn sub-agents. Agents are defined as config files and registered to a database — no code changes needed to add a new one.

- **Executor framework** — run agents via Claude CLI (harnessed mode) or raw API calls, with automatic sandboxing via bubblewrap
- **Session management** — persistent conversation threads with turn-by-turn message history, completion tracking, and stale session recovery on restart
- **Memory** — agents read and write shared memory files in object storage, organized by topic, with automatic sync on session end
- **Scheduler** — cron-like recurring agent invocations (daily standups, weekly retros, PR monitors) with run history and health tracking
- **Projects & Tasks** — hierarchical project management with dependency-aware task execution, budget tracking, and Linear sync
- **Sub-agent orchestration** — agents can spawn other agents, with result delivery queues and parent-child session linking
- **Gateway system** — pluggable output gateways (Slack, WebSocket) so agents can push replies to any surface

### Slack Agent Gateway (SAG)

The bridge between Slack and AHS. Each Slack bot app maps to an agent — SAG handles the Slack protocol, message buffering, and callback rendering so agents don't have to.

- **Multi-bot support** — one gateway serves multiple Slack bot apps, each with its own signing secret and bot token, configured via the `slack_agents` DB table
- **Smart buffering** — collects rapid-fire messages into a single agent turn, with configurable flush intervals
- **Rich rendering** — converts agent markdown responses into Slack blocks with collapsible sections, code blocks, and action buttons
- **Bot Father** — self-service Slack bot creation: request a new agent bot from Slack, approve it, and SAG provisions the app automatically
- **OAuth token storage** — encrypted token management for workspace installations
- **Channel filtering** — allowlist/denylist patterns to control which channels agents can respond in

### MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the platform's internal tools to any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.). Engineers connect once and get access to production databases, logs, and operational tools from their IDE.

- **Database tools** — query PostgreSQL (read-only) and BigQuery with cost guards and byte-limit checks
- **GCP tools** — search Cloud Logging alerts and query log entries with structured filters (optional; plug in your own for other clouds)
- **Agent tools** — manage agent schedules, search agent memory, browse artifacts, create projects/tasks
- **Collaboration tools** — create and read textual artifacts, post to Slack channels, search Twitter, file Sentry issues
- **Security tools** — log and query security incidents with severity tracking
- **Dual auth** — DevToken mode (CLI-created bearer tokens) for engineers, OAuth mode (Google login) for broader access
- **Audit logging** — every tool call is logged with caller identity, arguments, and execution time

### War Room

`apps/war-room/` — Next.js 16 admin UI for AHS. A purpose-built command center to browse sessions with live streaming, manage agents and schedules, drive project boards, and monitor Slack gateway health. Talks REST to AHS and SAG.

- **Sessions** — paginated list, single-session live feed, side-by-side comparison
- **Agents** — list, edit prompts, edit configs
- **Projects** — Kanban task boards with status transitions
- **Schedules** — create, trigger, cancel recurring agent runs
- **Feedback** — per-message rating and comments

Run locally: `cd apps/war-room && bun install && bun run dev` (port 3009). See `apps/war-room/README.md`.

### Streamlit Server

Operational dashboards for monitoring and managing the agent platform. Pages focused on agent health, performance, and debugging.

- **Agent Console** — browse agents, sessions, messages, and feedback signals in a searchable thread view
- **Agent Dashboard** — operational analytics: session counts, costs, latency percentiles, error rates, feedback trends, schedule health
- **Agent Projects** — view project hierarchies, track task status and dependencies, monitor budget spend
- **Agent Schedules** — manage recurring agent calls, view run history, check schedule health
- **Anomaly Detection** — catch cost and latency regressions early with coefficient-of-variation analysis and variance bands

## Quick Start

### 1. Install dependencies

```bash
poetry install
```

### 2. Set up environment

```bash
cp .env.example .env
```

Fill in the required values (LLM provider API keys, Postgres connection, etc.). The setup wizard below auto-generates all internal secrets (signing keys, Fernet encryption keys, etc.).

### 3. Set up CLI tools

Make sure you have [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex) installed and logged in. AHS uses these as agent executors.

### 4. Run the monolith setup wizard

The wizard checks Postgres/Redis connectivity, runs migrations, seeds roles (ADMIN, ENGINEER, MCP_USER), and creates your first admin user + optional MCP dev token:

```bash
python -m ypl.mono_server.setup
```

See [`DEPLOYMENT.md`](./DEPLOYMENT.md) for detailed deployment options (Docker Compose, bare-metal VM, MacBook).

## Run Locally

### Monolith (recommended for dev + self-hosted)

Runs AHS + SAG + MCP + Streamlit in a single process:

```bash
uvicorn ypl.mono_server.server:app --port 8090 --reload
```

Visit `http://localhost:8090/docs` for the OpenAPI page. Streamlit still runs separately on port 8501:

```bash
streamlit run ypl/streamlit_server/app.py
```

### Standalone services (optional)

The services can also be run independently — useful for debugging or scaling one component:

```bash
./ypl/agent_harness_service/entrypoint.sh      # AHS (port 8090)
./ypl/slack_agent_gateway/entrypoint.sh        # SAG
./ypl/mcp_server/entrypoint.sh                 # MCP Server
```

### TUI client

```bash
# Local
python -m ypl.agent_harness_service.tui --host localhost:8090

# Remote — set AHS_HOST or pass --host
python -m ypl.agent_harness_service.tui --host https://ahs.example.com
```

### War Room (Next.js admin UI)

Self-contained Bun/Next.js app on port 3009.

```bash
cd apps/war-room
cp .env.example .env.local          # fill in AHS_API_KEY, Google OAuth, AUTH_SECRET
bun install
bun run dev                         # http://localhost:3009
```

The app talks REST to your local AHS (`AHS_HOST=http://localhost:8090` by default) and optionally SAG (`http://localhost:8080`). See `apps/war-room/README.md` and `apps/war-room/AGENTS.md` for details.

## Repository Layout

```
yupp-agent/
├── ypl/
│   ├── agent_harness_service/   # AHS — agent runtime, scheduler, projects
│   ├── slack_agent_gateway/     # SAG — Slack bot bridge
│   ├── mcp_server/              # MCP — tool server for IDEs
│   ├── mcp_common/              # Shared MCP utilities
│   ├── streamlit_server/        # Operational dashboards
│   ├── mono_server/             # Single-process entrypoint (AHS + SAG + MCP)
│   ├── tools/                   # Operator CLIs (ahs-artifact, …) — see ypl/tools/README.md
│   ├── backend/                 # Shared backend (DB, config, utils)
│   └── db/                      # SQLModel database models + migrations
├── apps/
│   ├── war-room/                # Next.js 16 admin UI for AHS (Bun)
│   └── artifact-viewer/         # Starlette read-only viewer for artifacts (Python)
├── data/                        # Feature flags, config files
├── scripts/                     # Operational scripts
└── .github/workflows/           # CI/CD
    └── lint_and_test.yml        # Lint + mypy + tests
```

## Database Tools

### Dump and restore between Postgres instances

For moving data between environments (e.g. loading a prod snapshot into a fresh monolith VM), use the env-driven pair:

- `ypl/db/tools/dump_prod_yadb.py` — pure `pg_dump` wrapper, reads `PG_SOURCE_URL`
- `ypl/db/tools/restore_dump.py` — `pg_restore` wrapper that clears and reloads the target

```bash
# On any machine that can reach the source DB:
export PG_SOURCE_URL='postgresql://USER:PASS@HOST:5432/yadb?sslmode=require'
python -m ypl.db.tools.dump_prod_yadb
# Writes yadb_prod_<timestamp>.dump to ~/tmp/yadb-dumps/

# On the destination VM:
export PG_DEST_URL='postgresql://postgres:postgres@127.0.0.1:5432/yadb'
python -m ypl.db.tools.restore_dump --file ~/tmp/yadb-dumps/yadb_prod_<timestamp>.dump --stamp-alembic
```

Both scripts have `--help` with the full flag list.

## Operator CLIs

Installed via `[project.scripts]` in `pyproject.toml`, so `pip install -e .` / `poetry install` puts them on `$PATH`.

- **`ahs-artifact`** — create, read, search, and archive textual artifacts via the AHS REST API. See [`ypl/tools/README.md`](./ypl/tools/README.md) for the full command reference.

## Database migrations

Alembic manages the `agentdb` schema.

```bash
# Create a new migration:
alembic -c alembic.ini revision --autogenerate -m "description"

# Run locally:
alembic -c alembic.ini upgrade head
```

## License

Licensed under the [Apache License, Version 2.0](./LICENSE). See the `LICENSE` file for the full text.

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
