---
name: investigate-agent-sessions
description: Investigate agent harness session performance and failures. Use when debugging why an AHS session failed, hit max turns, was slow, or didn't achieve the desired work. Accepts session IDs, Lit console URLs, or message IDs.
allowed-tools: mcp__yuppster-mcp-server__query_agentdb, mcp__yuppster-mcp-server__query_yuppdb, mcp__yuppster-mcp-server__search_gcp_logs, mcp__yuppster-mcp-server__create_yuppaste, mcp__yuppster-mcp-server__get_agent_memory, mcp__yuppster-mcp-server__store_agent_memory, Bash, Read, Glob, Grep, Skill
---

# Investigate Agent Harness Sessions

Use this skill to investigate why an AHS (Agent Harness Service) session failed, was slow, hit max turns, or otherwise didn't achieve the desired outcome.

## Invoke This Skill When

- User asks to investigate an agent session, debug an AHS failure, or analyze agent performance
- User provides a session ID, a Lit console URL, or a message ID
- User mentions an agent task that failed or was unsatisfactory

## Input Parsing

The user may provide:

1. **Session ID** (UUID): e.g., `8e717e0e-4e66-405b-845b-70fe1ec0f7e7`
2. **Lit console URL**: e.g., `https://lit.yupp.ai/agent_harness_console?session_id=8e717e0e-4e66-405b-845b-70fe1ec0f7e7` — extract the `session_id` query parameter
3. **Message ID** (UUID): Look up the session via `agent_session_messages.agent_session_message_id`

If a message ID is given, resolve the session first:
```sql
SELECT agent_session_id FROM agent_session_messages
WHERE agent_session_message_id = '<message_id>'
```

## Schema Reference

If you need to write custom queries beyond the examples below, read the ORM files first for exact column names and enum values — do not guess:

```
Read("yupp-agent/ypl/db/agent_harness.py")         # all core tables
Read("yupp-agent/ypl/db/agent_memory_index.py")    # memory tables
```

## Agent Memory

Before investigating, use the `/agent-memory` skill to check for relevant learnings from past AHS investigations:
1. List available topics with `get_agent_memory()`
2. Read `ahs-investigation-learnings` and any other relevant topics

After investigating, store any reusable insights you discovered.

---

## Phase 1: Session Overview (run all queries in parallel)

Get session metadata, messages, and the result event in a single pass.

### 1a. Combined Overview Query

Run these three queries in parallel:

```sql
-- Query 1: Session metadata
SELECT s.agent_session_id, s.agent_id, s.status, s.trigger, s.title,
       s.model, s.workspace, s.llm_session_id, s.slack_session_id,
       s.created_at, a.name as agent_name
FROM agent_sessions s
LEFT JOIN agents a ON s.agent_id = a.agent_id
WHERE s.agent_session_id = '<session_id>'
```

```sql
-- Query 2: All messages with basic stats
SELECT agent_session_message_id, turn_number, role, llm_name,
       cost_usd, duration_ms, num_agent_turns,
       LENGTH(content) as content_length,
       jsonb_array_length(raw_events) as event_count,
       created_at
FROM agent_session_messages
WHERE agent_session_id = '<session_id>'
ORDER BY turn_number, created_at
```

```sql
-- Query 3: Result event (cost, outcome, model usage) — THE MOST IMPORTANT QUERY
SELECT
    m.turn_number,
    e->>'subtype' as result_subtype,
    (e->>'num_turns')::int as num_turns,
    (e->>'duration_ms')::int as duration_ms,
    COALESCE(
        (e->>'total_cost_usd')::numeric,
        (e->>'estimated_total_cost_usd')::numeric,
        (e->>'estimated_cost_usd')::numeric,
        m.cost_usd
    ) as total_cost_usd,
    e->'modelUsage' as model_usage,
    e->>'stop_reason' as stop_reason
FROM agent_session_messages m,
     LATERAL jsonb_array_elements(m.raw_events) AS e
WHERE m.agent_session_id = '<session_id>'
  AND m.role = 'AGENT'
  AND e->>'type' = 'result'
ORDER BY m.turn_number
```

### Key Signals from the Result Event

| Field | What it tells you |
|---|---|
| `result_subtype` | `success`, `error_max_turns`, `error_tool_use` — immediate outcome |
| `total_cost_usd` | Normal range: $0.50-$3.00. Above $5 = red flag. Above $10 = severe |
| `num_turns` | Normal: 5-25. At 51 = max turns hit. At 92+ = CRON/special config |
| `modelUsage.*.cacheReadInputTokens` | Growing cache reads = context bloat (>10M is concerning) |
| `modelUsage.*.outputTokens` | >50K output tokens = agent writing a lot of code or being verbose |

### Baseline Benchmarks (from production data)

| Metric | Successful Sessions | Max-Turns Failures |
|---|---|---|
| Avg cost | $1.30 | $6.84 |
| Avg turns | 15.9 | 51.0 |
| Avg duration | 2.8 min | 10.8 min |
| Cost per turn | $0.107 | $0.134 |

Sessions costing >5x the success average ($6.50+) warrant investigation even if they "succeeded."

### 1b. Check for Sub-Sessions and Feedback (parallel)

```sql
-- Sub-sessions
SELECT agent_session_id, agent_id, status, trigger, title, created_at
FROM agent_sessions
WHERE parent_session_id = '<session_id>'
ORDER BY created_at
```

```sql
-- Feedback
SELECT rating, comment, created_at
FROM agent_feedbacks
WHERE agent_session_id = '<session_id>'
ORDER BY created_at
```

---

## Phase 2: Tool Usage Analysis (the fastest diagnostic)

**Start here before reading content or logs.** Tool usage patterns are the single most informative signal and are fast to query.

### 2a. Tool Usage Breakdown

Tool calls exist in two shapes depending on the runner:
- **Standard Claude SDK sessions**: embedded inside `assistant` events as `message.content[]` blocks
- **Raw/Codex runner sessions**: emitted as top-level `tool_use` events

Use a UNION to capture both:

```sql
-- Embedded tool calls (standard Claude SDK)
SELECT content_block->>'name' as tool_name, count(*) as calls
FROM agent_session_messages m,
     LATERAL jsonb_array_elements(m.raw_events) AS evt,
     LATERAL jsonb_array_elements(evt->'message'->'content') AS content_block
WHERE m.agent_session_id = '<session_id>'
  AND m.role = 'AGENT'
  AND evt->>'type' = 'assistant'
  AND content_block->>'type' = 'tool_use'
GROUP BY content_block->>'name'

UNION ALL

-- Top-level tool_use events (raw/codex runner)
SELECT evt->>'name' as tool_name, count(*) as calls
FROM agent_session_messages m,
     LATERAL jsonb_array_elements(m.raw_events) AS evt
WHERE m.agent_session_id = '<session_id>'
  AND m.role = 'AGENT'
  AND evt->>'type' = 'tool_use'
GROUP BY evt->>'name'
```

To get a combined ranking, wrap both in a subquery:

```sql
SELECT tool_name, sum(calls) as total_calls
FROM (
    SELECT content_block->>'name' as tool_name, count(*) as calls
    FROM agent_session_messages m,
         LATERAL jsonb_array_elements(m.raw_events) AS evt,
         LATERAL jsonb_array_elements(evt->'message'->'content') AS content_block
    WHERE m.agent_session_id = '<session_id>'
      AND m.role = 'AGENT'
      AND evt->>'type' = 'assistant'
      AND content_block->>'type' = 'tool_use'
    GROUP BY content_block->>'name'
    UNION ALL
    SELECT evt->>'name' as tool_name, count(*) as calls
    FROM agent_session_messages m,
         LATERAL jsonb_array_elements(m.raw_events) AS evt
    WHERE m.agent_session_id = '<session_id>'
      AND m.role = 'AGENT'
      AND evt->>'type' = 'tool_use'
    GROUP BY evt->>'name'
) combined
GROUP BY tool_name
ORDER BY total_calls DESC
```

### Red Flag Patterns (from real production failures)

| Pattern | Example | Root Cause |
|---|---|---|
| **Edit > 50 calls** | 242 Edits in a logging migration | Agent editing files one-by-one instead of using a batch script |
| **Read > 2x the number of files** | 115 Reads for 17 files | Agent re-reading files after each edit for verification |
| **search_slack > 20 calls** | 71 search_slack calls in a CRON | Agent trying to read email-forwarded Slack messages (returns empty), brute-forcing with keyword searches |
| **Bash > 50 calls** | 64 Bash calls for PR handling | Agent running git operations one at a time |
| **Edit:Read ratio > 2:1** | | Agent not reading enough before editing, likely making errors |
| **ToolSearch > 3 calls** | | Agent repeatedly loading the same deferred tools |
| **Agent (subagent) calls** | | Delegation that may compound turn usage |

### 2b. Event Type Distribution

```sql
SELECT e->>'type' as event_type, count(*) as cnt
FROM agent_session_messages m,
     LATERAL jsonb_array_elements(m.raw_events) AS e
WHERE m.agent_session_id = '<session_id>'
  AND m.role = 'AGENT'
GROUP BY e->>'type'
ORDER BY cnt DESC
```

Expected healthy distribution: ~50% assistant, ~45% user (tool results), few system/result events.

### 2c. Raw Events Structure Reference

Events in `raw_events` are NOT the simple `tool_use`/`tool_result` types you might expect. The actual structure is:

- **`system`** events: Have `subtype` (e.g., `init`, `hook_started`, `hook_response`). The `init` event contains `model`, `tools`, `permissionMode`, `session_id`.
- **`assistant`** events: Contain `message.content[]` array with blocks of type `thinking`, `text`, or `tool_use`. Tool calls are **inside** assistant events, not separate events.
- **`user`** events: Tool results returned to the agent (text content).
- **`result`** events: Final summary with `subtype`, `num_turns`, `total_cost_usd`, `duration_ms`, `modelUsage`.

**Important:** Tool calls exist in two shapes depending on the runner:
- **Standard Claude SDK**: look at `assistant` events → `message.content[]` → blocks where `type = 'tool_use'`
- **Raw/Codex runner**: emits top-level `tool_use` events directly in `raw_events`

Always query both shapes (see Phase 2a UNION query) to avoid undercounting tool usage.

---

## Phase 3: Content Analysis (targeted, not exhaustive)

Only read content after tool analysis gives you a hypothesis. Focus on specific turns.

### 3a. User Request + Agent Summary

```sql
SELECT turn_number, role, LEFT(content, 2000) as content_preview,
       duration_ms, num_agent_turns
FROM agent_session_messages
WHERE agent_session_id = '<session_id>'
ORDER BY turn_number, created_at
```

**What to look for:**
- **User message**: Was the task clearly scoped? Too broad?
- **Agent final content**: Does it claim success? What was actually accomplished?
- **Multi-turn sessions**: Did the user have to say "Please continue" (retry after max turns)?
- **Content length**: Very short agent content (<500 chars) after many turns = agent ran out of turns before summarizing

### 3b. Efficiency Indicators in Content

- "Let me read all the target files" → about to do N sequential reads
- "Let me check..." / "Let me verify..." → defensive re-verification wasting turns
- "Let me try a more targeted search" → previous approach failed, pivoting
- "No matches found" / empty results → wasted tool call
- Agent delegating to `Agent` subagent → check if the subagent also hit limits

---

## Phase 4: GCP Log Analysis (use sparingly — quota-sensitive)

**Only use GCP logs when DB analysis is insufficient.** Common cases: understanding timing gaps, infrastructure errors, SAG relay issues.

### Important: AHS Infrastructure

- **AHS runs on a GCE VM**, not Cloud Run. Logs are under `resource.type="gce_instance"` with label `service_name="agent-harness-service"`.
- **SAG (Slack Agent Gateway)** runs on Cloud Run as `slack-agent-gateway-production`.
- **Streamlit server** (Lit console) runs on Cloud Run as `streamlit-server-production`.

### 4a. Efficient Log Queries

**Always use tight timestamp ranges.** Compute from `created_at` + `duration_ms`:

```
search_gcp_logs(
    query='"<session_id>" AND labels.service_name="agent-harness-service" AND timestamp>="<start>" AND timestamp<="<end>"',
    max_results=50
)
```

**For timing analysis, focus on milestones only:**
```
search_gcp_logs(
    query='"<session_id>" ("Agent task completed" OR "Claude CLI process exited" OR "Agent config loaded" OR "Added worktree dirs") AND timestamp>="<start>" AND timestamp<="<end>"',
    max_results=20
)
```

**For errors only:**
```
search_gcp_logs(
    query='"<session_id>" severity>="WARNING" AND timestamp>="<start>" AND timestamp<="<end>"',
    max_results=50
)
```

### Key Log Patterns

| Log Message | Meaning |
|---|---|
| `session {sid} [AGENT] [assistant]: tool_call: {tool}` | Agent called a tool |
| `session {sid} [AGENT] [user]: {text}` | Tool result returned |
| `session {sid} [AGENT] [result]: est_cost=$X turns=N duration=Xms` | Session completed |
| `Agent task completed` + `result_subtype` | Final outcome |
| `Eager persist UPDATE session {sid}` | Incremental DB save (every ~5 tool calls) |
| `Claude CLI process exited` + `returncode` | CLI exit (0=normal, non-0=error) |
| `Agent config loaded` | Model, permissions, sandbox config |

### 4b. SAG Logs (Slack-triggered sessions only)

```
search_gcp_logs(
    query='resource.labels.service_name="slack-agent-gateway-production" "<session_id>" AND timestamp>="<start>" AND timestamp<="<end>"',
    max_results=50
)
```

### 4c. When to Use GCP Logs vs DB

| Question | Use DB | Use GCP Logs |
|---|---|---|
| What tools were called? | raw_events query | |
| How much did it cost? | result event query | |
| What was the outcome? | result event subtype | |
| What was the timing between turns? | | GCP logs timestamps |
| Were there infrastructure errors? | | GCP severity >= WARNING |
| Was there a SAG relay delay? | | SAG service logs |
| What model/config was used? | result event modelUsage; init system event query (see below) | GCP `Agent config loaded` log |

#### Init Event Query (model and config details)

```sql
SELECT
    e->'model' as model,
    e->'tools' as tools,
    e->'permissionMode' as permission_mode,
    e->'session_id' as session_id_from_event
FROM agent_session_messages m,
     LATERAL jsonb_array_elements(m.raw_events) AS e
WHERE m.agent_session_id = '<session_id>'
  AND e->>'type' = 'system'
  AND e->>'subtype' = 'init'
LIMIT 1
```

---

## Phase 5: Diagnosis — Common Failure Patterns

### Pattern 1: Batch Task Exhausting Turns (most common, ~60% of failures)

**Symptoms:** `error_max_turns`, Edit/Read counts >> file count, TASK trigger, migration/refactor title

**Root cause:** Agent edited files one-by-one (read → edit → verify per file) instead of writing a batch script. Each file consumes 3-5 turns.

**Real example:** 17-file logging migration used 58 Edits, 69 Reads, 71 Bash calls = all 51 turns exhausted.

**What to report:** "Agent used an O(n) editing strategy for n files. A batch sed/python script would have completed in 2-3 turns."

### Pattern 2: Tool Returning Empty/Useless Results

**Symptoms:** High call count for one specific MCP tool, agent trying alternative approaches

**Root cause:** Tool has a known limitation (e.g., `read_slack_thread` returns empty for email-forwarded messages). Agent keeps retrying or works around it with many search queries.

**Real example:** 71 `search_slack` calls in a CRON job because `read_slack_thread` returned empty for email messages. Agent "reverse-engineered" content via keyword searches.

**What to report:** "Tool X returned empty/error, agent burned N turns working around it. Consider [fixing the tool / handling this edge case in the agent prompt]."

### Pattern 3: Succeeded on Retry After Max Turns

**Symptoms:** Multi-turn session where turn 1 hit max_turns, turn 2 ("Please continue") succeeded in <15 turns

**Root cause:** Agent spent initial turns exploring/understanding the codebase. On retry, it had context from the worktree already set up and just needed to finish.

**Real example:** "Optimize Slack Thread Fetching" — 51 turns (fail), then 13 turns (success). First attempt was all exploration; second was just commit + PR.

**What to report:** "Task was achievable but agent front-loaded too much exploration. The retry cost was $X. Consider: tighter task descriptions, pre-injecting relevant file paths."

### Pattern 4: Multi-Turn Conversation Stalling

**Symptoms:** SLACK trigger, many user messages, user eventually says "ok i have done these manually"

**Root cause:** Agent investigation took too long or went in circles. User lost patience and did the work themselves.

**Real example:** "Context Length Errors" — 61 agent turns across 4 user messages. User manually fixed the models after the agent's initial investigation.

**What to report:** "Agent investigation was thorough but too slow for the user. Time from first message to actionable answer: X min. Consider: faster initial triage, surfacing quick wins first."

### Pattern 5: Expensive Successful Sessions

**Symptoms:** `result_subtype=success` but cost > $5, or duration > 10 min

**Root cause:** Agent completed the task but inefficiently. May have used subagents, read too many files, or made excessive tool calls.

**What to report:** "Task succeeded but at ${cost} (benchmark: $1.30). Key inefficiency: [specific tool/pattern]. This could be reduced by [specific recommendation]."

---

## Phase 6: Report

### Report Structure

Present findings with this structure, tailored to what the user cares about:

```
## Session: {title}
**ID:** {session_id} | **Agent:** {agent_name} | **Trigger:** {trigger}
**Duration:** {X min} | **Turns:** {N}/{max} | **Cost:** ${X}
**Outcome:** {result_subtype} — {one-line summary}

## What Happened
{2-3 sentences: what was requested, what the agent did, what went wrong}

## Tool Usage
| Tool | Calls | Flag |
|---|---|---|
| ... | ... | {normal / excessive / wasted} |

## Root Cause
{1-2 sentences identifying the specific behavior that caused the failure}

## Actionable Recommendations
1. {Specific, implementable fix} — saves ~{X turns / $Y}
2. {Second recommendation if applicable}

## Evidence
{Link to lit console: https://lit.yupp.ai/agent_harness_console?session_id=...}
{Link to yuppaste if created}
```

### Creating Yuppaste for Detailed Evidence

For complex investigations, create a yuppaste with raw data:

```
create_yuppaste(
    content="<formatted investigation report with log excerpts>",
    name="AHS Investigation: <session_title>"
)
```

### What NOT to Include in Reports

- Raw JSON dumps of events (summarize instead)
- Every tool call (top 3-5 is enough)
- GCP log entries verbatim (extract the relevant fields)
- Speculation without evidence

---

## Quick Reference: Investigation Efficiency Tips

1. **Start with the result event query** — it gives you outcome, cost, turns, and model in one query
2. **Then tool usage breakdown** — this reveals the pattern in seconds
3. **Only read content if tool usage doesn't explain the failure** — e.g., agent used reasonable tools but wrong approach
4. **Only query GCP logs for timing or infrastructure issues** — DB has everything else
5. **For multi-turn sessions, focus on the failing turn** — the turn that hit max_turns or had the error
6. **Compare against benchmarks** — is this session 2x or 10x the average cost?
7. **Check if the session was retried** — multi-turn sessions often have a "Please continue" retry that succeeded
