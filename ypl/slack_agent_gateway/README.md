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
| Staging | `https://slack-agent-gateway-staging.example.com` |
| Production | `https://slack-agent-gateway-production.example.com` |

## How to Add a New Agent

Adding a new agent is simple - no service code changes or deploys required!

### Recommended: Use the `/add-slack-agent` Skill

The easiest way to add a new agent is to use the Claude Code skill:

```
/add-slack-agent
```

This skill walks through the full bot provisioning process. The actual
persistence layer — app_id + encrypted `bot_token` / `signing_secret` — lands
in the `slack_agents` DB table. See [Manual Setup Reference](#manual-setup-reference)
below for the step-by-step.

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

#### 3. Insert the agent row

Register the bot in the `slack_agents` table with the bot token + signing
secret encrypted by `ypl.slack_agent_gateway.crypto.encrypt_secret`. When
BotFather provisions a bot, it does this for you; manual SQL looks like:

```sql
INSERT INTO slack_agents (
    slack_agent_id, app_id, agent_name, bot_name, display_name,
    bot_token_encrypted, signing_secret_encrypted, status
) VALUES (
    gen_random_uuid(), 'A123…', 'sre', 'examplebot', 'Example Bot',
    $1, $2, 'ACTIVE'
);
```

where `$1` / `$2` are the Fernet-encrypted values (key =
`SLACK_AGENT_GW_ENCRYPTION_KEY`). If you'd rather skip DB-resident secrets,
leave those two columns NULL and set the matching env vars instead — see
[`docs/secrets.md`](../../docs/secrets.md).

#### 6. Deploy and Configure Event Subscriptions

1. Deploy to staging: `/deploy-to-staging slack-agent-gateway`
2. In Slack app settings, configure **Interactivity & Shortcuts**:
   - Toggle **Interactivity** to **ON**
   - Set **Request URL** to the appropriate endpoint:
     - **Staging**: `https://slack-agent-gateway-staging.example.com/api/v1/slack/interactions`
     - **Production**: `https://slack-agent-gateway-production.example.com/api/v1/slack/interactions`
3. Configure **Event Subscriptions**:
   - Set **Request URL** to the appropriate endpoint:
     - **Staging**: `https://slack-agent-gateway-staging.example.com/api/v1/slack/events`
     - **Production**: `https://slack-agent-gateway-production.example.com/api/v1/slack/events`
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
# Copy the repo .env (populated via ``python -m ypl.mono_server.setup``)
cp ../../.env .

# Run the server
poetry run uvicorn ypl.slack_agent_gateway.server:app --reload --port 8080
```

For local testing, insert the agent into the `slack_agents` table (via BotFather's OAuth flow, or a direct SQL insert for self-hosted setups) and set the matching secret env vars in your `.env`:
```
SLACK_AGENT_GATEWAY_EXAMPLEBOT_BOT_TOKEN=xoxb-...
SLACK_AGENT_GATEWAY_EXAMPLEBOT_SIGNING_SECRET=...
```
The env-var key is built from the row's `bot_name` uppercased with dashes replaced by underscores.

For local testing with Slack, use a tunnel service (ngrok, cloudflared) to expose your local server.
