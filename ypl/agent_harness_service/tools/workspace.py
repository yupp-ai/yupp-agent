"""Git workspace management tools for the harness MCP server.

Provides tools for agents to create isolated git worktrees, list available
repositories, and open GitHub pull requests attributed to the requesting user.
"""

from __future__ import annotations
import os
import shutil
from typing import Any

from ypl.agent_harness_service.common.constants import AHS_REPOS_DIR, AHS_SESSIONS_DIR, SESSION_INFRA_DIRS

# TODO: Expose a narrow public function in github_auth.py (e.g. get_or_initiate_github_token)
# to replace these private imports and make the module boundary explicit.
from ypl.agent_harness_service.tools.github_auth import _get_valid_github_token, _initiate_device_flow
from ypl.agent_harness_service.tools.mcp_instance import (
    _get_current_message_user_id,
    _resolve_pr_attribution,
    _validate_session_id,
    mcp,
)
from ypl.agent_harness_service.tools.repo_manager import (
    create_worktree,
    ensure_repo_cloned,
    list_repos_with_metadata,
    load_shared_repos_config,
    push_and_create_pr,
    remove_repo_from_disk,
    repo_name_from_url,
)
from ypl.structured_logger import get_logger

logger = get_logger()

# Minimum free disk space (bytes) required before add_shared_repo will
# attempt a clone. Cheap belt-and-suspenders against an agent filling the
# volume with `add_shared_repo('https://github.com/torvalds/linux')` etc.
# 2 GiB leaves headroom for everything else on the VM.
_MIN_FREE_DISK_BYTES_FOR_CLONE = 2 * 1024 * 1024 * 1024


@mcp.tool(
    name="request_write_access",
    description=(
        "Request write access to a repository by creating a git worktree. "
        "Creates an isolated worktree for the current session. The new workspace "
        "will be available on your next turn (after the current turn completes). "
        "Use list_available_repos to see available repos first. If list_available_repos "
        "shows the repo with on_disk=False (configured but not yet cloned), call "
        "add_shared_repo first to clone it immediately — otherwise this returns an "
        "error and you must wait up to 5 minutes for the next pull tick."
    ),
)
def request_write_access(session_id: str, repo: str, branch: str | None = None) -> dict[str, str]:
    """Request write access to a repo by creating a git worktree.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        repo: Repository name (e.g., 'yupp-agent').
        branch: Optional branch name. Defaults to 'agent/{session_id}'.

    Returns:
        Dict with workspace path, branch name, and status. If the repo is
        configured in shared_repos.yaml but not yet cloned on this VM,
        returns ``{"status": "pending_clone", ...}`` with a hint to call
        ``add_shared_repo`` first.
    """
    logger.info("MCP tool: request_write_access", session_id=session_id, repo=repo, branch=branch)
    _validate_session_id(session_id)

    # Helpful UX: if the repo is in shared_repos.yaml but not on disk yet,
    # tell the agent how to fix it rather than bubbling a generic
    # "Repo not found" ValueError from _validate_repo_name.
    repo_dir = os.path.join(AHS_REPOS_DIR, repo)
    if not os.path.isdir(repo_dir):
        for entry in load_shared_repos_config():
            if entry["name"] == repo:
                return {
                    "status": "pending_clone",
                    "repo": repo,
                    "url": entry["url"],
                    "message": (
                        f"Repo {repo!r} is configured in shared_repos.yaml but not yet cloned on this VM. "
                        f"Call add_shared_repo(url={entry['url']!r}) to clone it now, "
                        "or wait up to 5 minutes for the next pull tick."
                    ),
                }

    return create_worktree(repo=repo, session_id=session_id, branch=branch)


@mcp.tool(
    name="list_available_repos",
    description=(
        "List all shared repositories with config + on-disk metadata. Each "
        "entry: name, path (None if configured but not yet cloned), "
        "in_config (True if listed in shared_repos.yaml), protected (True "
        "if listed as protected — cannot be removed), url (clone URL from "
        "config), on_disk (True if currently cloned). "
        "Only on_disk=True entries can be used with request_write_access / "
        "create_pr; for on_disk=False entries call add_shared_repo first."
    ),
)
def list_available_repos() -> list[dict[str, Any]]:
    """List all shared repositories with config metadata.

    Returns:
        List of dicts with name, path, in_config, protected, url, on_disk.
    """
    logger.info("MCP tool: list_available_repos")
    return list_repos_with_metadata()


@mcp.tool(
    name="add_shared_repo",
    description=(
        "Clone a new repository into the shared repos directory on this VM "
        "(so every session can read it). Use this to make a repo available "
        "immediately; to persist it across VM recreations, also add the "
        "entry to ypl/agent_harness_service/deploy/shared_repos.yaml via a PR. "
        "The /clone-new-repo skill orchestrates both steps. "
        "Accepts https://github.com/{owner}/{repo} URLs (with or without .git). "
        "Idempotent: returns status='exists' if the repo is already cloned. "
        "Refuses to clone if free disk space on AHS_REPOS_DIR is below 2 GiB. "
        "Note: private repos outside the yupp-ai org will fail to clone — "
        "the GitHub App used for auth only has access to yupp-ai/*."
    ),
)
def add_shared_repo(session_id: str, url: str, name: str | None = None) -> dict[str, str]:
    """Clone a repo into AHS_REPOS_DIR on this VM.

    Args:
        session_id: Your harness session ID (audit-logged with the clone).
        url: HTTPS GitHub clone URL (e.g. ``https://github.com/owner/repo``).
        name: Optional directory name override. Defaults to the URL's repo slug.

    Returns:
        Dict with status (``cloned`` | ``exists`` | ``error``), path, and url.
    """
    _validate_session_id(session_id)
    # Audit log: every clone is attributable to a session — helps with
    # incident response if an agent fills disk or stages malicious content.
    logger.info(
        "MCP tool: add_shared_repo",
        session_id=session_id,
        url=url,
        name=name,
    )
    try:
        derived = repo_name_from_url(url)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    final_name = name or derived

    # Soft disk-quota check. ensure_repo_cloned's short-circuit for already
    # cloned repos means we only spend disk on *new* clones, but a single
    # large clone (e.g. linux kernel ~5GB) can still blow up a tight VM.
    try:
        free_bytes = shutil.disk_usage(AHS_REPOS_DIR).free
    except OSError:
        # Best effort — if the dir doesn't exist yet, let ensure_repo_cloned create it.
        free_bytes = None
    if free_bytes is not None and free_bytes < _MIN_FREE_DISK_BYTES_FOR_CLONE:
        return {
            "status": "error",
            "error": (
                f"Insufficient free disk space on {AHS_REPOS_DIR!r}: "
                f"{free_bytes // (1024 * 1024)} MiB free, "
                f"need at least {_MIN_FREE_DISK_BYTES_FOR_CLONE // (1024 * 1024)} MiB. "
                "Remove unused repos with remove_shared_repo or expand the volume."
            ),
            "name": final_name,
            "url": url,
        }

    result = ensure_repo_cloned(final_name, url)
    if result["status"] == "cloned":
        logger.info(
            "add_shared_repo cloned new repo (audit)",
            session_id=session_id,
            name=final_name,
            url=url,
            path=result.get("path", ""),
        )
    return {**result, "name": final_name, "url": url}


@mcp.tool(
    name="remove_shared_repo",
    description=(
        "Remove a shared repository from this VM's repos directory. "
        "Refuses to remove repos that are either (a) in the code-level "
        "_ALWAYS_PROTECTED list (currently just yupp-agent — the harness "
        "needs it to run) or (b) marked `protected: true` in "
        "shared_repos.yaml. Also refuses if shared_repos.yaml exists but "
        "can't be parsed (fail-closed when we can't read the protection list). "
        "Note: this only removes the repo from the current VM. If the "
        "repo is listed in shared_repos.yaml, the next pull tick will "
        "re-clone it. To permanently remove a repo, also delete its "
        "entry from shared_repos.yaml via a PR."
    ),
)
def remove_shared_repo(session_id: str, name: str) -> dict[str, str]:
    """Remove a shared repo's directory from AHS_REPOS_DIR.

    Args:
        session_id: Your harness session ID (audit-logged with the removal).
        name: Directory name under AHS_REPOS_DIR.

    Returns:
        Dict with status (``removed`` | ``missing`` | ``error``) and path.
    """
    _validate_session_id(session_id)
    logger.info("MCP tool: remove_shared_repo", session_id=session_id, name=name)
    result = remove_repo_from_disk(name)
    if result["status"] == "removed":
        logger.info(
            "remove_shared_repo removed repo (audit)",
            session_id=session_id,
            name=name,
            path=result.get("path", ""),
        )
    return result


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
