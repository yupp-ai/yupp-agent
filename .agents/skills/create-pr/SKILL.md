---
name: create-pr
description: "Create a GitHub pull request with a standardized description format. Use when creating PRs for the yupp-agent repo. Enforces consistent title prefixes, attribution, TL;DR, Problem/Solution structure, and notes. Usage: /create-pr [base_branch]"
---

# Create Pull Request

Create a GitHub PR with a standardized description following the team conventions.

## Arguments

- `$ARGUMENTS` — optional base branch (default: `main`)

## PR Title Format

```
[Component] Imperative description of the change
[Component/Sub] Imperative description of the change
```

Rules:
- **Always** prefix with a `[Component]` tag that gives quick context. Use `/Sub` for specificity.
- The tag doesn't need to be from a canonical list — just be concise and recognizable. Examples: `[AHS]`, `[Chat/Streaming]`, `[Feed]`, `[Router]`, `[Leaderboard]`, `[SAG]`, `[Infra]`, `[DB]`, `[ModelPicker]`, `[Skills]`.
- Keep total length under 72 characters. Sentence case after the tag. No trailing period.
- Imperative mood: "Fix", "Add", "Remove", "Refactor" — not "Fixed", "Adds", "Adding".
- If a Linear ticket is known, append it: `[AHS] Fix session attribution (YUP-10649)`
- For multi-part series, add a sequence number: `[BotFather][2] Add core logic`
- If the change spans multiple components, use the primary one. Mention others in the TL;DR.

## PR Description Format

### Attribution Line

Always start with an attribution line. Pick the appropriate format:

**When Claude (interactive session with a human) creates the PR:**
```
🤖 Created by *{agent_tool_name}* on behalf of *{human_github_username}*
```
or, if the model name is known:
```
🤖 Created by *{agent_tool_name} ({model_name})* on behalf of *{human_github_username}*
```
Examples: "Claude Code", "Claude Code (Opus 4.6)", "Claude Code (Sonnet 4.6)".

**When an AHS agent creates the PR:**
```
🤖 *{agent_name}* for *{human_name}* · 📋 [{project} / {task}]({task_url})
🔗 [Session]({lit_session_url}) · [Slack]({slack_thread_url})
```

**When a human creates the PR themselves (and you're just formatting):**
```
👤 Created by *{human_github_username}*
```

### TL;DR Line

One line, immediately after attribution. Links to all known context.

```
**TL;DR** [{Component}] One sentence describing the problem and the solution. {context_links}
```

Context links — include whichever are known, omit the rest:
- Linear ticket: `[YUP-XXXXX](https://linear.app/...)`
- Design doc / artifact: `[Design doc](http://go/p/...)`
- Agent project: `[Project](https://example.com/projects/...)`
- Agent task: `[Task](https://example.com/tasks/...)`
- Slack thread: `[Slack thread](https://agentic-couch.slack.com/...)`
- Lit session: `[Session](https://lit.example.com/agent_harness_console?session_id=...)`
- Related PR: `#1234`

### Problem Section

```markdown
## Problem

Why this change is needed. What's broken, missing, or suboptimal.
Include evidence: error messages, metrics, user reports, screenshots.
Link to the investigation artifact if this PR originated from an oncall alert.
```

### Solution Section

```markdown
## Solution

What this PR does — one paragraph overview first.

### {Sub-section for a specific part of the change}
Details if the change has multiple logical parts.

### {Another sub-section}
Keep it concise. Bullet points are fine.
```

### Notes Section (optional)

Only include if there's something worth calling out. Use emoji sparingly — only for genuinely important warnings.

```markdown
## Notes
- ⚠️ DB migration: adds index on `turns.created_at` (concurrent, no lock)
- ⚠️ Breaking: removes deprecated `/v1/old-endpoint`
- 🔧 Manual step: add `NEW_SECRET` to GCP secrets
- Drive-by: fixed typo in logging message
```

### Evidence Section (optional, for investigation PRs)

```markdown
## Evidence
http://go/p/{slug}
```

### Test Plan (only when explicitly requested by human)

Do NOT include a test plan section by default. Only add it when the human explicitly asks.

```markdown
## Test plan
- [ ] Step 1
- [ ] Step 2
```

## Steps

1. **Determine what changed**: Run `git log` and `git diff` against the base branch to understand all commits.

2. **Identify the component**: Match the primary area of change to the component vocabulary. If unsure, look at which directories were modified.

3. **Gather context links**: Check if any of these are available in the conversation or can be inferred:
   - Linear ticket IDs (from branch name, commit messages, or conversation)
   - Design docs or artifact links discussed in the session
   - Agent project/task IDs (from AHS session context)
   - Slack thread links (from investigation context)
   - Related PRs (from git log or conversation)

4. **Draft the title**: `[Component] Imperative description`

5. **Draft the description**: Follow the format above. Be concise — the goal is for a reviewer to understand the *why* and *what* in under 60 seconds.

6. **Create the PR** using the `create_pr` MCP tool:
   ```python
   create_pr(session_id, title="[Component] Description", body="...", draft=True)
   ```

   **IMPORTANT:** Always use the `create_pr` MCP tool when available. Do not use `gh pr create` directly — the MCP tool handles authentication, user attribution, and draft mode correctly.

   If the user is not authorized, the tool will return an `auth_required` response with a device code. Prompt the user to authorize by presenting the device code in a fenced code block for easy copying:

   > Please go to https://github.com/login/device and enter the following code:
   > ```
   > YOUR_DEVICE_CODE
   > ```

   Then use `check_github_auth_status` to programmatically verify authorization before retrying `create_pr`.

7. **Report the PR URL to the user — always.** When `create_pr` returns `{"status": "created", "pr_url": "..."}`, your very next user-facing message MUST surface that URL as a clickable link. Users don't see tool results, so silently finishing the turn after `create_pr` leaves them unaware the PR exists. Treat this as part of the `create_pr` call itself, not an optional follow-up.

   - **Slack:** `Done — PR <{pr_url}|#{number} {title}> (draft)`
   - **Markdown:** `Done — PR [#{number} {title}]({pr_url}) (draft)`

   The `add_artifact` registration is for tracking — it does not replace announcing the URL to the human.

## Rules

- **Do not use `gh pr create` when `create_pr` is available** — always use the `create_pr` MCP tool, which handles authentication, user attribution, and proper PR creation.
- Do NOT include `🤖 Generated with Claude Code` or `Co-Authored-By` lines.
- Do NOT include a test plan unless the human explicitly asks.
- Do NOT over-use emoji. Only use them in the attribution line and for ⚠️/🔧 warnings in Notes.
- Keep the overall description scannable. A reviewer should get the gist from the TL;DR alone.
- For trivial changes (typo fixes, config bumps), the TL;DR line alone is sufficient — skip Problem/Solution sections.

## Examples

### Minimal (trivial change)
```
Title: [Infra] Bump ruff to 0.8.2

Body:
🤖 Created by *Claude Code (Opus 4.6)* on behalf of *wangtian24*

**TL;DR** [Infra] Bump ruff from 0.7.9 to 0.8.2 to pick up new lint rules.
```

### Standard
```
Title: [AHS] Fix session attribution to use session user

Body:
🤖 Created by *Claude Code (Opus 4.6)* on behalf of *wangtian24*

**TL;DR** [AHS] Fix project/resource creator attribution — was using token owner instead of session user. [TEAM-123](https://linear.app/your-workspace/issue/TEAM-123)

## Problem

When AHS creates projects or resources on behalf of a user, the creator was attributed
to the service account token owner rather than the actual session user. This meant all
agent-created work appeared to come from the same bot account.

## Solution

Read the `X-AHS-User-ID` header in the project and resource creation endpoints and use
it as the creator ID when present, falling back to the token owner otherwise.

### Endpoint changes
- `POST /projects` — reads `X-AHS-User-ID` header
- `POST /resources` — same

## Notes
- Drive-by: removed unused `get_token_owner()` helper
```

### Investigation PR
```
Title: [Chat/Streaming] Fix retry loop on provider timeout

Body:
🤖 Created by *Claude Code (Opus 4.6)* on behalf of *wangtian24*

**TL;DR** [Chat/Streaming] Provider timeouts caused infinite retry loop crashing workers. [Slack thread](https://yupp-ai.slack.com/archives/C0123/p1234)

## Problem

Streaming completions from provider X would occasionally timeout after 30s.
The retry logic had no max-retry cap, causing the worker to loop indefinitely
and eventually OOM. First reported via oncall alert at 2026-03-15 02:30 UTC.

## Solution

Add a max retry count (3) and exponential backoff to the streaming retry loop.
Log a structured error on final failure instead of silently retrying.

## Notes
- ⚠️ Breaking: requests that previously retried forever will now fail after 3 attempts

## Evidence
http://go/p/streaming-timeout-investigation
```
