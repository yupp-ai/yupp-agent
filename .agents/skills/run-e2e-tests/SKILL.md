---
name: run-e2e-tests
description: Run E2E tests against a running local monolith with real Slack. Requires /setup-e2e-tests to have been run first. Use when you want to verify the SAG → AHS → SAG flow works end-to-end.
allowed-tools: Bash, Read, Grep, AskUserQuestion
---

# Run E2E Tests

Run the end-to-end test suite against a running local monolith with real Slack integration.

**Prerequisite**: `/setup-e2e-tests` must have been completed first (`.env.e2e` configured, staging DB dumped, bot created and configured).

---

## Step 1: Verify prerequisites

Check that everything is running:

```bash
# Monolith health
curl -sf http://localhost:8090/health && echo "Monolith: OK" || echo "Monolith: NOT RUNNING"

# ngrok tunnel
curl -s http://localhost:4040/api/tunnels | python3 -c "import json,sys; t=json.load(sys.stdin)['tunnels']; print(f'ngrok: {t[0][\"public_url\"]}' if t else 'ngrok: NOT RUNNING')" 2>/dev/null || echo "ngrok: NOT RUNNING"

# .env.e2e exists
[ -f .env.e2e ] && echo ".env.e2e: OK" || echo ".env.e2e: MISSING"
```

---

## Step 1a: Start monolith if not running

If the monolith is not running, ask the user to start it in a separate terminal.

**Always tee output to a log file** so the agent can read it for debugging:

> **The monolith is not running.** Please start it in a separate terminal:
> ```bash
> ./scripts/run_local.sh --e2e 2>&1 | tee /tmp/monolith_local.log
> ```
> Or in background mode:
> ```bash
> ./scripts/run_local.sh --e2e --background
> ```
> (Background mode automatically logs to `/tmp/monolith_local.log`)

**Important**: The monolith must be started by the **user** (not the Bash tool) because it needs GCP Application Default Credentials to fetch Slack bot secrets from Secret Manager. The Bash tool sandbox may not have gcloud credentials in its environment.

After the user confirms it's running, verify:
```bash
curl -sf http://localhost:8090/health && echo "Monolith: OK" || echo "Still not running"
```

If the monolith starts but Slack events return 500 ("No Slack agent apps configured"), check the logs:
```bash
tail -50 /tmp/monolith_local.log | grep -i "error\|fail\|No Slack\|Permission"
```

Common startup failures:
- "No Slack agent apps configured" → `ENVIRONMENT` not set to `staging`, or DB not dumped, or GCP Secret Manager permission denied
- "rejected SSL upgrade" → `.env.e2e` missing `ENABLE_CLOUDSQL_PROXY=true`
- "type already exists" → run `poetry run alembic -c alembic.ini stamp head`

To stop a backgrounded monolith later:
```bash
kill $(cat /tmp/monolith_local.pid)
```

---

## Step 1b: Start ngrok if not running

If ngrok is not running, try starting it in the background (logs to `/tmp/ngrok_local.log`):

```bash
ngrok http 8090 --log=stdout > /tmp/ngrok_local.log 2>&1 &
echo $! > /tmp/ngrok_local.pid
sleep 3
curl -s http://localhost:4040/api/tunnels | python3 -c "import json,sys; t=json.load(sys.stdin)['tunnels']; print(t[0]['public_url'] if t else 'ngrok: NOT RUNNING')"
```

If that doesn't work, ask the user to start it in a separate terminal (always tee to log file):

> **Please start ngrok in a separate terminal:**
> ```bash
> ngrok http 8090 --log=stdout 2>&1 | tee /tmp/ngrok_local.log
> ```

Then get the URL:
```bash
curl -s http://localhost:4040/api/tunnels | python3 -c "import json,sys; t=json.load(sys.stdin)['tunnels']; print(t[0]['public_url'] if t else 'ngrok: NOT RUNNING')"
```

**Log files for debugging** (always available regardless of how services were started):
- Monolith: `/tmp/monolith_local.log`
- ngrok: `/tmp/ngrok_local.log`

**If the ngrok URL changed** (free tier generates a new URL each session), the user needs to update the Slack Event Subscription URL:

> **ngrok URL may have changed.** Please verify at https://api.slack.com/apps → your e2e bot → Event Subscriptions:
> - The Request URL should be: `{ngrok_url}/gw/slack/slack/events`
> - If it's different, update it and click Save Changes
> - Slack will re-verify the URL (should show a green checkmark)

---

## Step 1c: Verify Slack events reach the monolith

Before running the full test suite, verify the pipeline works by checking ngrok's request log:

```bash
curl -s "http://localhost:4040/api/requests/http?limit=3" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
reqs = data.get('requests', [])
if not reqs:
    print('No recent requests through ngrok — Slack may not be sending events')
for req in reqs:
    r = req.get('request', {})
    resp = req.get('response', {})
    print(f\"{r.get('method')} {r.get('uri', '?')} -> {resp.get('status_code', '?')}\")
"
```

If you see `500` responses with "No Slack agent apps configured":
- The monolith was not started with `--e2e` flag (needs `ENVIRONMENT=staging` to load bot configs from DB)
- Ask the user to restart: `./scripts/run_local.sh --e2e 2>&1 | tee /tmp/monolith_local.log`

If no requests appear at all:
- Slack Event Subscription URL may be wrong or unverified
- Ask the user to check Event Subscriptions page

---

## Step 2: Check required env vars

The tests need Slack environment variables. Check if they're in `.env.e2e`:

```bash
grep -q "^SLACK_E2E_USER_TOKEN=" .env.e2e 2>/dev/null && echo "SLACK_E2E_USER_TOKEN: set" || echo "SLACK_E2E_USER_TOKEN: NOT SET"
grep -q "^SLACK_E2E_CHANNEL_ID=" .env.e2e 2>/dev/null && echo "SLACK_E2E_CHANNEL_ID: set" || echo "SLACK_E2E_CHANNEL_ID: NOT SET"
grep -q "^SLACK_E2E_BOT_USER_ID=" .env.e2e 2>/dev/null && echo "SLACK_E2E_BOT_USER_ID: set" || echo "SLACK_E2E_BOT_USER_ID: NOT SET"
```

If any are missing, use `AskUserQuestion`:

> The e2e tests need these environment variables in `.env.e2e`. Provide them:
>
> - **SLACK_E2E_USER_TOKEN**: A Slack user token (`xoxp-...`) with `chat:write` scope. Create one at https://api.slack.com/apps → OAuth & Permissions → User Token Scopes → `chat:write`, `channels:history`, `channels:read`. **Must be a user token, not a bot token** — bot tokens don't trigger `app_mention` events.
> - **SLACK_E2E_CHANNEL_ID**: The channel ID for `#ahs-e2e-testing`. Find it by right-clicking the channel → View channel details → Channel ID at the bottom.
> - **SLACK_E2E_BOT_USER_ID**: The e2e test bot's Slack user ID. Find it by clicking the bot's name in Slack → View app details → Member ID.

After the user provides the values, **append them to `.env.e2e`** so they persist across sessions:

```bash
cat >> .env.e2e <<EOF
SLACK_E2E_USER_TOKEN=<provided_token>
SLACK_E2E_CHANNEL_ID=<provided_channel_id>
SLACK_E2E_BOT_USER_ID=<provided_bot_user_id>
EOF
```

---

## Step 3: Run the tests

Source `.env.e2e` to load all env vars (including Slack tokens and API key), then run:

```bash
source /opt/homebrew/Caskroom/miniforge/base/etc/profile.d/conda.sh && conda activate ys-dev && \
set -a && source .env.e2e && set +a && \
poetry run pytest tests/e2e/ -v -m e2e --timeout=120
```

**Important**: Use `set -a && source .env.e2e && set +a` to export all variables from the file. This ensures `SLACK_E2E_*`, `AGENT_HARNESS_SERVICE_API_KEY`, and other vars are available to the tests. Do **not** add inline env var assignments (e.g. `SLACK_E2E_USER_TOKEN=...`) to the command — they would override the values sourced from `.env.e2e` and break the skip logic in `conftest.py` if set to placeholder strings.

---

## Step 4: Report results

After tests complete, report:

- Number of tests passed / failed / skipped
- For each failure: test name, error message, and likely cause
- Suggestions for fixing failures

If Slack tests fail but health/auth pass, check the logs for clues:
```bash
# Monolith logs — look for event processing errors
tail -50 /tmp/monolith_local.log | grep -i "error\|500\|fail\|No Slack"

# ngrok logs — check if requests are arriving and their status codes
tail -50 /tmp/ngrok_local.log

# ngrok request inspector — see recent requests and response codes
curl -s "http://localhost:4040/api/requests/http?limit=5" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for req in data.get('requests', []):
    r = req.get('request', {})
    resp = req.get('response', {})
    print(f\"{r.get('method')} {r.get('uri', '?')} -> {resp.get('status_code', '?')}\")
if not data.get('requests'):
    print('No recent requests through ngrok')
"
```

Common failure patterns:

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `missing_scope` | Wrong token type (bot vs user) | Use a `xoxp-...` user token with `chat:write` |
| `not_in_channel` | Bot or sender not in the test channel | Invite them via `/invite @bot-name` |
| `invalid_auth` | Token expired or revoked | Get a fresh token |
| `Bot did not reply within 30s` | ngrok down, monolith not started with `--e2e`, or Event Subscription URL wrong | Check ngrok requests (step 1c), monolith logs, and Slack Event Subscriptions |
| `No Slack agent apps configured` | Monolith not started with `--e2e` flag | Restart with `./scripts/run_local.sh --e2e` |
| `assert sessions >= 1` | API key mismatch | Ensure `AGENT_HARNESS_SERVICE_API_KEY` is sourced from `.env.e2e` |
| `404 on /slack-agent-gateway/*` | Monolith missing legacy route mounts | Ensure PR #179 is merged |

---

## Notes

- Tests take ~25-30 seconds (waiting for Slack webhook delivery + bot processing)
- Each test run sends real messages to `#ahs-e2e-testing` — expect test messages to appear there
- The `parrot-bubba` mock executor is used by default (no LLM API calls)
- Tests are tagged with `@pytest.mark.e2e` and excluded from regular `pytest` runs via `-m 'not e2e'`
- The monolith can be started in background with `./scripts/run_local.sh --e2e --background` (logs to `/tmp/monolith_local.log`)
- To stop a backgrounded monolith: `kill $(cat /tmp/monolith_local.pid)`
- ngrok free tier generates a new URL each session — update Slack Event Subscription URL if it changes
