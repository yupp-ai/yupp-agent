---
name: feed-insights
description: Investigate what happened to a feed post (interesting turn candidate). Use when debugging why a turn was rejected, approved, or stuck in moderation. Accepts a turn_id or feed post URL, and optional environment (production/staging).
argument-hint: <turn_id_or_url> [staging|production]
allowed-tools: mcp__agcouch-mcp-server__query_yuppdb, mcp__agcouch-mcp-server-staging__query_yuppdb, mcp__agcouch-mcp-server__search_gcp_logs, mcp__agcouch-mcp-server-staging__search_gcp_logs
---

# Feed Post Investigation Guide

Investigate what happened to a feed post (interesting turn candidate) given a turn ID.

## Arguments

- `$0` — The turn ID (UUID) or a feed post URL. Required.
- `$1` — Environment: `staging` or `production`. Optional if a URL is provided (environment is inferred from the domain).

## Parsing the Input

The input can be either a raw UUID or a feed post URL.

**URL format**: `https://<domain>/featured/post/<turn_id>`

Examples:
- `https://yupp.ai/featured/post/6e853b8d-a8a1-4caa-a060-8477788c942e` — production
- `https://chaos.yupp.ai/featured/post/0b05adc5-cd8d-42c1-bf19-e28f84e2aecc` — staging

**Extracting turn_id**: Take the last path segment (the UUID).

**Inferring environment from domain**:
- `yupp.ai` (no subdomain, or `www.yupp.ai`) → **production**
- Any other subdomain (e.g., `chaos.yupp.ai`, `staging.yupp.ai`) → **staging**

If an explicit environment argument (`$1`) is provided, it takes precedence over the domain inference.

## Tool Selection

Based on the environment, use the correct MCP tools:
- **production**: `mcp__agcouch-mcp-server__query_yuppdb` and `mcp__agcouch-mcp-server__search_gcp_logs`
- **staging**: `mcp__agcouch-mcp-server-staging__query_yuppdb` and `mcp__agcouch-mcp-server-staging__search_gcp_logs`

## Step 1: Gather Core Data

Run these queries in parallel to get the full picture:

### 1a. Interesting Turn Candidate record

```sql
SELECT itc.interesting_turn_candidate_id, itc.turn_id, itc.suggested_title,
       itc.suggested_description, itc.source, itc.status, itc.rejection_reason,
       itc.interesting_turn_id, itc.canonical_turn_id, itc.slack_thread_id,
       itc.support_ticket_id, itc.created_at, itc.modified_at
FROM interesting_turn_candidates itc
WHERE itc.turn_id = '<turn_id>'
```

### 1b. Turn metadata

```sql
SELECT turn_id, chat_id, sequence_id, created_at, modified_at, blinded, modality
FROM turns
WHERE turn_id = '<turn_id>'
```

### 1c. Turn Interest Attributes (scoring data)

```sql
SELECT * FROM turn_interest_attributes
WHERE turn_id = '<turn_id>'
```

### 1d. Turn Interest Attributes Audits (detailed scoring audit)

```sql
SELECT * FROM turn_interest_attributes_audits
WHERE turn_id = '<turn_id>'
ORDER BY created_at
```

### 1e. Turn Quality (moderation & quality labels)

```sql
SELECT turn_id, prompt_is_safe, prompt_difficulty, prompt_novelty,
       prompt_unsafe_reasons, prompt_moderation_model_name,
       is_suggested_followup, is_conversation_starter, is_copy_paste,
       quality, is_recent_complex_prompt_bank, created_at
FROM turn_qualities
WHERE turn_id = '<turn_id>'
```

### 1f. Support Ticket Audits (if support_ticket_id is present)

If the candidate record has a non-null `support_ticket_id`, query the audit trail to see moderator actions:

```sql
SELECT sta.audit_id, sta.actor_id, sta.event_type, sta.event_details,
       sta.comment, sta.created_at
FROM support_ticket_audits sta
WHERE sta.ticket_id = '<support_ticket_id>'
ORDER BY sta.created_at
```

Event types: `CREATED`, `STATUS_CHANGED`, `PRIORITY_CHANGED`, `ASSIGNED`, `COMMENT_ADDED`, `RESOLVED`. The `event_details` JSONB field contains transition info (e.g., `{"from": "NEW", "to": "CLOSED"}`).

### 1g. Interesting Turn record (if promoted to feed)

If `interesting_turn_id` is not null in the candidate record:

```sql
SELECT * FROM interesting_turns
WHERE turn_id = '<turn_id>'
```

## Step 2: Interpret the Status

Based on the candidate's `status`, follow the relevant diagnostic path:

### Status: `AUTO_REJECTED`

Check the `rejection_reason` field:

| Rejection Reason | What Happened | Where to Look |
|---|---|---|
| `LOW_EFFORT` | Failed validation — `prompt_is_safe` was `false` in `turn_qualities` | Check `prompt_unsafe_reasons` in `turn_qualities` for the specific moderation flag (e.g., `SPECIALIZED_ADVICE`, `HARMFUL_CONTENT`) |
| `LOW_DIVERGENCE` | Scoring pipeline ran but scores were too low | Check `turn_interest_attributes` for `interestingness_score` and `divergence_score`. Low scores indicate model responses were too similar |
| `DUPLICATE_DETECTED` | Embedding similarity check found a similar existing candidate | Check `canonical_turn_id` in the candidate record for the original turn |

### Status: `PENDING_MODERATOR_REVIEW`

The turn passed validation but is waiting for human review. Common reasons:

1. **`score_turn` returned `None`** — Two distinct cases, both return `None`:

   **a. Turn was skipped** — A `turn_interest_attributes` record exists with `interestingness_skipped = true`. Check the `interestingness_skipped_reasons` column for the specific reasons (e.g., attachments, language, suggested prompt). GCP log message: `"Turn skipped during single-turn scoring"`.

   **b. Turn was ineligible** — No `turn_interest_attributes` record exists at all. Common causes:
   - `sequence_id` in `turns` table is not `0` (follow-up turns are ineligible)
   - Fewer than 2 successful assistant messages
   - GCP log message: `"Turn not eligible for interestingness scoring"`

2. **`score_turn` failed** — The scoring task threw an exception. Check GCP logs for: `"score_turn background task failed for interesting turn candidate"`

3. **Scoring succeeded but auto-approve conditions not met** — Check `turn_interest_attributes`:
   - `interestingness_score` below auto-approve threshold
   - `needs_moderation` flag was `true` in the audit details

### Status: `PENDING_USER_APPROVAL`

System-initiated candidate (source: `INTERESTING_TURN_FINDER_AUTO_SUBMISSION`) where the chat is private. The automated pipeline found the turn interesting but the user hasn't made the chat public yet. Check:
- `source` field — should be `INTERESTING_TURN_FINDER_AUTO_SUBMISSION`
- Whether the chat is public: `SELECT is_public FROM chats JOIN turns ON turns.chat_id = chats.chat_id WHERE turns.turn_id = '<turn_id>'`
- If the chat is now public but status hasn't progressed, the approval flow may not have been triggered

### Status: `PENDING_VALIDATION`

The candidate was just created and hasn't been validated yet. This is a transient state — if it persists, the validation task may have failed.

### Status: `APPROVED` / `AUTO_APPROVED`

The turn was accepted. Check `interesting_turns` table for the feed entry details (visibility, feed_title, feed_description).

### Status: `REJECTED`

Manually rejected by a moderator. Check `rejection_reason` for details.

### Status: `DUPLICATE`

Marked as duplicate of another turn. Check `canonical_turn_id` for the original.

## Step 3: Check GCP Logs (only if DB data is insufficient)

Only search GCP logs when the database tables don't fully explain the outcome. Focus exclusively on the submission/scoring pipeline — **ignore** engagement, interaction, streaming, reward, eval, and other unrelated logs.

```
query: jsonPayload.turn_id="<turn_id>"
hours_back: 3
max_results: 30
```

**Important**: The structured logger may store fields in the `message` object (not `jsonPayload`), so when parsing results, look for:
- `message.message` — the log message text
- `message.module` — source module
- `message.func_name` — function name
- `message.turn_id`, `message.candidate_id`, etc. — contextual fields

### Relevant Modules

Only these modules matter for feed post investigation. **Filter out everything else** when parsing results:
- `feed` — submission entry point
- `service` — validation and candidate status updates
- `analyze_interesting_turns` — scoring eligibility
- `process_turn_interest_attributes` — filter pipeline

### Key Log Messages to Look For

| Log Message | Module | Meaning |
|---|---|---|
| `"Processing started for feed.submit_entry"` | `feed` | Submission received |
| `"Turn skipped during single-turn scoring"` | `analyze_interesting_turns` | Turn was skipped (check `skip_reasons`) |
| `"Turn not eligible for interestingness scoring"` | `analyze_interesting_turns` | Failed eligibility check (e.g., `sequence_id != 0`, < 2 assistant messages) |
| `"Validated user submission: moved to PENDING_MODERATOR_REVIEW"` | `service` | Passed safety validation |
| `"Validated user submission: auto-rejected (LOW_EFFORT)"` | `service` | Failed safety validation |
| `"Updated interesting turn candidate status"` | `service` | Status transition |
| `"score_turn background task failed"` | `service` | Scoring error |
| `"Processed interesting turn candidate"` | `process_turn_interest_attributes` | Completed filter pipeline |

## Step 4: Summarize Findings

Present a clear summary including:

1. **Turn metadata**: chat_id, sequence_id, when it was created
2. **Candidate status**: current status and rejection reason (if any)
3. **Root cause**: why the turn ended up in its current state
4. **Scoring data**: interestingness/divergence/refusal scores (if available)
5. **Moderation data**: prompt_is_safe, unsafe_reasons (if relevant)
6. **Timeline**: key events from submission to final state

## Eligibility Requirements Reference

For a turn to pass the full pipeline, it must satisfy:

1. **Validation** (`validate_interesting_turn_candidate`):
   - `TurnQuality.prompt_is_safe == True`

2. **Scoring eligibility** (`check_turn_eligibility`):
   - `sequence_id == 0` (first turn in chat)
   - At least 2 successful assistant messages
   - Not in excluded categories
   - Passes attachment, language, and suggested-prompt filters

3. **Filter pipeline** (`process_interesting_turn_candidate`):
   - Interestingness score >= threshold
   - User message length within allowed range
   - Vibe score difference below threshold
   - Model age within allowed range
   - Model characteristics match
   - No duplicate detected via embedding similarity

4. **Auto-approve** (optional):
   - `auto_approve_enabled` in settings
   - Interestingness score >= auto-approve threshold
   - `needs_moderation == false`

## Related Tables

| Table | Purpose |
|---|---|
| `interesting_turn_candidates` | Candidate submissions and their status |
| `interesting_turns` | Approved feed entries |
| `turn_interest_attributes` | Scoring data (interestingness, divergence, refusal) |
| `turn_interest_attributes_audits` | Detailed scoring audit with model version and reasons |
| `turn_qualities` | Moderation results, difficulty, novelty, safety |
| `turns` | Turn metadata (sequence_id, chat_id) |
| `support_ticket_audits` | Audit trail for linked support tickets |

## Disclaimer

Always include this at the end of your output:

> The investigation is based on an LLM authored skill. AI can make mistakes. Please help improve the skill if something is amiss.