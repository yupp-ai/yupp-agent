# E2E Test Setup Guide

End-to-end tests for the SAG → AHS → SAG flow using a real Slack bot and local monolith.

## Prerequisites

- Docker (for Postgres + Redis)
- `ngrok` or `cloudflared` (for exposing local server to Slack)
- A Slack bot created via Bot Father (or manually)
- Access to staging DB (for `dump_staging_to_local`)

## Setup Steps

### 1. Install ngrok

```bash
brew install ngrok
# or
brew install cloudflared
```

### 2. Start infrastructure

```bash
# Start Postgres + Redis (if not already running)
docker-compose up -d
```

### 3. Dump staging DB to local

This gives you real agent configs, user data, and the Slack bot registration.

```bash
poetry run python -m ypl.db.tools.dump_staging_to_local
```

### 4. Create a Slack bot (one-time)

Use Bot Father in Slack:
1. Run `/create-agent` in any channel with the Bot Father bot
2. Fill in: agent name (e.g., `e2e-test`), slack name, display name
3. Wait for approval and OAuth completion
4. Note the **App ID** (visible in the Bot Father approval message or at https://api.slack.com/apps)

Or create manually at https://api.slack.com/apps with scopes:
- `app_mentions:read`, `chat:write`, `channels:history`, `channels:read`, `reactions:write`

### 5. Create a test channel

Create `#ahs-e2e-testing` (or similar) in your Slack workspace and invite the bot.

### 6. Start the monolith

```bash
./scripts/run_local.sh
```

Wait for `AHS + MCP + SAG running at http://localhost:8090` to appear.

### 7. Start ngrok

In a separate terminal:

```bash
ngrok http 8090
```

Note the forwarding URL (e.g., `https://abc123.ngrok-free.app`).

### 8. Configure Slack Event Subscriptions

1. Go to https://api.slack.com/apps → select your bot
2. **Event Subscriptions** → Enable Events
3. Set **Request URL** to: `https://<ngrok-url>/gw/slack/slack/events`
   - Slack sends a verification challenge — it should return 200 if the monolith is running
   - If it fails: check ngrok is forwarding, monolith is running, URL ends with `/gw/slack/slack/events`
4. **Subscribe to bot events** → Add `app_mention`
5. Click **Save Changes**

### 9. Verify manually

In `#ahs-e2e-testing`, type:

```
@your-bot-name hello
```

You should see:
1. A `:eyes:` reaction (ack from SAG)
2. A "Looking into it..." placeholder message
3. A reply from the agent (or an error if the agent isn't configured)

### 10. Run e2e tests

```bash
poetry run pytest tests/e2e/ -v -m e2e --timeout=60
```

## Troubleshooting

### Slack says "Request URL didn't respond"
- Is the monolith running? Check `curl http://localhost:8090/health`
- Is ngrok running? Check the ngrok terminal for requests
- URL must end with `/gw/slack/slack/events` (double `slack` because the router is mounted at `/gw/slack/`)

### Bot doesn't respond to mentions
- Is the bot invited to the channel?
- Check `Event Subscriptions` → `app_mention` is subscribed
- Check monolith logs for errors (SAG event processing)
- Run `curl https://<ngrok-url>/health` to verify the tunnel works

### "No Slack agent apps configured"
- The bot's `slack_agents` row may be missing from local DB
- Re-run `python -m ypl.db.tools.dump_staging_to_local` to sync

### "I'm not allowed to chat in this channel"
- The channel allowlist is blocking the channel
- Check dynamic app settings or add the channel pattern to the allowlist

### Session not created in AHS
- Check that `AGENT_HARNESS_SERVICE_BASE_URL` in `.env` points to `http://localhost:8090`
- Check monolith logs for AHS session creation errors
- The bot's `agent_name` must match a valid agent config (filesystem or DB)

## Environment Variables

Key variables in `.env` for e2e testing:

```
ENVIRONMENT=local
DEFAULT_DB=agentdb
AGENT_HARNESS_SERVICE_BASE_URL=http://localhost:8090
GATEWAY_BASE_URL=http://localhost:8090
POSTGRES_CONNECTION_AGENTDB={"user":"postgres","password":"...","host":"localhost:5432","database":"yadb"}
REDIS_URL=redis://localhost:6379/1
```

## Architecture

```
Slack (real)
  │
  │ POST /gw/slack/slack/events (via ngrok)
  ▼
Local Monolith (:8090)
  ├── SAG (event processing, channel check, user resolution)
  │     │
  │     │ POST /ahs/session/create (localhost, same process)
  │     ▼
  ├── AHS (session lifecycle, executor)
  │     │
  │     │ parrot-bubba mock executor (no LLM calls)
  │     │
  │     │ callback to SAG
  │     ▼
  ├── SAG (posts reply via Slack SDK)
  │     │
  │     ▼
Slack (real reply appears in channel)
```
