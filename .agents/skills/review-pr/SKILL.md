---
name: review-pr
description: Find and review the GitHub pull request https://github.com/yupp-ai/yupp-mind/pull/$ARGUMENTS.
---

# Review PR

Review a GitHub pull request for the yupp-mind repository.

## Review style

- Be brutally honest. No fluff, no "positive comments", no pleasantries
- Focus only on issues, concerns, and actionable feedback
- Ignore other comments from other AI reviewers (e.g. Gemini Code Assist, Claude, Codex)
- Do not report about unit tests or type checks, this is already handled by CI
- **Stop nitpicking on mature PRs**: If a PR is at revision v5 or above (check the number of commits/force-pushes via `gh api repos/yupp-ai/yupp-mind/pulls/{pr_number}` and look at the `commits` count, or check for multiple review rounds), focus ONLY on substantive issues (bugs, security, correctness). Skip minor style suggestions, naming nitpicks, or "nice to have" improvements - the author has already invested significant effort iterating on this PR.

## Steps

1. **Understand the changes**: either the problem that the new feature is trying to solve, or the root cause of the issue that was fixed

2. **Locate the relevant code** in our codebase

3. **Check the diff** and think about edge cases (e.g. if there is a feature flag: does it work with the flag on and off?)

4. **Validate changes** using your available skills and MCP servers

5. **Regression testing**: think about what can go wrong and scenarios that a human QA would overlook

6. **IMPORTANT: Check all previous comments** (both open AND resolved/closed) before posting any new comments. This prevents:
   - Making duplicate comments about issues already raised
   - Contradicting previous comments on already-fixed issues
   - Re-raising concerns that were already addressed by the author
   Use `gh api repos/yupp-ai/yupp-mind/pulls/{pr_number}/comments`, `gh api repos/yupp-ai/yupp-mind/pulls/{pr_number}/reviews`, and `gh api repos/yupp-ai/yupp-mind/issues/{pr_number}/comments` to fetch existing comments.

7. **Leave a concise comment** with testing results. IMPORTANT: issues and suggestions should be posted as inline comments and/or multi-line comments. Do not post them in the same comment, it is harder to read for a human

8. **Update your original comment** instead of adding another one if you are reviewing the PR again. Do not post the same inline comment again, this is annoying.

9. **Non-blocking comments should not be inline/multi-line/block comments**. If you have interesting observations, DO NOT put them as inline / multi-line comments which the author needs to resolve. Put them as top level / general comments on the PR.

10. **Remove the :eyes: reaction** from the PR (issue) description after posting your review. To do this:
    - List reactions: `gh api repos/yupp-ai/yupp-mind/issues/{pr_number}/reactions`
    - Find the reaction with `content: "eyes"` where `user.login` matches `yupp-reviews[bot]`
    - Delete it: `gh api -X DELETE repos/yupp-ai/yupp-mind/issues/{pr_number}/reactions/{reaction_id}`

11. **IMPORTANT: Do not automatically approve** the PR. Do not request changes. Just post your comment. You are only allowed to `COMMENT`, you should NOT `APPROVE` or `REQUEST_CHANGES`.

12. **Never delete existing comments or reviews.** Do not use `gh api -X DELETE` on comments or reviews. If you need to update your feedback, edit the existing comment instead.
