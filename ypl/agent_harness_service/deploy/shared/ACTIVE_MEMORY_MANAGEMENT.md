# Active Memory Management

Memories live as `MEMORY` artifacts in the artifact registry, addressed by **(scope, subject, slug)**:

| Scope   | Subject                  | Visibility                                            |
| ------- | ------------------------ | ----------------------------------------------------- |
| `agent` | your `agent_name`        | Your private notebook (default scope on every tool)   |
| `user`  | the caller's `user_id`   | The current user's notes — only they (and you, while serving them) can read them |
| `topic` | (none)                   | Globally shared — readable and writable by anyone     |

The DB is the source of truth; the `agent_memories/` directory in your sandbox is a materialized working copy seeded from the DB at session start (so `grep` / `cat` work without round-tripping the API).

## Before every task

Search what's already known:
```
search_memory(query="<task keywords>")
```
That covers your full visibility (your own agent + user + all topics). Narrow with `scope="topic"` when you only want shared knowledge, or `scope="agent"` for your own notes. To browse without a query, call `list_memory()` (or `list_memory(scope="topic")`).

To read a specific memory:
```
load_memory(topic="<slug>")              # defaults to scope="agent"
load_memory(topic="<slug>", scope="topic")
```

## After tasks

Record reusable learnings — write through to the DB; the disk copy is refreshed automatically:
```
save_memory(topic="<slug>", content="<markdown>")                # scope="agent" by default
save_memory(topic="<slug>", content="<markdown>", scope="topic") # team-shared
```
Each save creates a new version under the same `(scope, subject, slug)` — no compare-and-swap, no `expected_generation`, no GCS.

Cross-scope writes are rejected by the server: you cannot save into another agent's notebook or another user's notes.

For full docs on memory organization, write hygiene, and recording tool errors, use the `/memory-guide` Claude Code skill.
