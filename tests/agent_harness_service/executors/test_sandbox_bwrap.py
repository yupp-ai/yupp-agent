"""Unit tests for sandbox bwrap command building.

Tests build_bwrap_cli_command to ensure it correctly handles git subdirectories,
including pre-creating lazy directories like .git/worktrees/.
Would have caught PR #43 where repos without worktrees dir failed in bwrap.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from ypl.agent_harness_service.executors.sandbox import (
    _GIT_RW_SUBDIRS,
    _resolve_inner_symlinks,
    build_bwrap_cli_command,
)


class TestBuildBwrapCliCommand:
    """Test build_bwrap_cli_command generates correct bwrap args."""

    @pytest.fixture
    def workspace_with_repo(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a workspace with a symlinked repo containing .git/."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        repo = tmp_path / "repos" / "my-repo"
        repo.mkdir(parents=True)
        git_dir = repo / ".git"
        git_dir.mkdir()

        # Symlink repo into workspace
        (workspace / "my-repo").symlink_to(repo)

        return workspace, repo

    def test_precreates_missing_git_subdirs(self, workspace_with_repo: tuple[Path, Path]) -> None:
        """Repos without .git/worktrees/ should have it pre-created."""
        workspace, repo = workspace_with_repo
        git_dir = repo / ".git"

        # Verify worktrees doesn't exist yet
        assert not (git_dir / "worktrees").exists()

        # Mock _resolve_workspace_symlinks to return our repo
        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        # All _GIT_RW_SUBDIRS should now exist
        for subdir in _GIT_RW_SUBDIRS:
            assert (git_dir / subdir).is_dir(), f".git/{subdir} should be pre-created by build_bwrap_cli_command"

    def test_git_rw_subdirs_appear_as_bind_mounts(self, workspace_with_repo: tuple[Path, Path]) -> None:
        """Each _GIT_RW_SUBDIRS entry should appear as --bind in bwrap args."""
        workspace, repo = workspace_with_repo
        git_dir = repo / ".git"

        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            args = build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        args_str = " ".join(args)
        for subdir in _GIT_RW_SUBDIRS:
            subdir_path = str(git_dir / subdir)
            assert subdir_path in args_str, f".git/{subdir} should be bind-mounted"

    def test_workspace_is_rw_bind(self, workspace_with_repo: tuple[Path, Path]) -> None:
        """Workspace should appear as --bind (read-write)."""
        workspace, repo = workspace_with_repo

        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            args = build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        # Find --bind workspace workspace
        found = False
        for i, arg in enumerate(args):
            if arg == "--bind" and i + 2 < len(args) and args[i + 1] == str(workspace):
                found = True
                break
        assert found, "Workspace should be bind-mounted read-write"

    def test_namespace_isolation_flags(self, workspace_with_repo: tuple[Path, Path]) -> None:
        """CLI bwrap should unshare pid/ipc/uts but NOT net."""
        workspace, repo = workspace_with_repo

        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            args = build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        assert "--unshare-pid" in args
        assert "--unshare-ipc" in args
        assert "--unshare-uts" in args
        assert "--unshare-net" not in args  # CLI needs network

    def test_die_with_parent(self, workspace_with_repo: tuple[Path, Path]) -> None:
        workspace, repo = workspace_with_repo

        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            args = build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        assert "--die-with-parent" in args

    def test_command_appended_at_end(self, workspace_with_repo: tuple[Path, Path]) -> None:
        workspace, repo = workspace_with_repo

        with (
            patch(
                "ypl.agent_harness_service.executors.sandbox._resolve_workspace_symlinks",
                return_value=([(str(repo), str(workspace / "my-repo"))], []),
            ),
            patch(
                "ypl.agent_harness_service.executors.sandbox._bwrap_system_mounts",
                return_value=[],
            ),
        ):
            args = build_bwrap_cli_command(["claude", "-p", "hello"], str(workspace))

        # Last 3 args should be the original command
        assert args[-3:] == ["claude", "-p", "hello"]


class TestResolveInnerSymlinks:
    """Test _resolve_inner_symlinks security and correctness."""

    def test_allowed_symlink_generates_ro_bind(self, tmp_path: Path) -> None:
        target = tmp_path / "allowed" / "skills"
        target.mkdir(parents=True)
        link_dir = tmp_path / "links"
        link_dir.mkdir()
        (link_dir / "my-skill").symlink_to(target)

        result = _resolve_inner_symlinks(str(link_dir), (str(tmp_path / "allowed"),))
        assert len(result) >= 2  # --ro-bind <target> <target>
        assert "--ro-bind" in result

    def test_disallowed_symlink_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "forbidden" / "secret"
        target.mkdir(parents=True)
        link_dir = tmp_path / "links"
        link_dir.mkdir()
        (link_dir / "evil").symlink_to(target)

        result = _resolve_inner_symlinks(str(link_dir), (str(tmp_path / "allowed"),))
        assert result == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        result = _resolve_inner_symlinks(str(tmp_path / "noexist"), ())
        assert result == []

    def test_no_symlinks_returns_empty(self, tmp_path: Path) -> None:
        regular_dir = tmp_path / "regular"
        regular_dir.mkdir()
        (regular_dir / "file.txt").write_text("hello")
        result = _resolve_inner_symlinks(str(regular_dir), ())
        assert result == []
