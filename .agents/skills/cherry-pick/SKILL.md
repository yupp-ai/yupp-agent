---
name: cherry-pick
description: Cherry-pick a commit onto a production tag for hotfix deployments. Creates a new branch from a tag and cherry-picks the specified commit.
---

# Cherry-Pick for Hotfix

Cherry-pick a commit onto a production tag to create a hotfix branch.

## Usage

```
/cherry-pick <SHA> <BRANCH_NAME>                    # Cherry-pick onto latest-production
/cherry-pick <SHA> <BRANCH_NAME> <TAG>              # Cherry-pick onto specified tag
```

### Examples

```
/cherry-pick 7771754 lg/cp_mcp                      # Cherry-pick commit 7771754 onto latest-production
/cherry-pick abc1234 lg/cp_fix_auth latest-production  # Explicit tag
/cherry-pick def5678 lg/cp_urgent v2.3.1            # Cherry-pick onto specific version tag
```

## Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `SHA` | Yes | - | The commit SHA to cherry-pick (can be short or full) |
| `BRANCH_NAME` | Yes | - | Name for the new branch (e.g., `lg/cp_feature_name`) |
| `TAG` | No | `latest-production` | The tag to base the branch on |

## Steps

### 1. Parse arguments

Extract the SHA, branch name, and optional tag from the arguments:
- First argument: SHA (required)
- Second argument: Branch name (required)
- Third argument: Tag (optional, defaults to `latest-production`)

### 2. Fetch latest tags

```bash
git fetch --tags --force
```

### 3. Checkout the base tag

```bash
git checkout <TAG>
```

### 4. Delete existing branch if it exists (with confirmation)

If the branch already exists locally, ask user for confirmation before deleting:

```bash
git branch -D <BRANCH_NAME>
```

### 5. Create new branch

```bash
git checkout -b <BRANCH_NAME>
```

### 6. Cherry-pick the commit

```bash
git cherry-pick <SHA>
```

If there are conflicts:
1. Show the conflicting files
2. Help resolve conflicts if requested
3. Continue with `git cherry-pick --continue`

### 7. Verify the cherry-pick

Show the user:
- `git log --oneline -3` to verify the commit is applied
- `git diff HEAD~1 --stat` to show what changed

### 8. Push the branch

After user confirms everything looks correct:

```bash
git push origin <BRANCH_NAME>
```

If the branch already exists on remote, ask user if they want to force push:

```bash
git push --force-with-lease origin <BRANCH_NAME>
```

## Post-Cherry-Pick

After pushing the branch, use `AskUserQuestion` to ask what deployment action to take:

### Question: Deployment Action

**Options:**
1. **Create cherry-pick release candidate** - Build image and deploy to staging (can then promote to production)
2. **Create cherry-pick production cron image** - Build image for next production cron job
3. **Do nothing** - Just push the branch, no deployment

### If user selects "Create cherry-pick release candidate"

Ask a follow-up question about which services to deploy:

**Options:**
1. **Deploy all services** - Backend, Leaderboard, Webhooks, Risk, Payments, Internal Servers
2. **Deploy specific services** - Let user select which services
3. **Build only (no deploy)** - Just create the release candidate image

If "Deploy specific services" is selected, ask which services with multiSelect:
- Backend
- Leaderboard Backend (GCE)
- Webhooks Service
- Risk Service
- Partner Payments Server
- Internal Servers (Admin, Discord, Streamlit, Slack, MCP)

Then trigger the workflow:

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref <BRANCH_NAME> \
  -f deployment_type="create cherry-pick release candidate" \
  -f deploy_backend=<true|false> \
  -f deploy_leaderboard_backend_gce=<true|false> \
  -f deploy_webhooks_service=<true|false> \
  -f deploy_risk_service=<true|false> \
  -f deploy_partner_payments_server=<true|false> \
  -f deploy_internal_servers=<true|false>
```

### If user selects "Create cherry-pick production cron image"

Trigger the workflow with the cron image option:

```bash
gh workflow run "Build and Deploy v2 - step 1 (Build+Deploy to staging)" \
  --ref <BRANCH_NAME> \
  -f deployment_type="create cherry-pick production cron image (Will apply to next prod cron job !!!)" \
  -f deploy_backend=false \
  -f deploy_leaderboard_backend_gce=false \
  -f deploy_webhooks_service=false \
  -f deploy_risk_service=false \
  -f deploy_partner_payments_server=false \
  -f deploy_internal_servers=false
```

### After triggering workflow

Get the workflow run URL and show it to the user:

```bash
sleep 3 && gh run list --workflow="Build and Deploy v2 - step 1 (Build+Deploy to staging)" --limit 1 --json databaseId,url,status,headBranch
```

### Monitor staging deployment and prompt for production deployment

After triggering the "Create cherry-pick release candidate" workflow:

1. **Store deployment context** for later use:
   - `WORKFLOW_RUN_ID`: The workflow run ID from the previous step
   - `BRANCH_NAME`: The cherry-pick branch name
   - `DEPLOYED_SERVICES`: Which services were deployed (backend, leaderboard_gce, webhooks, risk, partner_payments, internal_servers)
   - `LEADERBOARD_TIER`: The leaderboard tier if leaderboard was deployed (stable or latest)

2. **Monitor the workflow every 5 minutes** until completion:

```bash
gh run view <WORKFLOW_RUN_ID> --json status,conclusion
```

Check the status:
- If `status` is `in_progress` or `queued`: Wait 5 minutes and check again
- If `status` is `completed` and `conclusion` is `success`: Proceed to step 3
- If `status` is `completed` and `conclusion` is `failure`: Inform the user the deployment failed and provide the workflow URL for debugging

3. **When staging deployment succeeds**, use `AskUserQuestion` to ask if user wants to deploy to production:

**Question:** "Staging deployment succeeded! Do you want to deploy the cherry-pick image to production?"

**Options:**
1. **Yes, deploy to production** - Deploy the cherry-pick-release-candidate image to production
2. **No, stop here** - Keep the image on staging only

4. **If user selects "Yes, deploy to production"**, ask which services to deploy:

**Question:** "Which services should be deployed to production?"

**Options:**
1. **Same services as staging** - Deploy the same services that were just deployed to staging
2. **All services** - Deploy all services (Backend, Leaderboard, Webhooks, Risk, Payments, Internal Servers)
3. **Deploy specific services** - Select which services to deploy

If "Deploy specific services" is selected, ask which services with multiSelect:
- Backend
- Leaderboard Backend (GCE)
- Webhooks Service
- Risk Service
- Partner Payments Server
- Internal Servers (Admin, Discord, Streamlit, Slack, MCP)

5. **Trigger production deployment** using the deploy-only workflow:

```bash
gh workflow run "Build and Deploy v2 - step 2 (Deploy only, anywhere)" \
  --ref <BRANCH_NAME> \
  -f environment=production \
  -f image_tag=cherry-pick-release-candidate \
  -f deploy_backend=<true|false based on selection> \
  -f deploy_leaderboard_backend_gce=<true|false based on selection> \
  -f leaderboard_tier=<same tier as staging, e.g., "stable"> \
  -f deploy_webhooks_service=<true|false based on selection> \
  -f deploy_risk_service=<true|false based on selection> \
  -f deploy_partner_payments_server=<true|false based on selection> \
  -f deploy_internal_servers=<true|false based on selection>
```

6. **Get and display the production workflow URL**:

```bash
sleep 3 && gh run list --workflow="Build and Deploy v2 - step 2 (Deploy only, anywhere)" --limit 1 --json databaseId,url,status,headBranch
```

## Error Handling

- If SHA doesn't exist: Show error and suggest checking the PR page for correct SHA
- If tag doesn't exist: List available tags with `git tag -l | tail -20`
- If cherry-pick fails due to conflicts: Guide through conflict resolution
- If push fails: Check if branch exists on remote and offer force push option
