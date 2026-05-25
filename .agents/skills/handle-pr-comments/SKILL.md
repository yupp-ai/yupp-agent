---
name: handle-pr-comments
description: >-
  Review a GitHub PR, address all review comments (AI and human) and all CI
  failures (ruff, mypy, biome, tests), rebase onto main, reply to each comment
  with a verdict, resolve threads, and produce a summary. Runs fully
  automatically with no human confirmation steps. Usage: /handle-pr-comments <PR_URL_OR_NUMBER>
---

# Address PR Comments

## Usage

```
/handle-pr-comments https://github.com/yupp-ai/yupp-agent/pull/10572
/handle-pr-comments 10572
/handle-pr-comments stack    # Handle comments on all PRs in the current Graphite stack
```

## Input Parsing

If the argument is `stack`, enter **Stack Mode** (see Phase 9 below).

Otherwise, extract the PR number from the argument. Accept either:
- A full URL: `https://github.com/yupp-ai/yupp-agent/pull/<NUMBER>`
- A bare number: `<NUMBER>`

Store as `<PR_NUMBER>` for use throughout.

---

## Workflow Preference Detection

Before starting, check the user's `~/.claude/CLAUDE.md` for git workflow preferences. Look for indicators like:
- "use Graphite" / "prefer gt" / "use gt instead of git"
- "NEVER use git fetch, git pull" / "use gt sync"

Store as `USE_GRAPHITE` (true/false). **Default to false (use git)** if no preference is found.

| Preference | Sync Command | Restack Command | Push Command |
|------------|--------------|-----------------|--------------|
| git (default) | `git fetch origin main` | `git rebase origin/main` | `git push origin <branch> --force-with-lease` |
| Graphite | `gt sync --no-interactive` | `gt restack --no-interactive` | `gt submit --no-interactive` |

Use the appropriate commands throughout Phases 5 and 9 based on this preference.

---

## Phase 0: Automation Level

This skill runs **fully automatically**: Fix → Commit → Rebase → Push → Reply & Resolve — no human confirmation at any step.

---

## Phase 1: Gather Context

### 1.1 Get PR metadata

```bash
gh pr view <PR_NUMBER> --json title,body,headRefName,baseRefName,author,state,mergeable,labels,reviewDecision
```

### 1.2 Checkout the PR branch

**If `USE_GRAPHITE` is true:**
```bash
gt sync --no-interactive
gt checkout <headRefName>
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin
git checkout <headRefName>
git pull origin <headRefName>
```

### 1.3 Identify changed files

```bash
gh pr diff <PR_NUMBER> --name-only                                    # all changed files
gh pr diff <PR_NUMBER> --name-only | grep '\.py$'                     # → PYTHON_FILES (for ruff/mypy)
gh pr diff <PR_NUMBER> --name-only | grep -E '\.(ts|tsx|js|jsx|json)$' # → FRONTEND_FILES (for biome)
```

If `FRONTEND_FILES` is empty, skip biome steps in later phases.

---

## Phase 2: Fetch All Review Comments

### 2.1 Get all review threads (resolved and unresolved)

```bash
gh api graphql -f query='
query {
  repository(owner: "yupp-ai", name: "yupp-agent") {
    pullRequest(number: <PR_NUMBER>) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 20) {
            nodes {
              databaseId
              author { login }
              body
              path
              line
              originalLine
              createdAt
            }
          }
        }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)'
```

### 2.2 Also get top-level PR review comments (non-inline)

```bash
gh api repos/yupp-ai/yupp-agent/pulls/<PR_NUMBER>/reviews --jq '.[] | {id, user: .user.login, state: .state, body: .body}'
```

### 2.3 Categorize each comment

For every unresolved comment thread, classify:

| Source | Category | Priority | Handling |
|--------|----------|----------|----------|
| PR owner / assignee | **Owner** | Highest | Must address. Never dismiss without asking. |
| Human reviewer (non-owner) | **Human** | High | Treat with care. If you disagree, ask the user before dismissing. |
| `yupp-reviews[bot]` | **AI (Yupp)** | Medium | Address if valid. Can decline with reason. |
| `gemini-code-assist[bot]` / `github-actions[bot]` / `copilot[bot]` | **AI (Other)** | Medium | Address if valid. Can decline with reason. |

### 2.4 Count prior fix rounds

Count how many "PR Comment Addressing Summary" comments (Phase 7 summaries) already exist on this PR from previous fix rounds. Each summary represents one complete fix round. Store as `PRIOR_ROUNDS`. This determines how aggressive to be about declining marginal AI comments (see Phase 4.2).

### 2.5 Deduplicate overlapping comments

Multiple AI reviewers often flag the **same issue** independently — expect 30-50% duplication. Before addressing comments:

1. **Group** comments referencing the same concern (same file + same logical issue, or same pattern across files).
2. **Fix once**, then reply to duplicates with a brief cross-reference: `"(AI reply) **FIXED** — Same as [thread on file.py:42]."`
3. **Contradictory** advice from different reviewers → flag as **NEEDS HUMAN INPUT**.

---

## Phase 3: Check CI Status

### 3.1 Get all check runs

```bash
gh pr checks <PR_NUMBER> --json name,state,bucket,link
```

### 3.2 Identify failures

Look for checks with `bucket: "fail"` or `state: "FAILURE"`. Common check names:
- Python lint: `lint`, `Lint`, `ruff`, `pre-commit`
- Python type check: `mypy`, `type-check`
- Frontend lint: `lint`, `eslint`, `format-check` (for `apps/couch` / frontend code)
- Tests: `test`, `Test`, `tests`, `pytest`, `ci`, `CI`, `unit-tests`, `integration-tests`

### 3.3 Fetch failed test logs

For each failed check:
```bash
# Extract run ID from the check link URL
gh run view <RUN_ID> --log-failed
```

---

## Phase 4: Address Everything

**Prerequisite**: Ensure the `ys-dev` conda env is active for all Python tooling in this phase.

Work through issues in this order:

### 4.1 Fix CI failures FIRST (lint, mypy, biome, tests)

Fix all CI failures — the PR cannot merge until every check is green.

1. **Python lint + type check**:
   ```bash
   poetry run ruff format <files>
   poetry run ruff check <files> --output-format=github --fix
   poetry run mypy --config-file=pyproject.toml <files>
   ```

2. **Biome** (if `FRONTEND_FILES` is non-empty):
   ```bash
   npx @biomejs/biome check --write <frontend_files>
   ```
   If `--write` cannot auto-fix (unused imports, a11y violations, complexity rules), fix manually.

3. **Test failures** — reproduce locally, then fix:
   ```bash
   poetry run pytest <test_file>::<test_name> -v --tb=long
   ```

4. **Any other failing checks** — read logs with `gh run view <RUN_ID> --log-failed` and fix. If unfixable (infra flake, permissions), flag as **NEEDS HUMAN INPUT**.

### 4.2 Address review comments

For EACH unresolved comment thread:

1. **Read the comment and the referenced code**. Use the `Read` tool (not Bash `cat`) to examine the file at the referenced line — e.g., `Read(file_path="ypl/backend/foo.py", offset=<line-10>, limit=30)`. Verify the reviewer's claims:
   - Does the file/function/line still exist? If deleted or rewritten → **NOT APPLICABLE**.
   - Is the claimed bug real? AI reviewers often misread diffs. Don't fix what isn't broken.
   - Was it already fixed in a later commit? → **NOT APPLICABLE** with "Already resolved in `<sha>`."

2. **Assess severity**:
   - **Bug**: Will cause a crash, data loss, security hole, or incorrect behavior in a reachable code path.
   - **Robustness**: Defensive coding for an edge case that is unlikely but possible.
   - **Nit**: Style, naming, theoretical concern, or "what if a future refactor changes X."

3. **Decide on a verdict** — one of:
   - **FIXED**: Made a code change to address the comment. Use for bugs and valid robustness issues.
   - **ACKNOWLEDGED**: Valid point, no code change needed (e.g., out of scope, consistent with existing patterns, or a good future improvement).
   - **WON'T FIX**: The observation is technically valid but the fix would add more complexity or risk than the issue warrants. **Only for AI reviewer comments.** Use for:
     - Purely theoretical edge cases that cannot occur given the current callers
     - Defensive coding where the input is trusted internal code, not user-facing
     - Suggestions where the fix itself would expand the diff and create new review surface area without meaningful safety improvement
     - Nits on code that is already clear and correct

     **Don't lose the signal** — a WON'T FIX doesn't mean "forget it." If the observation has genuine future value (robustness improvement, security hardening, refactoring opportunity), capture it:
     - Add a `# TODO:` comment at the relevant code location with a one-line description
     - Collect the item for the deferred-work doc update in Phase 4.4
     - In your reply, mention that a TODO was added and/or a tracking ticket will be created
   - **NOT APPLICABLE**: The suggestion doesn't apply, references deleted code, or is factually incorrect. Provide a clear reason.
   - **NEEDS HUMAN INPUT**: You're unsure or the comment involves a design/product decision. **Ask the user via AskUserQuestion** before proceeding.

   **Severity gate for later rounds**: If `PRIOR_ROUNDS >= 2`, only fix AI reviewer comments classified as **Bug**. Decline **Robustness** and **Nit** AI comments with WON'T FIX to stop the fix-review-fix loop. Human reviewer comments are always addressed regardless of round count.

   **Security escalation rule**: When security-related comments escalate into increasingly exotic attack vectors (e.g., SSRF → redirect bypass → DNS rebinding → IPv4-mapped IPv6), fix the **common/obvious vectors** (direct SSRF, path traversal, injection) but use **ACKNOWLEDGED** for exotic vectors (DNS rebinding, timing attacks, side channels). Add TODOs in code and collect them for the deferred-work doc (Phase 4.4). Don't try to achieve zero-vulnerability in a single PR — each defensive layer you add is new code that gets reviewed and generates more comments.

4. **Make the fix** if applicable. Use the `Edit` tool (not Bash `sed`/heredoc) for all code changes. Read the surrounding context first with `Read` to ensure the edit is accurate.

5. **Review your own fix** — Re-read the change in context. Check: does it affect other code paths? Could it introduce a new invalid state? Does it interact with fixes for other comments? Address secondary effects now.

6. **Scan for the same pattern** — If the fix addresses a class of bug, use `Grep` (not Bash `grep -rn`) to search for the same pattern in the codebase: `Grep(pattern="<pattern>", glob="**/*.py", output_mode="content")`. Fix all instances at once.

7. **Track the comment** — store: `<COMMENT_ID>`, `<THREAD_ID>`, verdict, severity, reply text, source category.

**IMPORTANT for human reviewer comments**: If you disagree with a human reviewer's suggestion, do NOT dismiss it. Instead, set verdict to **NEEDS HUMAN INPUT** and ask the user.

### 4.3 Re-run all lint tools after fixes

Re-run the same commands from 4.1 (ruff, mypy, biome) on all changed files. Fix any new issues. Repeat until clean.

### 4.4 Collect deferred work (TODOs, docs, Linear)

Gather every **WON'T FIX** and **ACKNOWLEDGED** item with genuine future value. Don't let deferred items disappear.

1. **TODO comments in code** — add `# TODO:` at the relevant location with a one-line description and PR reference.
2. **Update roadmap/docs** — if the module has a `ROADMAP.md` or similar, append under "Future Work". Create one if 3+ deferred items for the same module.
3. **Offer to create Linear tickets** — list the deferred items and ask the user if they want tickets created via `create_issue` MCP tool.

---

## Phase 5: Commit, Rebase, and Push

### 5.1 Stage and commit

```bash
git add <changed_files>
git commit -m "address PR review comments and fix CI failures"
```

### 5.2 Sync and rebase with main

Before pushing, sync and rebase onto the latest `main` (or `baseRefName` from Phase 1.1) to ensure the branch is up to date.

**If `USE_GRAPHITE` is true:**
```bash
gt sync --no-interactive
gt restack --no-interactive
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin main
git rebase origin/main
```

#### 5.2.1 Handle rebase conflicts

If the rebase produces conflicts:

1. **Identify conflicting files**:
   ```bash
   git diff --name-only --diff-filter=U
   ```

2. **For each conflicting file, classify the conflict**:

   | Conflict Type | Example | Action |
   |---------------|---------|--------|
   | **Code conflict (resolvable)** | Two branches edited the same function | Resolve by merging both changes logically, then `git add <file> && git rebase --continue` |
   | **Alembic migration conflict** | `alembic/versions/` has conflicting `down_revision` pointers | **STOP — escalate to human** (see below) |
   | **Lock file conflict** | `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml` | Regenerate: `poetry lock --no-update` or `npm install` or `pnpm install`, then `git add <file> && git rebase --continue` |
   | **Generated file conflict** | Auto-generated code, protobuf output | Regenerate from source, then `git add <file> && git rebase --continue` |
   | **Unknown / complex conflict** | Large merge conflict spanning many files | **STOP — escalate to human** |

3. **Alembic migration conflicts** require special handling — **always escalate to the human**.

   First, gather diagnostic information to present clearly:

   ```
   # 1. Show the conflict markers in the migration file
   Use: Grep(pattern="<<<<<<<", path=<conflicting_migration_file>, output_mode="content", context=5)

   # 2. Find the current head on main (what down_revision should point to)
   git show origin/main:ypl/db/alembic/current_head.txt

   # 3. Find what our branch's migration currently points to
   Use: Grep(pattern="down_revision", path=<conflicting_migration_file>, output_mode="content")

   # 4. Find what current_head.txt says on our branch
   Use: Read(file_path="ypl/db/alembic/current_head.txt")
   ```

   Then present the conflict to the user with this diagnostic summary:

   > ⚠️ **Rebase conflict in Alembic migration files.**
   >
   > Another migration was merged into `main` since this branch was created. Here's the revision chain state:
   >
   > **Before (our branch):**
   > ```
   > ... → <our_down_revision> → <our_revision> (current_head: <our_head>)
   > ```
   >
   > **After (main):**
   > ```
   > ... → <new_main_head_revision> (current_head on main: <main_head>)
   > ```
   >
   > **Conflicting files:**
   > ```
   > <list of conflicting files with conflict markers shown>
   > ```
   >
   > **Our migration file:** `<filename>`
   > - Revision: `<our_revision>`
   > - Current `down_revision`: `<our_down_revision>`
   > - Needs to point to: `<new_main_head>` (the new head on main)
   >
   > **This requires manual resolution.** Please handle this conflict — I'll abort the rebase for now.

   After presenting the diagnostic, run `git rebase --abort` and stop. Do NOT attempt to resolve alembic conflicts automatically — do not run any alembic commands, do not modify revision pointers, do not update `current_head.txt`. Getting the revision chain wrong can break production deployments.

4. **For resolvable code conflicts**: show the conflict to the user briefly (file + conflicting lines), resolve it, then continue:
   ```bash
   git add <resolved_files>
   git rebase --continue
   ```

5. **After successful rebase**: re-run lint tools (same as Phase 4.1) to catch issues introduced by the rebase.

### 5.3 Push

**If `USE_GRAPHITE` is true:**
```bash
gt submit --no-interactive
```

**If `USE_GRAPHITE` is false (default):**
```bash
git push origin <headRefName> --force-with-lease
```

Use `--force-with-lease` because the rebase rewrites history — it will fail safely if someone else pushed in the meantime.

**CRITICAL: Push BEFORE replying to or resolving any comments.** Resolving threads before pushing may trigger auto-merge without your fixes. Verify push succeeded with `git status` before proceeding.

---

## Phase 6: Reply to Comments and Resolve Threads

**GATE: Do NOT enter this phase until Phase 5 push is confirmed successful.**

### 6.2 Process EVERY tracked thread

For each tracked comment thread from Phase 4 (no exceptions — every thread in the tracking list must be processed):

For each thread, do all three steps: **react → reply → resolve**.

#### 6.2.1 React

```bash
# FIXED / ACKNOWLEDGED → thumbs up; WON'T FIX / NOT APPLICABLE (AI reviewer) → thumbs down
gh api repos/yupp-ai/yupp-agent/pulls/comments/<COMMENT_ID>/reactions -X POST -f content="+1"  # or "-1"
```

#### 6.2.2 Reply

Always prefix `(AI reply)` and include the verdict. Verify 201 response; log and move on if it fails.

```bash
gh api repos/yupp-ai/yupp-agent/pulls/<PR_NUMBER>/comments/<COMMENT_ID>/replies \
  -X POST -f body="(AI reply) **<VERDICT>** — <explanation>"
```

#### 6.2.3 Resolve

```bash
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<THREAD_ID>"}) { thread { isResolved } } }'
```

**Exception**: Do NOT resolve threads with verdict **NEEDS HUMAN INPUT**.

### 6.3 Final sweep and completion

Re-fetch unresolved threads (same query as Phase 2.1). Compare against your tracking list — threads from the last review round are commonly dropped. Any missed threads must get a verdict, reply, and resolution.

Report to the user: threads resolved, threads left open (NEEDS HUMAN INPUT), threads skipped.

---

## Phase 7: Post Summary

Post a top-level comment on the PR with a structured summary:

```bash
gh pr comment <PR_NUMBER> --body "$(cat <<'EOF'
## PR Comment Addressing Summary

(AI reply) Automated review of all comments and CI failures.

### Comments Addressed

| # | File | Source | Verdict | Summary |
|---|------|--------|---------|---------|
| 1 | `path/to/file.py:42` | @reviewer | FIXED | Added null check |
| 2 | `path/to/other.py:10` | yupp-reviews | NOT APPLICABLE | Import is used |
| ... | ... | ... | ... | ... |

### CI Failures Fixed

| Check | Issue | Fix |
|-------|-------|-----|
| mypy | Missing type annotation in `foo()` | Added `-> None` return type |
| ruff | Unused import `os` | Removed import |
| biome | Unused variable in `Component.tsx` | Removed variable |
| pytest | `test_bar` assertion failure | Fixed expected value |

### Statistics

**(FIX ROUND N)** Total comments: X (Y unique issues, Z duplicates), A fixed, B acknowledged, C won't fix, D not applicable, E needs human input, F CI checks fixed.

> Only include non-zero counts. For example, if won't fix and not applicable are both 0:
> **(FIX ROUND 1)** Total comments: 5 (3 unique issues, 2 duplicates), 4 fixed, 1 acknowledged.

### Deferred Items (TODOs added)

| # | File | Issue | Tracking |
|---|------|-------|----------|
| 1 | `path/to/file.py:55` | DNS rebinding protection | TODO in code, ROADMAP.md |
| 2 | `path/to/other.py:120` | IPv4-mapped IPv6 handling | TODO in code, Linear YUP-XXXX |

### Nature of Issues

<Brief analysis of the types of issues encountered — e.g., "Most comments were about missing type annotations and unused imports. Two human reviewer comments raised valid architectural concerns that need owner input.">
EOF
)"
```

---

## Phase 8: Post-Push Verification (10-minute check)

AI reviewers (yupp-reviews, gemini-code-assist, etc.) may re-review after the push and leave new comments.

### 8.1 Wait for CI and AI reviewers

```
Wait approximately 10 minutes after the push.
```

Use `create_agent_schedule` MCP tool to schedule a follow-up check, OR if running interactively, inform the user:

> Push complete. I'll check back in ~10 minutes for new AI reviewer comments and CI results.

### 8.2 Re-check comments and CI

- Fetch unresolved threads (same query as Phase 2.1).
- Check CI: `gh pr checks <PR_NUMBER> --json name,state,bucket`

### 8.3 Handle new issues

- **New CI failures**: fix and push again (CI must pass).
- **New AI comments that flag a bug introduced by your fix**: fix it.
- **New AI nits/robustness comments on your fix code**: use WON'T FIX or ACKNOWLEDGED. The reviewer will always find something in new code — this is where the loop stops.
- **If clean**: post `"(AI reply) Post-push verification complete. All clear."`

**Only do ONE verification round.** If AI reviewers keep posting nits, decline and report to the user.

---

## Phase 9: Stack Mode (`/handle-pr-comments stack`)

When the argument is `stack`, handle comments across **all PRs in the current Graphite stack** instead of a single PR.

### Tool Selection: Graphite vs Git

This mode respects the `USE_GRAPHITE` preference detected earlier (see "Workflow Preference Detection"). Use git commands by default; use Graphite if the user prefers it.

| Operation | Git (default) | Graphite |
|-----------|---------------|----------|
| Checkout | `git checkout <branch>` | `gt checkout <branch>` |
| Push | `git push --force-with-lease` (for each branch) | `gt submit --no-interactive` |
| Restack | `git rebase main` (then rebase dependent branches manually) | `gt restack --no-interactive` |
| List stack | Manual branch listing | `gt log short` |
| Continue rebase | `git rebase --continue` | `gt continue` |

### 9.1 Discover PRs in the stack

```bash
gt log short
```

For each branch, get the PR number and count unresolved comments:
```bash
for branch in $(gt log short --json 2>/dev/null | jq -r '.[].name' 2>/dev/null); do
  pr_num=$(gh pr list --head "$branch" --json number --jq '.[0].number' 2>/dev/null)
  if [ -n "$pr_num" ] && [ "$pr_num" != "null" ]; then
    count=$(gh api graphql -f query="
      query {
        repository(owner: \"yupp-ai\", name: \"yupp-agent\") {
          pullRequest(number: $pr_num) {
            reviewThreads(first: 50) {
              nodes { isResolved }
            }
          }
        }
      }" --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length' 2>/dev/null)
    if [ "$count" -gt 0 ]; then
      echo "PR #$pr_num ($branch): $count unresolved comments"
    fi
  fi
done
```

### 9.2 Process each PR (bottom to top)

Work from the **bottom of the stack up** to minimize merge conflicts. For each PR with unresolved comments:

1. **Checkout the branch**: `gt checkout <branch>` (or `git checkout <branch>`)
2. **Run Phases 2-4** (fetch comments, check CI, address everything) — the same verdict system, deduplication, severity assessment, and deferred-work tracking apply. Include biome for frontend files.
3. **Amend the commit** after fixing (instead of creating a new commit):
   ```bash
   git add <files> && git commit --amend --no-edit
   ```
4. **DO NOT reply or resolve yet** — wait until after pushing the entire stack.

### 9.3 Restack and rebase to propagate changes

**If `USE_GRAPHITE` is true:**
```bash
gt sync --no-interactive
gt restack --no-interactive
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin main
git rebase origin/main
# Then rebase dependent branches onto each other manually
```

If there are merge conflicts:
1. Check conflict markers: use `Grep(pattern="<<<<<<<", path=<file>, output_mode="content")` (not Bash `grep`).
2. **Alembic migration conflicts**: escalate to the human (same rules as Phase 5.2.1 — never silently resolve migration revision chains).
3. **Code conflicts**: resolve by merging both changes logically using the `Edit` tool.
4. Continue: `git add -A && gt continue` (or `git rebase --continue`)

### 9.4 Push the entire stack FIRST

**CRITICAL: Push before replying or resolving!**

**If `USE_GRAPHITE` is true:**
```bash
gt submit --no-interactive
```

**If `USE_GRAPHITE` is false (default):**
```bash
git push --force-with-lease  # for each branch in the stack
```

### 9.5 Reply, resolve, and summarize

After push succeeds, run **Phase 6** (reply + resolve) and **Phase 7** (summary) for each PR that had comments addressed.

### Stack Mode Tips

- If a fix in a lower PR affects higher PRs, the restack will propagate changes automatically.
- Skip Phase 8 (post-push verification) in stack mode — too slow for multiple PRs. Inform the user to check CI manually.

---

## Important Rules

**Hard constraints** (never violate):
1. **Never approve or request changes** — only post `COMMENT` reviews.
2. **Never delete existing comments or reviews.**
3. **Identify as AI** — prefix all replies with `(AI reply)`.
4. **Push before resolving** — resolving threads before pushing may trigger auto-merge without your fixes.

**Judgment principles**:
5. **Verify before fixing** — AI reviewers often misread diffs. Read the actual code before acting on a claim.
6. **Review your own fixes** — a fix that introduces a new bug is worse than the original issue.
7. **Fix patterns, not instances** — when fixing a class of bug, scan nearby for the same pattern.
8. **Exercise judgment on AI comments** — use WON'T FIX for nits that add more complexity than value. But capture the signal: TODOs, docs, Linear tickets.
9. **Increasing decisiveness** — by round 3+, strongly favor WON'T FIX for marginal AI comments. The goal is forward progress.
10. **Contain security scope** — fix common vectors (SSRF, injection). For exotic vectors (DNS rebinding, timing attacks), acknowledge and track.
11. **Human comments get extra care** — never dismiss without asking the PR owner.
12. **Don't drop threads** — always do a final sweep (Phase 6.3). Every tracked thread MUST get a reply and resolution.
