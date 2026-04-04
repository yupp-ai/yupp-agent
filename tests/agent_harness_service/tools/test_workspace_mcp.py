"""Tests for workspace.py — request_write_access, list_available_repos, create_pr."""

from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from ypl.agent_harness_service.tools.workspace import (
    create_pr as _create_pr_tool,
)
from ypl.agent_harness_service.tools.workspace import (
    list_available_repos as _list_available_repos_tool,
)
from ypl.agent_harness_service.tools.workspace import (
    request_write_access as _request_write_access_tool,
)

# Unwrap FunctionTool to get raw callables
create_pr = _create_pr_tool.fn
list_available_repos = _list_available_repos_tool.fn
request_write_access = _request_write_access_tool.fn

VALID_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
INVALID_SESSION = "bad-uuid"


# ---------------------------------------------------------------------------
# request_write_access
# ---------------------------------------------------------------------------


class TestRequestWriteAccess:
    def test_success(self) -> None:
        expected = {"status": "granted", "workspace": "/tmp/session/yupp-agent-fix-bug", "branch": "agent/fix-bug"}

        with patch(
            "ypl.agent_harness_service.tools.workspace.create_worktree",
            return_value=expected,
        ):
            result = request_write_access(session_id=VALID_SESSION, repo="yupp-agent")

        assert result == expected

    def test_branch_passed_through(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.workspace.create_worktree",
            return_value={"status": "granted"},
        ) as mock_create:
            request_write_access(session_id=VALID_SESSION, repo="yupp-agent", branch="my-feature")

        mock_create.assert_called_once_with(repo="yupp-agent", session_id=VALID_SESSION, branch="my-feature")

    def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            request_write_access(session_id=INVALID_SESSION, repo="yupp-agent")

    def test_empty_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="session_id is required"):
            request_write_access(session_id="", repo="yupp-agent")


# ---------------------------------------------------------------------------
# list_available_repos
# ---------------------------------------------------------------------------


class TestListAvailableRepos:
    def test_returns_repos_list(self) -> None:
        repos = [
            {"name": "yupp-agent", "path": "/data/repos/yupp-agent"},
            {"name": "yupp-mind", "path": "/data/repos/yupp-mind"},
        ]
        with patch("ypl.agent_harness_service.tools.workspace._list_repos", return_value=repos):
            result = list_available_repos()

        assert result == repos

    def test_empty_list(self) -> None:
        with patch("ypl.agent_harness_service.tools.workspace._list_repos", return_value=[]):
            result = list_available_repos()

        assert result == []


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


class TestCreatePr:
    async def test_no_workspace_dir_returns_error(self, tmp_path: Path) -> None:
        # Point AHS_SESSIONS_DIR to tmp_path but DON'T create the session subdir
        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="My PR", body="Description")

        assert result["status"] == "error"
        assert "No worktree" in result["error"]

    async def test_no_worktree_dirs_returns_error(self, tmp_path: Path) -> None:
        """Session dir exists but only contains symlinks / infra dirs."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        # Only infra dirs
        (session_dir / "history").mkdir()

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="My PR", body="Description")

        assert result["status"] == "error"
        assert "No worktree" in result["error"]

    async def test_multiple_worktrees_no_repo_returns_error(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()
        (session_dir / "yupp-mind-add-feature").mkdir()

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body")

        assert result["status"] == "error"
        assert "Multiple worktrees" in result["error"]

    async def test_repo_specified_but_no_match_returns_error(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body", repo="yupp-mind")

        assert result["status"] == "error"
        assert "yupp-mind" in result["error"]

    async def test_no_github_token_returns_auth_error(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()

        with (
            patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_current_message_user_id",
                new=AsyncMock(return_value="user-abc"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace._initiate_device_flow",
                new=AsyncMock(
                    return_value={
                        "status": "pending",
                        "verification_uri": "https://github.com/login/device",
                        "user_code": "ABCD-1234",
                        "expires_in_seconds": 900,
                        "instructions": "Visit the URL and enter the code",
                    }
                ),
            ),
        ):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body")

        assert result["status"] == "error"
        assert result.get("auth_required") == "true"
        assert "verification_uri" in result

    async def test_success_with_token(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()
        expected_result = {"status": "ok", "pr_url": "https://github.com/yupp-ai/yupp-agent/pull/42"}

        with (
            patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value="ghp_token123"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.push_and_create_pr",
                return_value=expected_result,
            ),
        ):
            result = await create_pr(session_id=VALID_SESSION, title="My PR", body="Description")

        assert result["status"] == "ok"
        assert result["pr_url"] == expected_result["pr_url"]

    async def test_repo_selection_with_single_match(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()
        (session_dir / "yupp-mind-feature").mkdir()

        with (
            patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value="ghp_token123"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.push_and_create_pr",
                return_value={"status": "ok"},
            ) as mock_push,
        ):
            await create_pr(session_id=VALID_SESSION, title="PR", body="body", repo="yupp-agent")

        # Verify the correct workspace was selected
        call_kwargs = mock_push.call_args[1]
        assert "yupp-agent-fix-bug" in call_kwargs["workspace"]

    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError):
            await create_pr(session_id=INVALID_SESSION, title="PR", body="body")

    async def test_symlinks_excluded_from_worktree_search(self, tmp_path: Path) -> None:
        """Symlinked repo directories are not treated as worktrees."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        # Create a real dir to act as target of symlink
        real_repo = tmp_path / "yupp-agent"
        real_repo.mkdir()
        (session_dir / "yupp-agent").symlink_to(real_repo)

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body")

        assert result["status"] == "error"
        assert "No worktree" in result["error"]

    async def test_pending_device_flow_returns_retry_message(self, tmp_path: Path) -> None:
        """When device flow is already pending, returns a retry message."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()

        with (
            patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_current_message_user_id",
                new=AsyncMock(return_value="user-abc"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace._initiate_device_flow",
                # Returns pending (no verification_uri) — already in progress
                new=AsyncMock(return_value={"status": "pending", "message": "Already polling..."}),
            ),
        ):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body")

        assert result["status"] == "error"
        assert result.get("auth_required") == "true"
        assert "next_step" in result
