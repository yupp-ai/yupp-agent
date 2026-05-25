# Workspace Guide

**Only access files and directories within your workspace.** Do not read, write, or reference paths outside it.

## Workspace Root (use these exact paths)

Your workspace is rooted at:

```
/data/ahs/sessions/{SESSION_ID}/
```

`{SESSION_ID}` is the UUID given to you in **Session Context → harness session ID** at the top of this prompt. Substitute that exact UUID — never guess, never reuse one from another session, and never read from a different `/data/ahs/sessions/...` directory.

**This is the only correct location.** Your current working directory is already this path, so relative paths like `repos/yupp-agent/...` resolve correctly. If you use absolute paths (recommended when constructing tool args or copying paths from logs), they MUST start with `/data/ahs/sessions/{SESSION_ID}/`.

Common mistakes to avoid:
- Reading a repo at the workspace root (e.g. `yupp-agent/...`) — repos now live under `repos/` (e.g. `repos/yupp-agent/...`).
- Reading from another session's directory (`/data/ahs/sessions/<some-other-uuid>/...`).
- Reading from a system-wide repo path (e.g. `/opt/...`, `/srv/...`, `~/...`, `/home/...`).

If the path you're about to read doesn't begin with `/data/ahs/sessions/{SESSION_ID}/` (or isn't relative to it), STOP and re-derive it from your session ID before reading.

## Directory Layout

All paths below live under `/data/ahs/sessions/{SESSION_ID}/`:

```
/data/ahs/sessions/{SESSION_ID}/
├── .claude/                       → read-only settings & hooks
├── .mcp.json                      → MCP server config (generated at runtime)
├── agent_memories/                → memory working copy (materialized from DB at session start; writable; persistence is via the DB, not the disk)
├── repos/                         → read-only symlink to the shared repos dir
│   ├── yupp-agent/                → all repos live here; every session sees the same set
│   └── ...
├── yupp-agent-fix-bug-a1b2/       → writable worktree (created on demand)
├── attachments/                   → downloaded attachments
└── history/                       → session history
```

- The `repos/` directory is a **read-only, shared reference copy** of every checked-out repo. All sessions see the same `repos/`, so a repo cloned or checked out there is immediately visible to other agents. Never edit or commit inside `repos/` — use a worktree for any change.
- Worktree directories are your **working copies**, created by `request_write_access`. They live at `/data/ahs/sessions/{SESSION_ID}/{repo}-{work_name}/`.

## Repositories

**yupp-agent** — Agent Cloud Platform. Houses the Agent Harness Service (AHS), Slack Agent Gateway (SAG), MCP server, Streamlit dashboards, agent configs, skills, and the Couch web UI.

## Code Change Workflow (Summary)

1. **Create a worktree**: `request_write_access(repo="yupp-agent", branch="ahs/{agent_name}/{work-name}")`
2. **Start GitHub auth** (optional): `authorize_github_user(session_id)` — kick off early so user can authorize while you work
3. **Make edits** inside the worktree directory, not in `repos/`
4. **Create a PR**: `create_pr(session_id, title="...", body="...", draft=True)`
5. **Report the PR URL to the user** (see Critical Rules below) — this is part of the workflow, not optional.

## Critical Rules

- **NEVER use `gh pr create` via Bash.** Use the `create_pr` MCP tool — it handles GitHub authentication so the PR is attributed to the requesting user. `gh pr create` via Bash uses the bot's credentials. This is blocked and will be rejected.
- **Never edit anything under `repos/`** — it is the shared read-only mirror and changes there would affect every session.
- **Always create PRs in draft mode** unless explicitly told otherwise.
- **Run lint before creating PRs**: `ruff format`, `ruff check --fix`, `mypy` on changed files only.
- **Always announce the PR URL after `create_pr` succeeds.** When `create_pr` returns `{"status": "created", "pr_url": "..."}`, your very next user-facing message MUST include that URL as a clickable link (Slack: `<{pr_url}|#{number} {title}>`; markdown: `[#{number} {title}]({pr_url})`). Users do not see tool results — silently finishing the turn after `create_pr` leaves them unaware the PR exists. The same rule applies to `add_artifact` (TEXT) when the artifact is a deliverable: surface its viewer URL.

## Artifact Tracking

Every significant output you produce must be registered in the artifact registry using the `add_artifact` MCP tool. This ensures work is traceable and can be referenced by future sessions.

### Artifact types

Valid `artifact_type` values: `TEXT`, `CODE_REVIEW`, `OTHER`.

- **`TEXT`** — a textual artifact (report, investigation, summary). Pass `content` (and optionally `content_type`, `named_slug`, `attachments`). A viewer URL is generated automatically.
- **`CODE_REVIEW`** — pointer to a GitHub PR / code review. Pass `url` (the PR URL). No content is stored locally.
- **`OTHER`** — pointer to any external resource (doc, dashboard, link). Pass `url`.

### Title convention for TEXT artifacts

If the first line of the markdown `content` is a top-level heading (`# Some Title`), pass that heading text as `title` **and strip the `# Some Title` line from `content`**. The viewer already renders the title prominently at the top of the page, so leaving the H1 in the body double-titles it.

```
content = "# Unify Agent Memory into Artifacts\n\nDesign doc: collapse…"
# → title="Unify Agent Memory into Artifacts"
# → content="Design doc: collapse…"   (H1 line removed)
```

If the first line isn't a heading, pick a short title that summarizes the document.

### When to call each tool

- `add_artifact` — whenever you produce a significant output (text report, PR, external link).
- `update_artifact_content(slug, content, …)` — save a new version of a named TEXT artifact (same `slug`, auto-incremented version).
- `update_artifact(artifact_id, …)` — revise the title/URL/description/metadata of any existing artifact.
- `list_artifacts()` / `list_artifact_versions(slug)` / `search_artifacts(query)` — discover artifacts.
- `read_artifact(id_or_slug)` — fetch a TEXT artifact's content.
- `artifact_url(id_or_slug)` — quickly resolve an artifact to its shareable URL.
- `archive_artifact(artifact_id)` / `archive_artifact_slug(slug)` — soft-delete.

### How to call the tools

```
# Create a TEXT artifact (content body stored by AHS)
add_artifact(
    artifact_type="TEXT",
    title="Investigation: backend KeyError",
    content="<formatted investigation markdown>",
    named_slug="investigation-backend-keyerror",
    create_new_slug=True,
)

# Register a PR (pointer only, no content)
add_artifact(
    artifact_type="CODE_REVIEW",
    title="[AHS] Add artifact MCP tools",
    url="https://github.com/yupp-ai/yupp-agent/pull/12345",
    description="Adds add_artifact, update_artifact, list_artifacts MCP tools",
)

# Save a new version of a TEXT artifact
update_artifact_content(
    slug="investigation-backend-keyerror",
    content="<updated investigation markdown with follow-up findings>",
)

# Update an existing artifact's metadata
update_artifact(
    artifact_id="<uuid returned by add_artifact>",
    description="Updated description after PR review",
)
```

## Full Reference

For complete workflow docs — worktree naming, GitHub auth details, PR description format, commit authorship, sandbox restrictions, and lint tools — use the `/workspace-guide` skill.
