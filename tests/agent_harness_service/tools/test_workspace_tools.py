"""Tests for workspace_tools — filesystem and shell access for raw executors."""

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ypl.agent_harness_service.executors.sandbox import (
    build_bwrap_command,
    bwrap_available,
)
from ypl.agent_harness_service.tools.workspace_tools import (
    _validate_url,
    edit_file,
    list_files,
    read_file,
    resolve_workspace,
    run_command,
    safe_path,
    search_files,
    write_file,
)

# Test UUIDs
VALID_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INVALID_SESSION = "not-a-uuid"


# ---------------------------------------------------------------------------
# resolve_workspace
# ---------------------------------------------------------------------------


class TestResolveWorkspace:
    def _setup_session(self, tmp_path: Path) -> Path:
        """Create a session dir with the shared ``repos/`` symlink (mirrors session_lifecycle)."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir(parents=True)
        # Create the shared repos dir and symlink it into the session as ``repos/``.
        repos_dir = tmp_path / "repos"
        (repos_dir / "yupp-agent").mkdir(parents=True)
        (repos_dir / "other-repo").mkdir(parents=True)
        (session_dir / "repos").symlink_to(repos_dir)
        (session_dir / "history").mkdir()
        return repos_dir

    def test_worktree_found(self, tmp_path: Path) -> None:
        """Finds session worktree (real dir with slug suffix) when it exists."""
        self._setup_session(tmp_path)
        worktree = tmp_path / VALID_SESSION / "yupp-agent-fix-auth-bug"
        worktree.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = resolve_workspace(VALID_SESSION, repo="yupp-agent")
            assert result == str(worktree)

    def test_worktree_single_no_repo(self, tmp_path: Path) -> None:
        """Finds single worktree when no repo is specified."""
        self._setup_session(tmp_path)
        worktree = tmp_path / VALID_SESSION / "yupp-agent-fix-auth-bug"
        worktree.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = resolve_workspace(VALID_SESSION)
            assert result == str(worktree)

    def test_multiple_worktrees_requires_repo(self, tmp_path: Path) -> None:
        """Errors when multiple worktrees exist and no repo specified."""
        self._setup_session(tmp_path)
        (tmp_path / VALID_SESSION / "yupp-agent-fix-auth").mkdir(parents=True)
        (tmp_path / VALID_SESSION / "other-repo-add-feature").mkdir(parents=True)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="Multiple worktrees"),
        ):
            resolve_workspace(VALID_SESSION)

    def test_fallback_to_session_dir(self, tmp_path: Path) -> None:
        """Falls back to session dir (with repo symlinks) when no worktree exists."""
        self._setup_session(tmp_path)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = resolve_workspace(VALID_SESSION)
            assert result == str(tmp_path / VALID_SESSION)

    def test_fallback_to_shared_repo(self, tmp_path: Path) -> None:
        """Falls back to shared read-only repo at AHS_REPOS_DIR when no session dir."""
        repos_dir = tmp_path / "repos"
        (repos_dir / "yupp-agent").mkdir(parents=True)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path / "sessions")),
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_REPOS_DIR", str(repos_dir)),
        ):
            result = resolve_workspace(VALID_SESSION)
            assert result == str(repos_dir / "yupp-agent")

    def test_write_requires_worktree(self, tmp_path: Path) -> None:
        """require_write=True errors when only symlinked repo exists."""
        self._setup_session(tmp_path)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="Write access requires"),
        ):
            resolve_workspace(VALID_SESSION, require_write=True)

    def test_invalid_session_id(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            resolve_workspace(INVALID_SESSION)

    def test_no_workspace_found(self, tmp_path: Path) -> None:
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path / "sessions")),
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_REPOS_DIR", str(tmp_path / "repos")),
            pytest.raises(ValueError, match="No workspace found"),
        ):
            resolve_workspace(VALID_SESSION)

    def test_repo_traversal_rejected(self) -> None:
        """Repo names with '..' should be rejected to prevent path traversal."""
        with pytest.raises(ValueError, match="Invalid repo name"):
            resolve_workspace(VALID_SESSION, repo="../../etc")

    def test_repo_absolute_path_rejected(self) -> None:
        """Absolute repo paths should be rejected."""
        with pytest.raises(ValueError, match="Invalid repo name"):
            resolve_workspace(VALID_SESSION, repo="/etc/passwd")


# ---------------------------------------------------------------------------
# safe_path
# ---------------------------------------------------------------------------


class TestSafePath:
    def test_valid_path(self, tmp_path: Path) -> None:
        result = safe_path(str(tmp_path), "src/main.py")
        assert result == os.path.join(os.path.realpath(str(tmp_path)), "src", "main.py")

    def test_escape_via_dotdot(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Path escapes workspace"):
            safe_path(str(tmp_path), "../../../etc/passwd")

    def test_escape_via_symlink(self, tmp_path: Path) -> None:
        # Create a symlink that points outside workspace
        link = tmp_path / "escape"
        link.symlink_to("/tmp")

        with pytest.raises(ValueError, match="Path escapes workspace"):
            safe_path(str(tmp_path), "escape/something")

    def test_workspace_root_itself(self, tmp_path: Path) -> None:
        """Requesting the root itself should be valid (e.g., for glob operations)."""
        result = safe_path(str(tmp_path), ".")
        assert result == os.path.realpath(str(tmp_path))


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    def _setup_workspace(self, tmp_path: Path) -> str:
        """Create a workspace with a test file."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "test.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        return str(tmp_path)

    def test_basic_read(self, tmp_path: Path) -> None:
        workspaces_dir = self._setup_workspace(tmp_path)
        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", workspaces_dir):
            result = read_file(VALID_SESSION, "test.txt")
        assert isinstance(result, str)
        assert "1\tline1" in result
        assert "5\tline5" in result

    def test_offset_and_limit(self, tmp_path: Path) -> None:
        workspaces_dir = self._setup_workspace(tmp_path)
        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", workspaces_dir):
            result = read_file(VALID_SESSION, "test.txt", offset=2, limit=2)
        assert isinstance(result, str)
        assert "3\tline3" in result
        assert "4\tline4" in result
        assert "line1" not in result
        assert "line5" not in result

    def test_file_not_found(self, tmp_path: Path) -> None:
        workspaces_dir = self._setup_workspace(tmp_path)
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", workspaces_dir),
            pytest.raises(ValueError, match="File not found"),
        ):
            read_file(VALID_SESSION, "nonexistent.txt")

    def test_binary_file_blocked(self, tmp_path: Path) -> None:
        """Binary files (by extension) should be rejected."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "data.zip").write_bytes(b"\x00" * 100)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="Cannot read binary file"),
        ):
            read_file(VALID_SESSION, "data.zip")

    def test_long_line_truncation(self, tmp_path: Path) -> None:
        """Lines longer than _MAX_LINE_LENGTH should be truncated."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        long_line = "x" * 5000
        (workspace / "long.txt").write_text(long_line + "\n")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = read_file(VALID_SESSION, "long.txt")
        assert isinstance(result, str)
        assert "truncated to 2000 chars" in result
        # Should not contain the full 5000 char line
        assert "x" * 5000 not in result


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def test_create_new_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = write_file(VALID_SESSION, "new.txt", "hello world")
        assert "11 bytes" in result
        assert (workspace / "new.txt").read_text() == "hello world"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            write_file(VALID_SESSION, "deep/nested/file.txt", "content")
        assert (workspace / "deep" / "nested" / "file.txt").read_text() == "content"

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "existing.txt").write_text("old")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            write_file(VALID_SESSION, "existing.txt", "new")
        assert (workspace / "existing.txt").read_text() == "new"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


class TestEditFile:
    def test_single_replace(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("def foo():\n    pass\n")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = edit_file(VALID_SESSION, "code.py", "pass", "return 42")
        assert "1 occurrence" in result
        assert (workspace / "code.py").read_text() == "def foo():\n    return 42\n"

    def test_replace_all(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("a = 1\nb = a\nc = a\n")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = edit_file(VALID_SESSION, "code.py", "a", "x", replace_all=True)
        assert "3 occurrence" in result

    def test_not_found(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("hello")

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="not found"),
        ):
            edit_file(VALID_SESSION, "code.py", "xyz", "abc")

    def test_not_unique_errors(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("a = 1\nb = a\n")

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="2 times"),
        ):
            edit_file(VALID_SESSION, "code.py", "a", "x")


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_glob_pattern(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "main.py").write_text("")
        (workspace / "src" / "util.py").write_text("")
        (workspace / "README.md").write_text("")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = list_files(VALID_SESSION, "*.py", "src")
        assert "main.py" in result
        assert "util.py" in result
        assert "README" not in result

    def test_no_matches(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = list_files(VALID_SESSION, "*.py")
        assert result == "(no matches)"

    def test_skip_dirs(self, tmp_path: Path) -> None:
        """Should skip .git, node_modules, __pycache__, etc."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        (workspace / ".git").mkdir(parents=True)
        (workspace / "node_modules").mkdir(parents=True)
        (workspace / "src").mkdir(parents=True)
        (workspace / ".git" / "config").write_text("")
        (workspace / "node_modules" / "pkg.js").write_text("")
        (workspace / "src" / "app.py").write_text("")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            # Use **/* for recursive matching (pathlib glob semantics)
            result = list_files(VALID_SESSION, "**/*")
        assert "app.py" in result
        assert "config" not in result
        assert "pkg.js" not in result

    def test_star_matches_current_dir_only(self, tmp_path: Path) -> None:
        """* should match only in the current directory, not recursively."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        (workspace / "sub").mkdir(parents=True)
        (workspace / "top.py").write_text("")
        (workspace / "sub" / "nested.py").write_text("")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = list_files(VALID_SESSION, "*.py")
        assert "top.py" in result
        assert "nested.py" not in result

    def test_dotdot_pattern_rejected(self, tmp_path: Path) -> None:
        """Patterns with '..' segments should be rejected to prevent workspace escape."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="must not contain"),
        ):
            list_files(VALID_SESSION, "../*")

    def test_symlink_outside_workspace_skipped(self, tmp_path: Path) -> None:
        """Symlinks pointing outside workspace should be excluded from results."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("sensitive")
        (workspace / "secret.txt").symlink_to(outside / "secret.txt")
        (workspace / "normal.txt").write_text("ok")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = list_files(VALID_SESSION, "*")
        assert "normal.txt" in result
        assert "secret.txt" not in result

    def test_repo_symlink_paths_are_rendered_relative_to_session(self, tmp_path: Path) -> None:
        """Session repo symlinks should validate by real path but render by session path."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir(parents=True)
        repos_dir = tmp_path / "repos"
        repo = repos_dir / "yupp-agent"
        repo.mkdir(parents=True)
        (repo / "README.md").write_text("")
        (session_dir / "repos").symlink_to(repos_dir)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_REPOS_DIR", str(repos_dir)),
        ):
            result = list_files(VALID_SESSION, "*.md", "repos/yupp-agent")

        assert result == "repos/yupp-agent/README.md"


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


class TestSearchFiles:
    def test_regex_matching(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("def hello():\n    pass\ndef world():\n    pass\n")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = search_files(VALID_SESSION, r"def \w+")
        assert "code.py:1:def hello():" in result
        assert "code.py:3:def world():" in result

    def test_glob_filter(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        (workspace / "code.py").write_text("hello\n")
        (workspace / "notes.txt").write_text("hello\n")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = search_files(VALID_SESSION, "hello", glob="*.py")
        assert "code.py" in result
        assert "notes.txt" not in result

    def test_invalid_regex(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            pytest.raises(ValueError, match="Invalid regex"),
        ):
            search_files(VALID_SESSION, "[invalid")

    def test_symlink_outside_workspace_skipped(self, tmp_path: Path) -> None:
        """Files that are symlinks pointing outside workspace should be skipped."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)
        # Create a file outside workspace
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("sensitive data\n")
        # Create symlink inside workspace pointing outside
        (workspace / "secret.txt").symlink_to(outside / "secret.txt")

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = search_files(VALID_SESSION, "sensitive")
        assert "sensitive" not in result


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_basic_command(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = run_command(VALID_SESSION, "echo hello")
        assert "hello" in result

    def test_timeout(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = run_command(VALID_SESSION, "sleep 10", timeout=1)
        assert "timed out" in result

    def test_nonzero_exit(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = run_command(VALID_SESSION, "exit 1")
        assert "exit code: 1" in result

    def test_output_truncation(self, tmp_path: Path) -> None:
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            # Generate output larger than 100KB
            result = run_command(VALID_SESSION, "python3 -c 'print(\"x\" * 200000)'")
        assert "truncated" in result

    def test_exit_code_on_truncated_output(self, tmp_path: Path) -> None:
        """Exit code should be reported even when output is truncated."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            # Generate large output AND exit with non-zero
            result = run_command(
                VALID_SESSION,
                "python3 -c 'print(\"x\" * 200000)'; exit 42",
            )
        assert "truncated" in result
        assert "exit code: 42" in result

    def test_env_filtering(self, tmp_path: Path) -> None:
        """Commands should NOT see parent process secrets like API keys."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch.dict(os.environ, {"SUPER_SECRET_API_KEY": "s3cret123"}),
        ):
            result = run_command(VALID_SESSION, "echo $SUPER_SECRET_API_KEY")
        # The secret should not appear in output
        assert "s3cret123" not in result

    def test_null_byte_rejected(self, tmp_path: Path) -> None:
        """Commands with null bytes should be rejected."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        with patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)):
            result = run_command(VALID_SESSION, "echo hello\x00world")
        assert "null bytes" in result


# ---------------------------------------------------------------------------
# _validate_url (SSRF protection)
# ---------------------------------------------------------------------------


class TestValidateUrl:
    def test_http_allowed(self) -> None:
        """HTTP URLs to public hosts should pass validation."""
        # Should not raise
        _validate_url("https://example.com")

    def test_non_http_scheme_blocked(self) -> None:
        with pytest.raises(ValueError, match="Only http/https"):
            _validate_url("file:///etc/passwd")

    def test_no_hostname_blocked(self) -> None:
        with pytest.raises(ValueError, match="no hostname"):
            _validate_url("http://")

    def test_cloud_metadata_blocked(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _validate_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_local_hostname_suffix_blocked(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _validate_url("http://myservice.local/api")

    def test_internal_hostname_suffix_blocked(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            _validate_url("http://admin.internal/")

    def test_localhost_ip_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            _validate_url("http://127.0.0.1/admin")

    def test_private_ip_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/reserved"):
            _validate_url("http://10.0.0.1/internal")


# ---------------------------------------------------------------------------
# build_bwrap_command
# ---------------------------------------------------------------------------


class TestBuildBwrapCommand:
    def test_basic_structure(self) -> None:
        """Verify the command structure includes all expected bwrap flags."""
        with patch("ypl.agent_harness_service.executors.sandbox.os.path.exists", return_value=True):
            cmd = build_bwrap_command("echo hello", "/workspace/test")

        assert cmd[0] == "bwrap"
        assert "--bind" in cmd
        assert "/workspace/test" in cmd
        assert "--tmpfs" in cmd
        assert "--dev" in cmd
        assert "--proc" in cmd
        assert "--unshare-net" not in cmd  # network access is intentionally preserved
        assert "--unshare-pid" in cmd
        assert "--unshare-ipc" in cmd
        assert "--unshare-uts" in cmd
        assert "--die-with-parent" in cmd
        assert "--chdir" in cmd
        # The actual command at the end
        assert cmd[-3:] == ["bash", "-c", "echo hello"]

    def test_workspace_bound_readwrite(self) -> None:
        """Workspace should be bound read-write (--bind, not --ro-bind)."""
        with patch("ypl.agent_harness_service.executors.sandbox.os.path.exists", return_value=True):
            cmd = build_bwrap_command("ls", "/workspace/test")

        # Find the --bind for workspace (not --ro-bind)
        bind_indices = [i for i, v in enumerate(cmd) if v == "--bind"]
        found_workspace = False
        for idx in bind_indices:
            if cmd[idx + 1] == "/workspace/test":
                found_workspace = True
        assert found_workspace, "Workspace should be bound read-write with --bind"

    def test_skips_missing_paths(self) -> None:
        """Paths that don't exist on the host should be skipped."""

        def selective_exists(path: str) -> bool:
            return path != "/lib64"

        with patch("ypl.agent_harness_service.executors.sandbox.os.path.exists", side_effect=selective_exists):
            cmd = build_bwrap_command("ls", "/workspace/test")

        # /lib64 should not appear in ro-bind args
        ro_bind_pairs = []
        i = 0
        while i < len(cmd):
            if cmd[i] == "--ro-bind" and i + 2 < len(cmd):
                ro_bind_pairs.append(cmd[i + 1])
                i += 3
            else:
                i += 1
        assert "/lib64" not in ro_bind_pairs
        # But /usr should be present (exists returns True)
        assert "/usr" in ro_bind_pairs

    def test_chdir_set_to_workspace(self) -> None:
        with patch("ypl.agent_harness_service.executors.sandbox.os.path.exists", return_value=True):
            cmd = build_bwrap_command("pwd", "/my/workspace")

        chdir_idx = cmd.index("--chdir")
        assert cmd[chdir_idx + 1] == "/my/workspace"


# ---------------------------------------------------------------------------
# bwrap_available
# ---------------------------------------------------------------------------


class TestBwrapAvailable:
    def test_available_when_installed(self) -> None:
        """Returns True when bwrap --version succeeds."""
        bwrap_available.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("ypl.agent_harness_service.executors.sandbox.subprocess.run", return_value=mock_result):
            assert bwrap_available() is True
        bwrap_available.cache_clear()

    def test_unavailable_when_not_found(self) -> None:
        """Returns False when bwrap is not installed."""
        bwrap_available.cache_clear()
        with patch(
            "ypl.agent_harness_service.executors.sandbox.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert bwrap_available() is False
        bwrap_available.cache_clear()

    def test_unavailable_on_timeout(self) -> None:
        """Returns False when bwrap --version times out."""
        bwrap_available.cache_clear()
        with patch(
            "ypl.agent_harness_service.executors.sandbox.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="bwrap", timeout=5),
        ):
            assert bwrap_available() is False
        bwrap_available.cache_clear()


# ---------------------------------------------------------------------------
# run_command with bwrap
# ---------------------------------------------------------------------------


class TestRunCommandBwrap:
    def test_bwrap_flag_uses_bwrap_command(self, tmp_path: Path) -> None:
        """When bwrap=True and available, Popen should receive bwrap-prefixed command."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = MagicMock(return_value="")
        mock_proc.wait = MagicMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        bwrap_available.cache_clear()
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.executors.sandbox.bwrap_available", return_value=True),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
        ):
            run_command(VALID_SESSION, "echo hello", bwrap=True)
            # Verify the command starts with "bwrap"
            actual_cmd = mock_popen.call_args[0][0]
            assert actual_cmd[0] == "bwrap"
            assert "echo hello" in actual_cmd
        bwrap_available.cache_clear()

    def test_bwrap_fallback_when_unavailable(self, tmp_path: Path) -> None:
        """When bwrap=True but unavailable, falls back to plain bash."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = MagicMock(return_value="")
        mock_proc.wait = MagicMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        bwrap_available.cache_clear()
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.executors.sandbox.bwrap_available", return_value=False),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
        ):
            run_command(VALID_SESSION, "echo hello", bwrap=True)
            # Should fall back to plain bash
            actual_cmd = mock_popen.call_args[0][0]
            assert actual_cmd == ["bash", "-c", "echo hello"]
        bwrap_available.cache_clear()

    def test_bwrap_false_uses_plain_bash(self, tmp_path: Path) -> None:
        """When bwrap=False, always uses plain bash regardless of availability."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read = MagicMock(return_value="")
        mock_proc.wait = MagicMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace_tools.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
        ):
            run_command(VALID_SESSION, "echo hello", bwrap=False)
            actual_cmd = mock_popen.call_args[0][0]
            assert actual_cmd == ["bash", "-c", "echo hello"]

    def test_bwrap_popen_failure_falls_back(self, tmp_path: Path) -> None:
        """When bwrap Popen fails with OSError, should fall back to plain bash."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        # Return data once, then empty string to signal EOF
        mock_proc.stdout.read = MagicMock(side_effect=["fallback output", ""])
        mock_proc.wait = MagicMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        call_count = 0

        def popen_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("EPERM: Operation not permitted")
            return mock_proc

        bwrap_available.cache_clear()
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.executors.sandbox.bwrap_available", return_value=True),
            patch("ypl.agent_harness_service.tools.workspace_tools.subprocess.Popen", side_effect=popen_side_effect),
        ):
            result = run_command(VALID_SESSION, "echo hello", bwrap=True)
            assert "fallback output" in result
            assert call_count == 2  # first bwrap attempt, then plain bash fallback
        bwrap_available.cache_clear()


# ---------------------------------------------------------------------------
# Sandbox escape attempt tests
# ---------------------------------------------------------------------------


class TestSandboxEscapeAttempts:
    """Tests verifying that sandbox prevents various escape attempts."""

    def test_env_secrets_not_accessible(self, tmp_path: Path) -> None:
        """Python script trying to read parent env should not find secrets."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        script = (
            "import os; "
            "secrets = [k for k in os.environ if 'SECRET' in k or 'API_KEY' in k or 'PASSWORD' in k]; "
            "print(f'found_secrets={len(secrets)}')"
        )

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch.dict(os.environ, {"SUPER_SECRET_API_KEY": "s3cret", "DATABASE_PASSWORD": "dbpass"}),
        ):
            result = run_command(VALID_SESSION, f'python3 -c "{script}"')
        assert "found_secrets=0" in result

    def test_path_traversal_via_command(self, tmp_path: Path) -> None:
        """Command trying to read files outside workspace via absolute path should
        fail when bwrap is enabled (mocked), or at least not leak host secrets."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        # Without bwrap, the env filtering still prevents secret leakage
        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch.dict(os.environ, {"SECRET_KEY": "leaked"}),
        ):
            result = run_command(VALID_SESSION, "echo $SECRET_KEY")
        assert "leaked" not in result

    def test_python_env_enumeration_blocked(self, tmp_path: Path) -> None:
        """Python script enumerating all env vars should not find secrets."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        script = "import os; print(dict(os.environ))"

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret", "OPENAI_API_KEY": "sk-openai-secret"}),
        ):
            result = run_command(VALID_SESSION, f'python3 -c "{script}"')
        assert "sk-ant-secret" not in result
        assert "sk-openai-secret" not in result

    def test_python_subprocess_env_leak(self, tmp_path: Path) -> None:
        """Python subprocess spawning should not inherit parent secrets."""
        workspace = tmp_path / VALID_SESSION / "yupp-agent"
        workspace.mkdir(parents=True)

        script = "import subprocess; r = subprocess.run(['env'], capture_output=True, text=True); print(r.stdout)"

        with (
            patch("ypl.agent_harness_service.tools.workspace_tools.AHS_SESSIONS_DIR", str(tmp_path)),
            patch.dict(os.environ, {"DB_SECRET": "dbsecret123"}),
        ):
            result = run_command(VALID_SESSION, f'python3 -c "{script}"')
        assert "dbsecret123" not in result
