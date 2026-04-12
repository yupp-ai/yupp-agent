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
# Check Docker
docker --version || echo "MISSING: Install Docker Desktop"

# Check ngrok or cloudflared
ngrok version 2>/dev/null || cloudflared version 2>/dev/null || echo "MISSING: brew install ngrok"

# Check poetry
poetry --version || echo "MISSING: Install poetry"

# Check pg_isready
pg_isready --version || echo "MISSING: brew install libpq"
```

If any are missing, use `AskUserQuestion` to confirm the user wants to install them, then provide the install command.

---

## Step 2: Start Postgres + Redis

Check if they're running:

```bash
pg_isready -h localhost -p 5432 -q && echo "Postgres: OK" || echo "Postgres: NOT RUNNING"
redis-cli -h localhost -p 6379 ping 2>/dev/null && echo "Redis: OK" || echo "Redis: NOT RUNNING"
```

If not running:
```bash
docker-compose up -d
```

Wait for health checks to pass.

---

## Step 3: Sync staging data to local

Check if local DB has slack_agents data:

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d yadb -tAc "SELECT count(*) FROM slack_agents" 2>/dev/null
```

If the count is 0 or the DB doesn't exist, run the staging dump:

```bash
poetry run python -m ypl.db.tools.dump_staging_to_local
```

This is interactive — it will ask for confirmation before overwriting local data.

After the dump, verify the e2e bot exists:

```bash
PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d yadb -tAc \
  "SELECT agent_name, app_id, status FROM slack_agents WHERE bot_name LIKE '%e2e%'"
```

If no e2e bot exists, use `AskUserQuestion` to ask:
- "Do you have a Slack bot for e2e testing? If not, create one via `/create-agent` in Slack."
- Collect the App ID.
- Verify it's in the DB after re-dumping.

---

## Step 4: Check .env configuration

Read the .env file and verify critical variables:

```bash
grep -E "^(ENVIRONMENT|DEFAULT_DB|AGENT_HARNESS_SERVICE_BASE_URL|POSTGRES_CONNECTION_AGENTDB|REDIS_URL)=" .env
```

Required values for e2e:
- `ENVIRONMENT=local`
- `DEFAULT_DB=agentdb`
- `AGENT_HARNESS_SERVICE_BASE_URL=http://localhost:8090`
- `POSTGRES_CONNECTION_AGENTDB` must point to local DB
- `REDIS_URL` must point to local Redis

If `AGENT_HARNESS_SERVICE_BASE_URL` is missing or points to staging, warn the user and offer to fix it.

---

## Step 5: Start the monolith

```bash
./scripts/run_local.sh
```

Verify it started:
```bash
curl -sf http://localhost:8090/health && echo "Monolith: OK" || echo "Monolith: NOT RUNNING"
```

If it fails with a circular import error, check if the fix from PR #171 is applied.

---

## Step 6: Start ngrok

In a separate terminal:

```bash
ngrok http 8090
```

Extract the forwarding URL:
```bash
curl -s http://localhost:4040/api/tunnels | python3 -c "import json,sys; t=json.load(sys.stdin)['tunnels']; print(t[0]['public_url'] if t else 'ngrok not running')"
```

---

## Step 7: Configure Slack Event Subscriptions

Use `AskUserQuestion` to guide the user:

> **Configure Slack Event Subscriptions**
>
> 1. Go to https://api.slack.com/apps → select your e2e test bot
> 2. **Event Subscriptions** → Enable Events
> 3. Set **Request URL** to: `{ngrok_url}/gw/slack/slack/events`
> 4. **Subscribe to bot events** → Add `app_mention`
> 5. Click **Save Changes**
>
> Did the verification succeed? (Slack shows a green checkmark)

If verification fails:
- Check ngrok is running: `curl -s http://localhost:4040/api/tunnels`
- Check monolith is running: `curl http://localhost:8090/health`
- Check URL ends with `/gw/slack/slack/events` (double `slack`)
- Try sending the verification manually:
  ```bash
  curl -X POST {ngrok_url}/gw/slack/slack/events \
    -H "Content-Type: application/json" \
    -d '{"type":"url_verification","challenge":"test123"}'
  ```
  Should return `{"challenge":"test123"}`

---

## Step 8: Manual smoke test

Ask the user to send a test message:

> In `#ahs-e2e-testing`, type: `@your-bot-name hello`
>
> You should see:
> 1. An :eyes: reaction
> 2. A "Looking into it..." placeholder
> 3. A reply from the agent
>
> Did it work?

If not, debug:
- Check ngrok terminal for incoming requests
- Check monolith logs for errors
- Common issues:
  - "No Slack agent apps configured" → re-dump staging DB
  - "I'm not allowed in this channel" → channel allowlist issue
  - 404 on session/create → `AGENT_HARNESS_SERVICE_BASE_URL` wrong
  - No reaction at all → Event Subscriptions not configured or wrong URL

---

## Step 9: Run e2e tests

```bash
poetry run pytest tests/e2e/ -v -m e2e --timeout=60
```

Report results to the user.

---

## Teardown

When done testing:
- Stop the monolith (Ctrl+C)
- Stop ngrok (Ctrl+C)
- Optionally stop Docker: `docker-compose down` (keeps data) or `docker-compose down -v` (deletes data)
- The Slack Event Subscription URL will stop working (ngrok URL is ephemeral)

---

## Notes

- ngrok free tier generates a new URL each time — you'll need to update the Slack Event Subscription URL each session
- The local monolith uses the `parrot-bubba` mock executor by default (no LLM API calls needed)
- The bot's `agent_name` in the `slack_agents` table determines which agent config is used
- To change the agent config, update the `agent_name` in the local DB:
  ```sql
  UPDATE slack_agents SET agent_name = 'parrot-bubba' WHERE app_id = '<APP_ID>';
  ```
