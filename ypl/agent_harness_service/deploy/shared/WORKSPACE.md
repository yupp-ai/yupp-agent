# Workspace Guide

**Only access files and directories within your workspace.** Do not read, write, or reference paths outside it.

## Directory Layout

```
├── .claude/          → read-only settings & hooks
├── .mcp.json         → MCP server config (generated at runtime)
├── agent_memories/   → persistent memory (writable, survives across sessions)
├── yupp-mind/        → read-only repo symlink
├── yupp-head/        → read-only repo symlink
├── yupp-soul/        → read-only repo symlink
├── yupp-agent/       → read-only repo symlink
├── yupp-mind-fix-bug-a1b2/  → writable worktree (created on demand)
├── attachments/      → downloaded attachments
└── history/          → session history
```

- Symlinked repos are **read-only reference copies**. Never edit or commit inside them.
- Worktree directories are your **working copies**, created by `request_write_access`.

## Repositories

**yupp-mind** — Python backend (FastAPI, PostgreSQL, Redis). LLM routing, chat, leaderboard, rewards, promotions.

**yupp-head** — Public web app (Next.js 16, React 19, TypeScript, Tailwind v4, Turborepo monorepo).

**yupp-soul** — Internal admin dashboard (Next.js 15, TypeScript, Tailwind, Shadcn/UI).

**yupp-agent** — Agent Cloud Platform. Agent harness infrastructure, agent configs, skills, and shared agent tooling.

## Code Change Workflow (Summary)

1. **Create a worktree**: `request_write_access(repo="yupp-mind", branch="ahs/{agent_name}/{work-name}")`
2. **Start GitHub auth** (optional): `authorize_github_user(session_id)` — kick off early so user can authorize while you work
3. **Make edits** inside the worktree directory, not the read-only symlink
4. **Create a PR**: `create_pr(session_id, title="...", body="...", draft=True)`

## Critical Rules

- **NEVER use `gh pr create` via Bash.** Use the `create_pr` MCP tool — it handles GitHub authentication so the PR is attributed to the requesting user. `gh pr create` via Bash uses the bot's credentials. This is blocked and will be rejected.
- **Never edit the read-only repo symlinks** (e.g., `yupp-mind/`). Changes there affect all sessions.
- **Always create PRs in draft mode** unless explicitly told otherwise.
- **Run lint before creating PRs**: `ruff format`, `ruff check --fix`, `mypy` on changed files only.

## Artifact Tracking

Every significant output you produce must be registered in the artifact registry using the `add_artifact` MCP tool. This ensures work is traceable and can be referenced by future sessions.

### When to call `add_artifact`

Valid `artifact_type` values: `YUPPASTE`, `CODE_REVIEW`, `OTHER`.

- ✅ After creating a yuppaste → `artifact_type: "YUPPASTE"`, url = the `go_link`
- ✅ After creating a pull request or code review → `artifact_type: "CODE_REVIEW"`, url = PR URL
- ✅ Any other trackable output → `artifact_type: "OTHER"`

### When to call `update_artifact`

- When a draft PR is promoted to ready-for-review (update title/description)
- When a yuppaste is superseded by a newer version (update the URL)
- When you add meaningful metadata to a previously registered artifact

### How to call the tools

```
# Register a new artifact
add_artifact(
    artifact_type="CODE_REVIEW",
    title="[AHS] Add artifact MCP tools",
    url="https://github.com/yupp-ai/yupp-mind/pull/12345",
    description="Adds add_artifact, update_artifact, list_artifacts MCP tools",
)

# Update an existing artifact
update_artifact(
    artifact_id="<uuid returned by add_artifact>",
    description="Updated description after PR review",
)
```

## Full Reference

For complete workflow docs — worktree naming, GitHub auth details, PR description format, commit authorship, sandbox restrictions, and lint tools — use the `/workspace-guide` skill.
