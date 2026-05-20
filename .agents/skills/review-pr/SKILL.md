---
name: review-pr
description: Common guidelines for reviewing GitHub pull requests across all Yupp repositories.
---

# Review PR — General Guidelines

These guidelines apply to all PR reviews regardless of repository.

## Review Style

- Be brutally honest. No fluff, no "positive comments", no pleasantries
- Focus only on issues, concerns, and actionable feedback
- Ignore other comments from other AI reviewers (e.g. Gemini Code Assist, Claude, Codex)
- Do not report about unit tests or type checks, this is already handled by CI
- **Stop nitpicking on mature PRs**: If a PR is at revision v5 or above (check the number of commits/force-pushes via `gh api repos/{owner}/{repo}/pulls/{pr_number}` and look at the `commits` count, or check for multiple review rounds), focus ONLY on substantive issues (bugs, security, correctness). Skip minor style suggestions, naming nitpicks, or "nice to have" improvements — the author has already invested significant effort iterating on this PR.

## Review Focus

1. **Understand the changes**: either the problem that the new feature is trying to solve, or the root cause of the issue that was fixed
2. **Locate the relevant code** in the codebase
3. **Check the diff** and think about edge cases (e.g. if there is a feature flag: does it work with the flag on and off?)
4. **Validate changes** using your available skills and MCP servers
5. **Regression testing**: think about what can go wrong and scenarios that a human QA would overlook

## Comment Deduplication

**IMPORTANT: Check all previous comments** (both open AND resolved/closed) before posting any new comments. This prevents:
- Making duplicate comments about issues already raised
- Contradicting previous comments on already-fixed issues
- Re-raising concerns that were already addressed by the author

Use `gh api repos/{owner}/{repo}/pulls/{pr_number}/comments`, `gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews`, and `gh api repos/{owner}/{repo}/issues/{pr_number}/comments` to fetch existing comments.

## Posting Rules

_These rules apply when you are posting review results to GitHub directly. If you are a sub-reviewer producing structured output for a coordinator, skip this section — your coordinator handles posting._

- Issues and suggestions → **inline comments** on specific lines. Do not lump them into one big comment.
- Non-blocking observations → **top-level/general comments** on the PR (NOT inline, so the author doesn't have to resolve them).
- **Update your original comment** instead of adding another one if re-reviewing. Do not post the same inline comment twice.
- **Do not approve or request changes.** You are only allowed to `COMMENT`, never `APPROVE` or `REQUEST_CHANGES`.
- **Never delete existing comments or reviews.** If you need to update feedback, edit the existing comment instead.
- **Remove the :eyes: reaction** after posting your review:
  - List reactions: `gh api repos/{owner}/{repo}/issues/{pr_number}/reactions`
  - Find the reaction with `content: "eyes"` where `user.login` matches `yupp-reviews[bot]`
  - Delete it: `gh api -X DELETE repos/{owner}/{repo}/issues/{pr_number}/reactions/{reaction_id}`
