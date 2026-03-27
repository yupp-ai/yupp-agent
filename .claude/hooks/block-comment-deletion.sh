#!/bin/bash
# Pre-tool-use hook that blocks deletion of PR comments and reviews.
# Reaction deletions are still allowed (used for removing :eyes: reactions).

INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Block gh api DELETE calls targeting comments or reviews (but not reactions)
# Matches: repos/{owner}/{repo}/issues/comments/{id}
#          repos/{owner}/{repo}/pulls/comments/{id}
#          repos/{owner}/{repo}/pulls/{number}/reviews/{id}
# Does NOT match: repos/{owner}/{repo}/issues/{number}/reactions/{id}
# Handles all flag forms: -X DELETE, -XDELETE, --method DELETE, --method=DELETE
if grep -qE 'gh[[:space:]]+api' <<< "$COMMAND" && grep -qE '(-X[[:space:]]*|--method[[:space:]=])DELETE' <<< "$COMMAND" && grep -qE '(issues/comments|pulls/comments|pulls/[0-9]+/reviews)/[0-9]+' <<< "$COMMAND"; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Blocked: deleting PR comments or reviews is not allowed. Never delete existing comments or reviews."
    }
  }'
  exit 0
fi

# Block gh pr comment --delete-last which deletes the last PR comment
if grep -qE 'gh[[:space:]]+pr[[:space:]]+comment' <<< "$COMMAND" && grep -qE -e '--delete-last' <<< "$COMMAND"; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Blocked: deleting PR comments is not allowed. Never delete existing comments or reviews."
    }
  }'
  exit 0
fi

exit 0
