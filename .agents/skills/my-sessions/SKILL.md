---
name: my-sessions
description: Look up and summarize the current user's recent AHS sessions, grouped by theme, with links to Couch, Lit console, and any artifacts (PRs, artifacts). Use when a user asks "show me my sessions", "what have I been working on", "my recent sessions", or "sessions at a glance".
allowed-tools: mcp__harness__query_agentdb, mcp__harness__query_yuppdb, mcp__harness__send_slack_message
---

# My Sessions — At a Glance

Retrieve and summarize the current user's recent AHS sessions, group them by theme, and surface links to Couch, Lit console, PRs, and artifacts.

## When to Invoke

- User asks "show me my sessions", "what have I been working on", "my recent sessions", "session summary", or "sessions at a glance"
- User wants a digest of their recent agent activity

## Required Context

The current user's ID is always available in the session context system prompt under `User ID`. Extract it before querying.

---

## Phase 1: Fetch Sessions, Status, Artifacts, and Project/Task Links (run all queries in parallel)

### Query 1: Session List with Status

```sql
SELECT
  s.agent_session_id,
  s.title,
  s.status,
  s.trigger,
  s.created_at,
  a.name          AS agent_name,
  a.display_name  AS agent_display_name,
  s.context->>'project_name'  AS project_name,
  -- For SLACK sessions: first user message gives the topic
  CASE
    WHEN s.trigger = 'SLACK' AND s.context->>'slack_thread_prefetched' IS NOT NULL
    THEN LEFT(s.context->>'slack_thread_prefetched', 800)
    ELSE NULL
  END AS slack_topic_hint
FROM agent_sessions s
JOIN agents a ON s.agent_id = a.agent_id
WHERE s.creator_user_id = '<user_id>'
  AND s.deleted_at IS NULL
  AND s.created_at >= NOW() - INTERVAL '30 days'
ORDER BY s.created_at DESC
LIMIT 60
```

### Query 2: Detect "Waiting for Human" Sessions

An ACTIVE session is waiting for human input when the most recent message is from the AGENT (i.e., the agent asked a question or delivered output and is waiting for the user to respond).

```sql
SELECT DISTINCT m.agent_session_id
FROM agent_session_messages m
INNER JOIN (
  SELECT agent_session_id, MAX(turn_number) AS max_turn
  FROM agent_session_messages
  WHERE agent_session_id IN (
    SELECT agent_session_id FROM agent_sessions
    WHERE creator_user_id = '<user_id>'
      AND status = 'ACTIVE'
      AND deleted_at IS NULL
    ORDER BY created_at DESC
    LIMIT 60
  )
  GROUP BY agent_session_id
) latest ON m.agent_session_id = latest.agent_session_id
       AND m.turn_number = latest.max_turn
WHERE m.role = 'AGENT'
  AND m.completion_status != 'IN_PROGRESS'
```

### Query 3: Artifact Extraction (PR and artifact links per session)

```sql
SELECT
  m.agent_session_id,
  -- GitHub PR links
  array_agg(DISTINCT pr_match[1]) FILTER (WHERE pr_match IS NOT NULL)  AS pr_links,
  -- Artifact links (artifacts.agcouch.com or go/p/)
  array_agg(DISTINCT artifact_match[1]) FILTER (WHERE artifact_match IS NOT NULL) AS artifact_links
FROM agent_session_messages m
LEFT JOIN LATERAL (
  SELECT regexp_matches(m.content, 'https://github\.com/yupp-ai/[^/]+/pull/\d+', 'g') AS pr_match
) pr ON true
LEFT JOIN LATERAL (
  SELECT regexp_matches(m.content, '(?:https://artifacts\.agcouch\.com/artifacts/|http://go/p/)[a-z0-9_-]+', 'g') AS artifact_match
) yp ON true
WHERE m.agent_session_id IN (
  SELECT agent_session_id FROM agent_sessions
  WHERE creator_user_id = '<user_id>'
    AND deleted_at IS NULL
    AND created_at >= NOW() - INTERVAL '30 days'
  ORDER BY created_at DESC
  LIMIT 60
)
AND m.role = 'AGENT'
AND (
  m.content LIKE '%github.com/yupp-ai/%/pull/%'
  OR m.content LIKE '%artifacts.agcouch.com/artifacts/by-slug/%'
  OR m.content LIKE '%go/p/%'
)
GROUP BY m.agent_session_id
```

### Query 4: Project and Task Links for Sessions

Sessions are linked to tasks via the `assigned_session_ids` JSONB array on `agent_tasks`. This query finds any project/task associated with each session.

```sql
SELECT
  s.value::text AS agent_session_id,
  t.agent_task_id,
  t.title       AS task_title,
  t.status      AS task_status,
  p.agent_project_id,
  p.name        AS project_name,
  p.status      AS project_status
FROM agent_tasks t
CROSS JOIN LATERAL jsonb_array_elements_text(t.assigned_session_ids) AS s(value)
JOIN agent_projects p ON t.agent_project_id = p.agent_project_id
WHERE t.deleted_at IS NULL
  AND p.deleted_at IS NULL
  AND s.value::text IN (
    SELECT agent_session_id::text FROM agent_sessions
    WHERE creator_user_id = '<user_id>'
      AND deleted_at IS NULL
    ORDER BY created_at DESC
    LIMIT 60
  )
```

---

## Phase 2: Derive Topic for Each Session

For each session, determine its topic using this priority order:

1. **`title`** — use as-is if present (strip `[TASK]` / `[CRON]` prefix decorations)
2. **`slack_topic_hint`** — extract the bold/first sentence of the first user message. Slack sessions often start with `*Topic Name* - description...` — use the bold part as the topic.
3. **`project_name`** — use if it's a TASK or PROJECT trigger
4. **`agent_name` + trigger** — fall back to "CRON: {agent_display_name}" or "TASK: {project_name}"

**Do NOT read raw message content from the DB** — the `slack_topic_hint` from the context field is sufficient.

---

## Phase 3: Group Sessions into Themes

Using the derived topics, group sessions into **3–6 themes**. Each theme should be a concise label (2–4 words). Common themes based on past activity:

| Theme Label | Typical signals |
|---|---|
| Security & Safety | memory guardrails, prompt injection, security incidents, allowlists |
| AHS Performance | latency, startup time, queue wait, warm pool, instrumentation |
| Agent Infrastructure | A2A messaging, sandbox, session management, bwrap, CLI |
| PR / Code Review | "handle pr", "review", "rebase", "fix lint" |
| Research & Analysis | data study, usage analysis, leaderboard, summarize thread |
| Daily / CRON | CRON trigger, scheduled reports, daily self-assessment |
| Product / Features | new feature work that doesn't fit above categories |

You may create new themes if the user's sessions warrant it. Use your judgment — aim for **meaningful clusters, not exhaustive coverage**.

**Selection rule**: Within each theme, keep only the **2–4 most significant sessions**. Skip:
- Trivial sessions (e.g., "what is 1+1", one-line requests)
- Exact duplicates of a more complete session on the same topic
- Sessions where the user's question was answered in < 2 turns with no artifact

---

## Phase 4: Format the Output

Produce a Slack mrkdwn response. Use this template:

```
:mag: *Your Recent Sessions*
_(past N days · M sessions)_

*{Theme 1}*
{status_emoji} *{Topic}* — {agent_display_name} · {relative_date}
  <https://couch.agcouch.com/session/{session_id}|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id={session_id}|Lit>{optional_artifacts}{optional_project_task}

{status_emoji} *{Topic}* — ...

*{Theme 2}*
...
```

### Status Emoji Rules

Each session gets a status emoji prefix based on its state:

| Condition | Emoji | Meaning |
|---|---|---|
| ACTIVE + waiting for human (last message role = AGENT) | :raised_hand: | **Needs your attention** — agent asked something or delivered output |
| ACTIVE + not waiting | :hourglass_flowing_sand: | Running / in progress |
| COMPLETED | :white_check_mark: | Done |
| STALE | :dust_cloud: | Went stale (timed out or abandoned) |

If the session is linked to a task with status `IN_REVIEW`, also append `:eyes:` — the task output is awaiting human review.

**Attention summary**: If any sessions need attention, add a line at the top after the header:
```
:rotating_light: *{N} sessions need your attention*
```

### Artifact and Link Formatting

Where `{optional_artifacts}` is appended inline if artifacts exist:
- For each PR: ` · <{pr_url}|PR #{number}>`
- For each artifact: ` · <{artifact_url}|artifact>`

Where `{optional_project_task}` is appended if the session is linked to a project/task:
- ` · :card_index: <https://couch.agcouch.com/project/{project_id}|{project_name}> / {task_title}`
- If only a task (no project name): ` · :card_index: {task_title}`

### Relative Date Rules

- Today → `today`
- Yesterday → `yesterday`
- 2–6 days ago → `{N}d ago`
- 7+ days ago → `{M} Mar` (short month)

### Example Output

```
:mag: *Your Recent Sessions*
_(past 7 days · 18 sessions)_
:rotating_light: *2 sessions need your attention*

*Security & Safety*
:raised_hand: *Security Incident Tracking* — Raccoon · today
  <https://couch.agcouch.com/session/7a365f80-...|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id=7a365f80-...|Lit> · <https://github.com/yupp-ai/yupp-agent/pull/11216|PR #11216> · <https://artifacts.agcouch.com/artifacts/by-slug/agent-security-plan|artifact> · :card_index: <https://couch.agcouch.com/project/abc123-...|Agent Security Hardening> / Audit prompt injection vectors

:white_check_mark: *Agent Security Plan* — Raccoon · yesterday
  <https://couch.agcouch.com/session/c4f1f939-...|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id=c4f1f939-...|Lit>

*AHS Performance*
:white_check_mark: *Latency Metrics Breakdown* — SRE · today
  <https://couch.agcouch.com/session/47c64873-...|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id=47c64873-...|Lit> · <https://github.com/yupp-ai/yupp-agent/pull/11209|PR #11209>

*Agent Infrastructure*
:raised_hand: :eyes: *Tool Use Messages in SAG* — Raccoon · today
  <https://couch.agcouch.com/session/eea8bd9c-...|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id=eea8bd9c-...|Lit> · :card_index: <https://couch.agcouch.com/project/def456-...|SAG Improvements> / Add tool_use message support

:dust_cloud: *Agent-to-Agent Messaging* — Raccoon · 15 Mar
  <https://couch.agcouch.com/session/9f7d4ba5-...|Couch> · <https://lit.agcouch.com/agent_harness_console?session_id=9f7d4ba5-...|Lit>
```

---

## Formatting Rules

- Use Slack mrkdwn only (not standard Markdown)
- `*bold*` for emphasis, never `**bold**`
- `<url|label>` for links — never `[label](url)`
- No `#` headers — use `*Bold Text*` instead
- Bullet points with `•` character
- Keep each session entry to a single line (after the topic bullet) to avoid visual clutter

---

## Optional: Time Range Override

If the user specifies a time range (e.g., "last month", "past 30 days"), adjust the `LIMIT` and add a `WHERE s.created_at >= NOW() - INTERVAL 'N days'` clause to both queries.

Default: **last 30 days**, up to **60 sessions**, showing the **top 12–20 most important** after filtering.
