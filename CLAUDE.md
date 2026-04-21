# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

yupp-agent is a multi-agent cloud platform for building, deploying, and operating autonomous AI agents at scale. It contains four services: Agent Harness Service (AHS), Slack Agent Gateway (SAG), MCP Server, and Streamlit dashboards.

## Common Commands

### Lint & Format
```bash
poetry run ruff check . --output-format=github    # lint
poetry run ruff format . --check                   # format check
poetry run ruff format .                           # auto-format
poetry run ruff check . --fix                      # auto-fix lint issues
```

### Type Checking
```bash
poetry run mypy --config-file=pyproject.toml .
# For specific files:
poetry run mypy --config-file=pyproject.toml ypl/agent_harness_service/service.py
```

### Tests
```bash
poetry run pytest -m 'not alembic' --timeout=120   # all unit tests (skip DB migration tests)
poetry run pytest tests/agent_harness_service/      # AHS tests only
poetry run pytest tests/slack_agent_gateway/        # SAG tests only
poetry run pytest tests/path/to/test_file.py        # single test file
poetry run pytest tests/path/to/test_file.py::test_function_name  # single test
```

Alembic tests require a running Postgres (`docker-compose up -d`):
```bash
ALEMBIC_TEST_DB_URL=postgresql://postgres:postgres@localhost:5432/test_yadb \
  poetry run pytest --test-alembic tests/conftest.py tests/alembic/
```

### Database Migrations
```bash
alembic -c alembic.ini revision --autogenerate -m "description"  # create migration
alembic -c alembic.ini upgrade head                               # apply locally
```

### Running Services Locally
```bash
docker-compose up -d                                    # start Postgres + Redis
./ypl/agent_harness_service/entrypoint.sh               # AHS (port 8090)
python -m ypl.agent_harness_service.tui --host localhost:8090  # TUI client
./ypl/slack_agent_gateway/entrypoint.sh                 # SAG
./ypl/mcp_server/entrypoint.sh                          # MCP Server
streamlit run ypl/streamlit_server/app.py               # Dashboards (port 8501)
```

## Architecture

### Service Layout
```
ypl/
├── agent_harness_service/   # AHS — agent runtime (needs host filesystem: git repos + sandboxes)
├── slack_agent_gateway/     # SAG — Slack bot bridge
├── mcp_server/              # MCP tool server for IDEs (DevToken + OAuth modes)
├── mcp_common/              # Shared MCP utilities
├── streamlit_server/        # Operational dashboards
├── mono_server/             # Single-process entrypoint (AHS + SAG + MCP)
├── backend/                 # Shared backend (DB config, utils)
└── db/                      # SQLModel models + Alembic migrations

apps/
└── war-room/                # Next.js 16 admin UI for AHS (Bun, port 3009, standalone — see apps/war-room/AGENTS.md)
```

### AHS Layering (Critical Constraint)

AHS follows a strict three-layer architecture enforced by tests (`test_architecture.py`, `test_imports.py`):

- **Layer 0 (`common/`):** Types, config, constants. Zero AHS deps. Imported by everything.
- **Layer 1 (`core/`, `gateway/`, `executors/`, `tools/`, `projects/`):** Five independent packages. Each depends only on `common/`. They **must not** import from each other or from root wiring files.
- **Wiring layer (root files: `service.py`, `server.py`, `routes.py`, `orchestration.py`, `task_executor.py`, `scheduler.py`):** Composes Layer 1 packages together. Only place where cross-package imports are allowed.

When a Layer 1 package needs wiring-layer functionality, use the callback/DI pattern (see `tools/local_mcp_server.py` + `register_orchestration_callbacks()`).

### Key Invariants
1. One active turn per session — messages queued in order
2. Workspace created before executor starts
3. Metadata persisted to DB before runner subprocess spawns
4. Executors must not write to DB or import `service.py`
5. Gateway delivery via registered callbacks, never direct imports

## Code Conventions

- **Python 3.12**, managed by Poetry
- **Line length:** 120
- **Imports:** Use `sqlmodel.select` and `sqlmodel.ext.asyncio.session.AsyncSession`, not SQLAlchemy's versions (enforced by ruff banned-api)
- **Async:** pytest uses `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio`
- **Ruff rules:** B, C4, E, F, FURB, I, PERF, PIE, RET, SIM11, TID, UP, W, RUF1, DTZ (timezone-aware datetimes required)
- **MyPy:** Strict mode with Pydantic plugin. Excludes `ypl/db/alembic/`, `tmp/`, `scripts/`

## CI Pipeline

The `lint_and_test.yml` workflow runs on PRs and pushes to main. All four jobs must pass:
1. **Lint** — ruff check + format
2. **MyPy** — strict type checking
3. **Test** — `pytest -m 'not alembic' --timeout=120`
4. **Alembic tests** — only runs when `ypl/db/` or `alembic.ini` changes

## Deployment

- **Monolith VM:** the primary shape — `ypl.mono_server.server:app` runs AHS + SAG + MCP in one uvicorn process; Streamlit runs separately. See `DEPLOYMENT.md` for Docker Compose, bare-metal systemd, and MacBook options.
- **First-time setup:** `python -m ypl.mono_server.setup` runs Alembic migrations, seeds roles, and creates the admin + system users from `.env`.
- **Secrets:** `.env` on the VM. The platform does not require a hosted secret manager; plug one in via environment injection if you prefer.

## Key Files

| File | Purpose |
|------|---------|
| `ypl/agent_harness_service/service.py` | Session lifecycle hub (largest file, 121K) |
| `ypl/agent_harness_service/tools/local_mcp_server.py` | All local MCP tool definitions (92K) |
| `ypl/agent_harness_service/ARCHITECTURE.md` | Detailed AHS layering documentation |
| `ypl/db/models/agent_harness.py` | Core DB models (Agent, AgentSession, AgentTask, etc.) |
| `data/feature_flags.yml` | Runtime feature toggles |
| `docs/secrets.md` | How to populate `.env` from GCP Secret Manager / AWS SSM / Vault / k8s |
| `.agents/skills/` | 33 agent skill definitions (MCP tools) |
| `ypl/agent_harness_service/deploy/agent_configs/` | Per-agent config.json + ROLE.md files |
