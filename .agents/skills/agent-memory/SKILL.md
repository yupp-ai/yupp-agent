---
name: agent-memory
description: Read and write shared agent memory for cross-session learnings. Use before investigations to check for known patterns, and after to store reusable insights.
allowed-tools: mcp__agcouch-mcp-server__get_agent_memory, mcp__agcouch-mcp-server__store_agent_memory
---

# Agent Memory

Shared, persistent memory that agents can read and write across sessions. Stored as topic-based markdown files in GCS with optimistic locking for safe concurrent access.

## Reading Memory

```
get_agent_memory()                          # List available topics
get_agent_memory(topic="oncall-learnings")  # Read a specific topic
```

The response includes:
- `content`: The markdown content (or `null` if topic doesn't exist)
- `generation`: Version number for optimistic locking (pass to `store_agent_memory`)

**Fallback**: If the memory read fails (GCS outage, permission error), continue your task without blocking. Memory is supplementary context, not a prerequisite.

**Trust but verify**: Memory entries may be outdated. Use them to guide investigation direction, but always validate against fresh evidence (logs, code, database) before acting on them.

## Storing Memory

After completing a task, if you discovered something reusable, store it:

1. Read current content to get the latest `generation`:
   ```
   get_agent_memory(topic="oncall-learnings")
   ```
2. Merge your learning into the existing content, then write back:
   ```
   store_agent_memory(
       topic="oncall-learnings",
       content="<existing content with your addition merged in>",
       expected_generation=<generation from step 1>
   )
   ```

### Choosing a Topic

When storing a learning, first list existing topics with `get_agent_memory()`. Then:

1. **Check if an existing topic fits.** If one does, read it, merge your entry, and write back. Prefer adding to an existing topic over creating a new one — fewer topics are easier to discover and maintain.
2. **Create a new topic only when** the learning doesn't fit any existing topic and represents a distinct, recurring category (not a single entry). Use `expected_generation=0` to create it.

#### Suggested Topics (not exhaustive)

- `oncall-learnings` — General investigation patterns and cross-cutting insights (default)
- `provider-quirks` — Model provider-specific behavior and failure modes
- `service-gotchas` — Per-service debugging tips (backend, admin-service, cron jobs)
- `routing-patterns` — Model routing edge cases and known issues

When in doubt, use `oncall-learnings`.

### Entry Format

Each entry in a topic file should follow this format:

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

### Optimistic Locking

The `expected_generation` parameter prevents lost updates when multiple agents write concurrently:
- Pass `generation` from `get_agent_memory` when updating an existing topic
- Pass `0` when creating a new topic (fails if topic already exists)
- On `CONFLICT` error: another agent wrote first. Re-read, merge both changes, retry

### Size Limits and Compacting

Each topic file is capped at 32KB. If `store_agent_memory` rejects your write for exceeding this limit, compact the topic:

1. Read the topic with `get_agent_memory` (note the `generation`)
2. Review the content and remove:
   - Entries for issues that have been permanently fixed (look for references to merged PRs)
   - Duplicate or near-duplicate entries (consolidate into one)
   - Low-value entries (one-off transient errors, obvious issues)
3. Write back the compacted content with `store_agent_memory` using the `generation` from step 1
4. Then retry storing your new learning
