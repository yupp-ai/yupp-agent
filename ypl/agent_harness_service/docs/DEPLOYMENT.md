# Agent Harness Service — Deployment Playbook

## Architecture Overview

The Agent Harness Service (AHS) is a standalone FastAPI server that hosts AI agents.
It runs on a dedicated VM, spawns Claude Code CLI subprocesses to execute agent tasks,
and stores results in the shared Yupp PostgreSQL database.

```
               ┌──────────────┐
               │  Slack Bot   │
               └──────┬───────┘
                      │ HTTP (X-API-Key)
                      ▼
            ┌─────────────────────┐
            │  Agent Harness      │
            │  Service (FastAPI)  │ ← systemd: ahs.service
            │                     │
            │  ┌───────────────┐  │
            │  │ MCP Server    │  │ ← /mcp/harness (harness tools)
            │  ├───────────────┤  │
            │  │ Gateway Reg.  │  │ ← Slack gateway (outgoing replies)
            │  ├───────────────┤  │
            │  │ Scheduler     │  │ ← Scheduled/recurring agent calls
            │  └───────────────┘  │
            └──┬────────┬────┬────┘
               │        │    │
  ┌────────────┘        │    └────────────────┐
  ▼                     ▼                     ▼
┌─────────────┐  ┌───────────────┐  ┌─────────────────┐
│ CLI runners │  │  PostgreSQL   │  │ Raw executor    │
│ (claude,    │  │  (shared DB)  │  │ (direct API to  │
│  codex)     │  └───────────────┘  │  Anthropic/OAI) │
└──────┬──────┘                     └─────────────────┘
       │
       ▼
┌──────────┐
│ Git repos│ ← /data/repos/ (read-only)
│ Worktrees│ ← /data/workspaces/ (write, per-session)
└──────────┘
```

## Filesystem Layout

All data lives under `/data/` (configurable via `AHS_DATA_DIR`).

| Path | Purpose | Owner |
|------|---------|-------|
| `/opt/yupp-mind/` | Service code (git clone) | `ahs` |
| `/data/ahs/.env` | Environment variables (secrets) | `ahs` (mode 600) |
| `/data/agents/` | Agent config directories | `ahs` |
| `/data/agents/{name}/config.json` | Agent settings (model, tools, limits) | `ahs` |
| `/data/agents/{name}/ROLE.md` | Agent role, expertise & personality | `ahs` |
| `/data/shared/SOUL.md` | Shared identity/values (all agents) | `ahs` |
| `/data/shared/WORKSPACE.md` | Repository guide for agents | `ahs` |
| `/data/repos/` | Shared read-only repo checkouts | `ahs` |
| `/data/repos/yupp-mind/` | Main repo clone (auto-pulled every 5m) | `ahs` |
| `/data/workspaces/` | Per-session git worktrees (write access) | `ahs` |
| `/data/session_logs/` | Per-session debug logs | `ahs` |
| `/data/session_logs/pull_repos.log` | Cron job output | `ahs` |
| `/data/ahs/github-app-key.pem` | GitHub App private key (if using App auth) | `ahs` (mode 600) |
| `/data/ahs/gh_app_auth.sh` | GitHub App token refresh script | `ahs` (mode 700) |

## Prerequisites

- **VM**: Ubuntu 24.04 LTS (x86_64), 4 GB+ RAM. Python 3.12.12+ is required — the setup script builds from source if the system version is too old.
- **Database**: Access to the Yupp PostgreSQL database (staging or production)
- **Anthropic API key**: For Claude Code CLI
- **GitHub access**: For repo cloning and PR creation

---

## Fresh Install

### Step 1: Run the setup script

> **Important:** Complete Step 3 (GitHub authentication) first if the `ahs` user
> doesn't have GitHub access yet — the script clones repos and will hang at a
> credentials prompt otherwise. On a fresh VM, run Steps 1-3 in order: setup
> script (it will create the `ahs` user and install `gh`), then authenticate
> GitHub, then re-run the setup script to finish.

```bash
sudo bash /path/to/yupp-mind/ypl/agent_harness_service/deploy/setup_vm.sh
```

This installs all system dependencies, creates the `ahs` user, sets up the directory
structure, clones repos, installs Python deps, and enables the systemd service.

What it does:
1. Installs system packages (git, jq, python3.12, postgresql-client, etc.)
2. Installs GitHub CLI
3. Creates `ahs` service user
4. Installs Claude Code CLI as the `ahs` user (user-scoped)
5. Creates `/data/` directory structure
6. Clones yupp-mind to `/opt/yupp-mind/` (requires GitHub auth — see Step 3)
7. Creates Python venv and installs dependencies via Poetry
8. Copies env template, agent configs, and shared identity files
9. Clones repos into `/data/repos/`
10. Installs and enables systemd service
11. Sets up cron job for auto-pulling repos

### Step 2: Configure environment

Edit `/data/ahs/.env` with your actual values:

```bash
sudo -u ahs emacs /data/ahs/.env
```

**Required variables:**

| Variable | Description | Where to get it |
|----------|-------------|-----------------|
| `ENVIRONMENT` | `staging` or `production` | Depends on target |
| `AGENT_HARNESS_SERVICE_API_KEY` | Shared secret for API auth | Generate or get from team |
| `ANTHROPIC_API_KEY` | For Claude Code CLI | Anthropic Console |
| `POSTGRES_USER` | DB username | Copy from main backend `.env` |
| `POSTGRES_PASSWORD` | DB password | Copy from main backend `.env` |
| `POSTGRES_HOST` | DB host:port | Copy from main backend `.env` |
| `POSTGRES_HOST_NON_POOLING` | Direct DB host | Copy from main backend `.env` |
| `POSTGRES_DATABASE` | DB name | Copy from main backend `.env` |

For read replica vars, you can use the same values as the primary if there's no
separate replica. Copy the `POSTGRES_*_READ_REPLICA` vars from the main backend `.env`.

**Optional but recommended:**

| Variable | Description |
|----------|-------------|
| `GATEWAY_BASE_URL` | Main backend URL for Slack reply callbacks |
| `X_API_KEY` | API key for authenticating with the gateway (outbound callbacks) |
| `OPENAI_API_KEY` | For Codex CLI and raw executor OpenAI models |
| `YUPPSTER_MCP_TOKEN` | Token for yuppster MCP server access (agents with MCP) |
| `GCP_PROJECT_ID` | For structured logging |
| `GITHUB_TOKEN` | Alternative to `gh auth login` |
| `AHS_SCHEDULER_ENABLED` | Set to `false` to disable the scheduler (default: `true`) |
| `AHS_SCHEDULER_POLL_INTERVAL` | Scheduler polling interval in seconds (default: `10`) |

### Step 3: Set up GitHub authentication

GitHub access is required for cloning private repos, pulling updates (cron), and
creating PRs. You must complete this step **before** running the setup script,
since it clones repos as the `ahs` user.

#### Option A: GitHub App (recommended for production)

A GitHub App provides fine-grained permissions, no expiry tied to a person, and
audit-friendly identity.

**1. Create the GitHub App** (one-time, org admin):

- Go to https://github.com/organizations/yupp-ai/settings/apps/new
- **App name**: `AHS Agent Service` (must be globally unique on GitHub)
- **Homepage URL**: `https://github.com/yupp-ai/yupp-mind` (any valid URL)
- **Webhook**: Uncheck "Active" (not needed — PR triggers use GitHub Actions, not webhooks)
- **Callback URL**: Leave blank (no OAuth user login flow needed)
- **Permissions** (Repository):
  - **Contents**: Read & write (clone, pull, push branches)
  - **Pull requests**: Read & write (create PRs)
  - **Metadata**: Read-only (auto-selected)
  - Leave everything else as "No access"
- **Where can this app be installed?**: "Only on this account"
- Click **"Create GitHub App"**
- **Copy the App ID** from the app settings page (a number near the top)

**2. Generate a private key:**

- On the app settings page → "Private keys" → "Generate a private key"
- A `.pem` file will download to your machine

**3. Install the App on repos:**

- In the left sidebar → "Install App" → click "Install" next to `yupp-ai`
- Select "Only select repositories" → pick `yupp-mind` (add others later as needed)
- Click "Install"
- **Copy the Installation ID** from the URL: `.../installations/<INSTALLATION_ID>`

**4. Upload the private key to the VM:**

```bash
# From your laptop
scp ~/Downloads/*private-key.pem YOUR_VM_IP:/tmp/github-app-key.pem

# On the VM
sudo mkdir -p /data/ahs
sudo mv /tmp/github-app-key.pem /data/ahs/github-app-key.pem
sudo chown ahs:ahs /data/ahs/github-app-key.pem
sudo chmod 600 /data/ahs/github-app-key.pem
```

**5. Copy the auth script to the VM:**

The repo includes a helper script at `deploy/gh_app_auth.sh` that generates a
GitHub App installation token and authenticates `gh` CLI.

```bash
# Copy from the repo (if already cloned to /opt/yupp-mind)
sudo cp /opt/yupp-mind/ypl/agent_harness_service/deploy/gh_app_auth.sh /data/ahs/
# Or copy from your laptop if the repo isn't cloned yet
scp ypl/agent_harness_service/deploy/gh_app_auth.sh YOUR_VM_IP:/tmp/
sudo mv /tmp/gh_app_auth.sh /data/ahs/

sudo chown ahs:ahs /data/ahs/gh_app_auth.sh
sudo chmod 700 /data/ahs/gh_app_auth.sh
```

**6. Add App credentials to `.env`:**

```bash
# In /data/ahs/.env
GITHUB_APP_ID=<your-app-id>
GITHUB_APP_INSTALLATION_ID=<your-installation-id>
GITHUB_APP_PRIVATE_KEY_PATH=/data/ahs/github-app-key.pem
```

**7. Authenticate `gh` CLI:**

```bash
# Generate token and log in as the GitHub App
sudo -u ahs bash /data/ahs/gh_app_auth.sh

# Configure git to use gh for credentials
sudo -u ahs gh auth setup-git
```

You should see `Logged in to github.com account ...` on success.

> **Note:** GitHub App installation tokens expire after 1 hour. For long-running
> operations (cron pulls, agent sessions that create PRs), re-run the auth script
> periodically. A cron job can automate this:
> ```bash
> # Re-authenticate every 50 minutes (tokens last 1 hour)
> */50 * * * * /data/ahs/gh_app_auth.sh >> /data/session_logs/gh_auth.log 2>&1
> ```

#### Option B: Personal Access Token (simpler, for staging)

```bash
# Create a fine-grained PAT at https://github.com/settings/tokens?type=beta
# Scope to yupp-ai org and the repos agents need
# Permissions: Contents (read/write), Pull requests (read/write)

# Log in as the ahs user (interactive, paste the token)
sudo -u ahs gh auth login

# Then configure git credentials
sudo -u ahs gh auth setup-git
```

> **Caveat:** PATs are tied to a personal account. If that person leaves, the
> token stops working. Use a GitHub App for production.

### Step 4: Start the service

```bash
sudo systemctl start ahs
```

### Step 5: Verify

```bash
# Check service status
sudo systemctl status ahs

# Check health endpoint
curl -s http://localhost:8090/health

# Tail logs
journalctl -u ahs -f

# Check cron is set up
sudo -u ahs crontab -l
```

---

## Operations

### Service Management

```bash
# Start
sudo systemctl start ahs

# Stop
sudo systemctl stop ahs

# Restart
sudo systemctl restart ahs

# Status
sudo systemctl status ahs

# Enable on boot
sudo systemctl enable ahs

# Disable on boot
sudo systemctl disable ahs
```

### Viewing Logs

```bash
# Live tail (most useful)
journalctl -u ahs -f

# Last 100 lines
journalctl -u ahs -n 100

# Logs since last hour
journalctl -u ahs --since "1 hour ago"

# Logs from today
journalctl -u ahs --since today

# JSON structured logs (for grep/jq)
journalctl -u ahs -o json | jq .MESSAGE
```

### Session Debug Logs

Each agent session writes a detailed event log:

```bash
# List session logs
ls -lt /data/session_logs/

# Tail a specific session log (filename = Claude LLM session ID)
tail -f /data/session_logs/<llm-session-id>.log

# Search across session logs
grep -r "error" /data/session_logs/
```

### Deploy Scripts

All scripts live in `deploy/` and run as the `ahs` user (`sudo -u ahs bash <script>`).

| Script | What it does | Cron frequency |
|--------|-------------|----------------|
| `gh_app_auth.sh` | Refreshes GitHub App token (expires every 1 hour) | Every 50 min |
| `pull_agent_repos.sh` | Pulls all repos in `/data/repos/` (agent read-only checkouts) | Every 5 min |
| `sync_configs.sh` | Pulls `/opt/yupp-mind`, copies changed configs to `/data/` | Every 30 min |

**`sync_configs.sh`** — pulls the service repo and syncs config files:

```bash
# Pull repo + sync all configs to /data/
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh

# Skip git pull (already pulled manually)
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh --no-pull

# Sync only shared identity files
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh --no-pull --shared

# Sync only agent configs
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh --no-pull --agents

# Preview what would change
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh --dry-run
```

**`pull_agent_repos.sh`** — pulls `/data/repos/*` (yupp-mind, yupp-soul, yupp-head):

```bash
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/pull_agent_repos.sh
```

### Setting Up Cron

All cron jobs go in **root's crontab** and use `sudo -u ahs` to run as the
service user (which owns the repos and has GitHub auth).

Run `sudo crontab -e` and add:

```cron
# ============================================================
#  Agent Harness Service — cron jobs
# ============================================================

# Refresh GitHub App token every 50 min (tokens expire after 1 hour).
# Without this, git pull and gh CLI commands will fail with auth errors.
*/50 * * * * sudo -u ahs bash /data/ahs/gh_app_auth.sh >> /data/session_logs/gh_auth.log 2>&1

# Pull agent repos every 5 min (read-only checkouts in /data/repos/).
# Agents read from these repos; keeping them fresh means agents see latest code.
*/5 * * * * sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/pull_agent_repos.sh >> /data/session_logs/pull_agent_repos.log 2>&1

# Sync service code + configs every 30 min.
# Pulls /opt/yupp-mind, then copies changed .md and config.json to /data/.
# Agent/shared config changes take effect on the next session creation.
*/30 * * * * sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh >> /data/session_logs/sync_configs.log 2>&1
```

**Verify:**

```bash
sudo crontab -l
```

**Viewing logs:**

```bash
tail -f /data/session_logs/gh_auth.log          # token refresh
tail -f /data/session_logs/pull_agent_repos.log  # agent repo pulls
tail -f /data/session_logs/sync_configs.log      # service pull + config sync
```

### Disk Space

```bash
# Check overall disk usage
df -h /data

# Check workspaces (git worktrees accumulate over time)
du -sh /data/workspaces/*

# Check session logs
du -sh /data/session_logs/

# Clean up old worktrees (careful — active sessions use these)
# Only clean worktrees for sessions that are COMPLETED or STALE:
ls /data/workspaces/
```

### Updating the Service Code

```bash
# Pull latest code + sync configs in one step
sudo -u ahs bash /opt/yupp-mind/ypl/agent_harness_service/deploy/sync_configs.sh

# Update Python dependencies (if pyproject.toml changed)
cd /opt/yupp-mind
sudo -u ahs .venv/bin/poetry install --no-interaction

# Restart service
sudo systemctl restart ahs

# Re-copy systemd unit if it changed
sudo cp ypl/agent_harness_service/deploy/ahs.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ahs
```

### Updating Agent Configs

Agent configs live in `/data/agents/`. Changes checked into the repo are applied
automatically by the `sync_configs.sh` cron job every 30 min.
To edit configs directly on the VM:

```bash
# Edit an agent's config
sudo -u ahs emacs /data/agents/sre/config.json

# Edit an agent's role
sudo -u ahs emacs /data/agents/sre/ROLE.md

# Add a new agent
sudo -u ahs mkdir -p /data/agents/my-agent
sudo -u ahs emacs /data/agents/my-agent/config.json
sudo -u ahs emacs /data/agents/my-agent/ROLE.md

# Restart to pick up new agents
sudo systemctl restart ahs
```

### Updating Shared Identity

Changes checked into the repo are applied automatically by the `sync_configs.sh`
cron job every 30 min. To copy manually:

```bash
# Copy latest shared files from repo
sudo -u ahs cp /opt/yupp-mind/ypl/agent_harness_service/deploy/shared/SOUL.md /data/shared/
sudo -u ahs cp /opt/yupp-mind/ypl/agent_harness_service/deploy/shared/WORKSPACE.md /data/shared/
```

---

## API Reference

All endpoints require `X-API-Key` header. Base path: `/ahs`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (no auth) |
| `POST` | `/ahs/session/create` | Create or resume a session |
| `POST` | `/ahs/session/message` | Send a message (async, returns immediately) |
| `POST` | `/ahs/session/feedback` | Record feedback |
| `GET` | `/ahs/session/{id}/history` | Get message history |

### MCP tools (at `/mcp/harness`, token-protected)

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

### Quick smoke test

```bash
# Health check
curl http://localhost:8090/health

# Create a session
curl -X POST http://localhost:8090/ahs/session/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_HARNESS_SERVICE_API_KEY" \
  -d '{"agent_id": "sre", "trigger": "api", "message": "What repos are available?"}'
```

---

## Pre-installed Agents

### Harnessed agents (Claude Code CLI)

| Agent | Max Turns | Budget | MCP | Tools |
|-------|-----------|--------|-----|-------|
| `default` | 50 | $5 | Yes | Bash, Read, Write, Edit, Glob, Grep, WebFetch |
| `sre` | 100 | $3 | Yes | Bash, Read, Glob, Grep, WebFetch |
| `data-scientist` | 100 | $5 | Yes | Bash, Read, Write, Edit, Glob, Grep, WebFetch |
| `code-reviewer` | 100 | $2 | Yes | Read, Glob, Grep, WebFetch (read-only) |
| `eng-terse` | 100 | $3 | Yes | Bash, Read, Glob, Grep, WebFetch |
| `fixer` | 100 | $5 | Yes | Bash, Read, Write, Edit, Glob, Grep, WebFetch |
| `coordinator` | 50 | $15 | Yes | Read, Glob, Grep, WebFetch + spawns all subagents |
| `dual-reviewer` | 50 | $10 | Yes | Read, Glob, Grep, WebFetch + spawns reviewer/fixer |

### Harnessed agents (Codex CLI)

| Agent | Max Turns | Budget | Tools |
|-------|-----------|--------|-------|
| `codex-test` | 20 | $0.50 | Bash, Read, Glob, Grep |

### Raw executor agents (direct API)

| Agent | Model | Max Steps | Budget | Tools |
|-------|-------|-----------|--------|-------|
| `raw-test` | anthropic/claude-sonnet-4-6 | 10 | $1 | None (pure chat) |
| `reviewer` | (assigned at spawn) | 100 | $5 | Read, Glob, Grep, WebFetch |
| `router` | anthropic/claude-haiku-4-5 | 1 | $0.50 | None (one-shot classification) |

### Mock executor (testing)

| Agent | Budget | Description |
|-------|--------|-------------|
| `parrot-bubba` | $0 | Logs prompt without API call, returns synthetic response |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AHS_API_KEY not configured` | Missing env var | Set `AGENT_HARNESS_SERVICE_API_KEY` in `/data/ahs/.env` |
| `Agent config not found` | Agent dir missing | Check `/data/agents/{name}/config.json` exists |
| `CLI exited with code 1` | Claude CLI error | Check session logs in `/data/session_logs/`, verify `ANTHROPIC_API_KEY` |
| No gateway callbacks | Expected if no Slack | Agent still runs, replies just aren't pushed |
| DB connection errors | Wrong POSTGRES_* vars | Verify DB vars match the main backend `.env` |
| `claude: command not found` | CLI not in PATH | Re-run `curl -fsSL https://claude.ai/install.sh \| bash` as `ahs` user |
| Stale repo code | Cron not running | Check `sudo crontab -l` and `/data/session_logs/pull_agent_repos.log` |
| Disk full | Worktree accumulation | Clean up `/data/workspaces/` for completed sessions |
| Service won't start | Missing .env vars | Check `journalctl -u ahs -n 50` for the specific error |
| Raw executor fails | Missing API key | Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` for the model's provider |
| Scheduler not running | Disabled | Check `AHS_SCHEDULER_ENABLED` is not set to `false` |

---

## Agent Runtimes

The service supports multiple agent runtimes via the runner abstraction:

| Runtime | Status | Config key | Description |
|---------|--------|------------|-------------|
| Claude Code CLI | Production | `executor: "claude_code"` or `executor_config.harness: "claude-code-cli"` | Spawns `claude` CLI subprocess |
| Codex CLI | Production | `executor: "openai_codex"` or `executor_config.harness: "codex-cli"` | Spawns `codex` CLI subprocess |
| Raw executor | Production | `executor_config.type: "raw"` | Direct Anthropic/OpenAI API calls |
| Mock executor | Testing | `executor: "mock"` | Logs prompt, returns synthetic response |

Additional runtimes can be added by implementing the `AgentRunner` interface
in `runner.py`. Each needs its own API key in `/data/ahs/.env`.
