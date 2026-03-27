---
name: handle-all-open-prs
description: >-
  Loop through all open PRs for a caller (configurable), rebase merge conflicts,
  run /fix-lint-and-tests, and /handle-pr-comments on each. Usage: /handle-all-open-prs [username]
---

# Handle All Open PRs

Loop through all open PRs authored by a user, and for each PR: rebase to resolve merge conflicts, fix lint/test failures, and address review comments.

## Usage

```
/handle-all-open-prs              # Handle all open PRs for the current GitHub user
/handle-all-open-prs @username    # Handle all open PRs for a specific user
/handle-all-open-prs lguan        # Handle all open PRs for user "lguan"
```

## Input Parsing

Parse the optional argument to determine the target user:

1. **No argument**: Use the current GitHub user (`gh api user --jq '.login'`)
2. **`@username`**: Strip the `@` prefix and use as the target user
3. **`username`**: Use directly as the target user

Store as `TARGET_USER` for use throughout.

---

## Workflow Preference Detection

Before starting, check the user's `~/.claude/CLAUDE.md` for git workflow preferences. Look for indicators like:
- "use Graphite" / "prefer gt" / "use gt instead of git"
- "NEVER use git fetch, git pull" / "use gt sync"

Store as `USE_GRAPHITE` (true/false). **Default to false (use git)** if no preference is found.

| Preference | Sync Command | Rebase Command | Push Command |
|------------|--------------|-----------------|--------------|
| git (default) | `git fetch origin main` | `git rebase origin/main` | `git push origin <branch> --force-with-lease` |
| Graphite | `gt sync --no-interactive` | `gt restack --no-interactive` | `gt submit --no-interactive` |

---

## Phase 0: Choose Automation Level

Before starting, present the user with a menu of automation levels. Use `AskQuestion` with a structured multiple-choice prompt:

> I'll process all open PRs for `<TARGET_USER>`.
> **How much autonomy should I have?**

| Level | Name | Behavior |
|-------|------|----------|
| 1 | **Full auto** | For each PR: Rebase → Fix lint/tests → Handle comments → Push — no human confirmation. |
| 2 | **Confirm per-PR** | Show summary for each PR → **[ASK HUMAN]** → Process (rest is automatic). |
| 3 | **Confirm all steps** | Ask before rebase, before commit, before push, and before replying for each PR. |

Store the answer as `AUTOMATION_LEVEL` (1–3) for the rest of the session. Default to **Level 2** if the user doesn't choose.

---

## Phase 1: Discover Open PRs

### 1.1 Fetch all open PRs for the target user

```bash
gh pr list --author "<TARGET_USER>" --state open --json number,title,headRefName,baseRefName,isDraft,mergeable,reviewDecision,statusCheckRollup --jq '.[] | {number, title, branch: .headRefName, base: .baseRefName, draft: .isDraft, mergeable: .mergeable, reviewDecision: .reviewDecision, checks: (.statusCheckRollup // [])}'
```

Store as `OPEN_PRS` list.

### 1.2 Enrich with comment counts

For each PR, get the count of unresolved review comments:

```bash
for pr_num in <PR_NUMBERS>; do
  count=$(gh api graphql -f query="
    query {
      repository(owner: \"yupp-ai\", name: \"yupp-mind\") {
        pullRequest(number: $pr_num) {
          reviewThreads(first: 100) {
            nodes { isResolved }
          }
        }
      }
    }" --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length' 2>/dev/null)
  echo "PR #$pr_num: $count unresolved comments"
done
```

### 1.3 Categorize PRs by status

Create three categories:

| Category | Criteria | Priority |
|----------|----------|----------|
| **Needs Rebase** | `mergeable == "CONFLICTING"` | Process first |
| **Has Open Comments** | Unresolved comment count > 0 | Process second |
| **CI Failing** | Any check with `conclusion == "failure"` | Process third |

A PR may fall into multiple categories. Process in order: rebase conflicts → comments → CI.

### 1.4 Present summary to user

Display a summary of all discovered PRs:

```
Found <N> open PRs for <TARGET_USER>:

| # | PR | Title | Status | Comments | CI |
|---|-----|-------|--------|----------|-----|
| 1 | #123 | Add feature X | Conflicts | 3 | Failing |
| 2 | #456 | Fix bug Y | Clean | 0 | Passing |
| 3 | #789 | Refactor Z | Clean | 5 | Passing |

PRs to process: <count>
- Needs rebase: <count>
- Has open comments: <count>
- CI failing: <count>
```

If `AUTOMATION_LEVEL >= 2`, wait for user confirmation before proceeding.

---

## Phase 2: Process Each PR

For each PR in `OPEN_PRS` (sorted by: conflicts first, then most comments, then oldest):

### 2.1 Checkout the PR branch

**If `USE_GRAPHITE` is true:**
```bash
gt sync --no-interactive
gt checkout <headRefName>
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin
git checkout <headRefName>
git pull origin <headRefName> --rebase
```

### 2.2 Handle merge conflicts (if any)

If the PR has merge conflicts (`mergeable == "CONFLICTING"`):

1. **Attempt rebase onto main/base**:

   **If `USE_GRAPHITE` is true:**
   ```bash
   gt sync --no-interactive
   gt restack --no-interactive
   ```

   **If `USE_GRAPHITE` is false (default):**
   ```bash
   git fetch origin <baseRefName>
   git rebase origin/<baseRefName>
   ```

2. **If conflicts occur during rebase**:

   a. Identify conflicting files:
   ```bash
   git diff --name-only --diff-filter=U
   ```

   b. **Classify each conflict**:

   | Conflict Type | Action |
   |---------------|--------|
   | **Code conflict** | Attempt to resolve by merging both changes logically |
   | **Alembic migration** | **STOP — escalate to human**. Never auto-resolve migration chains. |
   | **Lock file** (`poetry.lock`, `package-lock.json`) | Regenerate: `poetry lock --no-update` or `npm install` |
   | **Complex/unknown** | **STOP — escalate to human** |

   c. For resolvable conflicts, use the `Edit` tool to merge changes, then:
   ```bash
   git add <resolved_files>
   git rebase --continue  # or `gt continue` if using Graphite
   ```

3. **Push the rebased branch**:

   **If `USE_GRAPHITE` is true:**
   ```bash
   gt submit --no-interactive
   ```

   **If `USE_GRAPHITE` is false (default):**
   ```bash
   git push origin <headRefName> --force-with-lease
   ```

### 2.3 Fix lint and tests

Invoke the `/fix-lint-and-tests` skill:

> Processing PR #<number>: Running /fix-lint-and-tests...

This will:
1. Check CI status for failures
2. Run `ruff format` and `ruff check --fix`
3. Run `mypy` and fix type errors
4. Run failing tests and fix them
5. Commit and push fixes

If `/fix-lint-and-tests` encounters issues it cannot fix, log them and continue to the next step.

### 2.4 Handle PR comments

Invoke the `/handle-pr-comments` skill:

> Processing PR #<number>: Running /handle-pr-comments...

Pass the appropriate automation level:
- If `AUTOMATION_LEVEL == 1`: Use Level 1 (full auto) for handle-pr-comments
- If `AUTOMATION_LEVEL == 2`: Use Level 2 (confirm before commit)
- If `AUTOMATION_LEVEL == 3`: Use Level 4 (confirm every step)

This will:
1. Fetch all unresolved review comments
2. Address each comment (fix, acknowledge, or decline)
3. Re-run lint tools
4. Commit, rebase, and push
5. Reply to and resolve comment threads

### 2.5 Record results

For each PR processed, track:
- `pr_number`: The PR number
- `conflicts_resolved`: Whether merge conflicts were resolved
- `lint_fixes`: Number of lint issues fixed
- `test_fixes`: Number of test failures fixed
- `comments_fixed`: Number of comments addressed
- `comments_remaining`: Number of comments still open
- `final_ci_status`: Passing/Failing/Pending
- `errors`: Any errors encountered

### 2.6 Per-PR gate (if applicable)

If `AUTOMATION_LEVEL >= 2`, after processing each PR:

1. Show a summary of what was done:
   ```
   PR #<number> processed:
   - Conflicts resolved: Yes/No
   - Lint fixes: <count>
   - Test fixes: <count>
   - Comments addressed: <count>
   - Comments remaining: <count>
   - CI status: <status>
   ```

2. Ask if the user wants to continue to the next PR.

---

## Phase 3: Generate Summary

After processing all PRs (or if the user stops early):

### 3.1 Build summary

```markdown
## All Open PRs Processing Summary

**User**: <TARGET_USER>
**PRs processed**: <count> / <total>
**Date**: <timestamp>

### Results by PR

| PR | Title | Conflicts | Lint | Tests | Comments | CI | Status |
|----|-------|-----------|------|-------|----------|-----|--------|
| #123 | Add feature X | ✅ Resolved | 3 fixed | 1 fixed | 5 → 0 | ✅ | Complete |
| #456 | Fix bug Y | N/A | 0 | 0 | 2 → 1 | ✅ | Partial |
| #789 | Refactor Z | ❌ Failed | — | — | — | — | Blocked |

### Totals

- **PRs fully processed**: <count>
- **PRs partially processed**: <count>
- **PRs blocked (need human)**: <count>
- **Total conflicts resolved**: <count>
- **Total lint fixes**: <count>
- **Total test fixes**: <count>
- **Total comments addressed**: <count>

### Blocked PRs (require human intervention)

| PR | Reason |
|----|--------|
| #789 | Alembic migration conflict |
| #101 | Complex merge conflict in 5+ files |

### Recommendations

<Any recommendations for follow-up actions>
```

### 3.2 Output to user

Display the full summary in the conversation.

---

## Phase 4: Offer Follow-up Actions

After presenting the summary, offer the user follow-up options:

> **What would you like to do next?**
>
> 1. Re-run on a specific PR that had issues
> 2. Mark PRs as ready for review (if currently draft)
> 3. Open all processed PRs in browser
> 4. Done

---

## Important Rules

1. **Never auto-resolve Alembic migrations** — Always escalate migration conflicts to the human. Getting the revision chain wrong breaks deployments.

2. **Respect automation level** — Never silently bypass a gate. The automation level is a contract with the user.

3. **Process in order** — Handle conflicts first, then comments, then CI. This order minimizes rework.

4. **Track all errors** — Log every error encountered so the user knows what needs manual attention.

5. **Identify as AI** — All PR comments must be prefixed with `(AI reply)` per project conventions.

6. **Never approve or request changes** — Only post `COMMENT` on PRs. This is a hard constraint.

7. **Push before resolving comments** — Resolving threads before pushing may trigger auto-merge without fixes.

8. **Use force-with-lease** — Always use `--force-with-lease` when pushing rebased branches to fail safely if someone else pushed.

9. **Don't lose context** — If switching between PRs, always re-checkout and pull the latest before making changes.

---

## Error Handling

### Network/API errors

If `gh` commands fail, retry up to 3 times with exponential backoff. If still failing, log the error for that PR and continue to the next.

### Stuck on a PR

If a PR cannot be processed after 3 attempts (conflicts keep appearing, CI keeps failing, etc.):
1. Mark as "blocked"
2. Log the specific issue
3. Continue to the next PR
4. Include in the final summary

### Partial progress

If the user interrupts or an error occurs mid-processing:
1. Save the current state
2. Report what was completed
3. Offer to resume from where it stopped

---

## Example Run

```
User: /handle-all-open-prs

AI: Fetching open PRs for lguan...

Found 4 open PRs for lguan:

| # | PR | Title | Status | Comments | CI |
|---|-----|-------|--------|----------|-----|
| 1 | #10850 | Add caching layer | Conflicts | 3 | Failing |
| 2 | #10842 | Fix auth bug | Clean | 5 | Passing |
| 3 | #10830 | Update docs | Clean | 0 | Passing |
| 4 | #10825 | Refactor routing | Clean | 2 | Failing |

PRs to process: 3 (skipping #10830 - no issues)
- Needs rebase: 1
- Has open comments: 2
- CI failing: 2

How much autonomy should I have?
1. Full auto
2. Confirm per-PR (Recommended)
3. Confirm all steps

User: 2

AI: Starting with PR #10850 (conflicts + comments + CI failing)...

[Checking out branch...]
[Rebasing onto main...]
[Resolving 2 code conflicts...]
[Running /fix-lint-and-tests...]
[Running /handle-pr-comments...]

PR #10850 processed:
- Conflicts resolved: Yes (2 files)
- Lint fixes: 4
- Test fixes: 1
- Comments addressed: 3 → 0
- CI status: Passing

Continue to PR #10842? [Y/n]

User: y

[... continues processing ...]

## All Open PRs Processing Summary

**User**: lguan
**PRs processed**: 3 / 4
**Date**: 2026-03-24

| PR | Title | Conflicts | Lint | Tests | Comments | CI | Status |
|----|-------|-----------|------|-------|----------|-----|--------|
| #10850 | Add caching layer | ✅ | 4 | 1 | 3 → 0 | ✅ | Complete |
| #10842 | Fix auth bug | N/A | 0 | 0 | 5 → 0 | ✅ | Complete |
| #10825 | Refactor routing | N/A | 2 | 0 | 2 → 0 | ✅ | Complete |

### Totals

- PRs fully processed: 3
- Total conflicts resolved: 2 files
- Total lint fixes: 6
- Total test fixes: 1
- Total comments addressed: 10

All PRs are now clean and ready for review!
```
