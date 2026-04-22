---
name: investigate-backend-alert
description: Investigate backend errors from #alert_backend Slack channel using GCP logs. Use when debugging production errors, analyzing error patterns, or tracing issues from alert notifications.
allowed-tools: mcp__agcouch-mcp-server__search_gcp_logs, mcp__agcouch-mcp-server__add_artifact, mcp__agcouch-mcp-server__get_gcp_alert_details, mcp__agcouch-mcp-server__get_agent_memory, mcp__agcouch-mcp-server__store_agent_memory, Bash, Read, Write, Skill
---

# Backend Alert Investigation Guide

Use this skill when investigating errors from the #alert_backend Slack channel using the `search_gcp_logs` MCP tool.

## Important: Timestamp-Based Queries

**Prefer using specific timestamp ranges** over `hours_back` to reduce impact on our logging API quota:

```
# Preferred: Use timestamp range when you know the time
search_gcp_logs(
    query='severity="ERROR" AND jsonPayload.message="<error message>" AND timestamp>="2026-01-25T17:40:00Z" AND timestamp<="2026-01-25T17:45:00Z"',
    max_results=100
)

# Fallback: Use hours_back only when time is unknown
search_gcp_logs(
    query='severity="ERROR" AND jsonPayload.message="<error message>"',
    hours_back=2,
    max_results=100
)
```

Since Slack alerts include timestamps, always extract and use them in your queries.

## Agent Memory

Before investigating, use the `/agent-memory` skill to check for relevant learnings from past investigations:

1. List available topics with `get_agent_memory()`
2. Read `oncall-learnings` and any other relevant topics (e.g., `service-gotchas` if the alert is service-specific)

If the memory contains a known pattern matching this alert, use that context to accelerate the investigation.

After investigating, store any reusable insights you discovered.

## Check for Existing PRs First

**Before starting a full investigation**, check if there's already a pending PR that addresses the same issue. This avoids duplicating work when another agent or developer has already investigated and created a fix.

### Search for Relevant PRs

Use `gh` to search for open PRs that might address the same error:

```bash
# Search by error message keywords
gh pr list --state open --search "<error_keyword> in:title,body"

# Search for recent automated investigation PRs (by branch name pattern)
gh pr list --state open --search "head:claude/slack-investigate-" --limit 20

# Search for all recent PRs (last 3 days) - useful when there aren't many open PRs
# macOS: date -v-3d +%Y-%m-%d | Linux: date -d '3 days ago' +%Y-%m-%d
gh pr list --state open --search "created:>=$(date -v-3d +%Y-%m-%d 2>/dev/null || date -d '3 days ago' +%Y-%m-%d)" --limit 20

# Search by service name if error is service-specific
gh pr list --state open --search "<service_name> fix in:title"
```

### What to Look For

1. **PR titles containing error keywords** - e.g., "Fix KeyError in cache warmup"
2. **PRs created after the alert time** - Someone may have already investigated
3. **PRs from `claude/slack-investigate-*` branches** - These are automated investigation PRs
4. **PR descriptions mentioning the same error message or stack trace**

### If a Relevant PR Exists

If you find a PR that likely addresses the same issue:
1. **Don't duplicate the investigation** - The work is already done
2. **Verify the PR addresses the issue** by reading its description and changes
3. **Report to the user**: "Found existing PR #123 that appears to address this issue: <link>"
4. **Optionally review the PR** for completeness if the user wants

### Example Check

```bash
# For an alert about "gemini-3-pro-online returned error"
gh pr list --state open --search "gemini-3-pro-online OR gemini error" --limit 10
```

If no relevant PRs are found, proceed to check for undeployed fixes.

## Check for Undeployed Fixes

Check for merged but undeployed pull requests that might already fix the issue. This requires comparing the deployed revision against recent commits on `main`.

### How to Check

1. **Get the deployed revision** - Use `build_git_sha` from the error log to identify what code is running. If not found in the error log, search logs from the same `revision_name` within a few minutes of the error.

2. **Find undeployed commits** - Compare the deployed SHA against `main`:
   ```bash
   # List commits on main that are not yet deployed
   git log --oneline <build_git_sha>..origin/main
   ```

3. **Search for relevant fixes** - Look through commit messages and associated PRs for keywords from the alert:
   ```bash
   # Search commit messages
   git log --oneline <build_git_sha>..origin/main | grep -i "<error_keyword>"

   # Get PR numbers from commits and check their descriptions
   gh pr list --state merged --search "<error_keyword>" --limit 10
   ```

### If a Relevant Fix is Already Merged

If you find a merged PR that addresses the issue:
1. **Note this in your findings** - The fix exists but hasn't been deployed yet
2. **Don't create a new PR** - The work is already done
3. **Report to the user** that the fix is pending deployment

If no undeployed fixes are found, proceed with the full investigation below.

## Investigation Methodology

### Step 1: Identify Time, Location, and Error

Your first goal is to identify three key pieces of information:
- **When**: The timestamp when the error occurred
- **Where**: The service/job and location (revision, instance)
- **What**: The error message or condition

#### Option A: Use GCP Alert URL (Preferred)

If the Slack message contains a **GCP alert URL** (e.g., `https://console.cloud.google.com/monitoring/alerting/alerts/0.o3gwwfx7rf2c?project=...`), use the `get_gcp_alert_details` tool:

```
get_gcp_alert_details(alert_id_or_url="<full_url_or_alert_id>")
```

The response contains structured metadata. Extract the key fields:

**For Cloud Run Services:**
```json
{
  "name": "projects/yupp-llms/alerts/0.o3n8b2k6q1iv",
  "state": "OPEN",
  "openTime": "2026-01-29T06:38:50Z",                    // WHEN
  "resource": {
    "type": "cloud_run_revision",
    "labels": {
      "revision_name": "admin-service-production-00035-tp2",  // WHERE (specific)
      "service_name": "admin-service-production",             // WHERE (service)
      "location": "us-east4"
    }
  },
  "log": {
    "extractedLabels": {
      "AlertSummary": "The garbage collector is trying to clean up..."  // WHAT
    }
  },
  "policy": { "displayName": "ADMIN SERVICE error alerts" }
}
```

**For Cloud Run Jobs (cron jobs):**
```json
{
  "name": "projects/yupp-llms/alerts/0.o3n6d1y7by1v",
  "state": "OPEN",
  "openTime": "2026-01-29T05:13:40Z",                    // WHEN
  "resource": {
    "type": "cloud_run_job",
    "labels": {
      "job_name": "cron-job-refresh-leaderboard-data-production",  // WHERE
      "location": "us-east4"
    }
  },
  "policy": { "displayName": "CRON JOB error alerts" }  // WHAT (check logs for details)
}
```

#### Option B: Extract from Slack Message (Fallback)

If there is no GCP alert URL, extract details manually from the Slack message:
- **Error message/summary** (e.g., `KeyError: '__import__'`)
- **Service name** (e.g., `backend`, `backend-leaderboard`)
- **Revision name** (e.g., `backend-00849-j5b`)
- **Timestamp** (from the Slack URL - see below)

##### Extracting Timestamp from Slack URL

Slack message URLs contain Unix timestamps that you MUST convert to ISO format:

**URL format**: `https://yuppai.slack.com/archives/<channel>/p<msg_ts>?thread_ts=<thread_ts>&cid=<channel>`

**Example**: `thread_ts=1769538386.295209`
- Take the integer part before the dot: `1769538386`
- Convert to ISO: `2026-01-27T19:06:26Z`

**Quick conversion**:
```bash
python3 -c "from datetime import datetime, timezone; print(datetime.fromtimestamp(1769538386, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))"
```

**IMPORTANT**: Always use `thread_ts` (original alert time), not the reply timestamp.

### Step 2: Investigate at the Site of the Problem

Search logs at the **same location** within a **+/- 1 hour window** around the error time. This focused search helps you understand what was happening on that specific service/instance.

**For Cloud Run Services:**
```
search_gcp_logs(
    query='resource.labels.service_name="<service_name>" AND timestamp>="<alert_time - 1hr>" AND timestamp<="<alert_time + 1hr>"',
    max_results=100
)
```

**For Cloud Run Jobs:**
```
search_gcp_logs(
    query='resource.labels.job_name="<job_name>" AND timestamp>="<alert_time - 1hr>" AND timestamp<="<alert_time + 1hr>"',
    max_results=100
)
```

**Narrow down with revision/instance** if you have it:
```
search_gcp_logs(
    query='resource.labels.revision_name="<revision_name>" AND timestamp>="<alert_time - 5min>" AND timestamp<="<alert_time + 5min>"',
    max_results=100
)
```

**Search for the specific error** to understand frequency:
```
search_gcp_logs(
    query='resource.labels.service_name="<service>" severity="ERROR" textPayload:"<error_pattern>" AND timestamp>="<start>" AND timestamp<="<end>"',
    max_results=100
)
```

At this point, look for:
- The full stack trace and error details
- What operation was being performed (check `module` field, HTTP request logs)
- Any preceding warnings or errors
- The `instanceId` and `trace` ID for correlation

### Step 3: Expand Search if Not Clear

If Step 2 doesn't reveal the root cause, expand your search along different axes:

#### 3a. Same Location, Across Time

Check if this error has happened before on the same service:

```
search_gcp_logs(
    query='resource.labels.service_name="<service>" severity="ERROR" textPayload:"<error_pattern>"',
    hours_back=24,
    max_results=200
)
```

This helps identify:
- Is this a recurring issue or a one-time event?
- Did something change recently (deployment, config)?
- Are there patterns (specific times, load-related)?

#### 3b. Same Time, Across the System

Check if other services had issues around the same time:

```
search_gcp_logs(
    query='severity="ERROR" AND timestamp>="<alert_time - 5min>" AND timestamp<="<alert_time + 5min>"',
    max_results=200
)
```

This helps identify:
- Was there a system-wide issue (database, network, external API)?
- Did a downstream service failure cascade upstream?
- Were multiple services affected simultaneously?

### Step 4: Analyze the Logs

When the results are large, save to file and use jq/grep to analyze:

**Chronological view:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message | tostring | .[0:300])"' | sort
```

**Filter by severity:**
```bash
cat <results_file> | jq -r '.results[] | select(.severity == "ERROR")'
```

**Search for specific patterns:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message)"' | grep -i "<pattern>"
```

**Get logs before an error:**
```bash
cat <results_file> | jq -r '.results[] | "\(.timestamp) \(.message | tostring | .[0:300])"' | sort | grep -B20 "<error_pattern>"
```

## Common Query Patterns

### Filter by service
```
resource.labels.service_name="backend"
```

### Filter by revision
```
resource.labels.revision_name="backend-00849-j5b"
```

### Filter by severity
```
severity="ERROR"
```

### Filter by time range
```
timestamp>="2026-01-25T17:40:00Z" AND timestamp<="2026-01-25T17:41:00Z"
```

### Search text payload
```
textPayload:"error message"
```

### Combine filters
```
resource.labels.service_name="backend" severity="ERROR" textPayload:"KeyError"
```

## Key Log Fields

Each log entry contains:
- `timestamp` - When the log was recorded
- `severity` - Log level (INFO, WARNING, ERROR, etc.)
- `message` - The log message (can be string or JSON object)
- `trace` - Request trace ID (for correlating across services)
- `labels.instanceId` - Container instance ID (for correlating logs on same instance)
- `labels.user_id` - User ID if available
- `resource.labels.service_name` - Service name
- `resource.labels.revision_name` - Deployment revision

## Investigation Tips

1. **Same instance correlation**: Use `instanceId` to see all activity on the same container instance around the error time. This helps identify what operations were happening before the error.

2. **Trace correlation**: If the error has a `trace` ID, search for all logs with that trace to see the full request flow.

3. **Time windowing**: Start with a narrow time window (2-3 seconds) around the error, then expand if needed.

4. **Module identification**: Look at the `module` field in log messages to identify which code path was executing.

5. **HTTP request correlation**: Look for HTTP request logs (format: `"POST /api/v1/... HTTP/1.1" 200 OK`) to identify which API endpoint was being called.

## Tracing to Code

Once you identify:
- The operation happening (from log messages)
- The module involved (from `module` field)
- Any HTTP clients being used (from `_client` module logs)

Search the codebase for:
1. The specific module name
2. The HTTP endpoints being called
3. Any libraries that might be involved (e.g., aiohttp, httpx, gcloud-aio)

## Preserving Evidence with Artifact

When investigating issues that may lead to a PR fix, **preserve critical evidence** using the `add_artifact` MCP tool (set `artifact_type="TEXT"`). It's best to create a separate artifact for each distinct piece of evidence (e.g., one for logs, another for database results). This creates shareable links that can be included in PR descriptions.

### What to Preserve

- **Error logs**: Stack traces, error messages, and surrounding context
- **Database query results**: Relevant rows that demonstrate the issue
- **Timeline of events**: Chronological log entries showing the chain of events
- **Configuration state**: Redis values, feature flags, or settings at the time of the error

### How to Use

```
add_artifact(
    artifact_type="TEXT",
    title="Investigation: <brief description>",
    content="<formatted logs or data>",
)
```

The tool returns a go-link (e.g., `https://artifacts.agcouch.com/artifacts/<uuid>`) that you can include in PR descriptions. Note that the content size is limited to 10MB.

### PR Description Format

When creating a PR to fix an investigated issue, include:

```markdown
## Investigation Evidence

- Error logs: http://go/p/<uuid1>
- Database state: http://go/p/<uuid2>
- Timeline analysis: http://go/p/<uuid3>
```

This provides reviewers with full context without cluttering the PR description.

