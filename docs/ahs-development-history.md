# Agent Harness Service (AHS): Development History & Technical Summary

> A comprehensive record of building a multi-agent cloud platform from zero to production in ~8 weeks,
> covering architecture, infrastructure, and the evolution of a consumer chat product into an autonomous agent platform.

---

## Executive Summary

AHS is the core runtime of **yupp-agent**, a multi-agent cloud platform for building, deploying, and operating autonomous AI agents at scale. The system grew from initial commit to production-grade platform in approximately 8 weeks (Feb 14 – Apr 13, 2026), encompassing:

- **4 integrated services**: Agent Harness Service (runtime), Slack Agent Gateway (chat bridge), MCP Server (IDE/agent tooling), Streamlit dashboards (ops UI)
- **27 named agent configurations** (infrastructure agents, specialized workers, personal agents, multi-model review teams)
- **174 merged PRs** in the standalone repo (18 days), plus ~150 agent-related PRs in the parent repo
- **70% test coverage** enforced in CI, up from 0%
- **Pluggable executor framework** supporting Claude Code CLI, Claude Agent SDK, OpenAI Codex, and raw LLM API loops
- **Agent-to-Agent messaging protocol** with deny-by-default authorization
- **One-box monolith mode** reducing deployment cost from ~$500/mo to ~$25/mo

---

## Context: The Parent Platform (yupp-mind)

AHS was built inside **yupp-mind**, a multi-LLM chat product with:

- **12,900+ commits** across 10+ contributors (Jul 2024 – Mar 2026)
- Model routing (multi-armed bandit → two-tower neural → cost-aware LLM router v4)
- Pairwise evaluation and Elo-based leaderboard ranking
- Cloaked testing infrastructure for model promotion
- Content moderation and risk scoring
- Streaming infrastructure with provider abstraction (Anthropic, OpenAI, Google, Azure, etc.)

This platform provided the database infrastructure, deployment pipelines, and operational maturity that AHS built upon.

---

## Phase 1: MCP Server & Agent Tooling Foundation (Jan 6 – Feb 13, 2026)

**Goal:** Give AI coding agents (Claude Code, Codex) access to internal infrastructure.

### What was built
- **MCP Server** (Model Context Protocol) — a tool server exposing internal systems to IDE-based agents
  - GCP log search, Vercel log search, BigQuery queries, Redis inspection
  - Read-only database access with audit logging
  - Google OAuth + dev token authentication
  - Rate limiting, transport security, Cloud Run deployment
- **Agent skills system** — 33+ reusable skill definitions (`.agents/skills/`) exposed as MCP resources
  - PR review, lint fixing, backend alert investigation, data science analysis
  - Skills like `handle-pr-comments`, `fix-lint-and-tests`, `investigate-sentry-issue`
- **Safety hooks** — pre-commit guardrails preventing AI agents from approving PRs or deleting comments

### Key decisions
- **FastMCP 2.0** chosen over raw protocol implementation — correct bet, later unified two FastMCP instances into one
- **Audit logging from day one** — every MCP tool call recorded (who, what, result, latency, context)
- Skills as markdown files with frontmatter — composable, versionable, agent-agnostic

---

## Phase 2: AHS + SAG Genesis Weekend (Feb 14–17, 2026)

**Goal:** Build a complete agent runtime and Slack integration in one weekend.

### Slack Agent Gateway (SAG) — Feb 14, one day
14 PRs landed in a single day:
- Multi-app Slack architecture (multiple bot identities, single backend)
- Session management with Redis (stateless gateway, no DB dependency)
- Append buffering for Slack's rate limits (50 req/min Tier 3)
- Message queue for resilience when AHS is unavailable
- Cloud Run deployment

### Agent Harness Service (AHS) — Feb 15–16, two days
5 PRs delivered the core runtime:
- **PR 0:** DB schema — `Agent`, `AgentSession`, `AgentSessionMessage` with Alembic migration
- **PR 1:** Core service — session lifecycle, Claude CLI runner, workspace management, turn management
- **PR 2:** Local MCP tools + repository management for agents
- **PR 3:** GCE VM deployment scripts and operational playbook
- **PR 4–5:** System prompts, CLI tooling, logging

### Wiring (Feb 17)
SAG ↔ AHS connected: real agents responding in Slack within 72 hours of the first commit.

---

## Phase 3: Rapid Feature Expansion (Feb 18 – Mar 8, 2026)

### Multi-model orchestration
- **V2 executor interfaces** — abstraction layer allowing pluggable agent runtimes
- **Subagent support** — `new_task` for delegation, `route_model` for model selection
- **Raw executor loop** — direct Anthropic/OpenAI API calls without CLI overhead
- **MCP support for raw executors** — non-CLI agents get the same tooling

### Context management (3-phase system)
1. **Tool result truncation** — large outputs compressed before storage
2. **Within-turn pruning + LLM compaction** — when context window fills mid-turn
3. **Cross-turn history** — conversation history across turns with smart truncation

### Security & sandboxing
- **Bubblewrap (bwrap)** OS-level sandboxing — filesystem isolation without Docker overhead
- Per-agent permission model (read/write/execute scoping)
- GitHub device flow for user-attributed commits (agents create PRs as the user)

### Agent scheduling
- `ScheduledAgentCall` model — cron-style recurring agent execution
- MCP tools for schedule CRUD (agents can schedule themselves and each other)
- Trigger-now endpoint for on-demand execution

### Infrastructure hardening
- Message queuing when agent is busy (ordered delivery guarantee)
- Token caching for API cost reduction
- WebSocket streaming endpoint for real-time output
- Session persistence to GCS for durability
- REST API for external integrations
- Attachment support with GCS storage
- Exponential backoff with dead-letter alerting

### Named agents proliferated
From 1 (harnessed Claude Code) to 15+ including `eng-terse` (raw executor), `eng-worker` (subagent), `bookkeeper` (memory retrieval on MiniMax), `data-scientist` (privacy-aware analysis), `sre` (infrastructure monitoring).

---

## Phase 4: Project Management & Linear Integration (Mar 6–17, 2026)

**Goal:** Agents that manage their own work.

### Agent project/task system
- `AgentProject` and `AgentTask` DB models with full lifecycle (PENDING → READY → IN_PROGRESS → COMPLETED/FAILED)
- Atomic task claiming via `SELECT ... FOR UPDATE SKIP LOCKED`
- Per-project rate limiting and capacity management
- Task dependency chains and sequencing
- Resumable failure handling

### Linear bidirectional sync
- Export AHS projects → Linear (with status/priority mapping)
- Import Linear projects → AHS (full issue sync)
- Continuous sync with conflict resolution strategies (LATEST_WINS, LINEAR_WINS, AHS_WINS, SKIP)
- Attachments sync (PRs, session links)

### Self-improvement loop
Agents began running **daily performance reports** analyzing their own session data, surfacing recommendations, and creating follow-up tasks — a self-improving feedback loop.

---

## Phase 5: Architecture Enforcement & Repo Extraction (Mar 20–30, 2026)

### Layered architecture (enforced by tests)
After `service.py` grew to 121K (3,489 lines, 58 functions), formal boundaries were introduced:

```
Layer 0 (common/)     — Types, config, constants. Zero AHS deps.
Layer 1 (5 packages)  — core/, gateway/, executors/, tools/, projects/
                        Each depends ONLY on common/. No cross-imports.
Wiring layer (root)   — service.py, server.py, routes.py, orchestration.py
                        Only place cross-package imports are allowed.
```

- `test_architecture.py` + `test_imports.py` enforce boundaries in CI
- Callback/DI pattern for cross-layer communication (e.g., tools needing orchestration)

### Service decomposition
- `service.py` split into a package: `run_task.py`, `session_lifecycle.py`, `queries.py`, `agent_crud.py`, `message_helpers.py`
- `_run_agent_task` (821 lines, 73 if-statements) decomposed into sequential phase functions
- Backward-compatible re-exports for 9 existing callers

### Repo extraction (Mar 23–27)
Agent infrastructure split from yupp-mind into standalone **yupp-agent** repository:
- Full git history preserved (12,900+ commits)
- Four services independently deployable
- Separate CI/CD pipeline
- 20+ PRs in 2 days stabilizing deployment

### Executor v2 — three parallel tracks
| Track | What | Speedup |
|-------|------|---------|
| **A: BCH Proxy** | Persistent Go binary replacing per-call bwrap spawns | ~300ms → ~10ms per tool call |
| **B: Claude Agent SDK** | In-process SDK replacing CLI subprocess | ~4s → ~200ms turn start |
| **C: Codex App Server** | WebSocket JSON-RPC sidecar replacing per-turn CLI | Eliminates cold start |

Expected: **~26s saved per 5-turn session, ~39% E2E speedup**.

---

## Phase 6: AHS Monolith — One-Box Deployment (Apr 2–4, 2026)

**Goal:** Run the entire platform on a single machine for $25/mo instead of $500/mo.

### 9-step implementation plan, executed in 3 days
1. Extract AHS lifespan and middleware
2. Decompose `local_mcp_server.py` (92K) into focused tool modules
3. Extract SAG lifespan
4. Extract MCP server lifespan
5. Create `ypl/mono_server/` unified entrypoint
6. Smoke test + auth middleware fix
7. Setup wizard + user management CLI
8. Unified MCP (merge both FastMCP instances)
9. Gateway plugin interface (SAG as first plugin, GitHub as second)

### Deployment modes
| Mode | Target | How |
|------|--------|-----|
| Docker Compose | Developer laptop | `docker-compose up` |
| Bare metal | GCE VM | `setup_vm.sh` + systemd |
| MacBook | Personal use | `scripts/run_local.sh` |
| Cloudflare Tunnel | Public access | Tunnel config in playbook |

Both distributed (Cloud Run + GCE + Cloud SQL) and monolith deployments remain supported and tested.

---

## Phase 7: Agent-to-Agent Messaging (A2A) (Apr 3–13, 2026)

**Goal:** Enable agents to communicate directly, forming collaborative networks.

### Design
- Every agent gets a **user identity** (`user_type=AGENT`)
- **Deny-by-default authorization** — explicit allowlists for which agents can message which
- **Two delivery scenarios:**
  - **Scenario A:** New session creation (BLPOP-based, creates fresh context)
  - **Scenario B:** Injection into existing session (turn-boundary drain, preserves context)
- At-least-once delivery with atomic DB claim and crash recovery
- `FELLOW_AGENT` turn type distinguishes agent messages from human messages

### Implementation (6 PRs)
1. `AgentMessage` ORM model and A2A enums
2. Alembic migration + agent user backfill
3. Agent user identity hooks (create_agent + startup sweep)
4. Authorization model
5. `send_agent_message` MCP tool
6. Session creation with `trigger=AGENT`

---

## Phase 8: Test Coverage Campaign (Apr 4–10, 2026)

Systematic push from **0% to 70% enforced coverage** in 6 days:

| Day | Coverage | What |
|-----|----------|------|
| Apr 4 | 33% | tools/ (~70%), common/core (90%/80%), SAG (60%) |
| Apr 6 | 45% | executors (60%), gateway (70%), MCP tools (70%) |
| Apr 7 | 55% | service wiring (50%), SAG callbacks (70%), backend (80%) |
| Apr 9 | 60% | redis_utils (21% → 94%), remaining gaps |
| Apr 10 | 70% | `fail_under=70%` enforced in CI |

30+ PRs, primarily by one contributor, covering all 4 services.

---

## Active Projects (as of Apr 13, 2026)

From the agent_projects table — work being tracked by agents themselves:

| Project | Status | Description |
|---------|--------|-------------|
| Linear ↔ AHS Sync | ACTIVE | Bidirectional project/task sync |
| First-Message Latency | ACTIVE | Reducing cold-start latency for all executors |
| AHS Performance | ACTIVE | Continuous daily self-assessment improvements |
| Slack Messaging | ACTIVE | Reducing bookkeeping burden for task-executor agents |
| Biz Intel Bot | ACTIVE | Market research automation with 7 pivot directions |
| Agent SRE Follow-ups | ACTIVE | Daily reliability improvements across all services |
| AHS Monolith | ACTIVE | One-box deployment consolidation |
| A2A Messaging | ACTIVE | Agent-to-agent communication protocol |
| Test Coverage 33%→80% | ACTIVE | Continuing coverage push |
| AHS/SAG Scaling Phase 1 | PAUSED | Pre-scale blockers for horizontal scaling |
| AHS/SAG Scaling Phase 2 | PAUSED | ~100 concurrent user capacity |
| AHS Search Engine | PAUSED | Full-text search (ILIKE v0, tsvector v1) |
| Conversation Security | COMPLETED | Attack detection, incident reporting, weekly audits |
| Executor v2 | COMPLETED | BCH proxy + Claude SDK + Codex sidecar |
| Remove AI Slop | PAUSED | Systematic cleanup of AI-generated code quality issues |

---

## Technical Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│                    Mono Server (optional)                │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌──────────┐  │
│  │   AHS   │  │   SAG   │  │   MCP    │  │Streamlit │  │
│  │ Runtime │  │ Gateway │  │  Server  │  │Dashboard │  │
│  └────┬────┘  └────┬────┘  └────┬─────┘  └──────────┘  │
│       │             │            │                       │
│  ┌────┴─────────────┴────────────┴──────────────────┐   │
│  │              Shared Backend Layer                 │   │
│  │  (DB config, SQLModel models, Alembic, utils)    │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │                              │
    ┌────┴────┐                   ┌─────┴─────┐
    │PostgreSQL│                   │   Redis   │
    │ (yadb)  │                   │(sessions) │
    └─────────┘                   └───────────┘

AHS Internal Layering:
  Layer 0: common/ (types, config, constants)
  Layer 1: core/ | gateway/ | executors/ | tools/ | projects/
  Wiring:  service/ | server.py | routes.py | orchestration.py

Executor Framework:
  ┌──────────────────┐
  │  ExecutorResult   │ ← unified contract
  ├──────────────────┤
  │ ClaudeCodeRunner  │ ← CLI subprocess
  │ ClaudeAgentSDK    │ ← in-process SDK
  │ CodexAppServer    │ ← WebSocket sidecar
  │ RawExecutorLoop   │ ← direct API calls
  │ MockRunner        │ ← testing
  └──────────────────┘
```

---

## Key Technical Decisions & Rationale

| Decision | Alternatives Considered | Why This Choice |
|----------|------------------------|-----------------|
| GCE VM (not containers) for AHS | ECS, Cloud Run, K8s | Agents spawn CLI subprocesses that need persistent filesystem, git repos, and long-running sessions |
| Bubblewrap sandboxing | Docker-in-Docker, gVisor | Process-level isolation without container overhead on VM workload |
| Redis for SAG state | PostgreSQL, in-memory | Stateless gateway design; Redis provides speed + TTL + pub/sub without schema migration |
| Callback/DI for cross-layer | Direct imports, event bus | Keeps Layer 1 packages independent; testable; no runtime overhead |
| Monolith as additive mode | Replace distributed | Both deployment targets must work; monolith for dev/personal, distributed for production scaling |
| FastMCP 2.0 | Raw MCP protocol, LangChain tools | Standards-based, composable, good Python ergonomics; later unified two instances into one |
| SQLModel over raw SQLAlchemy | Django ORM, Prisma | Pydantic integration for FastAPI; async support; type-safe queries |
| Atomic task claiming (FOR UPDATE SKIP LOCKED) | Redis queue, optimistic locking | PostgreSQL-native; prevents double-claiming without separate queue infrastructure |

---

## Development Velocity & Patterns

- **72 hours** from first commit to agents responding in Slack
- **18 days** to go from repo extraction to 174 merged PRs
- **6 days** for test coverage 0% → 70%
- **3 days** for full monolith implementation (9-step plan)
- **Sprint-and-stabilize rhythm**: rapid feature bursts followed by architecture enforcement, test campaigns, and reliability hardening
- **Self-improving system**: agents run daily performance reports, surface recommendations, and create their own follow-up tasks
- **AI contributors**: 251 commits authored by AI identities — agents contributing to their own platform

---

## Scale & Complexity Metrics

| Metric | Value |
|--------|-------|
| Total codebase commits (with history) | 12,900+ |
| Agent-specific PRs (yupp-agent standalone) | 174 in 18 days |
| Agent-specific PRs (yupp-mind era) | ~150 |
| Named agent configurations | 27 |
| MCP tools available to agents | 80+ |
| Agent skills | 33+ |
| DB models (agent domain) | 12+ (Agent, Session, Message, Task, Project, Schedule, Artifact, Memory, etc.) |
| Test coverage | 70% enforced |
| Active agent projects | 9 |
| Deployment modes | 4 (Docker Compose, bare metal, MacBook, Cloudflare Tunnel) |
| Lines in largest file at peak | service.py: 3,489 lines (before decomposition) |

---

## Contributors

| Contributor | Focus Areas | Notable |
|-------------|-------------|---------|
| **Tian Wang** | AHS architect & primary developer (~380 AHS commits). Core service, executor framework, context management, project system, A2A protocol, monolith, sandboxing, scheduling | Built the core runtime in a weekend; designed and enforced the layered architecture |
| **Lingfeng Guan** | SAG creator (62 commits), MCP server co-developer (59 commits), AHS contributor (100 commits). Linear integration, test coverage campaign (33%→70%), streaming, error handling | Built SAG in one day (14 PRs); drove systematic test coverage |
| **Gilad Mishne** | Platform founder. LLM routing, leaderboard, initial architecture | Created the parent platform; provided infrastructure AHS built upon |
| **AI Agents** | 251 commits across multiple identities. Oncall fixes, infrastructure hardening, automated review | Agents contributing to their own codebase — a recursive self-improvement loop |

---

*Document generated April 13, 2026. Based on git history, PR records, agent project database, and in-repo documentation.*
