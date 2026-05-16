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
from typing import Any

import yaml

from ypl.agent_harness_service.common.constants import AHS_REPOS_DIR, AHS_SESSIONS_DIR, SESSION_INFRA_DIRS
from ypl.structured_logger import get_logger

logger = get_logger()

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Regex to split branch names into word-like segments
_BRANCH_SLUG_SPLIT = re.compile(r"[-_/]+")

# Accepted clone URL shape — https GitHub URLs only. SSH origins are rewritten
# by _normalize_origin_to_https() at push time, but for new clones we require
# https up-front so the GitHub App credential helper handles auth.
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,99}?)(?:\.git)?/?$"
)

# Path to the shared_repos.yaml config that lists which repos should exist
# on every VM. Resolved relative to this module so it works in both the
# in-tree dev layout and the deployed /opt/yupp-agent layout.
_DEPLOY_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "deploy"))
SHARED_REPOS_CONFIG_PATH = os.environ.get(
    "AHS_SHARED_REPOS_CONFIG",
    os.path.join(_DEPLOY_DIR, "shared_repos.yaml"),
)


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


# ---------------------------------------------------------------------------
# Shared repo config — declarative list of "what should be on every VM"
# ---------------------------------------------------------------------------


def _parse_github_url(url: str) -> tuple[str, str]:
    """Parse a GitHub https clone URL into (owner, name).

    Raises ValueError if the URL doesn't look like an https GitHub URL.
    """
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Invalid GitHub URL: {url!r}. Expected https://github.com/{{owner}}/{{repo}}[.git]")
    return m.group("owner"), m.group("name")


def repo_name_from_url(url: str) -> str:
    """Derive a directory name (the repo slug) from a clone URL."""
    return _parse_github_url(url)[1]


def load_shared_repos_config() -> list[dict[str, Any]]:
    """Load the declarative shared_repos.yaml list.

    Returns an empty list if the file is missing or malformed (with a
    warning) — the pull loop falls back to discovering whatever is already
    on disk, so a broken config never silently strands a VM.

    Each entry has at least: ``name`` (str), ``url`` (str), ``protected``
    (bool, default False). Unknown keys are preserved but ignored.
    """
    if not os.path.isfile(SHARED_REPOS_CONFIG_PATH):
        logger.warning(
            f"shared_repos.yaml not found path={SHARED_REPOS_CONFIG_PATH!r}",
            path=SHARED_REPOS_CONFIG_PATH,
        )
        return []
    try:
        with open(SHARED_REPOS_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.error(
            f"Failed to load shared_repos.yaml path={SHARED_REPOS_CONFIG_PATH!r}",
            path=SHARED_REPOS_CONFIG_PATH,
            error=str(e),
        )
        return []

    raw = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        logger.warning(
            f"shared_repos.yaml missing 'repos' list path={SHARED_REPOS_CONFIG_PATH!r}",
            path=SHARED_REPOS_CONFIG_PATH,
        )
        return []

    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            logger.warning("Skipping shared_repos.yaml entry without name/url", entry=entry)
            continue
        if not _SAFE_NAME_RE.match(name):
            logger.warning(f"Skipping shared_repos.yaml entry with unsafe name name={name!r}", name=name)
            continue
        out.append(
            {
                "name": name,
                "url": url,
                "protected": bool(entry.get("protected", False)),
            }
        )
    return out


def is_repo_protected(name: str) -> bool:
    """Return True if the named repo is marked ``protected: true`` in config."""
    for entry in load_shared_repos_config():
        if entry["name"] == name:
            return bool(entry.get("protected", False))
    return False


def ensure_repo_cloned(name: str, url: str) -> dict[str, str]:
    """Clone the repo into AHS_REPOS_DIR if it isn't there yet.

    Idempotent — if the target directory already exists and looks like a
    git repo, returns ``{"status": "exists"}``. Otherwise runs
    ``git clone {url} {AHS_REPOS_DIR}/{name}`` and returns
    ``{"status": "cloned"}`` (or ``{"status": "error", "error": ...}``).

    Validates ``name`` against the safe-name regex and verifies the URL
    parses as an https GitHub URL. URL/name consistency is _not_ enforced
    (callers may want to alias) but the URL must still be a GitHub URL so
    the GitHub App credential helper applies.
    """
    if not _SAFE_NAME_RE.match(name):
        return {"status": "error", "error": f"Invalid repo name: {name!r}"}
    try:
        _parse_github_url(url)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    os.makedirs(AHS_REPOS_DIR, exist_ok=True)
    target = os.path.join(AHS_REPOS_DIR, name)
    if os.path.isdir(os.path.join(target, ".git")):
        return {"status": "exists", "path": target}
    if os.path.exists(target):
        return {
            "status": "error",
            "error": f"Path exists but is not a git repo: {target!r}",
        }

    try:
        subprocess.run(
            ["git", "clone", url, target],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"git clone timed out for {url!r}"}
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace")[:500] if e.stderr else ""
        logger.error(
            f"git clone failed name={name!r} url={url!r}",
            name=name,
            url=url,
            stderr=stderr,
        )
        return {"status": "error", "error": f"git clone failed: {stderr}"}

    logger.info(f"Cloned shared repo name={name!r} url={url!r} path={target!r}", name=name, url=url, path=target)
    return {"status": "cloned", "path": target}


def remove_repo_from_disk(name: str) -> dict[str, str]:
    """Remove a repo's directory from AHS_REPOS_DIR.

    Refuses if the repo is marked ``protected: true`` in shared_repos.yaml
    (e.g. yupp-agent itself, which the harness runs from).

    Refuses to recurse into anything outside AHS_REPOS_DIR even if a
    symlink points elsewhere — the symlink itself is unlinked, never
    followed.
    """
    if not _SAFE_NAME_RE.match(name):
        return {"status": "error", "error": f"Invalid repo name: {name!r}"}
    if is_repo_protected(name):
        return {
            "status": "error",
            "error": f"Repo {name!r} is protected (marked protected: true in shared_repos.yaml); refusing to remove.",
        }
    target = os.path.join(AHS_REPOS_DIR, name)
    if not os.path.exists(target) and not os.path.islink(target):
        return {"status": "missing", "path": target}

    # Symlinks: unlink without following. Used in local dev where repos are
    # often symlinks to a checkout elsewhere.
    if os.path.islink(target):
        try:
            os.unlink(target)
        except OSError as e:
            return {"status": "error", "error": f"failed to unlink: {e}"}
        logger.info(f"Removed shared repo (symlink) name={name!r} path={target!r}", name=name, path=target)
        return {"status": "removed", "path": target}

    # Regular directory: rm -rf, but only after we've confirmed it lives
    # directly under the real AHS_REPOS_DIR (defense in depth against a
    # crafted name like '..' slipping past the regex on a future change).
    real_parent = os.path.realpath(os.path.dirname(target))
    real_repos = os.path.realpath(AHS_REPOS_DIR)
    if real_parent != real_repos:
        return {
            "status": "error",
            "error": f"Refusing to remove path outside AHS_REPOS_DIR: {target!r}",
        }
    try:
        subprocess.run(["rm", "-rf", "--", target], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace")[:500] if e.stderr else ""
        return {"status": "error", "error": f"rm -rf failed: {stderr}"}
    logger.info(f"Removed shared repo name={name!r} path={target!r}", name=name, path=target)
    return {"status": "removed", "path": target}


def list_repos_with_metadata() -> list[dict[str, Any]]:
    """List shared repos with config metadata.

    Merges the on-disk view (``list_repos()``) with the declarative
    ``shared_repos.yaml`` view. Each entry has:

    - ``name``: directory name under AHS_REPOS_DIR
    - ``path``: absolute path (None if config entry isn't cloned yet)
    - ``in_config``: True if listed in shared_repos.yaml
    - ``protected``: True if config entry has ``protected: true``
    - ``url``: clone URL from config (None for ad-hoc on-disk repos)
    - ``on_disk``: True if directory exists under AHS_REPOS_DIR
    """
    by_name: dict[str, dict[str, Any]] = {}
    for cfg in load_shared_repos_config():
        by_name[cfg["name"]] = {
            "name": cfg["name"],
            "path": None,
            "in_config": True,
            "protected": cfg["protected"],
            "url": cfg["url"],
            "on_disk": False,
        }
    for disk in list_repos():
        existing = by_name.get(disk["name"])
        if existing is None:
            by_name[disk["name"]] = {
                "name": disk["name"],
                "path": disk["path"],
                "in_config": False,
                "protected": False,
                "url": None,
                "on_disk": True,
            }
        else:
            existing["path"] = disk["path"]
            existing["on_disk"] = True
    return sorted(by_name.values(), key=lambda e: e["name"])


def pull_all_repos() -> dict[str, bool]:
    """Ensure every configured repo is cloned, then pull every on-disk repo.

    The pull cycle has two phases:

    1. *Ensure-clone* — for each entry in ``shared_repos.yaml``, clone it
       into ``AHS_REPOS_DIR`` if missing. Failures here are logged but
       don't abort the cycle.
    2. *Pull* — for every repo currently on disk (whether from the config
       or added ad-hoc by ``add_shared_repo``), ``git pull --ff-only``.

    Returns:
        Dict mapping repo name to pull success status. Repos that were
        freshly cloned in phase 1 also get pulled in phase 2 (no-op fast
        path) so the return dict always reflects every repo on disk.
    """
    results: dict[str, bool] = {}

    # Phase 1: ensure every configured repo is cloned.
    for entry in load_shared_repos_config():
        clone_result = ensure_repo_cloned(entry["name"], entry["url"])
        if clone_result["status"] == "error":
            logger.warning(
                f"ensure_repo_cloned failed name={entry['name']!r}",
                name=entry["name"],
                error=clone_result.get("error", ""),
            )

    # Phase 2: pull every repo present on disk.
    if not os.path.isdir(AHS_REPOS_DIR):
        logger.warning(
            f"Repos directory not found path={AHS_REPOS_DIR!r}",
            path=AHS_REPOS_DIR,
        )
        return results

    for entry_name in sorted(os.listdir(AHS_REPOS_DIR)):
        full_path = os.path.join(AHS_REPOS_DIR, entry_name)
        if os.path.isdir(full_path) and os.path.isdir(os.path.join(full_path, ".git")):
            results[entry_name] = pull_repo(full_path)

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
