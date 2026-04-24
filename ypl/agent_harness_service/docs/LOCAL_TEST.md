# Agent Harness Service — Local Testing

## Prerequisites

- Local Postgres running with migrations applied (`poetry run alembic upgrade head`)
- Claude Code CLI installed and authenticated (`claude --version` should work)
- Your `.env` has `ENVIRONMENT=local`

### Verify Claude Code CLI works

Run this once to confirm the CLI is authenticated and functional:

```bash
claude -p 'Say hello in one word.' --output-format stream-json --verbose --max-turns 1
```

You should see JSON events streaming. If it hangs or prompts for login, fix that first.


## 1. Set up data directories

The server auto-detects agent configs from the in-tree `deploy/agent_configs/`
directory when no `/data/ahs/agents` or `$AHS_DATA_DIR/agents` directory exists.
You only need to create the repos and shared directories:

```bash
export AHS_DATA_DIR=/tmp/ahs
mkdir -p $AHS_DATA_DIR/repos

# Symlink shared files and repos
ln -sfn $(pwd)/ypl/agent_harness_service/deploy/shared $AHS_DATA_DIR/shared
ln -sfn $(pwd) $AHS_DATA_DIR/repos/yupp-agent
```

> **Note:** Agent configs are loaded directly from
> `ypl/agent_harness_service/deploy/agent_configs/` — no symlink needed for local dev.
> To override this, create `$AHS_DATA_DIR/agents/` or set `AHS_AGENTS_DIR` explicitly.

### What the server sees

```
/tmp/ahs/
├── repos/            ← agent starts here (cwd, read-only)
│   └── yupp-agent/   → symlink to your local checkout
├── shared/           → symlink to deploy/shared/
│   ├── SOUL.md                    (shared identity)
│   ├── WORKSPACE.md               (repo guide, injected into system prompt)
│   ├── ACTIVE_MEMORY_MANAGEMENT.md
│   └── SLACK_GATEWAY.md           (only for Slack-triggered sessions)
├── workspaces/       (auto-created, per-session git worktrees)
└── session_logs/     (auto-created, per-session event logs)

Agent configs (auto-detected from repo):
ypl/agent_harness_service/deploy/agent_configs/
├── code-reviewer/    (harnessed, read-only, MCP)
├── codex-test/       (harnessed via codex-cli)
├── coordinator/      (harnessed, spawns subagents)
├── data-scientist/   (harnessed, full access, MCP)
├── default/          (harnessed, full access, MCP)
├── dual-reviewer/    (harnessed, spawns reviewer + fixer)
├── eng-terse/        (harnessed, read-only + Bash, MCP)
├── fixer/            (harnessed, full access, MCP)
├── parrot-bubba/     (mock executor, for testing)
├── raw-test/         (raw executor, no tools)
├── reviewer/         (raw executor, read-only tools)
├── router/           (raw executor, one-shot classification)
└── sre/              (harnessed, Bash + read-only, MCP)
```

### Agent executor types

| Executor | How it runs | Examples |
|----------|-------------|---------|
| **harnessed** (claude-code-cli) | Spawns Claude Code CLI subprocess | sre, data-scientist, code-reviewer, coordinator |
| **harnessed** (codex-cli) | Spawns OpenAI Codex CLI subprocess | codex-test |
| **raw** | Direct Anthropic/OpenAI API calls | raw-test, reviewer, router |
| **mock** | Logs prompt, returns synthetic response | parrot-bubba |

## 2. Start the server

```bash
export AGENT_HARNESS_SERVICE_API_KEY=test-key-123
export AHS_DATA_DIR=/tmp/ahs

# Optional: override MCP base URL (defaults to http://127.0.0.1:8090)
# export AHS_MCP_BASE_URL=http://127.0.0.1:8090

# Optional: enable per-event debug logging from the Claude CLI
# export AGENT_RESPONSE_DEBUG=1

# Optional: disable the scheduler (enabled by default)
# export AHS_SCHEDULER_ENABLED=false

poetry run uvicorn ypl.agent_harness_service.server:app \
  --host 0.0.0.0 --port 8090 --reload \
  --log-level info
```

Verify it's up:

```bash
curl http://localhost:8090/health
# {"status":"ok"}
```

On startup, check the logs for:
- `"Agent Harness Service ready"` with `agent_count`
- `"Loaded agent config"` for each discovered agent
- `"Discovered agent specs"` for orchestration-capable agents
- `"Scheduler started"` (if scheduler is enabled)

## 3. Using ahscli (recommended)

`ahscli` is a lightweight CLI that wraps the AHS REST API. It auto-saves the
last session ID so you don't have to copy-paste UUIDs between commands.

### Setup

```bash
# Set the API key (same one the server uses)
export AGENT_HARNESS_SERVICE_API_KEY=test-key-123

# Optional: override host/port (defaults to localhost:8090)
# export AHS_HOST=localhost
# export AHS_PORT=8090
```

### Create a session and send a message

```bash
# Create a session with the sre agent and send a first message
poetry run python -m ypl.agent_harness_service.scripts.ahscli \
  create --agent sre 'What files are in the root of the repo? Just list the top 5.'

# Output:
# Session ID saved to /tmp/LAST_AHS_SESSION_ID
# Session: <uuid>
# Status:  ACTIVE
```

### Check history (poll for response)

Wait for the agent to finish (watch server logs for `"Agent task completed"`), then:

```bash
poetry run python -m ypl.agent_harness_service.scripts.ahscli history
```

### Send a follow-up message

```bash
poetry run python -m ypl.agent_harness_service.scripts.ahscli \
  message 'Now tell me which of those files is the biggest.'
```

### Send feedback

```bash
# Positive feedback on the session
poetry run python -m ypl.agent_harness_service.scripts.ahscli feedback 1

# Negative feedback on a specific message
poetry run python -m ypl.agent_harness_service.scripts.ahscli feedback 0 --message-id <msg-uuid>
```

### ahscli command reference

| Command | Description |
|---------|-------------|
| `ahscli create [--agent NAME] ['message']` | Create a session, optionally send first message |
| `ahscli message 'text'` | Send a message to the current session |
| `ahscli message 'text' --session-id ID` | Send a message to a specific session |
| `ahscli history` | Show message history for the current session |
| `ahscli history --session-id ID` | Show history for a specific session |
| `ahscli feedback 1` | Positive feedback on the current session |
| `ahscli feedback 0 --message-id ID` | Negative feedback on a specific message |

> **Tip:** You can alias `ahscli` for convenience:
> ```bash
> alias ahscli='poetry run python -m ypl.agent_harness_service.scripts.ahscli'
> ```

## 4. Using curl (manual API calls)

### Create a session

```bash
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key-123" \
  -d '{
    "agent_id": "sre",
    "trigger": "api"
  }' | python3 -m json.tool
```

Save the session ID:

```bash
export SESSION_ID="<paste uuid here>"
```

### Send a message (async — returns immediately)

```bash
curl -s -X POST http://localhost:8090/ahs/session/message \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key-123" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"message\": \"What files are in the root of the repo? Just list the top 5.\"
  }" | python3 -m json.tool
```

### Create session with immediate message (one-shot)

```bash
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key-123" \
  -d '{
    "agent_id": "sre",
    "trigger": "api",
    "message": "Say hello in one sentence."
  }' | python3 -m json.tool
```

### Poll for the response via history

Wait for `"Agent task completed"` in server logs, then:

```bash
curl -s http://localhost:8090/ahs/session/$SESSION_ID/history \
  -H "X-API-Key: test-key-123" | python3 -m json.tool
```

### Multi-turn conversation (--resume)

Send a second message to the same session. The service passes the saved
`llm_session_id` to `claude --resume`, so the agent retains context:

```bash
curl -s -X POST http://localhost:8090/ahs/session/message \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key-123" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"message\": \"Now tell me which of those files is the biggest.\"
  }" | python3 -m json.tool
```

> **Note:** If the first turn is still processing, the second message will be
> rejected with **409 Conflict**: `"A prior turn is still processing."` Wait for
> the agent to finish before sending the next message.

### Send feedback

```bash
MSG_ID="<paste agent_session_message_id here>"

curl -s -X POST http://localhost:8090/ahs/session/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: test-key-123" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"message_id\": \"$MSG_ID\",
    \"rating\": \"POSITIVE\",
    \"comment\": \"Good answer\"
  }"
```

### Auth errors

```bash
# Missing key -> 401
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "sre", "trigger": "api"}'

# Wrong key -> 403
curl -s -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: wrong-key" \
  -d '{"agent_id": "sre", "trigger": "api"}'
```

## 5. What to watch in the logs

The agent runs in the background. You should see these log entries in order:

1. `"Launching Claude Code CLI"` — includes the full `command` (copy-pasteable for debugging)
2. `"Agent content block received"` — each time the agent produces text (with `text_preview`)
3. `"Claude CLI process exited"` — with `returncode` and any `stderr_preview`
4. `"Agent task completed"` — with `cost_usd`, `duration_ms`, `response_length`

Since there's no gateway running locally, you'll also see:
- `"No gateway for trigger"` — this is expected for `trigger: "api"` sessions

## 6. Test MCP tools — list repos and cross-repo queries

Agents with `"has_mcp": true` in their config.json get MCP tools served by the
in-process HTTP server at `/mcp/harness`. The session ID is injected into the
agent's system prompt so it can pass it to MCP tools.

### Set up a second repo

To test cross-repo scenarios, clone or symlink another repo into the repos dir.
Any public repo works — just use one with a recognizable README. For example:

```bash
git clone https://github.com/yupp-ai/some-other-repo.git $AHS_DATA_DIR/repos/some-other-repo
```

### Test listing repos

```bash
ahscli create --agent code-reviewer \
  'List all available repos using the list_available_repos tool.'
```

In the logs, watch for:
- `"Launching Claude Code CLI"` — command should include `--mcp-config` pointing to a temp file
- The agent response should list `yupp-agent` and `some-other-repo`

### Test cross-repo query

```bash
ahscli create --agent code-reviewer \
  'Compare the README.md between yupp-agent and some-other-repo. What are the key differences?'
```

## 7. Test write access and PR creation

### Request write access

```bash
ahscli create --agent code-reviewer
ahscli message 'Use the request_write_access tool to get write access to yupp-agent.'
```

After the agent completes, check that a worktree was created:

```bash
SESSION_ID=$(cat /tmp/LAST_AHS_SESSION_ID)
ls -la $AHS_DATA_DIR/workspaces/$SESSION_ID/
# Should show: yupp-agent/ (plus any branch-suffixed worktree directory)
```

### Test PR creation (dry run)

> **Warning:** This will actually push a branch and create a PR. Only do this
> if you're okay with that, or use a test repo.

```bash
ahscli message \
  "Create a test file called test-agent.txt with 'hello from agent' content, commit it, then use the create_pr tool to open a PR with title 'test: agent harness PR creation' and body 'Testing PR creation from agent harness'."
```

Check the history for the PR URL:

```bash
ahscli history
```

### Verify worktree cleanup

Worktrees persist under `$AHS_DATA_DIR/workspaces/{session_id}/`. To clean up manually:

```bash
git -C $AHS_DATA_DIR/repos/yupp-agent worktree remove $AHS_DATA_DIR/workspaces/$SESSION_ID/yupp-agent --force
rm -rf $AHS_DATA_DIR/workspaces/$SESSION_ID
```

## 8. Test raw executor agents

Raw executor agents call the Anthropic/OpenAI API directly (no CLI subprocess).
They're configured with `executor_config.type: "raw"` in config.json.

### Test a raw agent (no tools)

```bash
ahscli create --agent raw-test 'What is 2 + 2? Reply in one sentence.'
```

Watch the logs for:
- `"Running raw executor"` with `model` and `provider`
- `"Raw executor completed"` with `cost_usd` and `tokens`

### Test a raw agent with MCP tools

The `reviewer` agent is a raw executor with read-only tools:

```bash
ahscli create --agent reviewer 'Read the README.md in the repo root and summarize it in 3 bullet points.'
```

Watch for MCP tool calls in the logs:
- `"MCP tool schemas fetched"` — tools available to the raw executor
- `"Raw executor tool call"` — each tool invocation

## 9. Test the mock executor

The `parrot-bubba` agent uses the mock executor — it logs prompts without
calling any real API. Useful for testing the session/message flow:

```bash
ahscli create --agent parrot-bubba 'This is a test message.'
ahscli history
# The agent response will be a synthetic message
```

## 10. Test subagent orchestration

Coordinator agents can spawn subagents via the `new_task` MCP tool.

### Test with the coordinator agent

```bash
ahscli create --agent coordinator \
  'Use list_agents to see available agents, then spawn a raw-test agent with the prompt "Say hello".'
```

Watch the logs for:
- `"MCP tool: new_task"` — subagent spawn request
- `"Running subagent"` — subagent execution
- `"Subagent completed"` — with result summary

### Test dual-reviewer (spawns reviewer + fixer)

```bash
ahscli create --agent dual-reviewer \
  'Use route_model to pick 2 models, then spawn two reviewer agents to review the file ypl/agent_harness_service/config.py.'
```

## 11. Test the Codex CLI agent

Requires the OpenAI Codex CLI installed (`codex --version`):

```bash
ahscli create --agent codex-test 'List the files in the root directory.'
```

## 12. Verify in DB

```bash
poetry run python -c "
import asyncio
from ypl.backend.db import get_async_session
from sqlmodel import select
from ypl.db.agent_harness import Agent, AgentSession, AgentSessionMessage

async def check():
    async with get_async_session() as s:
        agents = (await s.exec(select(Agent))).all()
        sessions = (await s.exec(select(AgentSession))).all()
        msgs = (await s.exec(select(AgentSessionMessage))).all()
        print(f'Agents: {len(agents)}')
        for a in agents:
            print(f'  {a.name} ({a.display_name})')
        print(f'Sessions: {len(sessions)}')
        for ss in sessions:
            print(f'  {ss.agent_session_id} status={ss.status.value} llm_session_id={ss.llm_session_id}')
        print(f'Messages: {len(msgs)}')
        for m in msgs:
            print(f'  turn={m.turn_number} role={m.role.value} cost={m.cost_usd} content={(m.content or \"\")[:80]}...')

asyncio.run(check())
"
```

## 13. Testing with gateway (optional)

To test the full AHS -> gateway -> Slack flow, start the gateway service and set:

```bash
export GATEWAY_BASE_URL=http://localhost:8200
```

Then create a session with a `session_id` matching the gateway's composite format
(`channel:thread_ts:app_id`). Each agent content block will be sent to the gateway
via `POST /slack-agent-gateway/sessions/reply` as it arrives.

Without `GATEWAY_BASE_URL`, the agent still runs and persists results — you just
won't see the gateway callback logs (only the skip warning).

## API reference

All endpoints except `/health` require `X-API-Key` header. Base path: `/ahs`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (no auth) |
| `POST` | `/ahs/session/create` | Create or resume a session |
| `POST` | `/ahs/session/message` | Send a message (async, returns immediately) |
| `POST` | `/ahs/session/feedback` | Record feedback |
| `GET` | `/ahs/session/{id}/history` | Get message history |

### MCP tools (served at `/mcp/harness`)

| Tool | Description |
|------|-------------|
| `request_write_access` | Create a git worktree for write access |
| `list_available_repos` | List available repos |
| `create_pr` | Push branch and open a PR |
| `request_feedback` | Post a feedback survey to Slack |
| `send_slack_message` | Send a proactive Slack message |
| `new_task` | Spawn a subagent in a new session |
| `route_model` | Pick executor model(s) for tasks |
| `list_agents` | List available agent configs |
| `schedule_agent_call` | Schedule a one-time agent call |
| `schedule_recurring_agent_call` | Schedule a recurring agent call |

> MCP endpoints are protected by a process-local token. Only agent subprocesses
> can access them — external callers get 401 Unauthorized.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `503 Service misconfigured` | Set `AGENT_HARNESS_SERVICE_API_KEY` env var |
| `Agent not found: sre` | Check `AHS_AGENTS_DIR` points to a dir containing `sre/config.json` |
| `claude: command not found` | Install Claude Code CLI: `npm install -g @anthropic-ai/claude-code` |
| Agent never completes | Check server logs for `"Claude CLI process exited"`. If missing, the CLI is hanging — see below |
| CLI hangs (no exit log) | Kill stuck processes: `ps aux \| grep 'claude -p' \| grep -v grep` then `kill <pid>`. Common cause: unauthenticated CLI |
| `Invalid agent name` | Agent names must be lowercase alphanumeric + hyphens |
| Only user message in history, no agent | Agent is still running. Wait for `"Agent task completed"` in logs, then poll again |
| `409 A prior turn is still processing` | Wait for the current agent turn to finish before sending a new message |
| `No worktree found` | Call `request_write_access` first to create a worktree before `create_pr` |
| `Multiple worktrees found` | Specify `repo` parameter in `create_pr` when the session has worktrees for multiple repos |
| MCP tools not available | Check `"has_mcp": true` in config.json and that the server is running on the expected port |
| MCP connection refused | Verify `AHS_MCP_BASE_URL` matches the server's listen address (default `http://127.0.0.1:8090`) |
| MCP returns 401 Unauthorized | Expected for external callers. Only agent subprocesses can access MCP endpoints |
| Raw executor errors | Check `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set for the model's provider |
| `codex: command not found` | Install Codex CLI for codex-test agent |
| Scheduler not running | Check `AHS_SCHEDULER_ENABLED` is not set to `false` |
