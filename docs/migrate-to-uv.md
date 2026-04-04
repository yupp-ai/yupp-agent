# Migrating yupp-agent from Poetry to uv

## Overview

Replace Poetry with [uv](https://github.com/astral-sh/uv) as the package manager and build tool. uv is significantly faster, supports PEP 621 natively, and provides workspace support already partially configured in this repo.

## Current State

- Build system: `poetry-core`
- Lock file: `poetry.lock` (+ a `uv.lock` already exists)
- Dependencies declared in `[tool.poetry.dependencies]` and `[tool.poetry.group.dev.dependencies]`
- A `[tool.uv.workspace]` section already exists with `example` as a member

---

## Step 1: Change the build system

Replace the Poetry build backend with hatchling:

```toml
# Before
[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"

# After
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

## Step 2: Move dependencies to PEP 621 format

### Production dependencies

Move all deps from `[tool.poetry.dependencies]` to `[project.dependencies]` as a list of PEP 508 strings.

```toml
# Before
[tool.poetry.dependencies]
python = ">=3.12.12,<3.13"
pydantic = {version = "2.11.10", extras = ["email"]}
pydantic-settings = "2.10.1"
python-dotenv = "^1.2.1"
alembic = "^1.17.2"
# ...

# After
[project]
name = "ypl-agent"
version = "0.0.1"
description = "Agent infrastructure service for Yupp"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
    "pydantic[email]==2.11.10",
    "pydantic-settings==2.10.1",
    "python-dotenv>=1.2.1",
    "alembic>=1.17.2",
    "asyncpg==0.31.0",
    "pgvector==0.2.5",
    "psycopg2-binary==2.9.11",
    "sqlalchemy[asyncio]==2.0.44",
    "sqlmodel==0.0.27",
    "cloud-sql-python-connector[asyncpg]>=1.20",
    "fastapi==0.122.0",
    "uvicorn[standard]==0.40.0",
    "uvloop==0.22.1",
    "orjson>=3.11.5",
    "python-multipart>=0.0.20",
    "httpx==0.28.1",
    "aiohttp==3.11.16",
    "requests>=2.32.5",
    "fastmcp>=2.14.4",
    # ... remaining deps follow the same pattern
]
```

### Dev dependencies

Move from `[tool.poetry.group.dev.dependencies]` to `[dependency-groups]`:

```toml
# Before
[tool.poetry.group.dev.dependencies]
mypy = "^1.19.0"
pytest = "^8.3.3"
ruff = "^0.14.9"
# ...

# After
[dependency-groups]
dev = [
    "mypy>=1.19.0",
    "pytest>=8.3.3",
    "pytest-alembic>=0.11.1",
    "pytest-asyncio>=0.24.0",
    "pytest-mock>=3.14.0",
    "ruff>=0.14.9",
    "pytest-timeout>=2.4.0",
    "types-requests>=2.33.0.20260327",
    "types-pyyaml>=6.0.12.20250915",
    "types-cachetools>=6.2.0.20260317",
]
```

### Key syntax differences

| Poetry                                                   | uv (PEP 621)                          |
| -------------------------------------------------------- | ------------------------------------- |
| `pydantic = {version = "2.11.10", extras = ["email"]}`   | `"pydantic[email]==2.11.10"`          |
| `"^1.17.2"` (caret)                                      | `">=1.17.2"` (explicit lower bound)   |
| `python = ">=3.12.12,<3.13"`                             | `requires-python = ">=3.12,<3.13"`    |
| `[tool.poetry.group.dev.dependencies]`                   | `[dependency-groups] dev = [...]`     |

### Remove the `[tool.poetry]` section entirely

Delete the entire `[tool.poetry]` block — its metadata is now in `[project]`.

## Step 3: Update all commands

| Old (Poetry)             | New (uv)            |
| ------------------------ | ------------------- |
| `poetry install`         | `uv sync`           |
| `poetry add foo`         | `uv add foo`        |
| `poetry add --group dev` | `uv add --group dev`|
| `poetry run pytest`      | `uv run pytest`     |
| `poetry run ruff check`  | `uv run ruff check` |
| `poetry lock`            | `uv lock`           |

## Step 4: Update CI workflows

**File:** `.github/workflows/lint_and_test.yml`

- Install uv (replace Poetry installation):
  ```yaml
  - name: Install uv
    uses: astral-sh/setup-uv@v4
  ```
- Replace `poetry install` with `uv sync`
- Replace all `poetry run` with `uv run`

## Step 5: Update Dockerfiles

- Install uv instead of Poetry:
  ```dockerfile
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
  ```
- Replace `poetry install --no-dev` with `uv sync --no-group dev`
- Replace `poetry install` with `uv sync`
- Copy `uv.lock` instead of `poetry.lock`

## Step 6: Update entrypoint scripts

Update all `entrypoint.sh` files that use `poetry run`:
- `ypl/agent_harness_service/entrypoint.sh`
- `ypl/slack_agent_gateway/entrypoint.sh`
- `ypl/mcp_server/entrypoint.sh`

Replace `poetry run` with `uv run` in each.

## Step 7: Update CLAUDE.md

Replace all `poetry run` references with `uv run` in the project instructions.

## Step 8: Clean up

- Delete `poetry.lock`
- Regenerate `uv.lock` with `uv lock`
- Verify with `uv sync` and run the full test suite: `uv run pytest -m 'not alembic' --timeout=120`

## Step 9: Verify workspace

The `[tool.uv.workspace]` section already exists. Ensure `example/pyproject.toml` has a valid `[project]` table so it participates in the workspace correctly.

---

## Validation checklist

- [ ] `uv sync` succeeds
- [ ] `uv run ruff check .` passes
- [ ] `uv run mypy --config-file=pyproject.toml .` passes
- [ ] `uv run pytest -m 'not alembic' --timeout=120` passes
- [ ] CI pipeline passes (lint, mypy, test, alembic)
- [ ] Docker build succeeds
- [ ] Local services start via entrypoint scripts
