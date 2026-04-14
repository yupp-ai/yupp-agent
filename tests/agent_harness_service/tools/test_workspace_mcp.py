"""Tests for workspace.py — request_write_access, list_available_repos, create_pr."""

from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.agent_harness_service.tools.mcp_instance import _resolve_pr_attribution
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
VALID_TASK_ID = "11111111-2222-3333-4444-555555555555"
VALID_PROJECT_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
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
                "ypl.agent_harness_service.tools.workspace._resolve_pr_attribution",
                new=AsyncMock(return_value=None),
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
                "ypl.agent_harness_service.tools.workspace._resolve_pr_attribution",
                new=AsyncMock(return_value=None),
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

    async def test_task_session_prepends_attribution(self, tmp_path: Path) -> None:
        """Task-triggered sessions get attribution header prepended to body."""
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()
        expected_result = {"status": "ok", "pr_url": "https://github.com/yupp-ai/yupp-agent/pull/42"}

        attribution = (
            "\U0001f916 *eng-raccoon* for *Tian Wang*"
            " · \U0001f4cb [My Project / My Task]"
            f"({AHS_LIT_BASE_URL}/agent_projects?project_id=proj-1&task_id=task-1)\n"
            f"\U0001f517 [Session]({AHS_LIT_BASE_URL}/agent_harness_console?session_id={VALID_SESSION})"
        )

        with (
            patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)),
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value="ghp_token123"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace._resolve_pr_attribution",
                new=AsyncMock(return_value=attribution),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.push_and_create_pr",
                return_value=expected_result,
            ) as mock_push,
        ):
            result = await create_pr(session_id=VALID_SESSION, title="My PR", body="## Summary\nSome changes")

        assert result["status"] == "ok"
        body_sent = mock_push.call_args[1]["body"]
        assert body_sent.startswith("\U0001f916")
        assert "## Summary" in body_sent
        assert "eng-raccoon" in body_sent
        assert "Tian Wang" in body_sent

    async def test_non_task_session_body_unchanged(self, tmp_path: Path) -> None:
        """Non-task sessions don't get attribution prepended."""
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
                "ypl.agent_harness_service.tools.workspace._resolve_pr_attribution",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.push_and_create_pr",
                return_value=expected_result,
            ) as mock_push,
        ):
            original_body = "## Summary\nSome changes"
            await create_pr(session_id=VALID_SESSION, title="My PR", body=original_body)

        body_sent = mock_push.call_args[1]["body"]
        assert body_sent == original_body

    async def test_agent_already_included_attribution_not_duplicated(self, tmp_path: Path) -> None:
        """If the agent already put the 🤖 prefix, don't prepend again."""
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
                "ypl.agent_harness_service.tools.workspace._resolve_pr_attribution",
                new=AsyncMock(return_value="\U0001f916 *eng-raccoon* for *Tian Wang*"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.push_and_create_pr",
                return_value=expected_result,
            ) as mock_push,
        ):
            original_body = "\U0001f916 *eng-raccoon* for *Tian Wang*\n\n## Summary\nChanges"
            await create_pr(session_id=VALID_SESSION, title="PR", body=original_body)

        body_sent = mock_push.call_args[1]["body"]
        # Should not have double attribution
        assert body_sent == original_body


# ---------------------------------------------------------------------------
# _resolve_pr_attribution
# ---------------------------------------------------------------------------


def _mock_db_session(fetchone_return: object = None, scalar_return: object = None) -> AsyncMock:
    """Create a mock async DB session context manager."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = fetchone_return
    mock_result.scalar_one_or_none.return_value = scalar_return
    mock_db.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    return mock_ctx


class TestResolvePrAttribution:
    async def test_task_session_returns_full_attribution(self) -> None:
        """Task-triggered session produces attribution with all fields."""
        context = {
            "task_id": VALID_TASK_ID,
            "project_id": VALID_PROJECT_ID,
            "project_name": "My Project",
            "user_name": "Jane Doe",
        }
        session_row = ("eng-raccoon", context, "TASK")

        mock_db = AsyncMock()
        # First execute: session query
        session_result = MagicMock()
        session_result.fetchone.return_value = session_row
        # Second execute: task title query
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = "Fix the bug"

        mock_db.execute = AsyncMock(side_effect=[session_result, task_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert "eng-raccoon" in result
        assert "Jane Doe" in result
        assert "My Project / Fix the bug" in result
        assert f"session_id={VALID_SESSION}" in result
        assert f"project_id={VALID_PROJECT_ID}" in result
        assert f"task_id={VALID_TASK_ID}" in result

    async def test_non_task_session_returns_none(self) -> None:
        """Non-task sessions return None."""
        context = {"user_name": "Jane Doe"}
        session_row = ("eng-raccoon", context, "SLACK")

        mock_db = AsyncMock()
        session_result = MagicMock()
        session_result.fetchone.return_value = session_row
        mock_db.execute = AsyncMock(return_value=session_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is None

    async def test_session_not_found_returns_none(self) -> None:
        """Missing session returns None."""
        mock_db = AsyncMock()
        session_result = MagicMock()
        session_result.fetchone.return_value = None
        mock_db.execute = AsyncMock(return_value=session_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is None

    async def test_invalid_session_id_returns_none(self) -> None:
        """Invalid UUID returns None without DB call."""
        result = await _resolve_pr_attribution("not-a-uuid")
        assert result is None

    async def test_missing_task_title_still_produces_attribution(self) -> None:
        """If task title lookup fails, attribution still works with project name."""
        context = {
            "task_id": VALID_TASK_ID,
            "project_id": VALID_PROJECT_ID,
            "project_name": "My Project",
            "user_name": "Jane Doe",
        }
        session_row = ("eng-raccoon", context, "TASK")

        mock_db = AsyncMock()
        session_result = MagicMock()
        session_result.fetchone.return_value = session_row
        task_result = MagicMock()
        task_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(side_effect=[session_result, task_result])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert "eng-raccoon" in result
        assert "My Project" in result
