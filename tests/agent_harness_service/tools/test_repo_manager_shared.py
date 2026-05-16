"""Tests for the shared-repo helpers in repo_manager.py.

Covers the additions made for shared_repos.yaml-driven cloning:

- ``_parse_github_url`` / ``repo_name_from_url``
- ``load_shared_repos_config``
- ``is_repo_protected``
- ``ensure_repo_cloned`` (idempotency + validation, mocking subprocess.run)
- ``remove_repo_from_disk`` (protection + path safety, mocking subprocess.run)
- ``list_repos_with_metadata`` (config + on-disk merge)
- ``pull_all_repos`` (ensure-clone + pull two-phase behavior)
"""

from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ypl.agent_harness_service.tools import repo_manager as rm

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestParseGithubUrl:
    @pytest.mark.parametrize(
        ("url", "expected_owner", "expected_name"),
        [
            ("https://github.com/yupp-ai/yupp-agent", "yupp-ai", "yupp-agent"),
            ("https://github.com/yupp-ai/yupp-agent.git", "yupp-ai", "yupp-agent"),
            ("https://github.com/yupp-ai/yupp-agent/", "yupp-ai", "yupp-agent"),
            ("https://github.com/wangtian24/pr-status-check", "wangtian24", "pr-status-check"),
            ("https://github.com/a/b.c.d", "a", "b.c.d"),
        ],
    )
    def test_valid_urls(self, url: str, expected_owner: str, expected_name: str) -> None:
        owner, name = rm._parse_github_url(url)
        assert owner == expected_owner
        assert name == expected_name

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
            "https://gitlab.com/owner/repo",
            "https://github.com/owner",  # missing repo
            "https://github.com/owner/repo/sub",  # extra path
            "http://github.com/owner/repo",  # http not https
            "",
        ],
    )
    def test_invalid_urls(self, url: str) -> None:
        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            rm._parse_github_url(url)

    def test_repo_name_from_url(self) -> None:
        assert rm.repo_name_from_url("https://github.com/yupp-ai/yupp-agent") == "yupp-agent"
        assert rm.repo_name_from_url("https://github.com/yupp-ai/yupp-agent.git") == "yupp-agent"


# ---------------------------------------------------------------------------
# Config loading + protection lookup
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body)


class TestLoadSharedReposConfig:
    def test_loads_normal_config(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            """
repos:
  - name: yupp-agent
    url: https://github.com/yupp-ai/yupp-agent.git
    protected: true
  - name: pr-status-check
    url: https://github.com/wangtian24/pr-status-check.git
""".strip(),
        )
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            entries = rm.load_shared_repos_config()
        assert entries == [
            {"name": "yupp-agent", "url": "https://github.com/yupp-ai/yupp-agent.git", "protected": True},
            {"name": "pr-status-check", "url": "https://github.com/wangtian24/pr-status-check.git", "protected": False},
        ]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        with patch(
            "ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH",
            str(tmp_path / "nope.yaml"),
        ):
            assert rm.load_shared_repos_config() == []

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        cfg_path.write_text("[: not valid yaml")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.load_shared_repos_config() == []

    def test_missing_repos_key(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "other_key: foo")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.load_shared_repos_config() == []

    def test_skips_entries_without_name_or_url(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            """
repos:
  - name: ok
    url: https://github.com/o/ok
  - url: https://github.com/o/missing-name
  - name: missing-url
  - not-a-dict
""".strip(),
        )
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            entries = rm.load_shared_repos_config()
        assert [e["name"] for e in entries] == ["ok"]

    def test_skips_unsafe_names(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            """
repos:
  - name: ../escape
    url: https://github.com/o/r
  - name: ok
    url: https://github.com/o/ok
""".strip(),
        )
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            entries = rm.load_shared_repos_config()
        assert [e["name"] for e in entries] == ["ok"]


class TestIsRepoProtected:
    def test_protected_true(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            "repos:\n  - name: yupp-agent\n    url: https://github.com/yupp-ai/yupp-agent\n    protected: true\n",
        )
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_repo_protected("yupp-agent") is True
            assert rm.is_repo_protected("anything-else") is False

    def test_protected_false_when_flag_absent(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "repos:\n  - name: foo\n    url: https://github.com/o/foo\n")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_repo_protected("foo") is False


# ---------------------------------------------------------------------------
# ensure_repo_cloned
# ---------------------------------------------------------------------------


class TestEnsureRepoCloned:
    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.ensure_repo_cloned("../escape", "https://github.com/o/r")
        assert result["status"] == "error"
        assert "Invalid repo name" in result["error"]

    def test_invalid_url_rejected(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.ensure_repo_cloned("ok", "git@github.com:o/r.git")
        assert result["status"] == "error"
        assert "Invalid GitHub URL" in result["error"]

    def test_already_cloned_returns_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "foo"
        (target / ".git").mkdir(parents=True)
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")
        assert result["status"] == "exists"
        assert result["path"] == str(target)

    def test_path_exists_but_not_git(self, tmp_path: Path) -> None:
        (tmp_path / "foo").mkdir()
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")
        assert result["status"] == "error"
        assert "not a git repo" in result["error"]

    def test_runs_git_clone_when_missing(self, tmp_path: Path) -> None:
        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")
        assert result["status"] == "cloned"
        assert result["path"] == str(tmp_path / "foo")
        # First arg is the command list
        args = mock_run.call_args[0][0]
        assert args[:2] == ["git", "clone"]
        assert args[2] == "https://github.com/o/foo"
        assert args[3] == str(tmp_path / "foo")


# ---------------------------------------------------------------------------
# remove_repo_from_disk
# ---------------------------------------------------------------------------


class TestRemoveRepoFromDisk:
    def test_invalid_name(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.remove_repo_from_disk("../escape")
        assert result["status"] == "error"

    def test_protected_refused(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            "repos:\n  - name: protected-one\n    url: https://github.com/o/p\n    protected: true\n",
        )
        with (
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
        ):
            result = rm.remove_repo_from_disk("protected-one")
        assert result["status"] == "error"
        assert "protected" in result["error"]

    def test_missing_returns_missing(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)):
            result = rm.remove_repo_from_disk("not-here")
        assert result["status"] == "missing"

    def test_symlink_unlinked_not_recursed(self, tmp_path: Path) -> None:
        # Decoy target that should NOT be removed
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "keepme.txt").write_text("dont touch")
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        (repos_dir / "ln").symlink_to(decoy)

        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)):
            result = rm.remove_repo_from_disk("ln")

        assert result["status"] == "removed"
        assert not (repos_dir / "ln").exists()
        # Decoy contents untouched
        assert (decoy / "keepme.txt").read_text() == "dont touch"

    def test_directory_removed(self, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        target = repos_dir / "foo"
        target.mkdir(parents=True)
        (target / "marker").write_text("x")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            result = rm.remove_repo_from_disk("foo")

        assert result["status"] == "removed"
        # Confirm the rm command was issued with the right path
        args = mock_run.call_args[0][0]
        assert args[:3] == ["rm", "-rf", "--"]
        assert args[3] == str(target)


# ---------------------------------------------------------------------------
# list_repos_with_metadata
# ---------------------------------------------------------------------------


class TestListReposWithMetadata:
    def test_merges_config_and_disk(self, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        # Configured + on disk
        (repos_dir / "yupp-agent" / ".git").mkdir(parents=True)
        # Ad-hoc on disk only
        (repos_dir / "adhoc" / ".git").mkdir(parents=True)

        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            """
repos:
  - name: yupp-agent
    url: https://github.com/yupp-ai/yupp-agent
    protected: true
  - name: not-yet-cloned
    url: https://github.com/o/nope
""".strip(),
        )

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
        ):
            meta = rm.list_repos_with_metadata()

        by_name = {e["name"]: e for e in meta}
        assert set(by_name) == {"adhoc", "not-yet-cloned", "yupp-agent"}

        assert by_name["yupp-agent"] == {
            "name": "yupp-agent",
            "path": str(repos_dir / "yupp-agent"),
            "in_config": True,
            "protected": True,
            "url": "https://github.com/yupp-ai/yupp-agent",
            "on_disk": True,
        }
        assert by_name["adhoc"] == {
            "name": "adhoc",
            "path": str(repos_dir / "adhoc"),
            "in_config": False,
            "protected": False,
            "url": None,
            "on_disk": True,
        }
        # Configured but not cloned yet
        assert by_name["not-yet-cloned"]["in_config"] is True
        assert by_name["not-yet-cloned"]["on_disk"] is False
        assert by_name["not-yet-cloned"]["path"] is None


# ---------------------------------------------------------------------------
# pull_all_repos (ensure-clone + pull)
# ---------------------------------------------------------------------------


class TestPullAllRepos:
    def test_clones_missing_then_pulls(self, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            """
repos:
  - name: foo
    url: https://github.com/o/foo
""".strip(),
        )

        # Simulate `git clone` creating the directory (so phase-2 pull finds it)
        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                (repos_dir / "foo" / ".git").mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run) as mock_run,
        ):
            results = rm.pull_all_repos()

        assert results == {"foo": True}
        # Should have run a clone and at least one pull
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[:2] == ["git", "clone"] for c in cmds)
        assert any(c[:2] == ["git", "pull"] for c in cmds)

    def test_pulls_orphan_adhoc_repos(self, tmp_path: Path) -> None:
        """Repos on disk but not in config still get pulled (preserves ad-hoc clones)."""
        repos_dir = tmp_path / "repos"
        (repos_dir / "adhoc" / ".git").mkdir(parents=True)

        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "repos: []")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            results = rm.pull_all_repos()

        assert results == {"adhoc": True}
