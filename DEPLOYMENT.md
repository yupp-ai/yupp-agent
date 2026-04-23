# Deployment Guide

One-box deployment: a single machine running the full agent platform. Cost ranges from ~$25/mo (small VM) to $0 (your laptop).

```
Machine (VM or MacBook)
├── uvicorn process (port 8090)   — AHS + MCP + gateways
├── streamlit process (port 8501) — Operational dashboards
├── PostgreSQL (localhost:5432)
├── Redis (localhost:6379)
└── cloudflared tunnel (optional, for external access)
```

The monolith is the primary deployment shape for self-hosted setups: one process, one VM, `.env`-driven config. All four services (AHS, SAG, MCP, Streamlit) ship together and can be run standalone if you need to scale a component independently.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [First-Time Setup](#2-first-time-setup)
3. [Option A — Docker Compose (recommended)](#3-option-a--docker-compose-recommended)
4. [Option B — Bare-Metal VM (systemd)](#4-option-b--bare-metal-vm-systemd)
5. [Option C — MacBook (local development)](#5-option-c--macbook-local-development)
6. [Cloudflare Tunnel (optional)](#6-cloudflare-tunnel-optional)
7. [Verification](#7-verification)
8. [Upgrades](#8-upgrades)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.12 | Required |
| Poetry | ≥ 1.8 | Dependency management |
| PostgreSQL | 16 | pgvector extension needed |
| Redis | 7 | |
| Docker + Compose | ≥ 24 | Option A only |
| cloudflared | latest | Option: public access |

**Secrets you'll need** (gather before running setup):

| Secret | Where to get it |
|--------|----------------|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `OPENAI_API_KEY` | platform.openai.com |
| `SLACK_BOT_TOKEN` | api.slack.com (optional, for Slack gateway) |
| `SLACK_SIGNING_SECRET` | api.slack.com (optional) |

The setup wizard auto-generates all internal secrets (DB password, signing keys, etc.).

---

## 2. First-Time Setup

The interactive setup wizard must be run **once** on every new box. It:

1. Checks PostgreSQL + Redis connectivity
2. Generates `.env` with prompted values + auto-generated secrets
3. Runs `alembic upgrade head` (schema migrations)
4. Seeds roles (ADMIN, ENGINEER, MCP_USER)
5. Creates first admin user + optional MCP dev token

```bash
# Clone the repo (bare-metal / MacBook)
git clone https://github.com/yupp-ai/yupp-agent.git
cd yupp-agent

# Install dependencies
poetry install --no-root --without dev

# Run setup wizard (interactive)
python -m ypl.mono_server.setup
```

For **Docker Compose**, run the wizard against the Compose postgres service:

```bash
# Start only the data services first
docker compose -f docker-compose.one-box.yml up -d postgres redis

# Run the wizard from host (pointing at localhost:5432)
python -m ypl.mono_server.setup
# → When prompted for Postgres host, enter: localhost:5432
# → When prompted for Redis URL, enter:    redis://localhost:6379/1

# Then bring up the full stack
docker compose -f docker-compose.one-box.yml up -d
```

---

## 3. Option A — Docker Compose (recommended)

**Best for:** most users, CI, cloud VMs without systemd.

### Build and start

```bash
# First time: build the image (takes ~5 min on first run)
docker compose -f docker-compose.one-box.yml build

# Start everything in the background
docker compose -f docker-compose.one-box.yml up -d

# Follow logs
docker compose -f docker-compose.one-box.yml logs -f app
docker compose -f docker-compose.one-box.yml logs -f streamlit
```

### Sandbox note

`bwrap` (the Linux sandbox used for agent tool calls) is unavailable in unprivileged Docker containers. The compose file sets `SANDBOX_ENABLED=false` by default — agents still work fully, just without filesystem-level process isolation.

To enable bwrap, add `--privileged` to the app service:

```yaml
# docker-compose.one-box.yml (override)
services:
  app:
    privileged: true
    environment:
      SANDBOX_ENABLED: "true"
```

### Useful commands

```bash
# Stop everything (preserves volumes)
docker compose -f docker-compose.one-box.yml down

# Wipe data volumes (destructive!)
docker compose -f docker-compose.one-box.yml down -v

# Rebuild after code changes
docker compose -f docker-compose.one-box.yml build app streamlit
docker compose -f docker-compose.one-box.yml up -d --no-deps app streamlit

# Run migrations after a schema change
docker compose -f docker-compose.one-box.yml exec app \
    python -m alembic upgrade head

# Open a psql shell
docker compose -f docker-compose.one-box.yml exec postgres \
    psql -U postgres yupp_agent
```

---

## 4. Option B — Bare-Metal VM (systemd)

**Best for:** dedicated VMs, always-on deployments, teams who want systemd lifecycle management.

### One-command bootstrap

```bash
# Run as root on a fresh Ubuntu/Debian VM
curl -fsSL https://raw.githubusercontent.com/yupp-ai/yupp-agent/main/deploy/bare-metal/install.sh | sudo bash
```

Or if you've already cloned the repo:

```bash
sudo bash deploy/bare-metal/install.sh
```

The script installs Python 3.12, PostgreSQL 16, Redis 7, Poetry, the app, and the systemd units.

### After bootstrap

```bash
# Run the interactive setup wizard
sudo -u ahs bash -c 'cd /opt/yupp-agent && .venv/bin/python -m ypl.mono_server.setup'

# Start services
sudo systemctl start ahs-mono ahs-streamlit

# Enable auto-start on boot (already done by install.sh)
sudo systemctl enable ahs-mono ahs-streamlit
```

### Managing the services

```bash
# Status
sudo systemctl status ahs-mono ahs-streamlit

# Live logs
journalctl -u ahs-mono    -f
journalctl -u ahs-streamlit -f

# Restart after config change
sudo systemctl restart ahs-mono

# Graceful reload (if/when uvicorn supports it)
sudo systemctl reload ahs-mono
```

### Manual systemd unit install

If you prefer not to run the install script:

```bash
sudo cp deploy/systemd/ahs-mono.service     /etc/systemd/system/
sudo cp deploy/systemd/ahs-streamlit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ahs-mono ahs-streamlit
```

The units expect:

| Path | Description |
|------|-------------|
| `/opt/yupp-agent/` | Application root (owned by `ahs` user) |
| `/data/ahs/.env` | Secrets file — `chmod 600` |
| `/opt/yupp-agent/.venv/` | Python virtual environment symlink |

---

## 5. Option C — MacBook (local development)

**Best for:** personal development, offline work, zero-cost experimentation.

> **macOS note:** `bwrap` (Linux sandbox) is unavailable on macOS. Set `SANDBOX_ENABLED=false` in `.env` — agents run normally without it.

### Install dependencies

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# PostgreSQL 16 + pgvector
brew install postgresql@16
brew link postgresql@16 --force
brew services start postgresql@16

# Install pgvector extension
brew install pgvector

# Redis 7
brew install redis
brew services start redis

# Python 3.12 + Poetry
brew install python@3.12
curl -sSL https://install.python-poetry.org | python3 -
```

### Setup

```bash
git clone https://github.com/yupp-ai/yupp-agent.git
cd yupp-agent

# Create the database
createdb yupp_agent

# Install Python deps
poetry install --no-root --without dev

# Run setup wizard
python -m ypl.mono_server.setup
# → Postgres host: localhost:5432
# → Redis URL:    redis://localhost:6379/1
```

### Running

Open **two terminal tabs**:

**Terminal 1 — AHS + MCP + gateways:**

```bash
cd yupp-agent
uvicorn ypl.mono_server.server:app \
    --host 0.0.0.0 --port 8090 \
    --reload  # optional: hot-reload on code changes
```

**Terminal 2 — Streamlit dashboards:**

```bash
cd yupp-agent
streamlit run ypl/streamlit_server/app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
```

### Process manager (optional)

Use [Overmind](https://github.com/DarthSim/overmind) or [Foreman](https://github.com/ddollar/foreman) to manage both processes with a single command:

```bash
# Procfile (create at repo root)
# web: uvicorn ypl.mono_server.server:app --host 0.0.0.0 --port 8090
# ui:  streamlit run ypl/streamlit_server/app.py --server.port 8501 --server.headless true

brew install overmind
overmind start
```

---

## 6. Cloudflare Tunnel (optional)

Expose the platform to the internet without opening firewall ports, using a free [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

### Setup

```bash
# Install cloudflared
# macOS:
brew install cloudflare/cloudflare/cloudflared

# Linux:
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare.gpg] \
    https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

# Authenticate (opens browser)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create ahs-mono
# → Note the tunnel UUID printed

# Add DNS routes (replace yourdomain.com)
cloudflared tunnel route dns ahs-mono agent.yourdomain.com
cloudflared tunnel route dns ahs-mono agent-ui.yourdomain.com  # optional: streamlit
```

### Configure

Copy and edit the config template:

```bash
mkdir -p ~/.cloudflared
cp deploy/cloudflared/config.yml ~/.cloudflared/config.yml

# Edit: set tunnel UUID and your domain
nano ~/.cloudflared/config.yml
```

### Run

```bash
# Foreground (test)
cloudflared tunnel run ahs-mono

# As a system service (VM)
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

After the tunnel is up, `https://agent.yourdomain.com` proxies to `http://localhost:8090`.

### Exposing individual services on their own subdomain

The mono-server runs AHS, MCP, and gateway plugins in a single process on
port 8090. To expose only a specific service on its own subdomain (e.g.
`mcp.yourdomain.com` for a remote Claude Code client), do two things:

1. **Cloudflare Tunnel:** the template `deploy/cloudflared/config.yml`
   already includes an `mcp.yourdomain.com` ingress rule pointing at
   `http://localhost:8090`. Add the DNS record:
   ```bash
   cloudflared tunnel route dns yupp-agent mcp.yourdomain.com
   ```
2. **App-level guard:** set `HOST_PATH_GUARD` in the mono-server `.env`:
   ```bash
   HOST_PATH_GUARD={"mcp.yourdomain.com":["/mcp","/health"]}
   ```
   Requests arriving on `mcp.yourdomain.com` are now restricted to `/mcp/*`
   and `/health`; everything else returns 404 even though the route exists
   in the app.

Subdomains not listed in `HOST_PATH_GUARD` are unaffected. Direct LAN and
localhost access keeps reaching every route.

### MCP client config

Add to your MCP client (e.g. Claude Desktop):

```json
{
  "mcpServers": {
    "ahs-mono": {
      "url": "https://agent.yourdomain.com/mcp",
      "headers": {
        "Authorization": "Bearer yupp_dev_<your-token>"
      }
    }
  }
}
```

---

## 7. Verification

### Health check

```bash
curl http://localhost:8090/health
# → {"status":"ok"}
```

### Streamlit

Open in browser: http://localhost:8501

### Create a test agent session

```bash
# Replace <your-api-key> with the AGENT_HARNESS_SERVICE_API_KEY from .env
curl -s -X POST http://localhost:8090/ahs/sessions \
    -H "Content-Type: application/json" \
    -H "X-API-Key: <your-api-key>" \
    -d '{
        "agent_name": "general-purpose",
        "user_message": "Hello, what tools do you have?",
        "max_turns": 3
    }' | python3 -m json.tool
```

Expected: HTTP 200 with a `session_id` field.

### MCP endpoint

```bash
# With a dev token created during setup
curl http://localhost:8090/mcp \
    -H "Authorization: Bearer yupp_dev_<your-token>"
# → FastMCP endpoint responds (SSE stream or JSON depending on client)
```

### Systemd survival test

```bash
# Restart the VM / Mac, then verify services came back up
sudo systemctl status ahs-mono ahs-streamlit
# Both should show: active (running)
```

---

## 8. Upgrades

### Docker Compose

```bash
cd /path/to/yupp-agent
git pull
docker compose -f docker-compose.one-box.yml build
docker compose -f docker-compose.one-box.yml up -d --no-deps app streamlit
docker compose -f docker-compose.one-box.yml exec app python -m alembic upgrade head
```

### Bare-metal VM

```bash
cd /opt/yupp-agent
sudo -u ahs git pull
sudo -u ahs poetry install --no-root --without dev --compile
sudo -u ahs python -m alembic upgrade head
sudo systemctl restart ahs-mono ahs-streamlit
```

### MacBook

```bash
cd yupp-agent
git pull
poetry install --no-root --without dev
python -m alembic upgrade head
# Restart both terminal processes (Ctrl+C, then re-run uvicorn / streamlit)
```

---

## 9. Troubleshooting

### AHS fails to start — database connection error

```
asyncpg.exceptions.ConnectionRefusedError: connection to server ... failed
```

- Verify PostgreSQL is running: `pg_isready -h localhost -U postgres`
- Check `POSTGRES_CONNECTION_AGENTDB` in `.env` — must be valid JSON: `{"user":…,"password":…,"host":…,"database":…}`
- For Docker Compose: ensure the `postgres` service is healthy before `app` starts (`depends_on: condition: service_healthy` is set)

### Streamlit shows blank page

- Streamlit starts after AHS (`After=ahs-mono.service` in the unit file). Wait 20–30 s for AHS to finish startup.
- Check logs: `journalctl -u ahs-streamlit -n 50` or `docker compose logs streamlit`

### bwrap sandbox errors in Docker

```
bwrap: Can't mount proc on /newroot/proc: Operation not permitted
```

Set `SANDBOX_ENABLED=false` in `.env`, or add `privileged: true` to the `app` service in `docker-compose.one-box.yml`.

### Port already in use

```
OSError: [Errno 98] Address already in use
```

```bash
# Find what's using the port
sudo lsof -i :8090
sudo lsof -i :8501
```

### Alembic migration fails

```bash
# Run manually with verbose output
python -m alembic -c alembic.ini upgrade head --sql   # print SQL only (dry run)
python -m alembic -c alembic.ini upgrade head         # apply
python -m alembic -c alembic.ini current              # check current revision
```

### View structured logs (JSON)

```bash
# Pretty-print journald JSON logs
journalctl -u ahs-mono -f -o json | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        e = json.loads(line)
        msg = e.get('MESSAGE', '')
        try:
            data = json.loads(msg)
            print(json.dumps(data, indent=2))
        except Exception:
            print(msg)
    except Exception:
        pass
"
```

### User management

```bash
# Add a user
python -m ypl.mono_server.manage add-user

# List users
python -m ypl.mono_server.manage list-users

# Create MCP dev token
python -m ypl.mono_server.manage create-mcp-token

# Full help
python -m ypl.mono_server.manage --help
```
