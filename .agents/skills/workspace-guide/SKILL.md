---
name: workspace-guide
description: Full workspace and code-change workflow guide — worktrees, PRs, GitHub auth, directory layout, lint, sandbox restrictions. Trigger before calling request_write_access, create_pr, or committing code.
---

# Workspace Guide (Full Reference)

This skill expands on the workspace summary already in your system prompt. See that for directory layout, repo descriptions, and critical rules.

## Agent Memory (`agent_memories/`)

You have a **persistent, writable** memory directory at `agent_memories/`. Files here are private to you — they survive across sessions and are only visible to your future sessions. Use this for personal context (user preferences, past conversations, corrections, project notes).

See the `/memory-guide` skill for full details on both memory systems and when/how to use them.

## Worktree Details

Call `request_write_access` to create an isolated git worktree. The worktree appears in your workspace on your next turn.

```
request_write_access(repo="yupp-agent", branch={BRANCH_NAME})
```

Use `list_available_repos()` to see which repos support write access.

**Naming conventions:**

Every piece of work needs a **work name** — a short slug of at most 4 dash-separated words (e.g., `fix-url-typo`, `add-retry-logic`). The user may provide one directly.

| Item | Pattern | Example |
|---|---|---|
| Work name | ≤ 4 dash-separated words | `fix-url-typo` |
| Worktree directory | `{repo}-{work_name}` | `yupp-agent-fix-url-typo` |
| Branch name | `ahs/{agent_name}/{work_name}` | `ahs/sre/fix-url-typo` |

**Important:**
- Worktrees are per-session — each session gets its own isolated copy.
- You can create worktrees for multiple repos in the same session.
- The read-only symlinked repos remain available for reference.

## GitHub Authorization

PRs must be attributed to the user — bot-attributed PRs are not allowed. You can start the device-auth flow early so the user has time to complete it while you work:

```
authorize_github_user(session_id)
# → {"status": "pending", "verification_uri": "https://github.com/login/device", "user_code": "ABCD-1234"}
# → {"status": "already_authorized", "message": "..."} if user already has a valid token
```

Tell the user something like:

"To attribute the PR to you, visit https://github.com/login/device and enter this code:"

```
ABCD-1234
```

**Important:** Do NOT wrap URLs in asterisks (`*`) or other Markdown formatting — Slack will mangle the link. Display user codes in their own code block (triple backticks) so they are prominent and easy to copy.

Then continue working — don't wait for them to finish.

**Note:** This step is optional. If you skip it, `create_pr` will auto-initiate the device flow when needed.

## Commit Authorship

Once the user is authorized, use `check_github_auth_status` to get their GitHub identity for commits:

```
check_github_auth_status(session_id)
# → {"status": "authorized", "github_username": "janesmith", "github_name": "Jane Smith", "github_email": "jane@example.com"}

# Configure git in the worktree using the returned values
git config user.name "Jane Smith"
git config user.email "jane@example.com"
```

This ensures commits are attributed to the user, matching the PR attribution.

## PR Creation Details

If the user is NOT authorized when you call `create_pr`, the call fails and auto-initiates the device flow:

```json
{
  "status": "error",
  "error": "GitHub authorization required to create PRs.",
  "auth_required": "true",
  "verification_uri": "https://github.com/login/device",
  "user_code": "ABCD-1234",
  "expires_in_seconds": "899",
  "instructions": "Visit https://github.com/login/device and enter code: ABCD-1234",
  "next_step": "After the user completes authorization, call check_github_auth_status to verify, then retry create_pr."
}
```

Prompt the user to authorize using the format from the GitHub Authorization section (URL as plain text, code in a fenced block), then use `check_github_auth_status` to verify completion before retrying `create_pr`.

**PR description rules:**
- Keep diffs small and reviewable.
- **Title format**: `[Component] Imperative description` — prefix with a concise tag for quick context (e.g. `[AHS]`, `[Chat/Streaming]`, `[Feed]`, `[Router]`, `[Infra]`). Under 72 chars, sentence case, no period. Imperative mood ("Fix", "Add", not "Fixed", "Adds"). Append Linear ticket if known: `[AHS] Fix attribution (YUP-10649)`.
- **Always follow this body structure:**

  ```markdown
  🤖 *{agent_name}* for *{user_name}* · 📋 [{project_name} / {task_title}]({task_url})
  🔗 [Session]({lit_session_url}) · [Slack]({slack_thread_url})

  **TL;DR** [{Component}] One sentence: the problem and the solution. {links to Linear ticket, design doc, agent project/task, Slack thread, related PRs — whichever are known}

  ## Problem

  Why this change is needed. What's broken, missing, or suboptimal.

  ## Solution

  Overview paragraph, then sub-sections if multi-part.

  ## Notes
  <!-- optional — only if relevant -->
  - ⚠️ DB migration / breaking change / manual step
  - Drive-by fixes

  ## Evidence
  <!-- optional — for investigation PRs -->
  http://go/p/{slug}
  ```

  - Attribution: always include agent name + user. Append project/task link if tied to a Yuppster project task.
  - **Task URL format**: `https://lit.agcouch.com/agent_projects?project_id={project_id}&task_id={task_id}` — use the project ID and task ID from your session context. Do NOT invent other domains or URL patterns.
  - Session link format: `https://lit.agcouch.com/agent_harness_console?session_id={session_id}`. Append Slack thread if applicable.
  - TL;DR: one sentence + all known context links. Reviewer should get the gist from this alone.
  - Do NOT include a test plan unless explicitly requested.
  - Do NOT include `🤖 Generated with Claude Code` or `Co-Authored-By` lines.
  - Use emoji sparingly — only for attribution and ⚠️/🔧 warnings in Notes.

## Sandbox Restrictions

The sandbox grants limited write access inside shared repos' `.git/` directories (only `objects/`, `refs/`, `worktrees/`, `logs/`). Do not modify any `.git/` content outside your own worktree — never touch `config`, `hooks/`, `info/`, `packed-refs`, or another agent's `worktrees/` entry. Never run `git gc`, `git pack-refs`, or similar repo-maintenance commands on shared repos.

## Lint Tools

Before creating a PR for Python code, **always run lint checks** on your changed files:

```bash
# Format code
ruff format <changed_files>

# Lint (auto-fix where possible)
ruff check <changed_files> --fix

# Type check
mypy --config-file=pyproject.toml <changed_files>
```

`ruff` and `mypy` are available in every sandboxed session. Always invoke them by bare command name — never hardcode an absolute path. Use the repo's `pyproject.toml` for configuration (ruff rules, mypy settings).

**Run lint only on files you changed** — not the entire repo. Fix any errors before committing.

For the TypeScript/Next.js code under `apps/war-room/`, use `pnpm biome check` instead.

## Working Style

- Use structured logging. Avoid print statements.
- Write type-annotated Python 3.12.
- When creating PRs, keep diffs small and reviewable.
- If you need write access to a repo, use the `request_write_access` tool.
- If you're unsure about something, ask rather than guess.
