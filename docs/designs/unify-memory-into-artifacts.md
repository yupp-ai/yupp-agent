# Unify Agent Memory into Artifacts

**Author:** Tian (with Claude)
**Date:** 2026-04-22
**Status:** Draft v2 (scoping model + reduced scope)

## TL;DR

Collapse the agent-memory subsystem into the artifact subsystem as a new
`MEMORY` artifact type. Memory content lives directly in Postgres — DB is the
source of truth. The agent sandbox keeps a local `agent_memories/` directory
as a **materialized working copy** (seeded from DB on session start, written
through to DB on save) so agents can `grep` cheaply without round-tripping
the API each lookup. No GCS involvement at all.

Access control is real: memory is scoped as **user**, **agent**, or **topic**,
enforced at the DB via scope columns — not by parsing the slug. For a session
where user X is working with agent Y, readable memory is
`scope=topic` ∪ `scope=user ∧ subject=X` ∪ `scope=agent ∧ subject=Y`.

**Not in this project (deferred to a follow-up):** re-implementing the
section-level hybrid/semantic/keyword search on the new MEMORY artifacts,
and dropping the legacy `agent_memory_sections(+_embeddings)` tables. This
project lands the storage/REST/viewer unification; the follow-up lands
search.

## Why now

The Streamlit `agent_memory_viewer` needs to stop reading GCS + Postgres
directly — it should talk to a REST API. Before building a parallel
`/ahs/memory/*` API, we should decide whether memory is actually a distinct
primitive. It isn't. The only reasons we have a separate subsystem today
are historical (it started as plain files) and that memory carries scope
semantics (per-user, per-agent) that the current artifact surface doesn't
model. Both are fixable without keeping two parallel stacks.

## Today: three places, triple-write

```
agent workdir            GCS                            Postgres
agent_memories/          gs://yupp-agents/              agent_memory_sections
  shared/…               agent_memory/                    topic, section_key
  private/{agent}/…      {agent_name}/…                   content, tsvector
                                                         agent_memory_section_embeddings
```

Write path: agent writes markdown file in sandbox → manifest-based rsync to
GCS → indexer walks changed files, splits into sections, upserts Postgres
with topic string `PRIVATE/{agent_name}/{stem}`, regenerates embeddings.
Two manifest files, three representations of the same bytes, and a symlink
dance between the per-session sandbox and the persistent memory directory.
This is the "symlink shit" we're here to kill.

Meanwhile, artifacts already do this cleanly: Postgres metadata + pluggable
BlobStore + full REST (`/ahs/artifacts/*`) + slug-based versioning +
soft-archive.

## Design

### Storage

Memory = `AgentArtifactType.MEMORY` artifacts with content stored **inline**
in Postgres. Two changes to `agent_artifacts`:

1. `inline_content TEXT NULL` — the markdown body.
2. CHECK: exactly one of `inline_content` / `url` is set. Memory uses
   `inline_content`; other artifact types keep `url` + BlobStore.

Content cap: 1 MiB (same as existing artifact content cap).

### Scoping

Two new columns on `agent_artifacts`, only populated when
`artifact_type = 'MEMORY'`:

```
memory_scope           text   -- 'user' | 'agent' | 'topic'
memory_scope_subject   text   -- users.user_id | agents.agent_name | NULL
```

CHECK constraints:

```sql
-- Memory artifacts must have a valid scope.
CHECK (
  (artifact_type = 'MEMORY' AND memory_scope IN ('user','agent','topic'))
  OR (artifact_type <> 'MEMORY' AND memory_scope IS NULL
      AND memory_scope_subject IS NULL)
)

-- user/agent scopes require a subject; topic scope forbids one.
CHECK (
  memory_scope IS NULL
  OR (memory_scope = 'topic' AND memory_scope_subject IS NULL)
  OR (memory_scope IN ('user','agent') AND memory_scope_subject IS NOT NULL)
)
```

Uniqueness (replaces global slug uniqueness for MEMORY):

```sql
CREATE UNIQUE INDEX uix_memory_scope_slug_version
  ON agent_artifacts (memory_scope, memory_scope_subject, named_slug, version)
  WHERE artifact_type = 'MEMORY'
    AND named_slug IS NOT NULL AND version IS NOT NULL;
```

Index for scope-filtered reads:

```sql
CREATE INDEX ix_memory_scope_subject
  ON agent_artifacts (memory_scope, memory_scope_subject)
  WHERE artifact_type = 'MEMORY';
```

**Subject identity:**

- `memory_scope='user'` → subject is `users.user_id` (the stable text PK in
  the `users` table). **Not** Slack user ID, not session user. If the same
  human shows up under two `user_id`s, that's a user-table problem, not a
  memory problem.
- `memory_scope='agent'` → subject is `agents.agent_name` (stable string
  identifier, e.g. `eng-raccoon`).
- `memory_scope='topic'` → subject is NULL. Topics are globally readable
  and globally writable (today's behavior).

### Slug and display form

The **slug** (`named_slug`) is just the title — e.g., `user_preferences`,
`routing_tips`, `feedback_style`. No scope prefix in the stored value.

The **display form** for humans and logs reconstructs the full address:

| scope   | display                             | example                               |
| ------- | ----------------------------------- | ------------------------------------- |
| user    | `u:{user_id}:{slug}`                | `u:USR_7f2a:user_preferences`         |
| agent   | `a:{agent_name}:{slug}`             | `a:eng-raccoon:feedback_style`        |
| topic   | `t:{slug}`                          | `t:routing_tips`                      |

The `u:/a:/t:` prefix is **illustrative**, not stored — it's generated on
the fly from scope columns. The viewer, MCP tool return values, and logs
use this form; the DB stores only the structured columns.

### Access control

Authz is enforced at the REST / MCP layer using the caller context:

- `caller.user_id` — stable user_id the caller is acting on behalf of
  (from the session's `user_id` field).
- `caller.agent_name` — the agent making the call.

**Read filter** (applied to every MEMORY list/search/read):

```sql
WHERE artifact_type = 'MEMORY' AND (
    memory_scope = 'topic'
 OR (memory_scope = 'user'  AND memory_scope_subject = :caller_user_id)
 OR (memory_scope = 'agent' AND memory_scope_subject = :caller_agent_name)
)
```

**Write rules:**

| scope  | subject     | can write                                            |
| ------ | ----------- | ---------------------------------------------------- |
| user   | X           | caller must have `user_id = X`                       |
| agent  | Y           | caller must have `agent_name = Y`                    |
| topic  | —           | anyone authenticated (unchanged from today)          |

Agent Y working with user X may write `u:X:*`, `a:Y:*`, and any topic.
Agent Y can **not** write `u:Z:*` (another user's) or `a:W:*` (another
agent's).

Enforcement lives in the REST route and MCP tool layer. The DB CHECK
constraints handle shape; route handlers handle identity.

### Disk as materialized working copy

Agents already read `agent_memories/*.md` on disk to inject into prompts
and to grep during a turn. We keep that convenience, but disk is now a
**cache**, not a source of truth:

- **On session start:** for agent Y acting on behalf of user X, materialize
  into the sandbox:

  ```
  agent_memories/
    topic/            ← all MEMORY artifacts with scope=topic
      {slug}.md
    user/             ← scope=user AND subject=X
      {slug}.md
    agent/            ← scope=agent AND subject=Y
      {slug}.md
  ```

- **On save (agent writes a file):** the write-through path is
  `POST /ahs/artifacts` with scope derived from the path prefix
  (`topic/`, `user/`, `agent/`). DB commit must succeed before the write
  is considered durable. If the API call fails, the disk write is reverted.

- **No background GCS sync, no manifest files, no symlinks.** The session
  ends, the sandbox is torn down, and nothing is lost because the DB has
  it.

This pattern gives agents cheap grep / file-read access during a turn
without inventing a new API, while keeping the DB as the single source of
truth across sessions.

### Versioning

Every save of a memory creates a new version of the slug (same mechanism
as existing artifacts). Readers see the latest non-archived version. The
unique index `(memory_scope, memory_scope_subject, named_slug, version)`
allows parallel saves across different scopes/subjects without collision,
while keeping each (scope, subject, slug) sequence monotonic.

Last-write-wins at the (scope, subject, slug) level. Strictly better than
today's GCS mtime race.

Version cleanup (keep latest N per slug) is out of scope for this project
— we'll look at disk/DB usage after a few weeks and add a cleanup job if
needed.

### REST surface

All via the existing `/ahs/artifacts` router:

| Operation                          | Endpoint                                                     |
| ---------------------------------- | ------------------------------------------------------------ |
| List my memories                   | `GET /ahs/artifacts?type=MEMORY&scope={user\|agent\|topic}`  |
| Read current content               | `GET /ahs/artifacts/by-slug/{slug}?scope=...&subject=...`    |
| Version history                    | `GET /ahs/artifacts/by-slug/{slug}/versions?scope=...&...`   |
| Read a specific version            | `GET /ahs/artifacts/{id}`                                    |
| Create / update (new version)      | `POST /ahs/artifacts` (body specifies `memory_scope`, etc.)  |
| Substring search (title + content) | `GET /ahs/artifacts/search?q=…&type=MEMORY`                  |
| Archive (soft-delete)              | `DELETE /ahs/artifacts/by-slug/{slug}?scope=...&subject=...` |

Every MEMORY-touching route applies the scope read filter above. A caller
who asks for `scope=user&subject=OTHER` gets 403 (or a filtered-out 404).

No `/sections/search` endpoint in this project — section-level search is
the follow-up project.

### Write path from agents

The existing MCP tools stay stable from the agent's point of view:

```python
save_memory(topic="user_preferences", content="...", scope="user")
load_memory(topic="user_preferences", scope="user")
```

Under the hood:

```
agent → MCP save_memory(topic, content, scope?)
      → POST /ahs/artifacts
         {
           type: MEMORY,
           memory_scope: <scope>,
           memory_scope_subject: <user_id | agent_name | null>,
           named_slug: <topic>,
           inline_content: <content>
         }
      → artifact row (new version)
```

Default scope when the agent doesn't specify: `agent` (the agent's own
notebook). Explicit scope required for `user` and `topic`.

The agent's sandbox also sees the write reflected in
`agent_memories/{scope}/{slug}.md` — this happens by writing through the
tool, not by a separate sync.

### Read path

- **Streamlit viewer** (`agent_memory_viewer.py`): rewritten to call REST.
  Sidebar groups by scope (`Topics`, `Users`, `Agents`). Clicking a user or
  agent reveals that subject's memories. Authenticated via the existing
  service API key; the viewer has admin-level read access (it shows
  everything — that's the point of the viewer).

- **Agent preload (ROLE.md style):** session start materialization (see
  above) IS the preload. The ROLE.md can also reference specific slugs via
  `{{memory:topic:routing_tips}}` placeholders if we want finer control
  later; not needed for v1.

- **Agent on-the-fly:** agent reads files under `agent_memories/` directly
  in the sandbox (grep, cat, etc.). Or calls `load_memory` / `search_memory`
  MCP tools for explicit lookups.

### Removed from the codebase (in this project)

- `ypl/agent_harness_service/core/memory_persistence.py` — deleted.
- `AHS_GCS_MEMORY_BUCKET`, `AHS_GCS_MEMORY_PREFIX` constants — retired.
  `AHS_MEMORIES_DIR` stays but now refers to the sandbox working-copy root.
- `PRIVATE_TOPIC_PREFIX = "PRIVATE"` — gone; scope is a column now.
- `.gcs_memory_sync_manifest.json`, `.private_memory_index_manifest.json`
  — gone.
- Symlink setup from session sandboxes into a shared persistent memory
  dir — gone (each session materializes fresh from DB).
- `ypl/backend/llm/agent_memory_search.py` (stub) stays — the follow-up
  project replaces it.

## Migration plan

**PR [1] — schema (DB-only, standalone per the repo rule):**

1. Alembic autogen revision:
   - Add `MEMORY` to `AgentArtifactType` enum.
   - Add `inline_content TEXT` column.
   - Add `memory_scope TEXT` and `memory_scope_subject TEXT` columns.
   - CHECK: `inline_content` / `url` are mutually exclusive.
   - CHECK: scope/subject validity (see schema above).
   - Unique index on `(memory_scope, memory_scope_subject, named_slug, version)` WHERE type=MEMORY.
   - Index on `(memory_scope, memory_scope_subject)` WHERE type=MEMORY.
2. Ship in a standalone PR. No code changes.

**PR [2] — REST + inline content + scope authz:**

1. `artifact_store.py` + `artifact_routes.py` accept and return
   `inline_content`, `memory_scope`, `memory_scope_subject`.
2. All MEMORY-touching routes apply the caller-context scope filter.
3. MCP tools (`save_memory`, `load_memory`, `search_memory`) thread
   caller context (`user_id`, `agent_name`) and reject cross-scope writes.
4. Tests: cross-user isolation, cross-agent isolation, topic visibility,
   write-authz rejection.

**PR [3] — writer swap (DB-authoritative, disk as working copy):**

1. On session start: materialize `agent_memories/{scope}/{slug}.md` from DB
   (topic + user's + agent's).
2. On memory save: write-through to DB via REST; disk reflects the commit.
3. Remove GCS sync from the turn-end pipeline.
4. Feature-flag gated (default on new path for staging → prod).
5. **Human checkpoint:** flag flip in prod.

**PR [4] — Streamlit viewer rewrite:**

1. `agent_memory_viewer.py` uses `/ahs/artifacts` REST. No direct GCS / DB
   access.
2. Sidebar groups by scope; subject selector for user / agent.
3. Search uses existing `/artifacts/search` endpoint (substring; section
   search lands in the follow-up project).

**PR [5] — cleanup:**

1. Delete `memory_persistence.py`, `PRIVATE_TOPIC_PREFIX`, GCS sync
   helpers, symlink setup, manifest files.
2. Remove the feature flag.
3. `agent_memory_sections` + embeddings tables stay (follow-up project
   drops them after porting search).

## Risks and open questions

1. **Scope subject drift.** If `users.user_id` ever changes for a human
   (merge accounts, re-key), their user memory loses its anchor. This is a
   user-table issue, not a memory one; document and live with it.
2. **Topic write contention.** Anyone can write topics. Last-write-wins.
   Fine for today's volume.
3. **Session start latency.** Materializing N files from DB at session
   start adds a bit of boot time. Expected to be ≤100ms for typical agent
   + user memory sizes. If it balloons, we lazy-load.
4. **Disk write reverted on API failure.** Need to be careful about the
   sequence so a failed API call doesn't leave a ghost file in the
   sandbox. Write to temp → API call → rename on success.
5. **Session contains multiple users?** Not in scope. One session, one
   `user_id`, one `agent_name`.
6. **Search.** The existing hybrid/semantic/keyword search is a stub in
   yupp-agent (full impl in yupp-mind). This project does NOT re-implement
   it. The follow-up project owns that plus the drop of the old section
   tables.

## Non-goals (this project)

- Section-level hybrid/semantic/keyword search.
- Porting the full search implementation from yupp-mind.
- Dropping `agent_memory_sections(+_embeddings)` tables.
- Populating embeddings for MEMORY artifacts.
- Project-scoped memory as a new scope (project/task tables already carry
  that data).
- Migrating `TEXT` / `CODE_REVIEW` artifacts to inline storage.
- Attachments on memory artifacts.

## Appendix: shape cheat sheet

```python
# Writing (agent-facing, scope optional; defaults to 'agent')
save_memory(topic="user_preferences", content="...", scope="user")

# Under the hood
POST /ahs/artifacts
{
  "type": "MEMORY",
  "memory_scope": "user",
  "memory_scope_subject": "USR_7f2a",       # caller's user_id
  "named_slug": "user_preferences",
  "inline_content": "...",
  "content_type": "text/markdown",
  "title": "user_preferences"
}

# Reading by slug
GET /ahs/artifacts/by-slug/user_preferences?scope=user&subject=USR_7f2a
→ 200  Content-Type: text/markdown
       <markdown body>

# Listing my memories
GET /ahs/artifacts?type=MEMORY     # default: returns my agent + my user + all topics
GET /ahs/artifacts?type=MEMORY&scope=topic
GET /ahs/artifacts?type=MEMORY&scope=user   # just mine
GET /ahs/artifacts?type=MEMORY&scope=agent  # just mine

# Display forms (logs / viewer / MCP return values)
u:USR_7f2a:user_preferences
a:eng-raccoon:feedback_style
t:routing_tips
```
