---
name: handle-pr-stack
description: "Loop through a Graphite PR stack from bottom to top, running /fix-lint-and-tests then /handle-pr-comments on each PR. Usage: /handle-pr-stack [PR_NUMBER]"
---

# Handle PR Stack

Loop through all PRs in a Graphite stack from bottom to top. For each PR: fix lint/tests, then address review comments, then move to the next PR.

## Usage

```
/handle-pr-stack              # Process the stack containing the current branch
/handle-pr-stack 11234        # Process the stack containing PR #11234
```

## Input Parsing

Parse the optional argument to determine the target stack:

1. **No argument**: Use the current branch to discover the stack
2. **PR number**: Look up the PR's branch, checkout that branch, then discover the stack from there

Store the starting PR (if provided) as `TARGET_PR`.

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

---

## Phase 0: Choose Automation Level

Before starting, present the user with a menu of automation levels. Use `AskUserQuestion` with a structured multiple-choice prompt:

> I'll process all PRs in this stack.
> **How much autonomy should I have?**

| Level | Name | Behavior | Maps to `/handle-pr-comments` level |
|-------|------|----------|--------------------------------------|
| 1 | **Full auto** | For each PR: Fix lint/tests -> Handle comments -> Move to next -- no human confirmation. | Level 1 |
| 2 | **Confirm per-PR** | Show summary for each PR -> **[ASK HUMAN]** -> Process (sub-skills confirm before commit). | Level 2 |
| 3 | **Confirm before reply** | Same as Level 2, but also confirm before posting replies/resolving threads. | Level 3 |
| 4 | **Confirm all steps** | Ask before committing, pushing, and replying for each PR. | Level 4 |

Store the answer as `AUTOMATION_LEVEL` (1-4) for the rest of the session. Default to **Level 2** if the user doesn't choose.

---

## Phase 1: Discover the Stack

### 1.1 Detect the repo

Detect the GitHub repo from the git remote:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
# e.g. "yupp-ai/yupp-agent"
REPO_OWNER=$(echo "$REPO" | cut -d/ -f1)
REPO_NAME=$(echo "$REPO" | cut -d/ -f2)
```

### 1.2 Checkout target (if PR number provided)

If `TARGET_PR` is set:

```bash
# Get the branch name for the PR
BRANCH=$(gh pr view <TARGET_PR> --json headRefName --jq '.headRefName')
```

**If `USE_GRAPHITE` is true:**
```bash
gt checkout "$BRANCH"
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin
git checkout "$BRANCH"
```

### 1.3 Sync and discover the stack

**If `USE_GRAPHITE` is true:**
```bash
gt sync --no-interactive
gt log short
```

**If `USE_GRAPHITE` is false (default):**
```bash
git fetch origin
```

**Note**: Stack discovery requires Graphite (`gt log short`) to determine branch ordering. If Graphite is not available, fall back to discovering PRs from the GitHub PR chain:

```bash
# Fallback: walk the PR base-branch chain to reconstruct the stack
current_pr=<TARGET_PR or current branch PR>
stack=()
while [ -n "$current_pr" ]; do
  branch=$(gh pr view "$current_pr" --json headRefName --jq '.headRefName')
  base=$(gh pr view "$current_pr" --json baseRefName --jq '.baseRefName')
  stack+=("$branch:$current_pr")
  if [ "$base" = "main" ] || [ "$base" = "master" ]; then
    break
  fi
  # Find the PR for the base branch
  current_pr=$(gh pr list --head "$base" --json number --jq '.[0].number' 2>/dev/null)
done
# Reverse to get bottom-to-top order
```

If Graphite is available, use `gt log short` for authoritative stack ordering.

Parse the output to get the ordered list of branches in the stack (bottom to top, excluding `main`).

### 1.4 Get PR info for each branch

For each branch in the stack:

```bash
gh pr list --head "<branch>" --json number,title,headRefName --jq '.[0] | {number, title, branch: .headRefName}'
```

Also get unresolved comment counts and CI status:

```bash
for branch in <STACK_BRANCHES>; do
  pr_num=$(gh pr list --head "$branch" --json number --jq '.[0].number' 2>/dev/null)
  if [ -n "$pr_num" ] && [ "$pr_num" != "null" ]; then
    # Get unresolved comment count
    comments=$(gh api graphql -f query="
      query {
        repository(owner: \"$REPO_OWNER\", name: \"$REPO_NAME\") {
          pullRequest(number: $pr_num) {
            reviewThreads(first: 100) {
              nodes { isResolved }
            }
          }
        }
      }" --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length' 2>/dev/null)

    # Get CI status
    ci_status=$(gh pr checks "$pr_num" --json bucket --jq '[.[] | select(.bucket == "fail")] | length' 2>/dev/null)

    echo "PR #$pr_num ($branch): $comments comments, $ci_status CI failures"
  fi
done
```

### 1.5 Present stack summary to user

Display the discovered stack:

```
Found <N> PRs in the stack (bottom -> top):

| # | PR | Title | Branch | Comments | CI Failures |
|---|-----|-------|--------|----------|-------------|
| 1 | #123 | Base change | lg/base-change | 3 | 1 |
| 2 | #124 | Middle change | lg/middle-change | 0 | 0 |
| 3 | #125 | Top change | lg/top-change | 5 | 2 |

Processing order: bottom -> top (#123 -> #124 -> #125)
```

If `AUTOMATION_LEVEL >= 2`, wait for user confirmation before proceeding.

---

## Phase 2: Process Each PR (Bottom to Top)

Work from the **bottom of the stack up** to minimize merge conflicts when restacking.

For each PR in the stack:

### 2.1 Checkout the branch

**If `USE_GRAPHITE` is true:**
```bash
gt checkout <branch>
```

**If `USE_GRAPHITE` is false (default):**
```bash
git checkout <branch>
```

### 2.2 Per-PR gate (if applicable)

If `AUTOMATION_LEVEL >= 2`, show a brief summary and ask for confirmation:

> **Processing PR #<number>: <title>**
> - Branch: `<branch>`
> - Unresolved comments: <count>
> - CI failures: <count>
>
> Proceed? [Y/n/skip]

If the user says "skip", move to the next PR. If they say "n" or "stop", end processing and go to Phase 3.

### 2.3 Fix lint and tests

Invoke the `/fix-lint-and-tests` skill on the current branch:

> Processing PR #<number>: Running /fix-lint-and-tests...

This will:
1. Check CI status for failures
2. Run `ruff format` and `ruff check --fix`
3. Run `mypy` and fix type errors
4. Run failing tests and fix them
5. Amend the commit with fixes

If `/fix-lint-and-tests` encounters issues it cannot fix, log them and continue to the next step.

### 2.4 Handle PR comments

Invoke the `/handle-pr-comments` skill with the PR number:

> Processing PR #<number>: Running /handle-pr-comments...

Pass the matching automation level directly to `/handle-pr-comments` (levels 1-4 map 1:1).

This will:
1. Fetch all unresolved review comments
2. Address each comment (fix, acknowledge, or decline)
3. Re-run lint tools
4. Commit, rebase, and push
5. Reply to and resolve comment threads

**Note**: Since `/handle-pr-comments` handles its own commit/push/reply cycle, let it complete fully before moving to the next PR.

### 2.5 Restack after each PR

After processing each PR, restack to propagate changes to PRs higher in the stack:

**If `USE_GRAPHITE` is true:**
```bash
gt restack --no-interactive
```

**If `USE_GRAPHITE` is false (default):**

Rebase each dependent branch in the stack (from next-above-current upward) onto its parent:

```bash
# For each branch above the current one in the stack (in order):
git checkout <next_branch>
git rebase <current_branch>
# If successful, continue to the next dependent branch
# Repeat until the top of the stack
# Then checkout back to the next PR to process
```

If there are merge conflicts during restack:
1. **Alembic migration conflicts**: **STOP -- escalate to human**. Never auto-resolve migration chains.
2. **Code conflicts**: Resolve by merging both changes logically.
3. **Lock file conflicts**: Regenerate (`poetry lock --no-update` or `npm install`).

After resolving:
```bash
git add <resolved_files>
git rebase --continue
```

### 2.6 Record results

For each PR processed, track:
- `pr_number`: The PR number
- `title`: PR title
- `lint_fixes`: Number of lint issues fixed
- `test_fixes`: Number of test failures fixed
- `comments_fixed`: Number of comments addressed
- `comments_remaining`: Number of comments still open
- `final_ci_status`: Passing/Failing/Pending
- `errors`: Any errors encountered
- `skipped`: Whether the user skipped this PR

---

## Phase 3: Generate Summary

After processing all PRs (or if the user stops early):

```markdown
## Stack Processing Summary

**Stack**: <bottom_branch> -> ... -> <top_branch>
**PRs processed**: <count> / <total>
**Date**: <timestamp>

### Results by PR (bottom -> top)

| # | PR | Title | Lint | Tests | Comments | CI | Status |
|---|-----|-------|------|-------|----------|-----|--------|
| 1 | #123 | Base change | 3 fixed | 1 fixed | 3 -> 0 | pass | Complete |
| 2 | #124 | Middle change | 0 | 0 | 0 | pass | Skipped (clean) |
| 3 | #125 | Top change | 2 fixed | 0 | 5 -> 2 | pass | Partial |

### Totals

- **PRs fully processed**: <count>
- **PRs skipped**: <count>
- **PRs blocked (need human)**: <count>
- **Total lint fixes**: <count>
- **Total test fixes**: <count>
- **Total comments addressed**: <count>

### Blocked PRs (require human intervention)

| PR | Reason |
|----|--------|
| #125 | 2 comments need human input |

### Recommendations

<Any recommendations for follow-up actions>
```

---

## Phase 4: Offer Follow-up Actions

After presenting the summary, offer the user follow-up options:

> **What would you like to do next?**
>
> 1. Re-run on a specific PR that had issues
> 2. Mark PRs as ready for review (if currently draft and branch does NOT start with `claude/`)
> 3. Open all processed PRs in browser
> 4. Submit the stack (`gt submit --no-interactive`)
> 5. Done

**Note**: Never mark a PR as ready for review if its branch name starts with `claude/` -- per repo rules, these must remain in draft.

---

## Important Rules

1. **Process bottom to top** -- Always work from the bottom of the stack upward. This order minimizes merge conflicts when restacking.

2. **Restack after each PR** -- Changes in lower PRs affect higher ones. Always restack after modifying a PR.

3. **Never auto-resolve Alembic migrations** -- Escalate migration conflicts to the human. Getting the revision chain wrong breaks deployments.

4. **Respect automation level** -- Never silently bypass a gate. The automation level is a contract with the user.

5. **Let sub-skills handle their own workflow** -- `/fix-lint-and-tests` and `/handle-pr-comments` each have their own commit/push cycles. Don't duplicate their work.

6. **Identify as AI** -- All PR comments must be prefixed with `(AI reply)` per project conventions.

7. **Never approve or request changes** -- Only post `COMMENT` on PRs. This is a hard constraint.

8. **Track all errors** -- Log every error encountered so the user knows what needs manual attention.

9. **Don't lose context** -- After restacking, verify you're on the correct branch before processing the next PR.

---

## Error Handling

### Restack failures

If restacking fails with unresolvable conflicts:
1. Log the error for the current and all subsequent PRs
2. Attempt to continue with the next PR if possible (checkout directly)
3. Include in the final summary with clear explanation

### Sub-skill failures

If `/fix-lint-and-tests` or `/handle-pr-comments` fails mid-execution:
1. Log what was completed and what failed
2. Ask the user if they want to continue to the next PR or stop
3. Include partial results in the final summary

### Network/API errors

If `gh` commands fail, retry up to 3 times. If still failing, log the error and continue to the next PR.

---

## Example Run

```
User: /handle-pr-stack 64

AI: Looking up PR #64 and discovering the stack...

Found 3 PRs in the stack (bottom -> top):

| # | PR | Title | Branch | Comments | CI Failures |
|---|-----|-------|--------|----------|-------------|
| 1 | #63 | Fix status mapping | lg/fix-linear-mapping | 2 | 0 |
| 2 | #64 | Add attach link tool | lg/linear-attach-link | 0 | 1 |
| 3 | #65 | Sync task links | lg/linear-sync-links | 5 | 2 |

Processing order: bottom -> top (#63 -> #64 -> #65)

How much autonomy should I have?
1. Full auto
2. Confirm per-PR (Recommended)
3. Confirm before reply
4. Confirm all steps

User: 2

AI: **Processing PR #63: Fix status mapping**
- Unresolved comments: 2
- CI failures: 0

Proceed? [Y/n/skip]

User: y

AI: Running /fix-lint-and-tests on PR #63...
[No CI failures found]

Running /handle-pr-comments on PR #63...
[Addressed 2 comments: 1 FIXED, 1 ACKNOWLEDGED]
[Pushed and resolved threads]

Restacking...
[Restack successful]

**Processing PR #64: Add attach link tool**
- Unresolved comments: 0
- CI failures: 1 (mypy)

Proceed? [Y/n/skip]

User: y

AI: Running /fix-lint-and-tests on PR #64...
[Fixed 1 mypy error, amended and pushed]

Running /handle-pr-comments on PR #64...
[No unresolved comments]

Restacking...
[Restack successful]

**Processing PR #65: Sync task links**
- Unresolved comments: 5
- CI failures: 2 (ruff, pytest)

Proceed? [Y/n/skip]

User: y

AI: Running /fix-lint-and-tests on PR #65...
[Fixed 2 ruff issues, 1 test failure, amended and pushed]

Running /handle-pr-comments on PR #65...
[Addressed 5 comments: 3 FIXED, 1 ACKNOWLEDGED, 1 NEEDS HUMAN INPUT]
[Pushed and resolved 4 threads, 1 left open]

## Stack Processing Summary

**Stack**: lg/fix-linear-mapping -> lg/linear-attach-link -> lg/linear-sync-links
**PRs processed**: 3 / 3
**Date**: 2026-03-31

| # | PR | Title | Lint | Tests | Comments | CI | Status |
|---|-----|-------|------|-------|----------|-----|--------|
| 1 | #63 | Fix status mapping | 0 | 0 | 2 -> 0 | pass | Complete |
| 2 | #64 | Add attach link tool | 1 fixed | 0 | 0 | pass | Complete |
| 3 | #65 | Sync task links | 2 fixed | 1 fixed | 5 -> 1 | pass | Partial |

### Totals
- PRs fully processed: 2
- PRs partially processed: 1 (1 comment needs human input)
- Total lint fixes: 3
- Total test fixes: 1
- Total comments addressed: 7

What would you like to do next?
1. Re-run on a specific PR
2. Mark PRs as ready for review
3. Open all PRs in browser
4. Submit the stack
5. Done
```
