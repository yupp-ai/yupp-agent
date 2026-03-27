---
name: add-slack-agent
description: Add a new Slack agent to the Slack Agent Gateway. Guides through Slack app creation, collects credentials, creates GCP secrets, and updates config files.
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, AskUserQuestion
---

# Add Slack Agent

This skill guides you through adding a new AI agent as a Slack bot to the Slack Agent Gateway.

## Overview

Adding a new agent requires:
1. Creating a Slack app
2. Configuring OAuth permissions
3. Collecting credentials (App ID, Bot Token, Signing Secret)
4. Creating GCP secrets for staging/production
5. Updating `data/secret-env-var-map.yml` to map secrets to env vars
6. Updating `data/dynamic_app_settings_base.yml`
7. (Optional) Adding to local .env for testing
8. Configuring Interactivity & Event Subscriptions (after deploying to staging)
9. Inviting the bot to channels

---

## Step 1: Collect Agent Information

Use `AskUserQuestion` to collect two names:

1. **Slack name** (`{slack_name}`) - lowercase, used in secrets, env vars, and config (e.g., `minqowski`, `giladovski`)
2. **Display name** (`{display_name}`) - Human-readable, shown in Slack UI (e.g., `Minqowski`, `Giladovski`)

```
Question: "What is the Slack bot name? (lowercase, e.g., 'minqowski')"
Header: "Slack name"
Options:
  - label: "Custom name"
    description: "I'll provide a lowercase name for secrets/config"
```

```
Question: "What is the display name? (shown in Slack, e.g., 'Minqowski')"
Header: "Display name"
Options:
  - label: "Custom name"
    description: "I'll provide the human-readable display name"
```

Then ask:
```
Question: "What AHS agent should this Slack bot connect to?"
Header: "AHS agent"
Options:
  - label: "bookkeeper"
    description: "Memory retrieval agent"
  - label: "data-scientist"
    description: "Data analysis agent"
  - label: "sre"
    description: "SRE/oncall agent"
  - label: "eng-terse"
    description: "Engineering agent (terse responses)"
  - label: "code-reviewer"
    description: "Code review agent"
```

---

## Step 2: Create Slack App

Prompt the user:

> Please visit https://api.slack.com/apps and click **Create New App**.
>
> Choose **"From scratch"** and enter:
> - **App Name**: `{display_name}` (this is how users will @mention the agent)
> - **Workspace**: Select your Slack workspace
>
> Reply when done.

---

## Step 3: Get App ID and Signing Secret

Prompt the user:

> In your new Slack app, go to **Basic Information**.
>
> Please provide:
> 1. **App ID** (found at the top, e.g., `A0AH4S96RQE`)
> 2. **Signing Secret** (under "App Credentials", click "Show")

Use `AskUserQuestion`:
```
Question: "What is the App ID? (e.g., A0AH4S96RQE)"
Header: "App ID"
```

```
Question: "What is the Signing Secret?"
Header: "Secret"
```

---

## Step 4: Configure OAuth & Permissions

Prompt the user:

> Go to **OAuth & Permissions** in the left sidebar.
>
> Under **Scopes → Bot Token Scopes**, add these scopes:
> - `app_mentions:read` - View messages that directly mention the bot
> - `channels:read` - Read channel info (required for channel allowlist filtering)
> - `channels:history` - View messages and content in public channels the bot is added to
> - `chat:write` - Send messages as the bot (also enables posting/updating interactive components like buttons)
> - `groups:read` - Read private channel info (required for channel allowlist filtering)
> - `groups:history` - View messages and content in private channels the bot is added to
> - `incoming-webhook` - Post messages to specific channels in Slack
> - `reactions:read` - View emoji reactions and their associated content
> - `reactions:write` - Add and edit emoji reactions
>
> **Note**: No additional scopes are required for receiving button clicks. Button click events are delivered via the Interactivity URL (configured in Step 10), not via Event Subscriptions.
>
> Then scroll up and click **Install to Workspace** → **Allow**.
>
> Reply when done.

---

## Step 5: Get Bot Token

Prompt the user:

> After installing, you should see **Bot User OAuth Token** at the top of OAuth & Permissions.
>
> Copy the token (starts with `xoxb-`).

Use `AskUserQuestion`:
```
Question: "What is the Bot User OAuth Token? (starts with xoxb-)"
Header: "Bot Token"
```

---

## Step 6: Create GCP Secrets

Create secrets for both staging and production:

```bash
# Staging
printf '%s' "{app_id}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-app-id-staging --data-file=- --project=yupp-llms
printf '%s' "{bot_token}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-bot-token-staging --data-file=- --project=yupp-llms
printf '%s' "{signing_secret}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-signing-secret-staging --data-file=- --project=yupp-llms

# Production
printf '%s' "{app_id}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-app-id-production --data-file=- --project=yupp-llms
printf '%s' "{bot_token}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-bot-token-production --data-file=- --project=yupp-llms
printf '%s' "{signing_secret}" | gcloud secrets create ym-slack-agent-gateway-{slack_name}-signing-secret-production --data-file=- --project=yupp-llms
```

Verify secrets were created:
```bash
gcloud secrets list --filter="name:{slack_name}" --format="table(name)" --project=yupp-llms
```

---

## Step 7: Update Secret-Env-Var Mapping

Edit `data/secret-env-var-map.yml` to map the GCP secrets to environment variables for the `slack-agent-gateway` service.

Add these entries (following the pattern of existing agents like giladovski, axandwich):

```yaml
- name: SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_APP_ID
  services:
  - slack-agent-gateway
  secret_names:
    staging: ym-slack-agent-gateway-{slack_name}-app-id-staging
    production: ym-slack-agent-gateway-{slack_name}-app-id-production
  version: latest
- name: SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_BOT_TOKEN
  services:
  - slack-agent-gateway
  secret_names:
    staging: ym-slack-agent-gateway-{slack_name}-bot-token-staging
    production: ym-slack-agent-gateway-{slack_name}-bot-token-production
  version: latest
- name: SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_SIGNING_SECRET
  services:
  - slack-agent-gateway
  secret_names:
    staging: ym-slack-agent-gateway-{slack_name}-signing-secret-staging
    production: ym-slack-agent-gateway-{slack_name}-signing-secret-production
  version: latest
```

**Important**: Without this mapping, the slack-agent-gateway service won't have access to the secrets in staging/production deployments.

---

## Step 8: Update Dynamic App Settings

Edit `data/dynamic_app_settings_base.yml` and add the new agent to `slack_agent_gateway_settings.agents`:

```yaml
- name: slack_agent_gateway_settings
  type: SlackAgentGatewaySettings
  value:
    agents:
      # ... existing agents ...
      - name: {slack_name}
        display_name: {display_name}
        agent_name: {ahs_agent_name}
```

---

## Step 9: (Optional) Add to Local .env

For local testing, add to `.env`:

```
SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_APP_ID={app_id}
SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_BOT_TOKEN={bot_token}
SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_SIGNING_SECRET={signing_secret}
SLACK_AGENT_GATEWAY_{SLACK_NAME_UPPER}_DISPLAY_NAME={display_name}
```

Also update `SLACK_AGENT_GATEWAY_AGENTS` to include the new agent:
```
SLACK_AGENT_GATEWAY_AGENTS={slack_name},...existing_agents...
```

---

## Step 10: Configure Interactivity & Event Subscriptions

**Note**: This step requires the slack-agent-gateway to be deployed with the new agent's secrets. Deploy to staging first using `/deploy-to-staging slack-agent-gateway` before configuring Event Subscriptions, otherwise the URL verification will fail.

Prompt the user:

> Go back to your Slack app at https://api.slack.com/apps/{app_id}
>
> ### Interactivity & Shortcuts
>
> Navigate to **Interactivity & Shortcuts** in the left sidebar:
>
> 1. Toggle **Interactivity** to **ON**
> 2. Set **Request URL** to:
>    - **Staging**: `https://slack-agent-gateway-staging.yupp.ai/api/v1/slack/interactions`
>    - **Production**: `https://slack-agent-gateway-production.yupp.ai/api/v1/slack/interactions`
> 3. Click **Save Changes**
>
> This enables the bot to receive button clicks and other interactive component events (e.g., survey feedback buttons).
>
> ### Event Subscriptions
>
> Navigate to **Event Subscriptions** in the left sidebar:
>
> 1. Toggle **Enable Events** to **ON**
> 2. Set **Request URL** to:
>    - **Staging**: `https://slack-agent-gateway-staging.yupp.ai/api/v1/slack/events`
>    - **Production**: `https://slack-agent-gateway-production.yupp.ai/api/v1/slack/events`
>    (Wait for the green "Verified" checkmark)
> 3. Under **Subscribe to bot events**, click **Add Bot User Event** and add:
>    - `app_mention` - Subscribe to message events that mention the bot (requires `app_mentions:read`)
>    - `reaction_added` - A member has added an emoji reaction to an item (requires `reactions:read`)
> 4. Click **Save Changes**
>
> Reply when done.

---

## Step 11: Invite Bot to Channels

Prompt the user:

> In Slack, invite the bot to the channels where it should be able to respond:
>
> 1. Go to a channel where you want the bot to work
> 2. Type `/invite @{display_name}` or click the channel settings → Integrations → Add apps
> 3. Repeat for each channel where the bot should be active
>
> Reply when done.

---

## Step 12: Commit and Submit PR

Stage and commit the changes:

```bash
git add data/dynamic_app_settings_base.yml data/secret-env-var-map.yml
gt create {initials}/add-{slack_name}-agent -m "Add {display_name} agent to Slack Agent Gateway" --no-interactive
gt submit --draft --no-edit --no-interactive
```

---

## Verification

After the PR is merged and settings sync to Redis (~60 seconds):

1. In Slack, go to a channel where the bot is invited
2. Type `@{display_name} hello`
3. The bot should respond (or show "Thinking..." if AHS is processing)

Check GCP logs if issues:
```
resource.labels.service_name="slack-agent-gateway-staging"
jsonPayload.message="Loaded agent configuration"
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "URL didn't respond with challenge" | Secrets not created or agent not in dynamic_app_settings |
| "Invalid Slack signature" | Signing secret mismatch - recreate the GCP secret |
| Bot not responding | Check AHS agent exists and is configured correctly |
| 401 Unauthorized | Bot token invalid or expired - regenerate in Slack |
