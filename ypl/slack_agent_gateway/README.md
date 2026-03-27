# Slack Agent Gateway

A gateway service that enables multiple AI agents to operate as distinct Slack bots. Each agent has its own Slack app with unique name and avatar, but all route through a single backend service.

For architecture and design details, see [DESIGN.md](./DESIGN.md).

## Quick Start

### Prerequisites

- Access to GCP Secret Manager
- Access to Slack API (api.slack.com)
- Graphite CLI (`gt`) for deployments

### Service URLs

| Environment | URL |
|-------------|-----|
| Staging | `https://slack-agent-gateway-staging.yupp.ai` |
| Production | `https://slack-agent-gateway-production.yupp.ai` |

## How to Add a New Agent

Adding a new agent is simple - no service code changes or deploys required!

### Recommended: Use the `/add-slack-agent` Skill

The easiest way to add a new agent is to use the Claude Code skill:

```
/add-slack-agent
```

This skill will guide you through the entire process interactively:
1. Collect agent information (slack name, display name, AHS agent)
2. Walk you through creating the Slack app
3. Collect credentials (App ID, Signing Secret, Bot Token)
4. Create GCP secrets for staging and production
5. Update `data/secret-env-var-map.yml` for deployment
6. Update `data/dynamic_app_settings_base.yml`
7. Configure Interactivity & Event Subscriptions
8. Create and submit the PR

See [`.agents/skills/add-slack-agent/SKILL.md`](../../.agents/skills/add-slack-agent/SKILL.md) for the full skill documentation.

### Manual Setup Reference

If you prefer to set up manually, here's the process:

<details>
<summary>Click to expand manual setup steps</summary>

#### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App**
2. Choose "From scratch"
3. Select your workspace
4. Set the app name (this is how users will @mention the agent)

#### 2. Configure OAuth & Permissions

Add these Bot Token Scopes:
- `app_mentions:read` - Receive mention events
- `channels:read` - Read channel info (required for channel allowlist filtering)
- `channels:history` - Read public channel messages
- `chat:write` - Post messages and interactive components (buttons, menus)
- `groups:read` - Read private channel info (required for channel allowlist filtering)
- `groups:history` - Read private channel messages
- `reactions:read` - Read emoji reactions
- `reactions:write` - Add emoji reactions

> **Note**: No additional scopes are required for receiving button clicks. Button click events are delivered via the Interactivity URL (not Event Subscriptions). The `chat:write` scope is sufficient to post messages with buttons and update them after clicks.

Then **Install to Workspace** and copy the **Bot User OAuth Token** (starts with `xoxb-`).

#### 3. Create GCP Secrets

Use `printf` (not `echo`) to avoid trailing newlines:

```bash
# Staging
printf '%s' "APP_ID" | gcloud secrets create ym-slack-agent-gateway-{name}-app-id-staging --data-file=- --project=yupp-llms
printf '%s' "xoxb-TOKEN" | gcloud secrets create ym-slack-agent-gateway-{name}-bot-token-staging --data-file=- --project=yupp-llms
printf '%s' "SIGNING_SECRET" | gcloud secrets create ym-slack-agent-gateway-{name}-signing-secret-staging --data-file=- --project=yupp-llms

# Production
printf '%s' "APP_ID" | gcloud secrets create ym-slack-agent-gateway-{name}-app-id-production --data-file=- --project=yupp-llms
printf '%s' "xoxb-TOKEN" | gcloud secrets create ym-slack-agent-gateway-{name}-bot-token-production --data-file=- --project=yupp-llms
printf '%s' "SIGNING_SECRET" | gcloud secrets create ym-slack-agent-gateway-{name}-signing-secret-production --data-file=- --project=yupp-llms
```

#### 4. Update Secret-Env-Var Mapping

Add entries to `data/secret-env-var-map.yml` to map the GCP secrets to environment variables for the `slack-agent-gateway` service.

#### 5. Add to Dynamic App Settings

Add the agent to `data/dynamic_app_settings_base.yml`:

```yaml
- name: slack_agent_gateway_settings
  type: SlackAgentGatewaySettings
  value:
    agents:
      # ... existing agents ...
      - name: {name}
        display_name: {DisplayName}
        agent_name: {ahs_agent}
```

#### 6. Deploy and Configure Event Subscriptions

1. Deploy to staging: `/deploy-to-staging slack-agent-gateway`
2. In Slack app settings, configure **Interactivity & Shortcuts**:
   - Toggle **Interactivity** to **ON**
   - Set **Request URL** to the appropriate endpoint:
     - **Staging**: `https://slack-agent-gateway-staging.yupp.ai/api/v1/slack/interactions`
     - **Production**: `https://slack-agent-gateway-production.yupp.ai/api/v1/slack/interactions`
3. Configure **Event Subscriptions**:
   - Set **Request URL** to the appropriate endpoint:
     - **Staging**: `https://slack-agent-gateway-staging.yupp.ai/api/v1/slack/events`
     - **Production**: `https://slack-agent-gateway-production.yupp.ai/api/v1/slack/events`
4. Subscribe to bot events: `app_mention`, `reaction_added`

</details>

## Troubleshooting

### "I'm not allowed to chat in this channel" with `.*` allowlist

If the bot responds with "I'm not allowed to chat in this channel. Please move to channels matching: `.*`" even though the allowlist is configured correctly, check GCP logs for:
```
"error": "missing_scope"
"needed": "channels:read,groups:read,mpim:read,im:read"
```

This means the Slack app is missing scopes required to look up channel names. The `needed` field shows all possible scopes for different conversation types. For this feature to work in public and private channels, add both `channels:read` and `groups:read` scopes in the Slack app's OAuth & Permissions settings, then reinstall the app to the workspace.

### "Invalid Slack signature - no signing secret matched"

- Verify the signing secret in GCP matches the one in Slack's Basic Information
- Check for trailing newlines in secrets (use `printf`, not `echo`)
- Ensure the agent is listed in `slack_agent_gateway_settings` in dynamic_app_settings
- Wait 60 seconds for the config to refresh from Redis

### "Your URL didn't respond with the value of the challenge parameter"

- Ensure the service is deployed and running
- Check that the agent's signing secret is configured in GCP
- Verify the URL path is correct: `/api/v1/slack/events`

### Check agent configuration loaded

Search GCP logs for:
```
resource.labels.service_name="slack-agent-gateway-staging"
jsonPayload.message="Loaded agent configuration from dynamic settings"
```

### View recent errors

```
resource.labels.service_name="slack-agent-gateway-staging"
severity>=WARNING
```

## Configuration Architecture

Agent configuration is loaded from two sources:

1. **Agent list**: `dynamic_app_settings` (Redis, synced from YAML)
2. **Agent secrets**: GCP Secret Manager (fetched at runtime)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    dynamic_app_settings                              │
├─────────────────────────────────────────────────────────────────────┤
│  YAML file (data/dynamic_app_settings_staging.yml)                  │
│    ↓ (GitHub Action syncs to Redis)                                 │
│  Redis: app_settings_key:slack_agent_gateway_settings               │
│    ↓ (read at runtime, cached 60s)                                  │
│  SlackAgentGatewaySettings { agents: [{name: "giladovski"}, ...] }   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    GCP Secret Manager                                │
├─────────────────────────────────────────────────────────────────────┤
│  For each agent in the list, fetch secrets using naming convention: │
│  - ym-slack-agent-gateway-{agent}-app-id-{env}                      │
│  - ym-slack-agent-gateway-{agent}-bot-token-{env}                   │
│  - ym-slack-agent-gateway-{agent}-signing-secret-{env}              │
└─────────────────────────────────────────────────────────────────────┘
```

## Existing Agents

| Agent | App ID | Description |
|-------|--------|-------------|
| Giladovski | `A0AEUTB436F` | Test agent |
| Lingfengovich | `A0AEWB10NM9` | Test agent |
| Tianfucius | `A0AEMFNK231` | Test agent |
| Axandwich | `A0AF30QSUUD` | Test agent |

## API Endpoints

All endpoints are available under both `/api/v1` (preferred) and `/slack-agent-gateway` (legacy) prefixes.

| Endpoint | Auth | Description |
|----------|------|-------------|
| `POST /api/v1/slack/events` | Slack signature | Receives Slack events (mentions, reactions) |
| `POST /api/v1/slack/interactions` | Slack signature | Receives interactive component events (button clicks, form submissions) |
| `POST /api/v1/sessions/info` | X-API-Key | Get session info |
| `POST /api/v1/sessions/reply` | X-API-Key | Post a new reply |
| `POST /api/v1/sessions/reply/append` | X-API-Key | Append to last reply (buffered) |
| `POST /api/v1/sessions/reply/update` | X-API-Key | Replace last reply content |

> **Note**: The `/slack-agent-gateway` prefix is deprecated. Please migrate to `/api/v1`.

## Local Development

```bash
# Copy .env from main repo if in worktree (adjust path as needed)
cp /path/to/yupp-mind/.env .

# Run the server
poetry run uvicorn ypl.slack_agent_gateway.server:app --reload --port 8080
```

For local testing, set the agent env vars directly in your `.env`:
```
SLACK_AGENT_GATEWAY_AGENTS=giladovski,tianfucius
SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID=A123...
SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN=xoxb-...
SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET=...
```

For local testing with Slack, use a tunnel service (ngrok, cloudflared) to expose your local server.
