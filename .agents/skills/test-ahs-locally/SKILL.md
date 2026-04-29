---
name: test-ahs-locally
description: End-to-end testing workflow for Agent Harness Service (AHS) changes. Use when developing new agents, modifying agent configs, or testing MCP tool integrations locally.
allowed-tools: Bash, Read, Write, Grep, Glob, AskUserQuestion
---

# Testing AHS Changes Locally

Use this skill when you need to test Agent Harness Service changes locally before submitting a PR. This covers testing new agents, config changes, tool permissions, and MCP integrations.

For comprehensive setup instructions, see `ypl/agent_harness_service/LOCAL_TEST.md`.

## Test Cases

Individual test cases are in the `tests/` directory:
- `tests/test-bookkeeper.md` — Test the bookkeeper memory retrieval agent

To run a specific test, read the test file and follow its steps after completing setup.

---

## Setup

### 1. Prerequisites

```bash
# If in a worktree, copy .env from main repo
cp ~/source/yupp-agent/.env .

# Ensure local database settings in .env
# DATABASE_HOST=127.0.0.1
# DATABASE_PORT=5432

# Run migrations
poetry run alembic upgrade head
```

### 2. Get API Key

**Get and export the API key** (in order of precedence):
1. Check if `$AGENT_HARNESS_SERVICE_API_KEY` is already set in the environment
2. Extract from `.env` file and export it:
   ```bash
   export AGENT_HARNESS_SERVICE_API_KEY=$(grep "^AGENT_HARNESS_SERVICE_API_KEY=" .env | cut -d'=' -f2 | tr -d '"')
   ```
3. If not found in either, use `AskUserQuestion` to prompt the user to provide it, then save to `.env` and export:
   ```bash
   echo 'AGENT_HARNESS_SERVICE_API_KEY="<user-provided-key>"' >> .env
   export AGENT_HARNESS_SERVICE_API_KEY="<user-provided-key>"
   ```

### 3. Start the AHS Server

```bash
export AHS_DATA_DIR=/tmp/ahs
export PYTHONPATH=$(pwd)

# Create data directories (one-time setup)
mkdir -p $AHS_DATA_DIR/repos
ln -sfn $(pwd)/ypl/agent_harness_service/deploy/shared $AHS_DATA_DIR/shared
ln -sfn $(pwd) $AHS_DATA_DIR/repos/yupp-agent

# Start server (logs to stdout; optionally redirect to file)
poetry run uvicorn ypl.agent_harness_service.server:app \
  --host 0.0.0.0 --port 8090 --log-level info 2>&1 | tee /tmp/ahs_server.log
```

### 4. (Optional) Start Streamlit Console

For a visual interface to view agent sessions and logs:

```bash
poetry run streamlit run ypl/streamlit_server/app.py
```

Then open http://localhost:8501/agent_harness_console in your browser.

---

## Running Tests

### Run a specific test
1. Read the test file: `Read .agents/skills/test-ahs-locally/tests/test-<name>.md`
2. Follow the test steps
3. Verify expected results

### Run all tests
1. List test files: `ls .agents/skills/test-ahs-locally/tests/`
2. Run each test sequentially
3. Report pass/fail for each

---

## API Quick Reference

### Create Session
```bash
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" \
  -d '{"agent_id": "<agent>", "trigger": "api", "message": "<prompt>"}' | jq .
```

### Check History
```bash
curl -s "http://localhost:8090/ahs/session/<session_id>/history" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" | jq .
```

### Send Follow-up Message
```bash
curl -s -X POST http://localhost:8090/ahs/session/message \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" \
  -d '{"session_id": "<session_id>", "message": "<follow-up>"}' | jq .
```

### Stop Session
```bash
curl -s -X POST http://localhost:8090/ahs/session/stop \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" \
  -d '{"session_id": "<session_id>"}' | jq .
```

### Check Logs
Server logs go to stdout by default. If you started the server with `| tee /tmp/ahs_server.log`:
```bash
tail -100 /tmp/ahs_server.log
```
Otherwise, check the terminal where the server is running, or use the Streamlit console.

---

## Common Issues

### `tool_count=0` after filtering
Tool permission names must match raw MCP tool names (e.g., `search_memory`, `load_memory`), not prefixed names.

### `{"detail":"Not Found"}`
Agent config doesn't exist or server needs restart.

### Model not calling tools
Use a model that supports tools: `anthropic/claude-haiku-4-5`, `anthropic/claude-sonnet-4-6`, `openai/gpt-4o`.

### API key errors (401)
Copy fresh `.env` from main repo or set `AGENT_HARNESS_SERVICE_API_KEY`.

---

## Cleanup

```bash
pkill -f "uvicorn ypl.agent_harness_service.server:app"
```
