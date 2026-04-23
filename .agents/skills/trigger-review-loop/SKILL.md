---
name: trigger-review-loop
description: >-
  Automated review-fix loop for a PR. Triggers AI review (yupp-reviews), handles
  comments via /handle-pr-comments, pushes fixes, and repeats for up to 5 rounds
  until no open comments remain. Usage: /trigger-review-loop <PR_URL_OR_NUMBER>
---

# Trigger Review Loop

Automated end-to-end review-fix loop that triggers AI review, addresses comments, pushes fixes, and iterates until the PR is clean or the maximum iteration limit is reached.

## Usage

```
/trigger-review-loop https://github.com/yupp-ai/yupp-agent/pull/10572
/trigger-review-loop 10572
```

## Input Parsing

Extract the PR number from the argument. Accept either:
- A full URL: `https://github.com/yupp-ai/yupp-agent/pull/<NUMBER>`
- A bare number: `<NUMBER>`

Store as `<PR_NUMBER>` for use throughout.

---

## Configuration

| Setting | Value | Description |
|---------|-------|-------------|
| `MAX_ITERATIONS` | 5 | Maximum number of review-fix cycles |
| `WAIT_TIME_DRAFT` | 3 minutes | Wait time after posting `/review` on a draft PR |
| `WAIT_TIME_PUBLISHED` | 3 minutes | Wait time for auto-review on a published PR |

---

## Phase 0: Initial Setup

### 0.1 Permission Check

Before starting, ask the user:

> I'll run an automated review-fix loop on PR #<PR_NUMBER>.
> This will invoke `/handle-pr-comments` multiple times, which may commit and push code.
> **Should I auto-commit and push, or ask you each time?**

Store the answer as `AUTO_PUSH` (true/false) for the rest of the session. This will be passed to each `/handle-pr-comments` invocation.

### 0.2 Get PR metadata

```bash
gh pr view <PR_NUMBER> --json title,headRefName,isDraft,state,author
```

Store:
- `PR_TITLE`: the PR title
- `HEAD_BRANCH`: the head branch name
- `IS_DRAFT`: whether the PR is a draft
- `PR_STATE`: the PR state (open, closed, merged)

### 0.3 Validate PR state

If `PR_STATE` is not `OPEN`, abort with message:
> PR #<PR_NUMBER> is not open (state: <PR_STATE>). Cannot run review loop.

### 0.4 Checkout the PR branch

```bash
git fetch origin
git checkout <HEAD_BRANCH>
git pull origin <HEAD_BRANCH>
```

### 0.5 Initialize tracking

```
ITERATION = 0
FIXES_MADE = []  # List of {iteration, comments_fixed, ci_fixed}
TOTAL_COMMENTS_FIXED = 0
TOTAL_CI_FIXED = 0
SEEN_ISSUES = {}  # Map of "file:line:issue_hash" -> count, for detecting stuck loops
```

---

## Phase 1: Review Loop

Repeat the following until `ITERATION >= MAX_ITERATIONS` or no open comments remain:

### 1.1 Increment iteration

```
ITERATION = ITERATION + 1
```

Log to user:
> Starting iteration <ITERATION> of <MAX_ITERATIONS>...

### 1.2 Refresh PR draft status

Re-fetch the PR's draft status at the start of each iteration (the author may have marked it ready-for-review during the loop):

```bash
gh pr view <PR_NUMBER> --json isDraft --jq '.isDraft'
```

Update `IS_DRAFT` with the current value.

### 1.3 Trigger AI review

**First iteration optimization**: Before waiting for AI review, check if there are already open comments from a previous review. If `ITERATION == 1` and open comments exist, skip the wait and proceed directly to handling them.

```bash
# Quick check for existing open comments
EXISTING_COMMENTS=$(gh api graphql -f query='...' --jq '[...] | length')
if [ "$ITERATION" -eq 1 ] && [ "$EXISTING_COMMENTS" -gt 0 ]; then
  # Skip wait, proceed to 1.4
fi
```

**If `IS_DRAFT` is true:**

Post a `/review` comment to trigger yupp-reviews:

```bash
gh pr comment <PR_NUMBER> --body "/review"
```

Then wait for yupp-reviews to process:

```
Wait 3 minutes for yupp-reviews to analyze the PR.
```

> **TODO**: Consider implementing a polling mechanism instead of fixed 2-minute wait — poll for new review threads from yupp-reviews every 30 seconds with a 10-15 minute timeout (see PR #10844).

Inform the user:
> Posted `/review` comment on draft PR. Waiting 3 minutes for yupp-reviews...

**If `IS_DRAFT` is false (published):**

yupp-reviews will auto-review published PRs. Wait for it:

```
Wait 3 minutes for yupp-reviews to auto-review.
```

Inform the user:
> PR is published. Waiting 3 minutes for yupp-reviews to auto-review...

### 1.4 Check for open comments

```bash
gh api graphql -f query='
query {
  repository(owner: "yupp-ai", name: "yupp-agent") {
    pullRequest(number: <PR_NUMBER>) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              author { login }
              createdAt
            }
          }
        }
      }
    }
  }
}' --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length'
```

Store as `OPEN_COMMENT_COUNT`.

> **TODO**: For PRs with >100 review threads, implement pagination using `pageInfo.hasNextPage` and `after` cursor to get accurate counts (see PR #10844).

### 1.5 Check CI status

Check both for failures AND for pending/in-progress checks:

```bash
# Get all check statuses
gh pr checks <PR_NUMBER> --json name,state,bucket
```

Count failures:
```bash
FAILED_CHECKS_COUNT=$(gh pr checks <PR_NUMBER> --json name,state,bucket --jq '[.[] | select(.bucket == "fail" or .state == "FAILURE")] | length')
```

Count pending/in-progress checks:
```bash
PENDING_CHECKS_COUNT=$(gh pr checks <PR_NUMBER> --json name,state,bucket --jq '[.[] | select(.state == "PENDING" or .state == "QUEUED" or .bucket == "pending")] | length')
```

### 1.6 Exit condition check

CI is only considered "passing" when all required checks are **completed** and successful — not just "no failures yet."

If `OPEN_COMMENT_COUNT == 0` AND `FAILED_CHECKS_COUNT == 0` AND `PENDING_CHECKS_COUNT == 0`:

Log to user:
> No open comments and CI is passing. Review loop complete!

Exit the loop and proceed to Phase 2.

If `PENDING_CHECKS_COUNT > 0` and `OPEN_COMMENT_COUNT == 0` and `FAILED_CHECKS_COUNT == 0`:

Log to user:
> No open comments but <PENDING_CHECKS_COUNT> CI checks still running. Waiting for CI to complete...

Wait 1 minute and re-check CI status. Repeat until checks complete or timeout (10 minutes max).

**On CI timeout (10 minutes elapsed with checks still pending):**
1. Do NOT start a new review cycle — there's nothing to fix yet
2. Log to user: "CI checks still pending after 10 minutes. Marking as PARTIAL and stopping."
3. Set final status to `PARTIAL` and proceed to Phase 2 (summary)
4. The summary should note that CI was still running and recommend manual follow-up

### 1.7 Fix CI failures

If `FAILED_CHECKS_COUNT > 0`, invoke the `/fix-lint-and-tests` skill first:

> Invoking /fix-lint-and-tests to address <FAILED_CHECKS_COUNT> CI failures...

```bash
# fix-lint-and-tests will:
# 1. Run ruff format and ruff check --fix
# 2. Run mypy and fix type errors
# 3. Run failing tests and fix them
# 4. Commit and push the fixes
```

After `/fix-lint-and-tests` completes, update tracking:
```
TOTAL_CI_FIXED += <number of CI checks fixed>
```

### 1.8 Handle PR comments

If there are open comments, invoke the `/handle-pr-comments` skill:

> Invoking /handle-pr-comments to address <OPEN_COMMENT_COUNT> comments...

**IMPORTANT**: When invoking `/handle-pr-comments`:
- Pass the `AUTO_PUSH` setting from Phase 0.1 (respect user's choice)
- Skip Phase 8 (post-push verification) since we'll trigger another review cycle anyway
- Track the number of comments fixed

After `/handle-pr-comments` completes, record:
```
FIXES_MADE.append({
  iteration: ITERATION,
  comments_fixed: <number of FIXED verdicts>,
  ci_fixed: <number of CI fixes>
})
TOTAL_COMMENTS_FIXED += <comments_fixed>
TOTAL_CI_FIXED += <ci_fixed>
```

### 1.9 Update issue tracking for stuck-loop detection

For each comment addressed in this iteration, compute a content-based key:
```
issue_key = hash(file_path + ":" + line_number + ":" + first_50_chars_of_comment_body)
```

Increment `SEEN_ISSUES[issue_key]`. If any `SEEN_ISSUES[key] >= 2`, the same issue has appeared twice — flag it as **NEEDS HUMAN INPUT** and exclude from future iterations.

### 1.10 Verify push completed

Confirm that changes were pushed:

```bash
git status
git log -1 --oneline
```

### 1.11 Continue loop

If `ITERATION < MAX_ITERATIONS`, go back to step 1.1.

If `ITERATION >= MAX_ITERATIONS` and there are still open comments:

Log to user:
> Reached maximum iterations (<MAX_ITERATIONS>). Exiting loop with <OPEN_COMMENT_COUNT> open comments remaining.

---

## Phase 2: Final Status Check

### 2.1 Final comment count

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
          comments(first: 1) {
            nodes {
              author { login }
            }
          }
        }
      }
    }
  }
}' --jq '{
  open: [.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length,
  resolved: [.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == true)] | length
}'
```

### 2.2 Final CI status

```bash
gh pr checks <PR_NUMBER> --json name,state,bucket
```

---

## Phase 3: Generate Summary

### 3.1 Build summary content

Create a summary with:

```markdown
## Review Loop Summary

**PR**: #<PR_NUMBER> - <PR_TITLE>
**Branch**: <HEAD_BRANCH>
**Iterations completed**: <ITERATION> / <MAX_ITERATIONS>
**Final status**: <SUCCESS | PARTIAL | MAX_ITERATIONS_REACHED>

### Iteration History

| Round | Comments Fixed | CI Issues Fixed |
|-------|----------------|-----------------|
| 1     | X              | Y               |
| 2     | X              | Y               |
| ...   | ...            | ...             |

### Totals

- **Total comments addressed**: <TOTAL_COMMENTS_FIXED>
- **Total CI issues fixed**: <TOTAL_CI_FIXED>
- **Remaining open comments**: <FINAL_OPEN_COUNT>
- **CI status**: <PASSING | FAILING>

### Final State

<Description of the PR's current state - whether it's ready for human review,
what issues remain (if any), and any recommendations>
```

### 3.2 Output to user

Display the full summary to the user in the conversation.

### 3.3 Post summary as PR comment

```bash
gh pr comment <PR_NUMBER> --body "$(cat <<'EOF'
## Review Loop Summary

(AI reply) Automated review-fix loop completed.

**Iterations completed**: <ITERATION> / <MAX_ITERATIONS>
**Total comments addressed**: <TOTAL_COMMENTS_FIXED>
**Total CI issues fixed**: <TOTAL_CI_FIXED>
**Remaining open comments**: <FINAL_OPEN_COUNT>

### Iteration History

| Round | Comments Fixed | CI Issues Fixed |
|-------|----------------|-----------------|
| 1     | X              | Y               |
| ...   | ...            | ...             |

### Final Status

<SUCCESS: All comments resolved and CI passing |
PARTIAL: Some comments remain open |
MAX_ITERATIONS: Reached limit with N comments remaining>

<Any recommendations for the PR author>
EOF
)"
```

---

## Important Rules

1. **Maximum 5 iterations** — Hard limit to prevent infinite loops. If issues persist after 5 rounds, stop and report to user.

2. **Wait for AI review** — Always wait the appropriate time for yupp-reviews to post comments before checking for open comments.

3. **Draft vs Published PRs**:
   - Draft PRs require posting `/review` to trigger AI review
   - Published PRs get auto-reviewed by yupp-reviews

4. **Respect AUTO_PUSH setting** — Ask for permission in Phase 0.1 and pass the user's choice to each `/handle-pr-comments` invocation.

5. **Track progress** — Keep detailed records of what was fixed in each iteration for the final summary.

6. **Exit early if clean** — If no open comments and CI is passing, exit the loop immediately rather than waiting for all iterations.

7. **Report remaining issues** — If the loop ends with open comments, clearly list what remains for human attention.

8. **Identify as AI** — All PR comments must be prefixed with `(AI reply)` per project conventions.

9. **Never approve or request changes** — Only post `COMMENT` on the PR. This is a hard constraint.

---

## Error Handling

### Network/API errors

If `gh` commands fail, retry up to 3 times with exponential backoff. If still failing, report the error and abort.

### Merge conflicts

If `/handle-pr-comments` encounters merge conflicts, report to user and abort the loop. Manual intervention required.

### Stuck in loop

If the same issue keeps appearing after being "fixed", this indicates a persistent issue. Detection uses content-based tracking:

1. For each comment, compute: `issue_key = hash(file_path + ":" + line_number + ":" + first_50_chars_of_comment_body)`
2. Track occurrence counts in `SEEN_ISSUES` map across iterations
3. If any `issue_key` appears 2+ times, the underlying issue wasn't actually resolved
4. Flag these as **NEEDS HUMAN INPUT** and exclude from future `/handle-pr-comments` invocations
5. Report to user which issues are recurring

---

## Example Run

```
User: /trigger-review-loop 10850
AI: Checking PR #10850...
AI: PR is a draft. Posting /review comment...
AI: Waiting 3 minutes for yupp-reviews...
AI: Starting iteration 1 of 5...
AI: Found 3 open comments and 1 CI failure.
AI: Invoking /handle-pr-comments...
[... handle-pr-comments runs, fixes issues, pushes ...]
AI: Iteration 1 complete. Fixed 3 comments, 1 CI issue.

AI: Starting iteration 2 of 5...
AI: Posting /review comment on draft PR...
AI: Waiting 3 minutes for yupp-reviews...
AI: Found 1 new comment from AI reviewer.
AI: Invoking /handle-pr-comments...
[... handle-pr-comments runs ...]
AI: Iteration 2 complete. Fixed 1 comment.

AI: Starting iteration 3 of 5...
AI: Posting /review comment on draft PR...
AI: Waiting 3 minutes for yupp-reviews...
AI: No open comments and CI is passing. Review loop complete!

## Review Loop Summary

**PR**: #10850 - Add caching layer for API responses
**Branch**: feature/api-caching
**Iterations completed**: 3 / 5
**Final status**: SUCCESS

| Round | Comments Fixed | CI Issues Fixed |
|-------|----------------|-----------------|
| 1     | 3              | 1               |
| 2     | 1              | 0               |
| 3     | 0              | 0               |

**Total comments addressed**: 4
**Total CI issues fixed**: 1
**Remaining open comments**: 0
```
