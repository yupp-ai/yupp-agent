---
name: memory-guide
description: Full guide to the unified memory system (MEMORY artifacts in the artifact registry, with a local agent_memories/ working copy). Covers scopes, search parameters, writing memories, slug hygiene, and recording tool errors. Trigger when doing more than a basic search_memory() lookup.
---

# Active Memory Management

You have one memory system, accessed two ways: MCP tools (canonical) and a local `agent_memories/` working copy (cheap grep / cat during a turn). Both are backed by `MEMORY` artifacts in the artifact registry — the database is the source of truth. There is no GCS, no compare-and-swap, and no `expected_generation`.

Use memory actively — not just when prompted, but as a habit before and after every task.

## Scopes

Each memory is addressed by **(scope, subject, slug)**:

| Scope   | Subject                | Visibility                                            | Use it for                                              |
| ------- | ---------------------- | ----------------------------------------------------- | ------------------------------------------------------- |
| `agent` | your `agent_name`      | Your private notebook (default scope on every tool)   | Things specific to you / your role across sessions      |
| `user`  | the caller's `user_id` | Only the current user (and you, while serving them)   | User preferences, communication style, prior corrections |
| `topic` | (none)                 | Globally shared — readable and writable by anyone     | Team-wide knowledge, debugging patterns, system quirks  |

Cross-scope writes are rejected by the server (you cannot write into another agent's notebook or another user's notes).

### Disk working copy: `agent_memories/`

At session start, your visible memories are materialized into:

```
agent_memories/
  topic/{slug}.md      ← all scope=topic memories
  user/{slug}.md       ← scope=user, subject=current user
  agent/{slug}.md      ← scope=agent, subject=your agent_name
```

Treat the directory as a **read-only cache** during a turn — `cat` and `grep` are fine for fast lookups. To **write**, always go through `save_memory` (the tool refreshes the disk copy automatically as a write-through). Editing files directly with bash bypasses the DB and the change will not survive the session.

## Reading Memory

```
list_memory()                                # Everything visible (topic + own user + own agent)
list_memory(scope="topic")                   # Just shared topics
list_memory(scope="agent")                   # Just your own notebook

search_memory(query="<keywords>")            # Substring search across your visibility
search_memory(query="<keywords>", scope="topic")

load_memory(topic="<slug>")                  # Default scope="agent"
load_memory(topic="<slug>", scope="topic")
load_memory(topic="<slug>", scope="user")
```

## Writing Memory

```
save_memory(topic="<slug>", content="<full markdown body>")               # scope="agent" by default
save_memory(topic="<slug>", content="...", scope="user")                  # store in current user's notes
save_memory(topic="<slug>", content="...", scope="topic")                 # share with the team
```

Each save creates a new version under the same `(scope, subject, slug)`; the latest non-archived version is what subsequent `load_memory` calls return.

## Before You Start a Task

Search what's already known:

```
search_memory(query="<keywords from user message>")
```

That covers your full visibility (your own agent + user + topics). Narrow with `scope="topic"` to focus on shared knowledge, or fall back to bash if the MCP tool isn't responsive:

```bash
grep -ri "<keywords>" agent_memories/ 2>/dev/null
```

This is not optional. Skipping it means you will forget things you or other agents already learned.

Don't announce to the user that you're checking memory — just do it silently. If you find something relevant, mention it naturally. If nothing comes up, say nothing about the search.

## After You Finish a Task

Write down what you learned. Pick the right scope:

- **`scope="user"`** — personal context, user preferences, feedback
- **`scope="agent"`** — your own notes for future sessions of the same agent
- **`scope="topic"`** — team-useful patterns, root causes, system quirks

```
save_memory(topic="user-preferences", content="<...>", scope="user")
save_memory(topic="oncall-learnings", content="<...>", scope="topic")
```

## What NOT to Store

- One-off transient issues (a single timeout, a flaky test that passed on retry)
- Obvious things any engineer would know
- Sensitive data (credentials, PII, tokens)

## Recording Tool Call Errors

At the end of a session (or after resolving a tricky tool error), use the `/record-tool-error` skill to automatically detect and record tool call failures:

```
/record-tool-error {your_agent_name}
```

The skill will search your session for tool failures caused by **incorrect usage** (wrong parameters, invalid arguments, API misunderstandings), present them for confirmation, and store them in your agent-scope memory under the slug `tool-use-errors`.

## Slug Hygiene

- Prefer adding to an existing slug over creating a new one — `load_memory` it, merge your entry into the body, `save_memory` it back
- When a slug grows large, compact it: drop entries for issues that have been permanently fixed, consolidate duplicates, and remove low-value noise
- See the `agent-memory` skill for the full mechanics (versioning, entry format, suggested slugs)
