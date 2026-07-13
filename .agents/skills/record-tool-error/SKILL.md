---
name: record-tool-error
description: Record a tool call failure to your agent-scope memory so future sessions can avoid the same mistake. Usage /record-tool-error
allowed-tools: mcp__harness__load_memory, mcp__harness__save_memory
---

# Record Tool Error

Detect tool call failures from the current session and record them to your **agent-scope** memory under the slug `tool_use_errors`. Future sessions of the same agent will see them and avoid the same mistakes.

## When to Use

Use this skill at the **end of a session** (or after resolving a tricky tool error) to capture learnings. The skill will:

1. Search the current session for tool call failures
2. Filter to failures caused by **incorrect usage** (not transient errors)
3. Append the error and correct approach to the `tool_use_errors` memory

Only failures that meet these criteria are worth recording:
- Caused by **calling the tool incorrectly** (wrong parameters, invalid arguments, API misunderstandings)
- Would help another agent (or your future self) avoid the same mistake
- Involve non-obvious API constraints, undocumented behavior, or easy-to-miss requirements

## Usage

```
/record-tool-error
```

No arguments. The new memory model namespaces by **agent automatically** via `scope="agent"` — you do not pass a username, and there is no `{username}/` slug prefix.

## Memory Address

Tool errors are stored at:

- **Address:** `a:{your_agent_name}:tool_use_errors`
- **Scope:** `agent` (the default — no `scope` argument needed)
- **Slug:** `tool_use_errors`
- **Subject:** the calling agent's `agent_name`, filled in by the server from caller context — you do not pass `subject`.

## Procedure

1. **Search the session for tool call failures**:
   - Look through the conversation history for tool calls that returned errors
   - Identify failures caused by incorrect usage (wrong parameters, invalid arguments, API misunderstandings)
   - Skip transient errors (timeouts, rate limits, network issues, permission errors)

2. **For each recordable failure, extract**:
   - Which tool failed
   - The error message
   - What was tried that didn't work
   - What eventually worked (the correct usage)

3. **Present findings to user for confirmation**:
   - Show the list of failures found
   - Ask which ones should be recorded (some may be too obvious or one-off)

4. **Read existing memory** to get current content. If the slug doesn't exist yet, `load_memory` returns `success=False` with a not-found error — treat the body as empty:
   ```
   load_memory(topic="tool_use_errors")     # defaults to scope="agent"
   ```

5. **Format each new entry**:
   ```markdown
   ### {tool_name}: {short description}
   - **Error**: {error message}
   - **Wrong usage**: {what didn't work}
   - **Correct usage**: {what works}
   - **Date**: {YYYY-MM-DD}
   ```

6. **Append to existing content** (or use just the new entries if the slug doesn't exist yet).

7. **Store the updated memory** — versioning is automatic:
   ```
   save_memory(topic="tool_use_errors", content="<merged content>")     # scope="agent" by default
   ```

8. **Confirm** the entries were recorded.

## Example Entry

```markdown
### query_appdb: column name error
- **Error**: `column "user_id" does not exist`
- **Wrong usage**: `SELECT user_id FROM chats`
- **Correct usage**: The column is `participant_id`, not `user_id`. Use `SELECT participant_id FROM chats`.
- **Date**: 2026-04-29

### search_gcp_logs: timestamp format
- **Error**: `Invalid timestamp format`
- **Wrong usage**: `start_time="2025-02-15"` (date only)
- **Correct usage**: Must include time and timezone: `start_time="2025-02-15T00:00:00Z"`
- **Date**: 2026-04-29
```

## What NOT to Record

- Transient errors (timeouts, rate limits, network issues)
- Permission errors (user doesn't have access)
- Obvious mistakes any engineer would immediately recognize
- Sensitive data (credentials, PII, internal tokens)
- One-off issues that won't recur

## Slug Maintenance

When the slug grows large, consolidate:
- Remove entries for issues fixed in tool implementations
- Merge duplicate entries about the same tool/error
- Keep entries concise and actionable

Each `save_memory` creates a new version under the same `(scope, subject, slug)` — last-write-wins, no compare-and-swap. Read-modify-write back-to-back to keep conflicts unlikely.
