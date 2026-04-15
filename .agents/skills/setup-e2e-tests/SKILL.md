---
name: setup-e2e-tests
description: Guide the user through setting up and running E2E tests for the SAG → AHS → SAG flow with a real Slack bot and local monolith. Use when setting up e2e testing for the first time or debugging a broken setup.
allowed-tools: Bash, Read, Write, Grep, Glob, AskUserQuestion, WebSearch
---

# Setup E2E Tests

Interactive guide to set up end-to-end testing for the Slack Agent Gateway → Agent Harness Service → Slack Agent Gateway flow.

For the full reference doc, see `tests/e2e/SETUP.md`.

---

## Step 1: Check prerequisites

Verify the required tools are installed:

```bash
docker --version || echo "MISSING: Install Docker Desktop"
ngrok version 2>/dev/null || cloudflared version 2>/dev/null || echo "MISSING: brew install ngrok"
poetry --version || echo "MISSING: Install poetry"
pg_isready --version || echo "MISSING: brew install libpq"
```

If any are missing, use `AskUserQuestion` to confirm the user wants to install them.

---

## Step 2: Start Postgres + Redis

```bash
pg_isready -h localhost -p 5432 -q && echo "Postgres: OK" || echo "Postgres: NOT RUNNING"
redis-cli -h localhost -p 6379 ping 2>/dev/null && echo "Redis: OK" || echo "Redis: NOT RUNNING"
```

If not running, check for Homebrew services first:
```bash
brew services stop postgresql@16 2>/dev/null
brew services stop redis 2>/dev/null
docker-compose up -d
```

---

## Step 3: Set up .env.e2e

Check if `.env.e2e` exists and has the critical settings:

```bash
if [ ! -f .env.e2e ]; then
  echo "MISSING: .env.e2e — copy from staging env and apply overrides"
fi
```

**Critical overrides to verify** (these are the most common sources of errors):

```bash
grep -E "^(ENVIRONMENT|AGENT_HARNESS_SERVICE_BASE_URL|GATEWAY_BASE_URL|ENABLE_CLOUDSQL_PROXY|POSTGRES_CONNECTION_YUPPDB|GOOGLE_APPLICATION_CREDENTIALS)=" .env.e2e
```

Expected values:
- `ENVIRONMENT="staging"` — NOT `local` (SAG loads bot configs from DB only in staging mode)
- `AGENT_HARNESS_SERVICE_BASE_URL="http://localhost:8090"` — MUST be `http`, not `https`
- `GATEWAY_BASE_URL="http://localhost:8090"` — MUST be `http`, not `https`
- `ENABLE_CLOUDSQL_PROXY="true"` — disables SSL for local Postgres (counterintuitive name)
- `POSTGRES_CONNECTION_YUPPDB=""` — must be empty (staging validator rejects test credentials)
- `GOOGLE_APPLICATION_CREDENTIALS="<path to SA key>"` — must point to a GCP service account with `secretmanager.versions.access` permission. The default local dev SA (`yupp-llms-shared-local-dev-service-account.json`) does NOT have this permission. To obtain a suitable key: ask a team lead for access to a SA with Secret Manager permissions, or create one in GCP Console → IAM & Admin → Service Accounts → create key (JSON). Store the key file locally (e.g. `~/yupp-secret-manager-sa.json`) and add to `.env.e2e`:
  ```
  GOOGLE_APPLICATION_CREDENTIALS=/Users/<you>/yupp-secret-manager-sa.json
  ```

Also verify DB connection points to `yadb` (not `yupp_agent`):
```bash
grep POSTGRES_CONNECTION_AGENTDB .env.e2e | grep -o '"database":"[^"]*"'
```
Should show `"database":"yadb"`.

Check for trailing `\n` in JSON values (common copy-paste issue):
```bash
grep '\\n"$' .env.e2e && echo "WARNING: found trailing \\n in JSON values — fix them"
```

If any are wrong, explain what each setting does and offer to fix it.

---

## Step 4: Create a Slack bot (one-time)

Use `AskUserQuestion` to check:
> Do you already have a Slack bot for e2e testing?
> If not, create one via `/create-agent` slash command in Slack.
> Note the App ID after creation.

---

## Step 5: Dump staging DB to local

Check if local DB has the bot:

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d yadb -tAc \
  "SELECT count(*) FROM slack_agents" 2>/dev/null
```

If 0 or DB doesn't exist, dump from staging:
```bash
poetry run python -m ypl.db.tools.dump_staging_to_local
```

**IMPORTANT**: After the dump, stamp alembic:
```bash
poetry run alembic -c alembic.ini stamp head
```
Without this, `run_local.sh` will fail trying to create tables that already exist.

Verify the bot exists:
```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d yadb -tAc \
  "SELECT app_id, agent_name, bot_name FROM slack_agents WHERE bot_name LIKE '%e2e%'"
```

---

## Step 6: Set bot to use parrot-bubba

The bot needs to use the mock executor for e2e testing (no LLM API calls):

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d yadb -c \
  "UPDATE slack_agents SET agent_name = 'parrot-bubba' WHERE app_id = '<APP_ID>';"
```

Use `AskUserQuestion` to get the App ID if not known.

---

## Step 7: Start the monolith

```bash
./scripts/run_local.sh --e2e
```

Verify:
```bash
curl -sf http://localhost:8090/health && echo "OK" || echo "FAILED"
```

**If it fails**, check common errors:
- "rejected SSL upgrade" → `ENABLE_CLOUDSQL_PROXY` must be `true`
- "test values in staging" → `POSTGRES_CONNECTION_YUPPDB` must be empty
- "type already exists" (alembic) → run `poetry run alembic -c alembic.ini stamp head`
- Circular import → ensure PR #171 fix is applied

---

## Step 8: Start ngrok

```bash
ngrok http 8090
```

Get the URL:
```bash
curl -s http://localhost:4040/api/tunnels | python3 -c \
  "import json,sys; t=json.load(sys.stdin)['tunnels']; print(t[0]['public_url'] if t else 'ngrok not running')"
```

---

## Step 9: Configure Slack Event Subscriptions

Use `AskUserQuestion`:

> **Configure Slack Event Subscriptions**
>
> 1. Go to https://api.slack.com/apps → select your e2e test bot
> 2. **Event Subscriptions** → Enable Events
> 3. Set **Request URL** to: `{ngrok_url}/gw/slack/slack/events`
>    (note: double `slack` — router at `/gw/slack/` + path `/slack/events`)
> 4. **Subscribe to bot events** → Add `app_mention`
> 5. Click **Save Changes**
>
> Did the verification succeed?

If verification fails:
- Check monolith is running: `curl http://localhost:8090/health`
- Check ngrok is running: `curl -s http://localhost:4040/api/tunnels`
- Test challenge locally:
  ```bash
  curl -X POST http://localhost:8090/gw/slack/slack/events \
    -H "Content-Type: application/json" \
    -d '{"type":"url_verification","challenge":"test123"}'
  ```
- "No Slack agent apps configured" → re-dump staging DB, then restart monolith

---

## Step 10: Smoke test

Ask the user:
> In `#ahs-e2e-testing`, type: `@your-bot-name hello`
>
> You should see:
> 1. An :eyes: reaction
> 2. A "Looking into it..." placeholder
> 3. A reply from parrot-bubba
>
> Did it work?

If not, debug by checking monolith logs for:
- "No Slack agent apps configured" → DB issue (see step 5)
- 404 on `/slack-agent-gateway/...` → missing legacy route mount (PR #179)
- 404 on `/mcp/harness/` → missing legacy mount or wrong mount order (PR #179)
- SSL errors → `https` in BASE_URLs (must be `http`)

---

## Step 11: Set up Slack test token (for automated e2e tests)

The e2e test suite sends real messages to Slack programmatically. It needs a **user token** (not a bot token).

Use `AskUserQuestion`:

> **Set up Slack user token for e2e tests**
>
> The tests need a Slack user token (`xoxp-...`) to send messages. Bot tokens (`xoxb-...`) don't work — Slack only fires `app_mention` events for user-sent messages.
>
> **To create one:**
> 1. Go to https://api.slack.com/apps → Create New App (or use an existing one)
> 2. OAuth & Permissions → User Token Scopes → add `chat:write`, `channels:history`, `channels:read`
> 3. Install to Workspace → copy the **User OAuth Token** (`xoxp-...`)
>
> **Also needed:**
> - **Channel ID**: Right-click `#ahs-e2e-testing` → View channel details → Channel ID (starts with `C`)
> - **Bot User ID**: Click the e2e test bot's name in Slack → View app details → Member ID (starts with `U`)
>
> Please provide:
> 1. SLACK_E2E_USER_TOKEN
> 2. SLACK_E2E_CHANNEL_ID
> 3. SLACK_E2E_BOT_USER_ID

After the user provides the values, **append them to `.env.e2e`** so they persist across sessions and are automatically loaded by `source .env.e2e` in `/run-e2e-tests`:

```bash
cat >> .env.e2e <<EOF
SLACK_E2E_USER_TOKEN=<provided_token>
SLACK_E2E_CHANNEL_ID=<provided_channel_id>
SLACK_E2E_BOT_USER_ID=<provided_bot_user_id>
EOF
```

---

## Step 12: Verify with e2e tests

Run `/run-e2e-tests` to verify the full setup works.

---

## Teardown

- Stop the monolith (Ctrl+C)
- Stop ngrok (Ctrl+C)
- Docker: `docker-compose down` (keeps data) or `docker-compose down -v` (deletes data)
- The Slack Event Subscription URL stops working when ngrok stops (free tier = ephemeral URL)

---

## Quick reference: common errors and fixes

| Error | Cause | Fix |
|-------|-------|-----|
| "No Slack agent apps configured" | `ENVIRONMENT=local` or bot not in DB | Set `ENVIRONMENT=staging`, re-dump staging |
| "rejected SSL upgrade" | `ENABLE_CLOUDSQL_PROXY` not true | Set `ENABLE_CLOUDSQL_PROXY="true"` |
| "test values in staging" | yuppdb has test creds in staging mode | Set `POSTGRES_CONNECTION_YUPPDB=""` |
| SSL record layer failure | BASE_URLs use `https://` | Change to `http://localhost:8090` |
| 404 on `/slack-agent-gateway/*` | Missing legacy route mount | Apply PR #179 |
| 404 on `/mcp/harness/` | Wrong mount order or missing mount | Mount `/mcp/harness` BEFORE `/mcp` |
| "type already exists" (alembic) | Missing alembic_version after dump | `alembic stamp head` |
| "Invalid JSON: trailing characters" | `\n` in JSON env values | Remove trailing `\n` from JSON strings |
