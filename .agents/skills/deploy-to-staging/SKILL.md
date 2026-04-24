---
name: deploy-to-staging
description: Deploy services to staging environment using GitHub Actions. Can deploy specific services or all services from a branch.
---

# Deploy to Staging

Deploy yupp-agent services to the staging environment using the "Build and Deploy v2 - step 1" GitHub Actions workflow.

## Usage

```
/deploy-to-staging                     # Deploy all services from current branch
/deploy-to-staging streamlit           # Deploy only Streamlit (and other internal servers)
/deploy-to-staging backend             # Deploy only backend
/deploy-to-staging <service>           # Deploy specific service(s)
/deploy-to-staging streamlit-only      # Deploy ONLY Streamlit server
/deploy-to-staging mcp-only            # Deploy ONLY MCP server
/deploy-to-staging slack-agent-gateway # Deploy ONLY Slack Agent Gateway
```

## Available Services

The workflow supports deploying the following services:

| Service | Description |
|---------|-------------|
| `backend` | Main backend service |
| `leaderboard` | Leaderboard Backend (GCE) |
| `webhooks` | Webhooks Service |
| `risk` | Risk Service |
| `payments` | Partner Payments Server |
| `internal` / `streamlit` | All internal servers (Admin, Discord, Streamlit, Slack Interaction, MCP, Slack Agent Gateway) |
| `slack-agent-gateway` | Slack Agent Gateway only (uses Deploy Internal Servers workflow) |
| `mcp-only` | MCP Server only (uses Deploy Internal Servers workflow) |

## Workflow Details

**Workflow file**: `.github/workflows/deploy-v2.yml`
**Workflow name**: `Build and Deploy v2 - step 1 (Build+Deploy to staging)`

### Workflow Inputs

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `deployment_type` | choice | `normal staging deployment` | Type of deployment |
| `deploy_backend` | boolean | `true` | Deploy Backend to staging |
| `deploy_leaderboard_backend_gce` | boolean | `true` | Deploy Leaderboard Backend (GCE) |
| `deploy_webhooks_service` | boolean | `true` | Deploy Webhooks Service |
| `deploy_risk_service` | boolean | `true` | Deploy Risk Service |
| `deploy_partner_payments_server` | boolean | `true` | Deploy Partner Payments Server |
| `deploy_internal_servers` | boolean | `true` | Deploy Admin, Discord, Streamlit, Slack, MCP |

## Steps

### 1. Determine the branch to deploy

Use the current branch or a specified branch:
```bash
# Get current branch
git branch --show-current
```

### 2. Verify CI Status

Before proceeding, verify that all CI checks have passed for the branch:
```bash
gh pr checks <branch-name>
```

If checks have not passed, inform the user and do not proceed with the deployment.

### 3. Print deployment summary and trigger

Print a human-readable summary of the deployment, then immediately trigger the workflow. The CLI permission system will handle user confirmation.

Example output:
```
Deploying to staging:

- **Branch**: lg/my-feature-branch
- **Services to deploy**:
  - Backend: No
  - Leaderboard Backend: No
  - Webhooks Service: No
  - Risk Service: No
  - Partner Payments: No
  - Internal Servers (Streamlit, Admin, Discord, Slack, MCP): Yes
```

### 4. Trigger the deployment workflow

Use the GitHub CLI to trigger the workflow:

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref <branch-name> \
  -f deployment_type="normal staging deployment" \
  -f deploy_backend=<true|false> \
  -f deploy_leaderboard_backend_gce=<true|false> \
  -f deploy_webhooks_service=<true|false> \
  -f deploy_risk_service=<true|false> \
  -f deploy_partner_payments_server=<true|false> \
  -f deploy_internal_servers=<true|false>
```

### 5. Monitor the deployment

Get the workflow run URL:
```bash
# List recent runs of the workflow
gh run list --workflow="Build and Deploy v2 - step 1 (Build+Deploy to staging)" --limit 1

# Get the run ID from the output and view it
gh run view <RUN_ID> --web
```

Or construct the URL directly:
```
https://github.com/yupp-ai/yupp-agent/actions/runs/<RUN_ID>
```

## Examples

### Deploy only Streamlit/internal servers

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref lg/my-feature-branch \
  -f deployment_type="normal staging deployment" \
  -f deploy_backend=false \
  -f deploy_leaderboard_backend_gce=false \
  -f deploy_webhooks_service=false \
  -f deploy_risk_service=false \
  -f deploy_partner_payments_server=false \
  -f deploy_internal_servers=true
```

### Deploy only backend

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref lg/my-feature-branch \
  -f deployment_type="normal staging deployment" \
  -f deploy_backend=true \
  -f deploy_leaderboard_backend_gce=false \
  -f deploy_webhooks_service=false \
  -f deploy_risk_service=false \
  -f deploy_partner_payments_server=false \
  -f deploy_internal_servers=false
```

### Deploy all services (default)

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref lg/my-feature-branch \
  -f deployment_type="normal staging deployment" \
  -f deploy_backend=true \
  -f deploy_leaderboard_backend_gce=true \
  -f deploy_webhooks_service=true \
  -f deploy_risk_service=true \
  -f deploy_partner_payments_server=true \
  -f deploy_internal_servers=true
```

## Service Mapping

When user specifies a service, map to the appropriate flags:

| User Input | Workflow | Flags |
|------------|----------|-------|
| `streamlit`, `internal`, `admin`, `discord`, `slack`, `mcp` | Build and Deploy v2 | `deploy_internal_servers=true` |
| `backend`, `api` | Build and Deploy v2 | `deploy_backend=true` |
| `leaderboard` | Build and Deploy v2 | `deploy_leaderboard_backend_gce=true` |
| `webhooks`, `webhook` | Build and Deploy v2 | `deploy_webhooks_service=true` |
| `risk` | Build and Deploy v2 | `deploy_risk_service=true` |
| `payments`, `partner` | Build and Deploy v2 | `deploy_partner_payments_server=true` |
| `all` (or no argument) | Build and Deploy v2 | All flags set to `true` |
| `streamlit-only` | Deploy Internal Servers | `deploy_streamlit_server=true` |
| `admin-only` | Deploy Internal Servers | `deploy_admin_service=true` |
| `discord-only` | Deploy Internal Servers | `deploy_discord_service=true` |
| `mcp-only` | Deploy Internal Servers | `deploy_mcp_server=true` |
| `mcp-oauth-only` | Deploy Internal Servers | `deploy_mcp_server_oauth=true` |
| `slack-agent-gateway`, `slack-gateway`, `slack-gateway-only` | Deploy Internal Servers | `deploy_slack_agent_gateway=true` |
| `slack-interaction-only` | Deploy Internal Servers | `deploy_slack_interaction_server=true` |

## Deploying Individual Internal Servers

For more granular control over internal server deployments, use the **"Deploy Internal Servers"** workflow. This allows deploying individual internal services (e.g., just Streamlit) rather than all internal servers together.

**Note on image building**: When using `image_tag=v2-latest` on staging, this workflow **builds a new image** from the specified branch before deploying. Only when using `release-candidate` or `cherry-pick-release-candidate` tags does it skip the build and use an existing image.

**Workflow file**: `.github/workflows/deploy-v2-internal-servers.yml`
**Workflow name**: `Deploy Internal Servers`

### Available Individual Internal Services

| Service | Workflow Flag | Description |
|---------|---------------|-------------|
| `streamlit` | `deploy_streamlit_server` | Streamlit Server |
| `admin` | `deploy_admin_service` | Admin Service |
| `discord` | `deploy_discord_service` | Discord Service |
| `slack-interaction` | `deploy_slack_interaction_server` | Slack Interaction Server |
| `mcp` | `deploy_mcp_server` | MCP Server |
| `mcp-oauth` | `deploy_mcp_server_oauth` | MCP Server OAuth |
| `slack-agent-gateway` | `deploy_slack_agent_gateway` | Slack Agent Gateway |

### Deploy a Single Internal Server

```bash
gh workflow run deploy-v2-internal-servers.yml \
  --ref <branch-name> \
  -f environment=staging \
  -f image_tag=v2-latest \
  -f deploy_streamlit_server=true
```

### Deploy Multiple Internal Servers

```bash
gh workflow run deploy-v2-internal-servers.yml \
  --ref <branch-name> \
  -f environment=staging \
  -f image_tag=v2-latest \
  -f deploy_streamlit_server=true \
  -f deploy_mcp_server=true
```

### Deploy All Internal Servers

```bash
gh workflow run deploy-v2-internal-servers.yml \
  --ref <branch-name> \
  -f environment=staging \
  -f image_tag=v2-latest \
  -f deploy_all=true
```

### When to Use Which Workflow

| Scenario | Workflow to Use |
|----------|-----------------|
| Deploying backend + internal servers together | `Build and Deploy v2 - step 1` |
| Deploying only one internal service (e.g., just Streamlit) | `Deploy Internal Servers` |
| Deploying multiple specific internal services (not all) | `Deploy Internal Servers` |
| Redeploy with existing release-candidate image | `Deploy Internal Servers` with `image_tag=release-candidate` |

## Important Notes

- The workflow builds a new Docker image from the specified branch before deploying
- Deployment to staging is automatic; production requires a separate manual step
- The workflow runs on schedule (9 AM/PM PST daily) deploying from main
- Internal servers include: Admin, Discord, Streamlit, Slack Interaction, MCP Server, MCP OAuth, and Slack Agent Gateway
