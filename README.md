# yupp-agent

**Agent Cloud Platform** — the infrastructure that powers Yupp's AI agents. Build, deploy, and operate autonomous agents that live in Slack, run on schedules, manage projects, and expose tools via MCP.

| Abbreviation | Service |
|---|---|
| **AHS** | Agent Harness Service — core agent runtime |
| **SAG** | Slack Agent Gateway — Slack bot bridge |
| **MCP** | Model Context Protocol Server — tool server for IDEs |

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

## Deployment

| Service | Platform | Workflow | Docker? |
|---------|----------|----------|---------|
| AHS | GCE VM | `deploy-ahs.yml` | No — git checkout + systemctl restart |
| SAG | Cloud Run | `deploy-servers.yml` | Yes — `agent-backend` image |
| MCP | Cloud Run | `deploy-servers.yml` | Yes — `agent-backend` image |
| Streamlit | Cloud Run | `deploy-servers.yml` | Yes — `agent-backend` image |

Docker images are fully separate from yupp-mind:
- Base: `gcr.io/yupp-llms/agent-base-py312`
- App: `gcr.io/yupp-llms/agent-backend`
