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

    def test_always_protected_overrides_missing_yaml(self, tmp_path: Path) -> None:
        """yupp-agent stays protected even if shared_repos.yaml is missing entirely."""
        with patch(
            "ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH",
            str(tmp_path / "does-not-exist.yaml"),
        ):
            assert rm.is_repo_protected("yupp-agent") is True

    def test_always_protected_overrides_yaml_drop(self, tmp_path: Path) -> None:
        """yupp-agent stays protected even if removed from shared_repos.yaml."""
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "repos:\n  - name: other\n    url: https://github.com/o/other\n")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_repo_protected("yupp-agent") is True

    def test_always_protected_overrides_explicit_false(self, tmp_path: Path) -> None:
        """yupp-agent stays protected even if shared_repos.yaml explicitly sets protected: false."""
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            "repos:\n  - name: yupp-agent\n    url: https://github.com/yupp-ai/yupp-agent\n    protected: false\n",
        )
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_repo_protected("yupp-agent") is True


class TestIsSharedReposConfigParseable:
    def test_missing_file_is_parseable(self, tmp_path: Path) -> None:
        with patch(
            "ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH",
            str(tmp_path / "missing.yaml"),
        ):
            assert rm.is_shared_repos_config_parseable() is True

    def test_well_formed_yaml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "repos: []")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_shared_repos_config_parseable() is True

    def test_malformed_yaml_is_unparseable(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        cfg_path.write_text("[: not valid yaml")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_shared_repos_config_parseable() is False

    def test_missing_repos_key_is_unparseable(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "something_else: 1")
        with patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)):
            assert rm.is_shared_repos_config_parseable() is False


class TestDetectDefaultBranch:
    def test_origin_master(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"origin/master\n")
            assert rm._detect_default_branch(str(tmp_path)) == "master"

    def test_origin_main(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"origin/main\n")
            assert rm._detect_default_branch(str(tmp_path)) == "main"

    def test_falls_back_to_main_on_failure(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=b"")
            assert rm._detect_default_branch(str(tmp_path)) == "main"

    def test_falls_back_on_oserror(self, tmp_path: Path) -> None:
        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=OSError):
            assert rm._detect_default_branch(str(tmp_path)) == "main"


class TestPullRepo:
    def test_uses_detected_default_branch(self, tmp_path: Path) -> None:
        """pull_repo passes the detected default branch to git pull."""
        calls = []

        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            calls.append(cmd)
            if cmd[:3] == ["git", "-C", str(tmp_path)] and "symbolic-ref" in cmd:
                return MagicMock(returncode=0, stdout=b"origin/develop\n")
            return MagicMock(returncode=0, stderr=b"")

        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run):
            assert rm.pull_repo(str(tmp_path)) is True

        # Find the actual pull call
        pull_cmds = [c for c in calls if c[:2] == ["git", "pull"]]
        assert len(pull_cmds) == 1
        assert pull_cmds[0] == ["git", "pull", "--ff-only", "origin", "develop"]

    def test_returns_false_on_filenotfound(self, tmp_path: Path) -> None:
        """Concurrent remove → FileNotFoundError → False (no exception)."""
        with patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=FileNotFoundError):
            assert rm.pull_repo(str(tmp_path)) is False


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

    def test_partial_clone_quarantined_and_retried(self, tmp_path: Path) -> None:
        """Stale non-git target is renamed to .broken.{ts} and the clone retries."""
        target = tmp_path / "foo"
        target.mkdir()
        (target / "junk").write_text("leftover")

        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                # Simulate clone creating .git
                (tmp_path / "foo" / ".git").mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run),
        ):
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")

        assert result["status"] == "cloned"
        # Old junk dir was quarantined, not deleted
        quarantined = [p for p in tmp_path.iterdir() if p.name.startswith("foo.broken.")]
        assert len(quarantined) == 1
        assert (quarantined[0] / "junk").read_text() == "leftover"
        # New clone present
        assert (tmp_path / "foo" / ".git").is_dir()

    def test_runs_git_clone_when_missing(self, tmp_path: Path) -> None:
        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                (tmp_path / "foo" / ".git").mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run) as mock_run,
        ):
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")
        assert result["status"] == "cloned"
        assert result["path"] == str(tmp_path / "foo")
        # Find the actual clone call (there may be other git calls)
        clone_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][:2] == ["git", "clone"]]
        assert len(clone_calls) == 1
        assert clone_calls[0] == ["git", "clone", "https://github.com/o/foo", str(tmp_path / "foo")]

    def test_failed_clone_cleans_up(self, tmp_path: Path) -> None:
        """A failed git clone removes any half-written target so the next call retries cleanly."""
        from subprocess import CalledProcessError

        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                # Simulate git creating a partial dir then failing
                (tmp_path / "foo").mkdir(parents=True, exist_ok=True)
                raise CalledProcessError(returncode=128, cmd=cmd, stderr=b"auth required")
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run),
        ):
            result = rm.ensure_repo_cloned("foo", "https://github.com/o/foo")

        assert result["status"] == "error"
        assert "git clone failed" in result["error"]
        # Cleanup happened — no stale target left to wedge the next call.
        assert not (tmp_path / "foo").exists()


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

    def test_always_protected_refused_even_without_yaml(self, tmp_path: Path) -> None:
        """yupp-agent is refused even if shared_repos.yaml is absent."""
        with (
            patch(
                "ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH",
                str(tmp_path / "absent.yaml"),
            ),
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(tmp_path)),
        ):
            result = rm.remove_repo_from_disk("yupp-agent")
        assert result["status"] == "error"
        assert "protected" in result["error"]

    def test_fail_closed_on_malformed_yaml(self, tmp_path: Path) -> None:
        """Malformed shared_repos.yaml → refuse ALL removals (fail-closed)."""
        cfg_path = tmp_path / "shared_repos.yaml"
        cfg_path.write_text("[: not valid yaml")  # exists but unparseable
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        (repos_dir / "foo" / ".git").mkdir(parents=True)
        with (
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
        ):
            result = rm.remove_repo_from_disk("foo")
        assert result["status"] == "error"
        assert "unparseable" in result["error"]
        # Directory was NOT removed.
        assert (repos_dir / "foo" / ".git").is_dir()

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

    def test_directory_removed_with_shutil(self, tmp_path: Path) -> None:
        """remove_repo_from_disk uses shutil.rmtree (no /bin/rm subprocess)."""
        repos_dir = tmp_path / "repos"
        target = repos_dir / "foo"
        target.mkdir(parents=True)
        (target / "marker").write_text("x")

        with patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)):
            result = rm.remove_repo_from_disk("foo")

        assert result["status"] == "removed"
        assert not target.exists()


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
            elif "symbolic-ref" in cmd:
                return MagicMock(returncode=0, stdout=b"origin/main\n")
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run) as mock_run,
        ):
            result = rm.pull_all_repos()

        assert result["pulls"] == {"foo": True}
        assert result["clone_failures"] == {}
        assert result["config_unparseable"] is False
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
            result = rm.pull_all_repos()

        assert result["pulls"] == {"adhoc": True}
        assert result["clone_failures"] == {}
        assert result["config_unparseable"] is False

    def test_clone_failure_surfaced(self, tmp_path: Path) -> None:
        """Configured-clone failures are reported in clone_failures dict."""
        from subprocess import CalledProcessError

        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(
            cfg_path,
            "repos:\n  - name: foo\n    url: https://github.com/o/foo\n",
        )

        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            if cmd[:2] == ["git", "clone"]:
                raise CalledProcessError(returncode=128, cmd=cmd, stderr=b"network error")
            return MagicMock(returncode=0, stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run),
        ):
            result = rm.pull_all_repos()

        assert result["pulls"] == {}
        assert "foo" in result["clone_failures"]
        assert "network error" in result["clone_failures"]["foo"]

    def test_malformed_yaml_signals_unparseable(self, tmp_path: Path) -> None:
        """Malformed shared_repos.yaml → config_unparseable=True."""
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        cfg_path = tmp_path / "shared_repos.yaml"
        cfg_path.write_text("[: not valid yaml")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
        ):
            result = rm.pull_all_repos()

        assert result["config_unparseable"] is True

    def test_concurrent_remove_during_pull_tolerated(self, tmp_path: Path) -> None:
        """A repo removed between listdir and git pull is skipped, doesn't crash."""
        repos_dir = tmp_path / "repos"
        (repos_dir / "vanish" / ".git").mkdir(parents=True)
        cfg_path = tmp_path / "shared_repos.yaml"
        _write_yaml(cfg_path, "repos: []")

        call_count = {"n": 0}

        def fake_run(cmd: list[str], **_kw: object) -> MagicMock:
            # First call simulates the dir vanishing under our feet
            if cmd[:2] == ["git", "pull"]:
                call_count["n"] += 1
                raise FileNotFoundError(2, "No such directory")
            return MagicMock(returncode=0, stdout=b"origin/main\n", stderr=b"")

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_REPOS_DIR", str(repos_dir)),
            patch("ypl.agent_harness_service.tools.repo_manager.SHARED_REPOS_CONFIG_PATH", str(cfg_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run),
        ):
            result = rm.pull_all_repos()

        # Pull was attempted but the FileNotFoundError was caught.
        assert call_count["n"] >= 1
        assert result["pulls"] == {"vanish": False}
