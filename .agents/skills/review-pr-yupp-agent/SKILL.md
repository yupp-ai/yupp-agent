---
name: review-pr-yupp-agent
description: Repo-specific review guidelines for yupp-agent (agent harness, Slack gateway, MCP server, Streamlit dashboards).
---

# Review PR — yupp-agent

Repo-specific guidelines for reviewing PRs in `yupp-ai/yupp-agent`.

## Architecture awareness

- **AHS three-layer architecture**: Layer 0 (`common/`), Layer 1 (`core/`, `gateway/`, `executors/`, `tools/`, `projects/`), Wiring layer (root files). Layer 1 packages must NOT import from each other or from root wiring files. Cross-package functionality uses callback/DI patterns.
- **Service boundaries**: AHS, SAG, MCP Server, and Streamlit are separate services. Changes should not create unintended coupling between them.
- **Executors must not write to DB or import `service.py`**.

## What to watch for

- **Import violations**: Layer 1 packages importing from each other or from wiring-layer files (`service.py`, `server.py`, `routes.py`, `orchestration.py`, `task_executor.py`, `scheduler.py`)
- **DB model changes**: Alembic migrations must be additive. Check that new columns are nullable or have defaults.
- **Secret handling**: Secrets come from GCP Secret Manager. Never hardcode secrets. Check `data/secret-env-var-map.yml` for correct mappings.
- **Feature flags**: Check `data/feature_flags.yml` for correct flag usage and that both on/off states work.
- **Async patterns**: Ensure proper use of `async`/`await`, especially around DB sessions (`get_async_session`).
- **Ruff rules**: Line length 120, timezone-aware datetimes (DTZ), use `sqlmodel.select` not SQLAlchemy's.
- **MyPy strict mode**: Proper type annotations, Pydantic plugin compatibility.
