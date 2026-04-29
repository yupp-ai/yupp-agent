---
name: memory-guide
description: Full guide to the unified memory system — scoped MEMORY artifacts (DB-authoritative) plus a per-session agent_memories/ disk cache for cheap grep/cat. Covers scopes, the on-disk working copy, search/read/write tools, slug hygiene, and recording tool errors. Trigger when doing more than a basic search_memory() lookup.
---

# Memory Guide

There is **one** memory system. It is backed by `MEMORY` artifacts in the artifact registry — the database is the source of truth. Versioning is automatic; there is no `expected_generation` and no compare-and-swap.

You access memory two ways:

1. **MCP tools** (`save_memory` / `load_memory` / `search_memory` / `list_memory`) — the canonical, durable surface.
2. **A per-session on-disk working copy** at `agent_memories/{scope}/{slug}.md` — a materialized cache for cheap `cat` / `grep` during a turn.

Use memory actively — not just when prompted, but as a habit before and after every task.

## Scopes

Each memory is addressed by **(scope, subject, slug)**:

| Scope   | Subject                | Default? | Visibility                                              | Use it for                                                |
| ------- | ---------------------- | -------- | ------------------------------------------------------- | --------------------------------------------------------- |
| `agent` | your `agent_name`      | yes      | Sessions of the same agent                              | Things specific to you / your role across sessions        |
| `user`  | the caller's `user_id` | opt-in   | Only the current user (and agents serving them)         | User preferences, communication style, prior corrections  |
| `topic` | (none)                 | opt-in   | Globally shared — readable and writable by anyone       | Team-wide knowledge, debugging patterns, system quirks    |

Cross-scope writes are rejected by the server: you cannot save into another agent's notebook or another user's notes. Subjects are inferred from caller context (`agent_name` / `user_id`) — you don't pass them yourself unless you have a specific reason.

## Disk Working Copy: `agent_memories/`

At session start, every memory visible to your (agent, user) pair is **materialized fresh** from the DB into your sandbox:

```
agent_memories/
  topic/{slug}.md      ← all scope=topic memories
  user/{slug}.md       ← scope=user, subject=current user
  agent/{slug}.md      ← scope=agent, subject=your agent_name
```

This is a **per-session cache**, not a persistent home. Three rules:

1. **`cat` / `grep` are fine.** Reading from disk during a turn is the cheap path — no API round-trip. Skim with `grep -ri "<keywords>" agent_memories/` whenever the MCP tools feel heavy.
2. **Direct writes are LOST at session end.** `cat > agent_memories/foo.md`, `echo "..." >> agent_memories/topic/x.md`, `Edit` calls — none of these touch the DB. The sandbox tears down and the change vanishes.
3. **To persist, use `save_memory`.** The MCP tool writes through to the DB and refreshes the on-disk file in the same call, so subsequent `cat`s in the same turn see the new content.

The workspace summary in your system prompt phrased this as "the memory directory is a per-session materialized cache from the DB" — this skill is the long form.

## Reading

```
list_memory()                                # Everything visible (topic + own user + own agent)
list_memory(scope="topic")                   # Just shared topics
list_memory(scope="agent")                   # Just your own notebook
list_memory(scope="user")                    # Just the current user's notes

search_memory(query="<keywords>")            # Substring search across your full visibility
search_memory(query="<keywords>", scope="topic")
search_memory(query="<keywords>", scope="agent")
search_memory(query="<keywords>", scope="user")

load_memory(topic="<slug>")                  # Default scope="agent"
load_memory(topic="<slug>", scope="topic")
load_memory(topic="<slug>", scope="user")
```

`search_memory` accepts `scope=user|agent|topic`. **Omit `scope` to search your full visibility** (topic + own user + own agent).

If the MCP tools are slow or unavailable, fall back to disk:

```bash
grep -ri "<keywords>" agent_memories/ 2>/dev/null
cat agent_memories/topic/oncall-learnings.md
```

Disk-fallback reads are fine; **disk-fallback writes are not** (see above).

## Writing

```
save_memory(topic="<slug>", content="<full markdown body>")               # scope="agent" by default
save_memory(topic="<slug>", content="...", scope="user")                  # current user's notes
save_memory(topic="<slug>", content="...", scope="topic")                 # team-shared
```

Each save creates a new version under the same `(scope, subject, slug)`. The latest non-archived version is what `load_memory` returns. There is no `expected_generation` and no compare-and-swap — versioning is automatic and last-write-wins. To minimize conflicts, read immediately before writing and include all existing content in your save body.

The tool's display form for the address is `t:slug` / `a:agent_name:slug` / `u:user_id:slug` — you'll see this in tool return values and viewer URLs.

## Before You Start a Task

Search what's already known:

```
search_memory(query="<keywords from user message>")
```

That covers your full visibility. Narrow with `scope="topic"` to focus on shared knowledge, or `scope="agent"` for your own notes. If the MCP tool isn't responsive:

```bash
grep -ri "<keywords>" agent_memories/ 2>/dev/null
```

This is not optional. Skipping it means you will forget things you or other agents already learned.

Don't announce to the user that you're checking memory — just do it silently. If you find something relevant, mention it naturally. If nothing comes up, say nothing about the search.

## After You Finish a Task

Write down what you learned. Pick the right scope:

- **`scope="user"`** — personal context, user preferences, feedback specific to this user
- **`scope="agent"`** — your own notes for future sessions of the same agent
- **`scope="topic"`** — team-useful patterns, root causes, system quirks

```
save_memory(topic="user-preferences", content="<...>", scope="user")
save_memory(topic="oncall-learnings", content="<...>", scope="topic")
save_memory(topic="my-routing-notes", content="<...>")    # default scope="agent"
```

## What NOT to Store

- One-off transient issues (a single timeout, a flaky test that passed on retry)
- Obvious things any engineer would know
- Sensitive data (credentials, PII, tokens)

## Recording Tool Call Errors

At the end of a session (or after resolving a tricky tool error), use the `/record-tool-error` skill to detect and record tool call failures:

```
/record-tool-error
```

The skill scans your session for tool failures caused by **incorrect usage** (wrong parameters, invalid arguments, API misunderstandings), presents them for confirmation, and saves them to your **agent-scope** memory under the slug `tool_use_errors`. Per-agent namespacing is handled automatically by `scope="agent"` — you no longer pass a username.

## Slug Hygiene

- Prefer adding to an existing slug over creating a new one — `load_memory` it, merge your entry into the body, `save_memory` it back
- When a slug grows large, compact it: drop entries for issues that have been permanently fixed, consolidate duplicates, and remove low-value noise
- See the `agent-memory` skill for full mechanics (display form, versioning, entry format, suggested slugs)
