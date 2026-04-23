#!/bin/bash
# Pre-tool-use hook that blocks `gh pr create` via Claude Code's Bash tool.
# PRs must be created via the `create_pr` MCP tool so that the PR is attributed
# to the requesting user (via device-flow GitHub token), not the bot.
#
# VM-only hook: deployed to ${AHS_REPOS_DIR}/yupp-agent/.claude/hooks/ on AHS
# VMs (default ${AHS_REPOS_DIR}=/data/ahs/repos). NOT installed locally — local
# Claude Code sessions are unaffected.

INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Scan the full command (not just the first line) to catch multiline bypasses.
# Also match subshell patterns: $(...), `...`, (...).
if echo "$COMMAND" | grep -qE '(^|\||&&|;|`|\$?\()\s*gh\s+pr\s+create\b'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": "Blocked: do not use `gh pr create` via Bash. Use the `create_pr` MCP tool instead — it handles GitHub authentication so the PR is attributed to the requesting user, not the bot."
    }
  }'
  exit 0
fi

exit 0
