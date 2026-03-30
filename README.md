# yupp-agent

A multi-agent cloud platform for building, deploying, and operating autonomous AI agents at scale.

The platform lets teams scale AI usage from a handful of ad-hoc prompts to a fleet of specialized agents working in parallel. Each agent gets a persistent identity, long-term shared memory, sandboxed code execution, cron-based scheduling, and the ability to spawn sub-agents — running as a first-class service with full observability, not a script bolted onto an LLM. New agents are added through configuration, not code, so the system grows organically as teams discover new workflows worth automating.

yupp-agent is designed for orchestration at scale: manage dozens of agents from a single control plane, coordinate multi-step projects with dependency-aware task execution, and evolve agent capabilities over time through shared memory and iterative feedback. The architecture treats agents as long-lived collaborators that learn and improve, not disposable one-shot tools.

Today, Yupp agents investigate production incidents, review pull requests, ship code, run daily standups, manage multi-week projects with Linear sync, and collaborate in Slack threads alongside the engineering team.

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
│    Skills, CLAUDE.md instructions     Shared (GCS) + Private      │
│    MCP Server (tool gateway)          Long-term, cross-session    │
│    External + Local MCPs                                          │
├───────────────────────────────────────────────────────────────────┤
│  Workspace                          Permissions & Auth            │
│    Sandbox (bubblewrap)               GitHub App OAuth            │
│    Git worktrees, session history     API keys, RBAC              │
├───────────────────────────────────────────────────────────────────┤
│  Infrastructure                                                   │
│    GCE VM (AHS)    Cloud Run (SAG, MCP, Streamlit)    Cloud SQL   │
└───────────────────────────────────────────────────────────────────┘
```

Requests enter through Slack (via SAG) or direct REST API calls. The orchestration layer coordinates multi-step work — scheduling jobs, managing projects, and routing tasks with dependency awareness. Each agent runs inside the AHS runtime with its own sandbox, memory, and tool access. The MCP server sits in the tools layer, exposing platform capabilities (databases, logs, Slack, GitHub) to agents and engineers' IDEs alike. The platform is layered so that adding a new agent, tool, or access point doesn't require changes to the layers above or below it.

## Services

### Agent Harness Service (AHS)

The core runtime for AI agents. AHS gives each agent a persistent identity, long-term memory, sandboxed repo access, and the ability to spawn sub-agents. Agents are defined as config files and registered to a database — no code changes needed to add a new one.

- **Executor framework** — run agents via Claude CLI (harnessed mode) or raw API calls, with automatic sandboxing via bubblewrap
- **Session management** — persistent conversation threads with turn-by-turn message history, completion tracking, and stale session recovery on restart
- **Memory** — agents read and write shared memory files in GCS, organized by topic, with automatic sync on session end
- **Scheduler** — cron-like recurring agent invocations (daily standups, weekly retros, PR monitors) with run history and health tracking
- **Projects & Tasks** — hierarchical project management with dependency-aware task execution, budget tracking, and Linear sync
- **Sub-agent orchestration** — agents can spawn other agents, with result delivery queues and parent-child session linking
- **Gateway system** — pluggable output gateways (Slack, WebSocket) so agents can push replies to any surface

**Runs on:** GCE VM (not containerized — needs filesystem access for git repos and sandboxes)

### Slack Agent Gateway (SAG)

The bridge between Slack and AHS. Each Slack bot app maps to an agent — SAG handles the Slack protocol, message buffering, and callback rendering so agents don't have to.

- **Multi-bot support** — one gateway serves multiple Slack bot apps, each with its own signing secret and bot token, configurable via settings
- **Smart buffering** — collects rapid-fire messages into a single agent turn, with configurable flush intervals
- **Rich rendering** — converts agent markdown responses into Slack blocks with collapsible sections, code blocks, and action buttons
- **Bot Father** — self-service Slack bot creation: request a new agent bot from Slack, approve it, and SAG provisions the app automatically
- **OAuth token storage** — encrypted token management for workspace installations
- **Channel filtering** — allowlist/denylist patterns to control which channels agents can respond in

**Runs on:** Cloud Run

### MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes Yupp's internal tools to any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.). Engineers connect once and get access to production databases, logs, and operational tools from their IDE.

- **Database tools** — query PostgreSQL (read-only) and BigQuery with cost guards and byte-limit checks
- **GCP tools** — search Cloud Logging alerts and query log entries with structured filters
- **Agent tools** — manage agent schedules, search agent memory, browse artifacts, create projects/tasks
- **Collaboration tools** — create and read yuppastes, post to Slack channels, search Twitter, file Sentry issues
- **Security tools** — log and query security incidents with severity tracking
- **Dual auth** — DevToken mode (CLI-created bearer tokens) for engineers, OAuth mode (Google login) for broader access
- **Audit logging** — every tool call is logged with caller identity, arguments, and execution time

**Runs on:** Cloud Run (two instances: DevToken and OAuth)

### Streamlit Server

Operational dashboards for monitoring and managing the agent platform. Six pages focused on agent health, performance, and debugging.

- **Agent Console** — browse agents, sessions, messages, and feedback signals in a searchable thread view
- **Agent Dashboard** — operational analytics: session counts, costs, latency percentiles, error rates, feedback trends, schedule health
- **Agent Memory Viewer** — browse shared agent memory files in GCS with metadata, timestamps, and inline content preview
- **Agent Projects** — view project hierarchies, track task status and dependencies, monitor budget spend
- **Agent Schedules** — manage recurring agent calls, view run history, check schedule health
- **Anomaly Detection** — catch cost and latency regressions early with coefficient-of-variation analysis and variance bands

**Runs on:** Cloud Run

## Quick Start

```bash
# Install dependencies
poetry install

# Set up environment
cp .env.example .env  # edit with your DB credentials

# Run AHS locally
./ypl/agent_harness_service/entrypoint.sh

# Run SAG locally
./ypl/slack_agent_gateway/entrypoint.sh

# Run MCP server locally
./ypl/mcp_server/entrypoint.sh

# Run Streamlit dashboards locally
streamlit run ypl/streamlit_server/app.py
```

## Architecture

```
yupp-agent/
├── ypl/
│   ├── agent_harness_service/   # AHS — agent runtime, scheduler, projects
│   ├── slack_agent_gateway/     # SAG — Slack bot bridge
│   ├── mcp_server/              # MCP — tool server for IDEs
│   ├── mcp_common/              # Shared MCP utilities
│   ├── streamlit_server/        # Operational dashboards
│   ├── backend/                 # Shared backend (DB, config, utils)
│   └── db/                      # SQLModel database models + migrations
├── data/                        # Feature flags, env var configs
├── scripts/                     # Deploy scripts, secret management
└── .github/workflows/           # CI/CD
    ├── lint_and_test.yml        # Lint + mypy + tests
    ├── deploy-servers.yml       # Cloud Run deploys (MCP, SAG, Streamlit)
    └── deploy-ahs.yml          # VM deploy (AHS — no Docker needed)
```

## Database Tools

### Dump staging agentdb to local

Pull a copy of the staging `agentdb` into your local Postgres for development and debugging. The script auto-fetches credentials from GCP Secret Manager, or prompts interactively if `gcloud` is unavailable.

Prerequisites: `pg_dump` and `psql` on your PATH (`brew install libpq && brew link --force libpq` on macOS).

Dumps are saved to `~/tmp/yadb-dumps/` with timestamps. Before restoring, the script asks for confirmation and then clears the local database (`DROP SCHEMA public CASCADE`).

```bash
# Dump staging + restore to local (asks before clearing local DB):
python -m ypl.db.tools.dump_staging_to_local

# Dump only, don't restore yet:
python -m ypl.db.tools.dump_staging_to_local --no-restore

# List saved dumps:
python -m ypl.db.tools.dump_staging_to_local --list

# Restore a saved dump by number (from --list):
python -m ypl.db.tools.dump_staging_to_local --restore 3

# Restore from an arbitrary file:
python -m ypl.db.tools.dump_staging_to_local --restore-file ./my_dump.sql

# Override the local destination:
python -m ypl.db.tools.dump_staging_to_local --dest postgresql://user:pass@localhost:5432/mydb

# Dump specific tables:
python -m ypl.db.tools.dump_staging_to_local --tables agents,agent_sessions

# Schema only / data only:
python -m ypl.db.tools.dump_staging_to_local --schema-only
python -m ypl.db.tools.dump_staging_to_local --data-only
```

Uses the **read replica** by default. Pass `--use-primary` to hit the primary instead. If GCP credentials aren't available, the script prompts for host/port/database/user/password interactively.

## Deployment

### Overview

| Service | Platform | Workflow | Docker? |
|---------|----------|----------|---------|
| AHS | GCE VM (`us-east5-c`) | `deploy-servers-1-build-staging.yml` | No — git checkout + systemctl restart |
| SAG | Cloud Run | `deploy-servers-1-build-staging.yml` / `deploy-servers-2-deploy.yml` | Yes — `agent-backend` image |
| MCP | Cloud Run (2 instances: DevToken + OAuth) | `deploy-servers-1-build-staging.yml` / `deploy-servers-2-deploy.yml` | Yes — `agent-backend` image |
| Streamlit | Cloud Run | `deploy-servers-1-build-staging.yml` / `deploy-servers-2-deploy.yml` | Yes — `agent-backend` image |

### Docker images

Fully separate from yupp-mind:
- **Base:** `gcr.io/yupp-llms/agent-base-py312` — rebuilt automatically when `pyproject.toml` or `poetry.lock` change
- **App:** `gcr.io/yupp-llms/agent-backend` — tagged `latest`, `release-candidate`, or `cherry-pick-release-candidate`

### Staging

Staging deploys run **automatically every 12 hours** via `deploy-servers-1-build-staging.yml`, or on manual trigger. The pipeline:

1. Runs Alembic migrations on staging agentdb (admin credentials from `ym-postgres-connection-agentdb-staging-admin`)
2. Rebuilds base image if `pyproject.toml`/`poetry.lock` changed
3. Builds and pushes `agent-backend:latest`
4. Deploys Cloud Run services (SAG, MCP, MCP OAuth, Streamlit)
5. Deploys AHS via VM script (optional, manual trigger)

### Production

Production deploys use `deploy-servers-2-deploy.yml`:

1. Triggered manually or via workflow_call with `environment: production`
2. Deploys a **release candidate** image (`release-candidate` or `cherry-pick-release-candidate` tag)
3. Runs Alembic migrations on production agentdb before deploying
4. Deploys selected Cloud Run services and/or AHS

### Secrets

All secrets live in **GCP Secret Manager** (project: `yupp-llms`). Naming convention: `ym-<name>-<environment>`.

```bash
# Pull secrets for local development:
python -m scripts.gcpsecrets pull-local-secrets

# Add a new secret:
python -m scripts.gcpsecrets add MY_SECRET --project yupp-llms

# Update an existing secret:
python -m scripts.gcpsecrets update-value MY_SECRET -e staging -e production
```

Secret-to-env-var mappings are defined in `data/secret-env-var-map.yml`.

### Database migrations

Alembic manages the `agentdb` schema. Migrations run automatically before each deployment.

```bash
# Create a new migration:
alembic -c alembic.ini revision --autogenerate -m "description"

# Run locally:
alembic -c alembic.ini upgrade head
```

Cloud SQL instances:
- **Staging:** `yupp-llms:us-east4:yupp-agent-dev`
- **Production:** `yupp-llms:us-east4:yupp-agent-prod`
