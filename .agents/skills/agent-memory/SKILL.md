---
name: agent-memory
description: Save and recall scoped memories (agent / user / topic) backed by MEMORY artifacts. Use to capture cross-session learnings — your own notebook, the current user's notes, or shared topics — and to look them up before doing similar work again.
allowed-tools: mcp__harness__list_memory, mcp__harness__search_memory, mcp__harness__load_memory, mcp__harness__save_memory
---

# Agent Memory (Scoped MEMORY Artifacts)

Memories live as `MEMORY` artifacts in the artifact registry. The database is the source of truth — versioning is automatic and there is no compare-and-swap. Each entry is addressed by **(scope, subject, slug)**.

## Scopes

| Scope   | Subject                | Default? | Visibility                                              |
| ------- | ---------------------- | -------- | ------------------------------------------------------- |
| `agent` | your `agent_name`      | yes      | Your private notebook — only sessions of the same agent |
| `user`  | the caller's `user_id` | opt-in   | Notes about the current user — only that user (and agents serving them) |
| `topic` | (none)                 | opt-in   | Globally shared — readable and writable by anyone       |

When you call a memory tool without a `scope` argument, the server uses **`scope="agent"`** with your own `agent_name` as the subject. Use `user` or `topic` only when you explicitly mean to.

The server rejects cross-scope writes — you can only write into your own agent notebook, the current user's notes, or shared topics.

## Display Form

Tool return values, the viewer, and logs render the address as a short prefix-form:

| Scope   | Display                 | Example                          |
| ------- | ----------------------- | -------------------------------- |
| `user`  | `u:{user_id}:{slug}`    | `u:USR_7f2a:user_preferences`    |
| `agent` | `a:{agent_name}:{slug}` | `a:eng-raccoon:feedback_style`   |
| `topic` | `t:{slug}`              | `t:routing_tips`                 |

The `u:` / `a:` / `t:` prefix is **illustrative** — it's reconstructed from the structured columns on the fly. The stored slug is just the topic, e.g. `user_preferences`.

## Tool Surface

```
list_memory()                                      # Everything visible (topic + own user + own agent)
list_memory(scope="topic")                         # Just shared topics
list_memory(scope="agent")                         # Just your own notebook

search_memory(query="<keywords>")                  # Substring search across your full visibility
search_memory(query="<keywords>", scope="topic")   # Narrow to a single scope

load_memory(topic="<slug>")                        # Default scope="agent"
load_memory(topic="<slug>", scope="topic")
load_memory(topic="<slug>", scope="user")

save_memory(topic="<slug>", content="<markdown>")                  # scope="agent" by default
save_memory(topic="<slug>", content="<markdown>", scope="user")    # caller's own user notes
save_memory(topic="<slug>", content="<markdown>", scope="topic")   # team-shared
```

`search_memory` accepts `scope=user|agent|topic` to narrow; **omit `scope` for full visibility**.

**Fallback**: If a memory tool fails, continue your task without blocking. Memory is supplementary context, not a prerequisite.

**Trust but verify**: Memory entries may be outdated. Use them to guide investigation direction, but always validate against fresh evidence (logs, code, database) before acting on them.

## Versioning

Every `save_memory` allocates the next version under the same `(scope, subject, slug)`. Versioning is fully automatic — you don't pass a version number, you don't read-then-CAS, you just save. `load_memory` returns the latest non-archived version; older versions remain in history if you ever need them.

Last-write-wins at the (scope, subject, slug) level: if you and another session save to the same slug, both writes succeed and the most recent one is what subsequent loads return. Reduce conflicts by reading immediately before writing and including all existing content in your save body.

## Choosing a Slug

When storing a learning, first browse with `list_memory()` and `search_memory()`. Then:

1. **Check if an existing slug fits.** If one does, `load_memory` it, merge your entry into the body, and call `save_memory` again with the same slug — that allocates a new version while keeping the (scope, subject, slug) sequence stable. Prefer adding to an existing slug over creating a new one.
2. **Create a new slug only when** the learning doesn't fit any existing slug and represents a distinct, recurring category (not a single entry). Just call `save_memory(topic="<new-slug>", content=...)` — there is no separate "create" call.

### Suggested Slugs (not exhaustive)

- `oncall-learnings` — General investigation patterns and cross-cutting insights (typically `scope="topic"`)
- `provider-quirks` — Model provider-specific behavior and failure modes (`scope="topic"`)
- `service-gotchas` — Per-service debugging tips (backend, admin-service, cron jobs) (`scope="topic"`)
- `routing-patterns` — Model routing edge cases and known issues (`scope="topic"`)
- `tool_use_errors` — Tool calling mistakes worth remembering (use `scope="agent"` — see `/record-tool-error`)
- `user-preferences` — Communication style, prior corrections (use `scope="user"`)

When in doubt, use `oncall-learnings` for shared knowledge or your own custom agent-scope slug.

## Entry Format

Each entry inside a memory body should follow this format:

```markdown
### <short description>
- **Service**: <service name, if applicable>
- **Date**: <YYYY-MM-DD when discovered>
- **Pattern**: <error pattern or symptom>
- **Root cause**: <what causes it>
- **Fix/Workaround**: <how to resolve>
- **Evidence**: <artifact link or PR link, if available>
```

## When to Store

Good candidates:
- Recurring error patterns with non-obvious root causes
- Service-specific debugging shortcuts
- Provider quirks (e.g., "provider X returns 429s during peak hours")
- Patterns linking symptoms to causes (e.g., "OOM on backend usually means unbounded streaming response accumulation")

Do **not** store:
- One-off transient errors (network blip, single timeout)
- Obvious issues that any engineer would diagnose immediately
- Sensitive data (credentials, PII, internal URLs with tokens)

## Compacting Verbose Entries

If a slug grows unwieldy, compact it:

1. `load_memory(topic="<slug>")` to read the latest version
2. Review the content and remove:
   - Entries for issues that have been permanently fixed (look for references to merged PRs)
   - Duplicate or near-duplicate entries (consolidate into one)
   - Low-value entries (one-off transient errors, obvious issues)
3. `save_memory(topic="<slug>", content=<compacted body>)` — this allocates a new version under the same slug
