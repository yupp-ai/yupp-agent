"""Repo management for Agent Harness Service.

Handles:
- Shared read-only checkouts of main in AHS_REPOS_DIR (default /data/ahs/repos/)
- Git worktrees for write sessions in AHS_SESSIONS_DIR/{session_id}/
  (default /data/ahs/sessions/{session_id}/)
- PR creation via `gh` CLI
- Auto-pulling repos on a schedule
- Workspace setup for new sessions
"""

import os
import re
import subprocess

from ypl.agent_harness_service.common.constants import AHS_REPOS_DIR, AHS_SESSIONS_DIR, SESSION_INFRA_DIRS
from ypl.structured_logger import get_logger

logger = get_logger()

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Regex to split branch names into word-like segments
_BRANCH_SLUG_SPLIT = re.compile(r"[-_/]+")


def _branch_to_dir_suffix(branch: str) -> str:
    """Extract a short directory suffix from a branch name.

    Takes the first 4 "words" after stripping any leading prefix segment
    (like ``agent/``, ``claude/``, or ``ahs/{agent_name}/``).
    Words are delimited by ``-``, ``_``, ``/``.

    Examples:
        ``agent/fix-auth-bug-a1b2c3``      →  ``fix-auth-bug-a1b2c3``
        ``claude/update-leaderboard``       →  ``update-leaderboard``
        ``ahs/sre/fix-url-typo``            →  ``fix-url-typo``
        ``ahs/code-reviewer/add-retry``     →  ``add-retry``
        ``my-feature``                      →  ``my-feature``
    """
    # Handle "ahs/{agent_name}/{work_name}" branches by extracting the work_name
    # segment directly from the raw string (agent names can contain dashes).
    slash_parts = branch.split("/")
    if len(slash_parts) >= 3 and slash_parts[0] == "ahs":
        # Everything after ahs/{agent_name}/ is the work_name portion
        work_name = "/".join(slash_parts[2:])
        parts = _BRANCH_SLUG_SPLIT.split(work_name)
    else:
        parts = _BRANCH_SLUG_SPLIT.split(branch)
        # Strip known leading prefixes (agent, claude, etc.)
        if parts and parts[0] in ("agent", "claude"):
            parts = parts[1:]
    # Take first 4 words.
    # TODO: this can produce collisions for branches differing only after
    # the 4th word (e.g., "my-feat-branch-extra-v1" vs "my-feat-branch-extra-v2").
    # Consider appending a short hash of the full branch name if collisions
    # become a problem in practice.
    slug = "-".join(parts[:4])
    return slug or branch.replace("/", "-")


def _validate_repo_name(repo: str) -> None:
    """Validate repo name and verify the repo directory exists.

    Checks:
    1. Name matches safe regex (no path traversal characters)
    2. Parent directory resolves to AHS_REPOS_DIR (prevents symlink escapes
       in the *name* component, while allowing the repo itself to be a symlink)
    3. Directory actually exists on disk

    Raises ValueError if any check fails.
    """
    if not _SAFE_NAME_RE.match(repo):
        raise ValueError(f"Invalid repo name: {repo!r}")
    # Resolve the parent to prevent path traversal, but don't resolve the
    # final component — this allows repos that are symlinks (e.g., local dev
    # where ln -sfn $(pwd) /tmp/ahs/repos/yupp-agent is standard practice).
    repo_path = os.path.join(os.path.realpath(AHS_REPOS_DIR), repo)
    if not os.path.isdir(repo_path):
        raise ValueError(f"Repo not found: {repo!r}")


def _validate_branch_name(branch: str) -> None:
    """Validate a git branch name using git check-ref-format.

    Raises ValueError if the branch name is invalid.
    """
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Invalid branch name: {branch!r}")


def list_repos() -> list[dict[str, str]]:
    """List available repos in the repos directory.

    Returns:
        List of dicts with repo name and path.
    """
    repos: list[dict[str, str]] = []

    if not os.path.isdir(AHS_REPOS_DIR):
        logger.warning(
            f"Repos directory not found path={AHS_REPOS_DIR!r}",
            path=AHS_REPOS_DIR,
        )
        return repos

    for entry in sorted(os.listdir(AHS_REPOS_DIR)):
        full_path = os.path.join(AHS_REPOS_DIR, entry)
        if os.path.isdir(full_path) and os.path.isdir(os.path.join(full_path, ".git")):
            repos.append({"name": entry, "path": full_path})

    return repos


def create_worktree(
    repo: str,
    session_id: str,
    branch: str | None = None,
) -> dict[str, str]:
    """Create a git worktree for a session.

    Args:
        repo: Repo name (e.g., 'yupp-agent')
        session_id: Session UUID string
        branch: Branch name. Defaults to 'agent/{session_id}'.

    Returns:
        Dict with workspace path, branch name, and status.

    Raises:
        ValueError: If repo not found.
        subprocess.CalledProcessError: If git command fails.
    """
    _validate_repo_name(repo)
    repo_path = os.path.join(AHS_REPOS_DIR, repo)

    branch = branch or f"agent/{session_id}"
    _validate_branch_name(branch)
    slug = _branch_to_dir_suffix(branch)
    workspace = os.path.join(AHS_SESSIONS_DIR, session_id, f"{repo}-{slug}")

    # Idempotent: if worktree already exists for this session+repo, return it
    if os.path.isdir(workspace):
        logger.info(
            f"Worktree already exists, reusing repo={repo!r} branch={branch!r} session_id={session_id}",
            repo=repo,
            session_id=session_id,
            workspace=workspace,
        )
        return {
            "status": "granted",
            "workspace": workspace,
            "branch": branch,
            "note": "Workspace already exists.",
        }

    os.makedirs(os.path.dirname(workspace), exist_ok=True)

    # Best-effort fetch — repos are periodically refreshed by pull_all_repos(),
    # so a stale origin/main is acceptable when the fetch fails (e.g. transient
    # network error or credential expiry between cron runs).
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = ""
        if hasattr(e, "stderr") and e.stderr:
            stderr = e.stderr.decode("utf-8", errors="replace")[:300]
        logger.warning(
            "git fetch origin failed, using cached origin/main",
            repo=repo,
            session_id=session_id,
            error=type(e).__name__,
            stderr=stderr,
        )

    # Create worktree with new branch from origin/main
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, workspace, "origin/main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Set upstream tracking to origin/<branch> (not origin/main).
    # This must happen here — outside the sandbox .git/config is writable,
    # but inside the sandbox it's read-only so `git push -u` can't update it.
    subprocess.run(
        ["git", "config", f"branch.{branch}.remote", "origin"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", f"branch.{branch}.merge", f"refs/heads/{branch}"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    logger.info(
        f"Created worktree repo={repo!r} branch={branch!r} session_id={session_id}",
        repo=repo,
        session_id=session_id,
        branch=branch,
        workspace=workspace,
    )

    return {
        "status": "granted",
        "workspace": workspace,
        "branch": branch,
        "note": "Workspace available on your next turn.",
    }


def create_worktree_for_branch(
    repo: str,
    session_id: str,
    branch: str,
) -> str:
    """Create a worktree checked out to an existing remote branch.

    Used for PR-triggered sessions where the branch already exists.

    Args:
        repo: Repo name
        session_id: Session UUID string
        branch: Existing branch name

    Returns:
        Workspace path.
    """
    _validate_repo_name(repo)
    repo_path = os.path.join(AHS_REPOS_DIR, repo)

    _validate_branch_name(branch)
    slug = _branch_to_dir_suffix(branch)
    workspace = os.path.join(AHS_SESSIONS_DIR, session_id, f"{repo}-{slug}")
    os.makedirs(os.path.dirname(workspace), exist_ok=True)

    subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Use -B to create a local tracking branch (avoids detached HEAD)
    subprocess.run(
        ["git", "worktree", "add", "-B", branch, workspace, f"origin/{branch}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    logger.info(
        f"Created worktree for existing branch repo={repo!r} branch={branch!r} session_id={session_id}",
        repo=repo,
        session_id=session_id,
        branch=branch,
        workspace=workspace,
    )

    return workspace


def cleanup_worktree(session_id: str, repo: str, worktree_dir: str | None = None) -> None:
    """Remove a worktree for a session.

    Args:
        session_id: Session UUID string
        repo: Repo name (used for the git repo path to run worktree remove)
        worktree_dir: Explicit worktree directory path. If not provided,
            scans the session directory for directories starting with ``{repo}-``.
    """
    repo_path = os.path.join(AHS_REPOS_DIR, repo)

    # Find worktree directory: either explicit or by scanning session dir
    workspaces: list[str] = []
    if worktree_dir:
        workspaces = [worktree_dir]
    else:
        session_dir = os.path.join(AHS_SESSIONS_DIR, session_id)
        if os.path.isdir(session_dir):
            for entry in os.listdir(session_dir):
                full = os.path.join(session_dir, entry)
                if entry.startswith(f"{repo}-") and os.path.isdir(full) and not os.path.islink(full):
                    workspaces.append(full)

    for workspace in workspaces:
        if not os.path.isdir(workspace):
            continue

        subprocess.run(
            ["git", "worktree", "remove", workspace, "--force"],
            cwd=repo_path,
            check=False,
            capture_output=True,
        )

        logger.info(
            f"Cleaned up worktree repo={repo!r} session_id={session_id}",
            session_id=session_id,
            repo=repo,
            workspace=workspace,
        )


def _normalize_origin_to_https(workspace: str) -> None:
    """Rewrite origin from SSH to HTTPS so GH_TOKEN auth applies on push.

    git push over SSH ignores env vars and uses only ``~/.ssh/`` identities /
    ``SSH_AUTH_SOCK``. When the shared repo's origin has drifted to an SSH
    URL (e.g. cloned with a deploy key), pushes get routed to a read-only
    deploy key and fail even when a valid GH_TOKEN is in the env. Rewriting
    to HTTPS lets the gh credential helper hand off GH_TOKEN for auth.
    Worktrees share config with the parent repo, so this also heals the
    shared clone for future sessions. Idempotent.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return
    url = result.stdout.strip()
    if url.startswith("git@github.com:"):
        https_url = "https://github.com/" + url[len("git@github.com:") :]
    elif url.startswith("ssh://git@github.com/"):
        https_url = "https://github.com/" + url[len("ssh://git@github.com/") :]
    else:
        return
    subprocess.run(
        ["git", "remote", "set-url", "origin", https_url],
        cwd=workspace,
        check=False,
        capture_output=True,
    )
    logger.info(
        f"Rewrote origin SSH -> HTTPS workspace={workspace!r}",
        workspace=workspace,
        old_url=url,
        new_url=https_url,
    )


def push_and_create_pr(
    workspace: str,
    title: str,
    body: str,
    branch: str | None = None,
    base: str | None = None,
    user_github_token: str | None = None,
    draft: bool = True,
) -> dict[str, str]:
    """Push the current branch and create a PR via `gh` CLI.

    Args:
        workspace: Path to the worktree
        title: PR title
        body: PR body
        branch: Branch name override. Defaults to current branch.
        base: Base branch for the PR (e.g., parent branch for stacked PRs).
            Defaults to the repository's default branch (main).
        user_github_token: User's GitHub token from device flow (stored in memory).
            If provided, the PR will be attributed to the user who authorized.
        draft: If True, create the PR in draft mode. Defaults to True.

    Returns:
        Dict with PR URL and status.
    """
    # Use user token if available (from device flow, stored in memory)
    # GH_TOKEN env var overrides default gh auth
    env = os.environ.copy()
    if user_github_token:
        env["GH_TOKEN"] = user_github_token
        logger.info("Using user GitHub token for PR")

    if not branch:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip()
    else:
        _validate_branch_name(branch)

    if base:
        _validate_branch_name(base)

    _normalize_origin_to_https(workspace)

    # Push the branch
    try:
        subprocess.run(
            ["git", "push", "-u", "origin", "--", branch],
            cwd=workspace,
            env=env,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace")[:500] if e.stderr else ""
        logger.error(
            f"Failed to push branch branch={branch!r}",
            branch=branch,
            stderr=stderr,
        )
        return {"status": "error", "error": f"git push failed: {stderr}"}

    # Create PR via gh CLI (GH_TOKEN in env overrides default auth)
    try:
        cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch]
        if base:
            cmd.extend(["--base", base])
        if draft:
            cmd.append("--draft")
        result = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        logger.error(
            f"Failed to create PR branch={branch!r}",
            branch=branch,
            stderr=stderr,
        )
        return {"status": "error", "error": f"gh pr create failed: {stderr}"}

    pr_url = result.stdout.strip()

    logger.info(
        f"Created PR branch={branch!r} pr_url={pr_url}",
        branch=branch,
        pr_url=pr_url,
    )

    return {"status": "created", "pr_url": pr_url, "branch": branch}


def pull_repo(repo_path: str) -> bool:
    """Pull latest main for a repo.

    Returns:
        True if pull succeeded, False otherwise.
    """
    try:
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Failed to pull repo repo_path={repo_path!r}",
            repo_path=repo_path,
            stderr=e.stderr.decode("utf-8", errors="replace")[:500] if e.stderr else "",
        )
        return False


def pull_all_repos() -> dict[str, bool]:
    """Pull all repos in the repos directory.

    Returns:
        Dict mapping repo name to success status.
    """
    results: dict[str, bool] = {}

    if not os.path.isdir(AHS_REPOS_DIR):
        logger.warning(
            f"Repos directory not found path={AHS_REPOS_DIR!r}",
            path=AHS_REPOS_DIR,
        )
        return results

    for entry in sorted(os.listdir(AHS_REPOS_DIR)):
        full_path = os.path.join(AHS_REPOS_DIR, entry)
        if os.path.isdir(full_path) and os.path.isdir(os.path.join(full_path, ".git")):
            results[entry] = pull_repo(full_path)

    ok = sum(1 for v in results.values() if v)
    fail = len(results) - ok
    logger.info(
        f"Pulled all repos: {ok} ok, {fail} failed (repos={list(results.keys())})",
        results=results,
    )
    return results


def parse_pr_url(pr_url: str) -> tuple[str, str]:
    """Extract repo name and branch from a GitHub PR URL.

    Uses `gh` CLI to get the branch name from the PR.

    Args:
        pr_url: GitHub PR URL (e.g., 'https://github.com/yupp-ai/yupp-agent/pull/123')

    Returns:
        Tuple of (repo_name, branch_name).

    Raises:
        ValueError: If URL format is invalid or PR not found.
    """
    match = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError(f"Invalid PR URL format: {pr_url}")

    repo_name = match.group(1)

    # Get branch name from PR via gh CLI
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "headRefName", "-q", ".headRefName"],
            check=True,
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise ValueError(f"Failed to get PR branch: {e}") from e

    return repo_name, branch


def setup_workspace(
    session_id: str,
    context: dict,
    default_repo: str = "yupp-agent",
) -> str:
    """Set up the workspace for a session based on context.

    If context has a pr_url, creates a worktree on the PR's branch.
    Otherwise, uses the shared read-only checkout.

    Args:
        session_id: Session UUID string
        context: Session context dict (may contain 'repo', 'pr_url')
        default_repo: Fallback repo name

    Returns:
        Workspace path.
    """
    repo = context.get("repo", default_repo)
    _validate_repo_name(repo)
    pr_url = context.get("pr_url")

    if pr_url:
        try:
            repo_name, branch = parse_pr_url(pr_url)
            return create_worktree_for_branch(repo_name, session_id, branch)
        except (ValueError, subprocess.CalledProcessError) as e:
            logger.error(
                f"Failed to set up PR worktree, falling back to read-only pr_url={pr_url!r} error={e!s}",
                error=str(e),
            )

    return os.path.join(AHS_REPOS_DIR, repo)


def scan_session_worktrees(session_id: str) -> list[str]:
    """Scan for worktree directories created for a session.

    Used after each turn to discover new worktrees created by the MCP server.
    Skips symlinks (those are read-only repo references) and only returns
    real directories (actual git worktrees).

    Args:
        session_id: Session UUID string

    Returns:
        List of worktree directory paths.
    """
    session_workspace_dir = os.path.join(AHS_SESSIONS_DIR, session_id)
    if not os.path.isdir(session_workspace_dir):
        return []

    worktrees: list[str] = []
    for entry in sorted(os.listdir(session_workspace_dir)):
        if entry.startswith("."):
            continue
        full_path = os.path.join(session_workspace_dir, entry)
        # Skip symlinks (repo references) and non-directories (e.g., .mcp.json)
        if os.path.islink(full_path) or not os.path.isdir(full_path):
            continue
        # Skip infrastructure subdirectories
        if entry in SESSION_INFRA_DIRS:
            continue
        worktrees.append(full_path)

    return worktrees
