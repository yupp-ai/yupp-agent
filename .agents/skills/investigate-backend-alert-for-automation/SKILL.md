---
name: investigate-backend-alert-for-automation
description: Investigate backend errors from alert messages, write findings to yuppaste, and optionally create a draft PR with a fix. Designed for automated invocation from Slack alert threads.
allowed-tools: mcp__yuppster-mcp-server__search_gcp_logs, mcp__yuppster-mcp-server__create_yuppaste, mcp__yuppster-mcp-server__get_gcp_alert_details, mcp__yuppster-mcp-server__read_yuppaste, mcp__yuppster-mcp-server__get_agent_memory, mcp__yuppster-mcp-server__store_agent_memory, mcp__yuppster-mcp-server__read_slack_thread, mcp__yuppster-mcp-server__search_slack, Bash, Read, Write, Edit, Glob, Grep, Task, Skill
---

# Automated Backend Alert Investigation

This skill is designed for automated invocation when a backend alert is received. It orchestrates the full investigation workflow: analyze the alert, investigate root cause, write findings to yuppaste, and optionally create a draft PR fix.

## Workflow

### Step 0: Read the Alert Thread

If you have a Slack channel ID and thread timestamp, use `read_slack_thread` to fetch the full alert thread. This gives you:
- The original alert message with error details, GCP alert URLs, and timestamps
- Any replies from teammates who may have already investigated or noted context

```
read_slack_thread(channel="<channel_id>", thread_ts="<thread_ts>")
```

Also use `search_slack` to check if the same error has been reported before:
```
search_slack(query="<error_pattern_or_service> in:#alert-backend")
```

If a prior thread already has a resolution or ongoing investigation, note it and avoid duplicate work.

### Step 1: Invoke the Investigation Skill

Use the `/investigate-backend-alert` skill to perform the actual investigation. This skill handles:
- Checking agent memory for known patterns
- Checking for existing open PRs that may already fix the issue
- Checking for merged but undeployed fixes
- Extracting timestamp, service, and error details from the alert
- Searching GCP logs with focused time windows
- Correlating errors across instances and services
- Tracing to code

Follow its full methodology to investigate the alert.

### Step 2: Infer a Title

Based on the alert content and investigation findings, **automatically generate a concise, descriptive title** for the investigation. The title should capture:
- The affected service or component (e.g., "backend", "admin-service", "cron-job-refresh-leaderboard")
- The nature of the error (e.g., "KeyError", "timeout", "connection refused", "OOM")
- Keep it short but informative (e.g., "backend KeyError in chat completion handler", "admin-service GC cleanup failure")

Use this title as the `name` parameter when creating the yuppaste.

### Step 3: Write Summary to Yuppaste

After completing the investigation, compile a comprehensive summary and write it to a yuppaste using the `create_yuppaste` MCP tool.

The summary should include:
- **Title**: The inferred title from Step 2
- **Alert Source**: Link to the original alert if available
- **Incident Summary**: Brief description of what happened
- **Timeline**: Chronological chain of events from the logs
- **Root Cause Analysis**: What caused the error and why
- **Impact Assessment**: Scope of the issue (how many users/requests affected, duration)
- **Evidence**: Key log entries, stack traces, or database state that support the analysis
- **Recommendation**: Whether this needs a code fix, config change, or is transient

```
create_yuppaste(
    content="<formatted investigation summary>",
    name="<inferred title from Step 2>"
)
```

### Step 4: Create a Draft PR (If Code Fix Needed)

If the investigation reveals a bug or issue that should be fixed through code changes:

1. **Create a fix** on a new branch using Graphite:
   ```bash
   gt create <branch-name> -m "<commit message>" --no-interactive
   ```
2. **Submit as a draft PR** (since this is an automated investigation, always keep it in draft):
   ```bash
   gt submit --publish --no-edit --no-interactive
   ```
3. Include the yuppaste link in the PR description under an `## Evidence` section.
4. Note: Branch names should be prefixed with `claude/` to indicate automated creation.

### Step 5: Post the Response

After the investigation is complete, format your response message with:

1. **The yuppaste link** using the following format:
   - Display text: the go-link (e.g., `http://go/p/<uuid>`)
   - URL: the resolved link (e.g., `https://yupp-soul.vercel.app/yuppastes/<uuid>`)
   - Markdown format: `[http://go/p/<uuid>](https://yupp-soul.vercel.app/yuppastes/<uuid>)`

2. **The PR link** (if a draft PR was created):
   - Include the GitHub PR URL

3. **A brief summary** of the findings (2-3 sentences max in the response itself - the full details are in the yuppaste)

Example response format:
```
Investigation complete for <service> <error type>.

**Root cause**: <1-2 sentence explanation>

**Full analysis**: [http://go/p/<uuid>](https://yupp-soul.vercel.app/yuppastes/<uuid>)

**Draft PR**: https://github.com/yupp-ai/yupp-mind/pull/<number>
```

### Step 6: Store Learnings (If Applicable)

If the investigation uncovered a reusable insight (recurring pattern, non-obvious root cause, debugging shortcut), use the `/agent-memory` skill to store it for future investigations.

## Important Notes

- Always use the `yupp-ai/yupp-mind` repository for any code investigation or PR creation.
- The go-link `http://go/p/<uuid>` resolves to `https://yupp-soul.vercel.app/yuppastes/<uuid>`. Always use the resolved URL as the hyperlink target and the go-link as display text.
- Draft PRs should have branch names prefixed with `claude/` so they are not accidentally marked as ready for review.
- Keep the response message concise - all detailed evidence belongs in the yuppaste.
