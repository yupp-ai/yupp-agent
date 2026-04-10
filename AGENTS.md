# AGENTS.md

## Cursor Cloud specific instructions

### Overview

yupp-agent is a Python 3.12 multi-service platform. See `CLAUDE.md` for full command reference (lint, test, type-check, run services).

### Environment

- **Python 3.12.12** via pyenv (configured in `~/.bashrc`; pyenv shim activates automatically in `/workspace`).
- **Poetry** manages all Python dependencies. The virtualenv lives at `/workspace/.venv`.
- **Docker** is required for Postgres (`pgvector/pgvector:pg16` on port 5432) and Redis (`redis:7-alpine` on port 6379), started via `sudo docker compose up -d`.
- Docker daemon must be started manually: `sudo dockerd &>/tmp/dockerd.log &` — wait a few seconds before running `docker compose`.
- Docker commands require `sudo` (the user is not in the `docker` group).

### Local `.env` file

A `.env` at the repo root is required. Minimum for local development:

```
ENVIRONMENT=local
DEFAULT_DB=agentdb
POSTGRES_CONNECTION_AGENTDB={"user":"postgres","password":"postgres","host":"localhost:5432","database":"yupp_agent"}
REDIS_URL=redis://localhost:6379/1
USE_GOOGLE_CLOUD_LOGGING=false
DISABLE_WRITE_GOOGLE_CLOUD_METRICS=true
AGENT_HARNESS_SERVICE_API_KEY=test-dev-key-12345
X_API_KEY=test-dev-key-12345
```

### Running services

1. Start infra: `sudo docker compose up -d`
2. Run migrations: `poetry run alembic -c alembic.ini upgrade head`
3. Start AHS: `poetry run uvicorn ypl.agent_harness_service.server:app --host 0.0.0.0 --port 8090 --loop uvloop --log-config=ypl/uvicorn-logging.yml`
4. Health check: `curl http://localhost:8090/health` → `{"status":"ok"}`

### Gotchas

- `poetry lock` may be needed if `pyproject.toml` has changed since the lockfile was last generated (Poetry 2.x is stricter about this).
- Alembic tests require a separate `test_yadb` database: `sudo docker exec workspace-postgres-1 psql -U postgres -c "CREATE DATABASE test_yadb;"`  
  Then run with: `ALEMBIC_TEST_DB_URL=postgresql://postgres:postgres@localhost:5432/test_yadb poetry run pytest --test-alembic tests/conftest.py tests/alembic/`
- The `.env` file is gitignored; each cloud agent session needs to recreate it.
- AHS API endpoints require `X-API-Key` header matching `AGENT_HARNESS_SERVICE_API_KEY` from `.env`.
