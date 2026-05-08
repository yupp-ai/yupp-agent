# Test: Bookkeeper Agent

## Description
Test the bookkeeper agent which searches and compiles context from the shared agent memory system.

## Prerequisites
- AHS server running (see main skill)
- Agent config exists at `ypl/agent_harness_service/deploy/agent_configs/bookkeeper/`
- `YUPPSTER_MCP_TOKEN` set in `.env` (run `python -m scripts.gcpsecrets pull-local-secrets` if missing)

## Test Steps

### 1. Create session and send query
```bash
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" \
  -d '{
    "agent_id": "bookkeeper",
    "trigger": "api",
    "message": "Find all memory entries related to streaming errors"
  }' | jq .
```

### 2. Wait and check history
```bash
# Wait for agent to complete (15-30 seconds)
sleep 20

curl -s "http://localhost:8090/ahs/session/<session_id>/history" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" | jq .
```

## Expected Results

### Server Logs
Check the server terminal output (or `/tmp/ahs_server.log` if you started with `| tee`) for:
- `tool_count=5` for agcouch MCP server (list_memory, search_memory, load_memory, save_memory, report_security_incident — see `bookkeeper/config.json` `tool_permissions`)
- `Agent config loaded for task  agent_name=bookkeeper`

### Session Response
- `status`: Should be `COMPLETED` when finished
- `messages`: Should contain AGENT response with markdown document listing memory entries or stating none found

## Validation Criteria
1. Agent successfully calls `list_memory`, `search_memory`, and/or `load_memory` tools
2. Agent returns structured markdown output
3. No tool permission errors in logs
4. No hallucinated `<function_calls>` text (tools should be actual API calls)

## Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `tool_count=0` | Wrong tool permission names | Use `search_memory` not `mcp__harness__search_memory` |
| `<function_calls>` in output | Model doesn't support tools | Use claude-haiku-4-5 or better |
| 401 Unauthorized | Bad API key | Check AGENT_HARNESS_SERVICE_API_KEY |
