"""Tests for cloning arbitrary GitHub repos into the shared repos directory."""

from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from ypl.agent_harness_service.tools import repo_manager
from ypl.agent_harness_service.tools.repo_manager import RepoCloneError, ensure_repo, local_repo_name


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_upstream(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", branch)
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-m", "init")
    return path


@pytest.fixture
def repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "repos"
    path.mkdir()
    monkeypatch.setattr(repo_manager, "AHS_REPOS_DIR", str(path))
    monkeypatch.setattr(repo_manager, "AHS_ALLOW_REPO_CLONE", True)
    return path


def _clone_from(upstream: Path, captured: dict[str, Any] | None = None) -> Any:
    """Patch subprocess.run so 'git clone' of a GitHub URL reads a local upstream instead."""
    real_run = subprocess.run

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:2] == ["git", "clone"]:
            if captured is not None:
                captured.update(cmd=list(cmd), env=kwargs.get("env"))
            cmd = [*cmd[:3], str(upstream), cmd[4]]
        return real_run(cmd, **kwargs)

    return patch.object(repo_manager.subprocess, "run", side_effect=fake_run)


@pytest.mark.parametrize(
    "spec",
    [
        "owner/example-repo",
        "https://github.com/owner/example-repo",
        "https://github.com/owner/example-repo.git",
        "git@github.com:owner/example-repo.git",
    ],
)
def test_local_repo_name_accepts_github_specs(spec: str) -> None:
    assert local_repo_name(spec) == "example-repo"


def test_plain_name_is_not_cloned(repos_dir: Path) -> None:
    assert ensure_repo("yupp-agent") == "yupp-agent"
    assert list(repos_dir.iterdir()) == []


@pytest.mark.parametrize("spec", ["https://evil.example/o/r", "--upload-pack=x/y", "o/../../etc", "a/b/c"])
def test_non_github_specs_are_never_cloned(repos_dir: Path, spec: str) -> None:
    with patch.object(repo_manager.subprocess, "run") as mock_run:
        assert ensure_repo(spec) == spec
    mock_run.assert_not_called()


def test_clone_passes_token_by_env_not_url(repos_dir: Path, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with _clone_from(_make_upstream(tmp_path / "up"), captured):
        assert ensure_repo("owner/example-repo", "secret-token") == "example-repo"

    assert (repos_dir / "example-repo" / ".git").is_dir()
    assert captured["env"]["GH_TOKEN"] == "secret-token"
    assert not any("secret-token" in part for part in captured["cmd"])
    assert captured["cmd"][3] == "https://github.com/owner/example-repo.git"
    assert [p.name for p in repos_dir.iterdir()] == ["example-repo"]


def test_clone_disabled_raises(repos_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo_manager, "AHS_ALLOW_REPO_CLONE", False)
    with pytest.raises(ValueError, match="cloning is disabled"):
        ensure_repo("owner/example-repo")


def test_failed_clone_leaves_nothing_behind(repos_dir: Path) -> None:
    error = subprocess.CalledProcessError(128, "git", stderr=b"repository not found")
    with patch.object(repo_manager.subprocess, "run", side_effect=error), pytest.raises(RepoCloneError):
        ensure_repo("owner/missing")
    assert list(repos_dir.iterdir()) == []


def test_existing_repo_with_same_owner_is_reused(repos_dir: Path, tmp_path: Path) -> None:
    existing = _make_upstream(repos_dir / "example-repo")
    _git(existing, "remote", "add", "origin", "https://github.com/Owner/example-repo.git")
    assert ensure_repo("owner/example-repo") == "example-repo"


def test_name_collision_with_other_owner_is_rejected(repos_dir: Path) -> None:
    existing = _make_upstream(repos_dir / "example-repo")
    _git(existing, "remote", "add", "origin", "https://github.com/someone-else/example-repo.git")
    with pytest.raises(ValueError, match="different repository"):
        ensure_repo("owner/example-repo")


def test_worktree_starts_from_default_branch_of_cloned_repo(
    repos_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(repo_manager, "AHS_SESSIONS_DIR", str(sessions))
    with _clone_from(_make_upstream(tmp_path / "up", branch="master")):
        ensure_repo("owner/legacy")

    with patch.object(repo_manager, "_git_env", return_value=None):
        result = repo_manager.create_worktree("legacy", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "feature-x")

    assert result["status"] == "granted"
    assert Path(result["workspace"]).is_dir()
