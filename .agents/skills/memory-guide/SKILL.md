---
name: memory-guide
description: Full guide to both memory systems (private agent_memories/ and shared GCS). Covers search parameters, writing memories, topic hygiene, and recording tool errors. Trigger when doing more than a basic search_agent_memory() lookup.
---

# Active Memory Management

You have two memory systems. Use them actively — not just when prompted, but as a habit before and after every task.

## Memory Types

### 1. Private Memory (`agent_memories/` directory)

A **local, writable directory** in your workspace that persists across sessions. Only you (your future sessions) can see it. Synced to GCS automatically.

**What to store here:**
- User preferences, personal info, communication style
- Corrections and feedback the user gave you
- Notes from past conversations ("user prefers X", "project Y decided Z")
- Anything specific to your relationship with this user

**How to read/write (bash):**
```bash
ls agent_memories/                        # List what you have
cat agent_memories/user_preferences.md    # Read a file
grep -ri "keyword" agent_memories/        # Search for something

# Write a new memory
cat > agent_memories/user_preferences.md << 'EOF'
- Prefers concise responses
- Works on the routing team
EOF
```

**How to organize:** Use descriptive filenames (`user_preferences.md`, `project_notes/auth_refactor.md`, `feedback.md`). Create sub-folders as needed.

### 2. Shared Memory (GCS directory accessed via MCP tools)

A **shared memory system** across all agents, stored in GCS. Any agent can read and write. Use this for team-wide knowledge.

**What to store here:**
- Non-obvious root causes and debugging patterns
- System quirks and "that's just how it works" behaviors
- Recurring review findings across PRs
- Provider/service quirks and workarounds
- Approaches that worked vs dead ends

**How to read/write:**
```
get_agent_memory()                                    # List all topics
get_agent_memory(topic="sre/streaming-bugs")          # Read a specific topic
store_agent_memory(topic="sre/streaming-bugs",        # Write to a topic
    content="...", expected_generation=N)              # (use expected_generation from get_agent_memory)
```

## Searching Memory (`search_agent_memory`)

Semantic search powered by embeddings — understands meaning, not just keywords. Works well with natural language queries, partial matches, and related concepts (e.g., searching "deployment issues" will find entries about "rollback failures").

One tool searches **both** memory systems. Use the `scope` parameter:

| scope | What it searches | Requires `agent_name`? |
|-------|-----------------|----------------------|
| `"public"` (default) | Shared team memory only | No |
| `"private"` | Your `agent_memories/` only | Yes |
| `"all"` | Both private and shared | Yes |

```
# Search both private + shared (recommended before starting any task)
search_agent_memory(query="<keywords>", scope="all", agent_name="{your_agent_name}")

# Search shared only (default if scope omitted)
search_agent_memory(query="race condition in provider teardown")

# Search private only
search_agent_memory(query="user preferences", scope="private", agent_name="{your_agent_name}")
```

Other parameters: `mode` ("hybrid" default, "semantic", "keyword"), `top_k` (default 5), `topic` (filter by topic), `full_content` (return full text instead of snippet).

## Before You Start a Task

Check **both** memory systems for relevant context:

1. **MCP search** (preferred) — searches private and shared in one call:
   ```
   search_agent_memory(query="<keywords from user message>", scope="all", agent_name="{your_agent_name}")
   ```
2. **Bash** (fallback if MCP unavailable):
   ```bash
   ls agent_memories/ 2>/dev/null && grep -ri "<keywords from user message>" agent_memories/ 2>/dev/null
   ```

This is not optional. Skipping it means you will forget things you or other agents already learned.

Don't announce to the user that you're checking memory — just do it silently. If you find something relevant, mention it naturally. If nothing comes up, say nothing about the search.

## After You Finish a Task

Write down what you learned:

- **Private memory** — personal context, user preferences, feedback. Write immediately with bash — don't wait until end of conversation.
- **Shared memory** — team-useful patterns, root causes, system quirks. Use `store_agent_memory()`.

## What NOT to Store

- One-off transient issues (a single timeout, a flaky test that passed on retry)
- Obvious things any engineer would know
- Sensitive data (credentials, PII, tokens)

## Recording Tool Call Errors

At the end of a session (or after resolving a tricky tool error), use the `/record-tool-error` skill to automatically detect and record tool call failures:

```
/record-tool-error {your_agent_name}
```

The skill will search your session for tool failures caused by **incorrect usage** (wrong parameters, invalid arguments, API misunderstandings), present them for confirmation, and store them in your agent-specific shared memory topic (`{agent_name}/tool_use_errors`).

## Topic Hygiene (Shared Memory)

- Prefer adding to an existing topic over creating a new one
- When a topic grows large, compact it: remove entries for issues that have been permanently fixed, consolidate duplicates, and drop low-value noise
- See the `agent-memory` skill for the full mechanics (optimistic locking, size limits, entry format)
