"""Git workspace management tools for the harness MCP server.

Provides tools for agents to create isolated git worktrees, list available
repositories, and open GitHub pull requests attributed to the requesting user.
"""

from __future__ import annotations
import os
from typing import Any

from ypl.agent_harness_service.common.constants import AHS_SESSIONS_DIR, SESSION_INFRA_DIRS

# TODO: Expose a narrow public function in github_auth.py (e.g. get_or_initiate_github_token)
# to replace these private imports and make the module boundary explicit.
from ypl.agent_harness_service.tools.github_auth import _get_valid_github_token, _initiate_device_flow
from ypl.agent_harness_service.tools.mcp_instance import (
    _get_current_message_user_id,
    _resolve_pr_attribution,
    _validate_session_id,
    mcp,
)
from ypl.agent_harness_service.tools.repo_manager import create_worktree, push_and_create_pr
from ypl.agent_harness_service.tools.repo_manager import list_repos as _list_repos
from ypl.structured_logger import get_logger

logger = get_logger()


@mcp.tool(
    name="request_write_access",
    description=(
        "Request write access to a repository by creating a git worktree. "
        "Creates an isolated worktree for the current session. The new workspace "
        "will be available on your next turn (after the current turn completes). "
        "Use list_available_repos to see available repos first."
    ),
)
def request_write_access(session_id: str, repo: str, branch: str | None = None) -> dict[str, str]:
    """Request write access to a repo by creating a git worktree.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        repo: Repository name (e.g., 'yupp-agent').
        branch: Optional branch name. Defaults to 'agent/{session_id}'.

    Returns:
        Dict with workspace path, branch name, and status.
    """
    logger.info("MCP tool: request_write_access", session_id=session_id, repo=repo, branch=branch)
    _validate_session_id(session_id)
    return create_worktree(repo=repo, session_id=session_id, branch=branch)


@mcp.tool(
    name="list_available_repos",
    description=(
        "List all available repositories that can be used with request_write_access "
        "and create_pr. Returns each repo's name and filesystem path."
    ),
)
def list_available_repos() -> list[dict[str, str]]:
    """List all available repositories.

    Returns:
        List of dicts with repo name and path for each available repo.
    """
    logger.info("MCP tool: list_available_repos")
    return _list_repos()


@mcp.tool(
    name="create_pr",
    description=(
        "Push the current branch and create a pull request. "
        "Pushes all committed changes on the session's worktree branch and "
        "opens a PR against the specified base branch (or repository default) via the GitHub CLI. "
        "PRs are created in draft mode by default. "
        "You must call request_write_access first to create a worktree. "
        "If the session has multiple worktrees, specify the 'repo' parameter. "
        "For stacked PRs, specify 'base' as the parent branch name."
    ),
)
async def create_pr(
    session_id: str,
    title: str,
    body: str,
    repo: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    draft: bool = True,
) -> dict[str, Any]:
    """Push the current branch and create a pull request.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        title: PR title.
        body: PR description body.
        repo: Repository name. Required if the session has multiple worktrees.
        branch: Branch name override. Defaults to the current branch in the workspace.
        base: Base branch for the PR (e.g., parent branch for stacked PRs).
            Defaults to the repository's default branch (main).
        draft: If True, create the PR in draft mode. Defaults to True.

    Returns:
        Dict with PR URL, branch name, and status.
    """
    logger.info(
        "MCP tool: create_pr", session_id=session_id, title=title, repo=repo, branch=branch, base=base, draft=draft
    )
    _validate_session_id(session_id)

    workspace_base = os.path.join(AHS_SESSIONS_DIR, session_id)
    if not os.path.isdir(workspace_base):
        return {"status": "error", "error": "No worktree found. Call request_write_access first."}

    # Find worktree directories: skip symlinks (repo references), dotfiles, and infrastructure dirs
    worktree_entries = sorted(
        e
        for e in os.listdir(workspace_base)
        if not e.startswith(".")
        and e not in SESSION_INFRA_DIRS
        and os.path.isdir(os.path.join(workspace_base, e))
        and not os.path.islink(os.path.join(workspace_base, e))
    )
    if not worktree_entries:
        return {"status": "error", "error": "No worktree found. Call request_write_access first."}

    if repo:
        # Match worktrees starting with "{repo}-" (new slug-suffixed naming)
        matching = [e for e in worktree_entries if e.startswith(f"{repo}-")]
        if not matching:
            return {"status": "error", "error": f"No worktree found for repo '{repo}'. Available: {worktree_entries}"}
        if len(matching) > 1:
            return {"status": "error", "error": f"Multiple worktrees for '{repo}': {matching}. Specify exact name."}
        workspace = os.path.join(workspace_base, matching[0])
    elif len(worktree_entries) == 1:
        workspace = os.path.join(workspace_base, worktree_entries[0])
    else:
        return {
            "status": "error",
            "error": f"Multiple worktrees found: {worktree_entries}. Specify 'repo' parameter.",
        }

    # Get user GitHub token from memory (if authorized via device flow and not expired)
    user_github_token = await _get_valid_github_token(session_id)

    if not user_github_token:
        # No valid token - initiate device flow and return error with auth details
        user_id = await _get_current_message_user_id(session_id)
        device_flow_result = await _initiate_device_flow(session_id, user_id)

        if device_flow_result.get("status") == "error":
            return {
                "status": "error",
                "error": f"GitHub authorization required but failed to initiate: {device_flow_result.get('error')}",
            }

        # Polling already in progress (e.g. from a prior create_pr or authorize_github_user call):
        # _initiate_device_flow returns {"status": "pending", "message": "..."} with no verification_uri.
        # Return a clear "check status and retry" message rather than falling through with empty strings.
        if "verification_uri" not in device_flow_result:
            return {
                "status": "error",
                "error": "GitHub authorization required to create PRs.",
                "auth_required": "true",
                "message": device_flow_result.get("message", "Authorization already in progress."),
                "next_step": "Call check_github_auth_status to verify, then retry create_pr.",
            }

        return {
            "status": "error",
            "error": "GitHub authorization required to create PRs.",
            "auth_required": "true",
            "verification_uri": device_flow_result.get("verification_uri", ""),
            "user_code": device_flow_result.get("user_code", ""),
            "expires_in_seconds": device_flow_result.get("expires_in_seconds", ""),
            "instructions": device_flow_result.get("instructions", ""),
            "next_step": (
                "After the user completes authorization, call check_github_auth_status to verify, then retry create_pr."
            ),
        }

    # For task-triggered sessions, prepend the attribution header if not already present.
    # Attribution is cosmetic — must not block PR creation on DB errors.
    try:
        attribution = await _resolve_pr_attribution(session_id)
    except Exception:
        logger.warning("Failed to resolve PR attribution, skipping", session_id=session_id)
        attribution = None
    if attribution and not body.startswith("\U0001f916"):
        body = attribution + "\n\n" + body

    return push_and_create_pr(
        workspace=workspace,
        title=title,
        body=body,
        branch=branch,
        base=base,
        user_github_token=user_github_token,
        draft=draft,
    )
