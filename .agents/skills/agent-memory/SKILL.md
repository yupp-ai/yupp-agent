---
name: agent-memory
description: Read and write agent memory for cross-session learnings. Use before investigations to check for known patterns, and after to store reusable insights.
allowed-tools: mcp__agcouch-mcp-server__list_memory, mcp__agcouch-mcp-server__search_memory, mcp__agcouch-mcp-server__load_memory, mcp__agcouch-mcp-server__save_memory
---

# Agent Memory

Persistent memory backed by `MEMORY` artifacts in the artifact registry. Each entry is addressed by **(scope, subject, slug)**:

| Scope   | Subject               | Visibility                                              |
| ------- | --------------------- | ------------------------------------------------------- |
| `agent` | your `agent_name`     | Your private notebook (default for every memory tool)   |
| `user`  | the caller's user_id  | The current user's notes — only visible to that user    |
| `topic` | (none)                | Globally shared across all agents and users             |

The DB is the source of truth. There is no GCS, no compare-and-swap, and no `expected_generation`. Each `save_memory` allocates the next version under the same `(scope, subject, slug)` automatically.

## Reading Memory

```
list_memory()                                # Everything visible to you (topic + own user + own agent)
list_memory(scope="topic")                   # Just shared topics
search_memory(query="oncall")                # Substring search across your visibility
load_memory(topic="oncall-learnings")        # Defaults to scope="agent" (your own notebook)
load_memory(topic="oncall-learnings", scope="topic")
```

**Fallback**: If a memory call fails, continue your task without blocking. Memory is supplementary context, not a prerequisite.

**Trust but verify**: Memory entries may be outdated. Use them to guide investigation direction, but always validate against fresh evidence (logs, code, database) before acting on them.

## Storing Memory

After completing a task, if you discovered something reusable:

```
save_memory(topic="oncall-learnings", content="<full markdown body>")                # scope="agent" by default
save_memory(topic="oncall-learnings", content="<full markdown body>", scope="topic") # team-shared
```

The server rejects cross-scope writes — you can only write into your own agent notebook, the current user's notes, or shared topics.

### Choosing a Slug

When storing a learning, first browse with `list_memory()` and `search_memory()`. Then:

1. **Check if an existing slug fits.** If one does, `load_memory` it, merge your entry into the body, and call `save_memory` again with the same slug — that allocates a new version while keeping the (scope, subject, slug) sequence stable. Prefer adding to an existing slug over creating a new one.
2. **Create a new slug only when** the learning doesn't fit any existing slug and represents a distinct, recurring category (not a single entry). Just call `save_memory(topic="<new-slug>", content=...)` — there is no separate "create" call.

#### Suggested Slugs (not exhaustive)

- `oncall-learnings` — General investigation patterns and cross-cutting insights (default)
- `provider-quirks` — Model provider-specific behavior and failure modes
- `service-gotchas` — Per-service debugging tips (backend, admin-service, cron jobs)
- `routing-patterns` — Model routing edge cases and known issues

When in doubt, use `oncall-learnings`.

### Entry Format

Each entry in a memory body should follow this format:

```markdown
### <short description>
- **Service**: <service name, if applicable>
- **Date**: <YYYY-MM-DD when discovered>
- **Pattern**: <error pattern or symptom>
- **Root cause**: <what causes it>
- **Fix/Workaround**: <how to resolve>
- **Evidence**: <artifact link or PR link, if available>
```

### When to Store

Good candidates:
- Recurring error patterns with non-obvious root causes
- Service-specific debugging shortcuts
- Provider quirks (e.g., "provider X returns 429s during peak hours")
- Patterns linking symptoms to causes (e.g., "OOM on backend usually means unbounded streaming response accumulation")

Do **not** store:
- One-off transient errors (network blip, single timeout)
- Obvious issues that any engineer would diagnose immediately
- Sensitive data (credentials, PII, internal URLs with tokens)

### Versioning and Concurrency

Every save creates a new version. Last-write-wins at the (scope, subject, slug) level — there is no CAS / `expected_generation`. If you and another session both append to the same slug, both versions are preserved in the version history; the "current" content is whichever one wrote last. Reduce conflicts by reading immediately before writing and including all existing content in your save body.

### Compacting Verbose Entries

If a slug grows unwieldy, compact it:

1. `load_memory(topic="<slug>")` to read the latest version
2. Review the content and remove:
   - Entries for issues that have been permanently fixed (look for references to merged PRs)
   - Duplicate or near-duplicate entries (consolidate into one)
   - Low-value entries (one-off transient errors, obvious issues)
3. `save_memory(topic="<slug>", content=<compacted body>)` — this writes a new version under the same slug
