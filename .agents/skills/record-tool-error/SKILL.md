---
name: record-tool-error
description: Record a tool call failure to agent memory so future sessions can avoid the same mistake. Usage /record-tool-error [username]
allowed-tools: Bash, mcp__yuppster-mcp-server__get_agent_memory, mcp__yuppster-mcp-server__store_agent_memory
---

# Record Tool Error

Automatically detect and record tool call failures from the current session to agent memory. This helps future sessions avoid repeating the same mistakes.

## When to Use

Use this skill at the **end of a session** (or after resolving a tricky tool error) to capture learnings. The skill will:

1. Search the current session for tool call failures
2. Filter to failures caused by **incorrect usage** (not transient errors)
3. Record the error and correct approach to memory

Only failures that meet these criteria are worth recording:
- Caused by **calling the tool incorrectly** (wrong parameters, invalid arguments, API misunderstandings)
- Would help another agent (or your future self) avoid the same mistake
- Involve non-obvious API constraints, undocumented behavior, or easy-to-miss requirements

## Usage

```
/record-tool-error [username]
```

### Username Parameter

- **AHS agents**: Pass your agent name (e.g., `sre`, `data-scientist`, `reviewer`)
- **Local Claude Code**: Omit the parameter — your GitHub username will be detected automatically

If no username is provided, detect the GitHub login using this fallback chain:

1. **Try `gh` CLI** (if installed and authenticated):
   ```bash
   gh api user --jq '.login'
   ```

2. **Try extracting from git email** (the part before `@` is often the username):
   ```bash
   git config user.email | cut -d'@' -f1
   ```

3. **Ask the user** if neither method works or if the extracted value looks wrong (contains spaces, special characters, etc.).

## Memory Topic

Tool errors are stored in a user/agent-specific topic:

```
{username}/tool_use_errors
```

Examples:
- `sre/tool_use_errors`
- `data-scientist/tool_use_errors`
- `lguan/tool_use_errors`

## Procedure

1. **Determine the username**:
   - If provided as argument, use it directly
   - Otherwise, try `gh api user --jq '.login'`
   - If `gh` fails, try extracting from git email: `git config user.email | cut -d'@' -f1`
   - If the extracted value looks invalid (spaces, special chars) or all methods fail, ask the user

2. **Search the session for tool call failures**:
   - Look through the conversation history for tool calls that returned errors
   - Identify failures caused by incorrect usage (wrong parameters, invalid arguments, API misunderstandings)
   - Skip transient errors (timeouts, rate limits, network issues, permission errors)

3. **For each recordable failure, extract**:
   - Which tool failed
   - The error message
   - What was tried that didn't work
   - What eventually worked (the correct usage)

4. **Present findings to user for confirmation**:
   - Show the list of failures found
   - Ask which ones should be recorded (some may be too obvious or one-off)

5. **Read existing memory** to get current content and generation:
   ```
   get_agent_memory(topic="{username}/tool_use_errors")
   ```

6. **Format each new entry**:
   ```markdown
   ### {tool_name}: {short description}
   - **Error**: {error message}
   - **Wrong usage**: {what didn't work}
   - **Correct usage**: {what works}
   - **Date**: {YYYY-MM-DD}
   ```

7. **Append to existing content** (or create new if topic doesn't exist)

8. **Store the updated memory**:
   ```
   store_agent_memory(
       topic="{username}/tool_use_errors",
       content="<merged content>",
       expected_generation=<generation from read, or 0 if new>
   )
   ```

9. **Confirm** the entries were recorded

## Example Entry

```markdown
### query_yuppdb: column name error
- **Error**: `column "user_id" does not exist`
- **Wrong usage**: `SELECT user_id FROM chats`
- **Correct usage**: The column is `participant_id`, not `user_id`. Use `SELECT participant_id FROM chats`.
- **Date**: {YYYY-MM-DD}

### search_gcp_logs: timestamp format
- **Error**: `Invalid timestamp format`
- **Wrong usage**: `start_time="2025-02-15"` (date only)
- **Correct usage**: Must include time and timezone: `start_time="2025-02-15T00:00:00Z"`
- **Date**: {YYYY-MM-DD}
```

## What NOT to Record

- Transient errors (timeouts, rate limits, network issues)
- Permission errors (user doesn't have access)
- Obvious mistakes any engineer would immediately recognize
- Sensitive data (credentials, PII, internal tokens)
- One-off issues that won't recur

## Topic Maintenance

When the topic grows large, consolidate:
- Remove entries for issues fixed in tool implementations
- Merge duplicate entries about the same tool/error
- Keep entries concise and actionable
