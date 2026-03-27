#!/bin/bash
# Pre-tool-use hook that blocks gh pr review --approve and --request-changes.
# Only COMMENT reviews are allowed.

INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Only check the first line of the command to avoid matching strings inside
# heredoc bodies or --body arguments (e.g. gh pr create --body "...--approve...")
read -r FIRST_LINE <<< "$COMMAND"

# Check for gh pr review with --approve or --request-changes
# Anchor to command boundary (start of line, after pipe, &&, or ;) so we only
# match when gh pr review is the actual command, not a substring in arguments.
if grep -qE '(^|\||&&|;)\s*gh\s+pr\s+review\b' <<< "$FIRST_LINE" && grep -qE '\-\-(approve|request-changes)|-[ar]\b' <<< "$FIRST_LINE"; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Blocked: only COMMENT reviews are allowed. Use --comment instead of --approve or --request-changes."
    }
  }'
  exit 0
fi

# Check for gh api calls that submit APPROVE or REQUEST_CHANGES reviews
if grep -qE 'gh\s+api\b.*pulls/.*/reviews' <<< "$COMMAND" && (grep -qE '"event"\s*:\s*"(APPROVE|REQUEST_CHANGES)"' <<< "$COMMAND" || grep -qE '(-[fF]|--field|--raw-field)\s+event=(APPROVE|REQUEST_CHANGES)' <<< "$COMMAND"); then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Blocked: only COMMENT reviews are allowed via the API. Use event=COMMENT instead."
    }
  }'
  exit 0
fi

exit 0
