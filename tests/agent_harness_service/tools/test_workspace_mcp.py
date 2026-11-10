"""Tests for workspace.py — request_write_access, list_available_repos, clone_repo, create_pr."""

from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.agent_harness_service.tools.mcp_instance import _resolve_pr_attribution
from ypl.agent_harness_service.tools.repo_manager import clone_repo_into_session
from ypl.agent_harness_service.tools.workspace import (
    clone_repo as _clone_repo_tool,
)
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
            {"name": "yupp-agent", "path": "/data/ahs/repos/yupp-agent"},
            {"name": "other-repo", "path": "/data/ahs/repos/other-repo"},
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
        (session_dir / "other-repo-add-feature").mkdir()

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body")

        assert result["status"] == "error"
        assert "Multiple worktrees" in result["error"]

    async def test_repo_specified_but_no_match_returns_error(self, tmp_path: Path) -> None:
        session_dir = tmp_path / VALID_SESSION
        session_dir.mkdir()
        (session_dir / "yupp-agent-fix-bug").mkdir()

        with patch("ypl.agent_harness_service.tools.workspace.AHS_SESSIONS_DIR", str(tmp_path)):
            result = await create_pr(session_id=VALID_SESSION, title="PR", body="body", repo="other-repo")

        assert result["status"] == "error"
        assert "other-repo" in result["error"]

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
        (session_dir / "other-repo-feature").mkdir()

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
        session_row = ("eng-raccoon", context)

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

    async def test_non_task_session_emits_attribution_without_task_link(self) -> None:
        """Non-task sessions still emit attribution — agent name + session link, no project/task line.

        Downstream tooling (e.g. a review agent's review-fix loop notification step) parses
        ``session_id=<UUID>`` out of the attribution to route follow-up agent messages, so
        the session link must be emitted for every AHS-driven trigger, not just TASK.
        """
        context = {"user_name": "Jane Doe"}
        session_row = ("eng-raccoon", context)

        mock_db = AsyncMock()
        session_result = MagicMock()
        session_result.fetchone.return_value = session_row
        mock_db.execute = AsyncMock(return_value=session_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert "eng-raccoon" in result
        assert "Jane Doe" in result
        assert f"session_id={VALID_SESSION}" in result
        # Non-TASK sessions don't carry project/task context — no project link is emitted.
        assert "project_id=" not in result
        assert "task_id=" not in result

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
        session_row = ("eng-raccoon", context)

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

    async def test_slack_triggered_session_appends_slack_link(self) -> None:
        """Slack-triggered session appends a ``[Slack](...)`` link to line 2."""
        context = {
            "user_name": "Jane Doe",
            "slack_channel_id": "C01ABCDEF",
            "slack_thread_ts": "1234567890.123456",
        }
        mock_ctx = _mock_db_session(fetchone_return=("eng-raccoon", context))

        # Force the workspace domain so the permalink builder returns a URL.
        with (
            patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx),
            patch("ypl.agent_harness_service.common.constants.SLACK_WORKSPACE_DOMAIN_NAME", "your-workspace"),
        ):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert f"session_id={VALID_SESSION}" in result
        # Slack permalink format: archives/<channel>/p<ts-no-dot>?thread_ts=...&cid=...
        assert "[Slack](https://your-workspace.slack.com/archives/C01ABCDEF/p1234567890123456" in result
        assert "thread_ts=1234567890.123456" in result
        assert "cid=C01ABCDEF" in result

    async def test_slack_link_omitted_when_workspace_domain_unset(self) -> None:
        """When ``SLACK_WORKSPACE_DOMAIN_NAME`` is empty, the Slack link is silently omitted."""
        context = {
            "user_name": "Jane Doe",
            "slack_channel_id": "C01ABCDEF",
            "slack_thread_ts": "1234567890.123456",
        }
        mock_ctx = _mock_db_session(fetchone_return=("eng-raccoon", context))

        with (
            patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx),
            patch("ypl.agent_harness_service.common.constants.SLACK_WORKSPACE_DOMAIN_NAME", ""),
        ):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert f"session_id={VALID_SESSION}" in result
        assert "[Slack]" not in result

    async def test_non_slack_session_has_no_slack_link(self) -> None:
        """When the session has no Slack context fields, no Slack link is emitted
        even if the workspace domain is configured."""
        mock_ctx = _mock_db_session(fetchone_return=("eng-raccoon", {"user_name": "Jane Doe"}))

        with (
            patch("ypl.agent_harness_service.tools.mcp_instance.get_async_session", return_value=mock_ctx),
            patch("ypl.agent_harness_service.common.constants.SLACK_WORKSPACE_DOMAIN_NAME", "your-workspace"),
        ):
            result = await _resolve_pr_attribution(VALID_SESSION)

        assert result is not None
        assert "[Slack]" not in result


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------

clone_repo = _clone_repo_tool.fn


class TestCloneRepo:
    async def test_no_token_returns_auth_required(self) -> None:
        with (
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
                    }
                ),
            ),
            patch("ypl.agent_harness_service.tools.workspace.clone_repo_into_session") as mock_clone,
        ):
            result = await clone_repo(session_id=VALID_SESSION, github_repo="octo/private")

        assert result["status"] == "error"
        assert result["auth_required"] == "true"
        assert "clone_repo" in result["next_step"]
        mock_clone.assert_not_called()

    async def test_success_passes_token_server_side(self) -> None:
        expected = {"status": "granted", "workspace": "/s/private-clone", "repo": "private", "branch": "main"}
        with (
            patch(
                "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
                new=AsyncMock(return_value="ghu_tok"),
            ),
            patch(
                "ypl.agent_harness_service.tools.workspace.clone_repo_into_session",
                return_value=expected,
            ) as mock_clone,
        ):
            result = await clone_repo(session_id=VALID_SESSION, github_repo="octo/private", branch="feat")

        assert result == expected
        assert "ghu_tok" not in str(result)
        mock_clone.assert_called_once_with(
            github_repo="octo/private", session_id=VALID_SESSION, user_github_token="ghu_tok", branch="feat"
        )

    async def test_invalid_repo_returns_error(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.workspace._get_valid_github_token",
            new=AsyncMock(return_value="ghu_tok"),
        ):
            result = await clone_repo(session_id=VALID_SESSION, github_repo="../etc/passwd")

        assert result["status"] == "error"
        assert "Invalid GitHub repo" in result["error"]

    async def test_invalid_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            await clone_repo(session_id=INVALID_SESSION, github_repo="octo/private")


class TestCloneRepoIntoSession:
    def test_rejects_bad_names(self, tmp_path: Path) -> None:
        for bad in ["noslash", "a/b/c", "../x", "o/..", "o/.hidden", "-o/x", "o/x;rm"]:
            with (
                patch("ypl.agent_harness_service.tools.repo_manager.AHS_SESSIONS_DIR", str(tmp_path)),
                pytest.raises(ValueError),
            ):
                clone_repo_into_session(github_repo=bad, session_id=VALID_SESSION, user_github_token="t")

    def test_clone_uses_env_token_and_session_dir(self, tmp_path: Path) -> None:
        calls: list[tuple[list[str], dict[str, str] | None]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            env = kwargs.get("env")
            calls.append((cmd, env if isinstance(env, dict) else None))
            out = MagicMock()
            out.stdout = "main\n"
            return out

        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=fake_run),
        ):
            result = clone_repo_into_session(
                github_repo="octo/private", session_id=VALID_SESSION, user_github_token="ghu_tok"
            )

        workspace = str(tmp_path / VALID_SESSION / "private-clone")
        assert result["status"] == "granted"
        assert result["workspace"] == workspace
        assert result["branch"] == "main"
        clone_cmd, clone_env = calls[0]
        assert clone_cmd == ["gh", "repo", "clone", "octo/private", workspace]
        # Token only travels via env — never on the command line or in the result.
        assert clone_env is not None
        assert clone_env["GH_TOKEN"] == "ghu_tok"
        assert all("ghu_tok" not in " ".join(c) for c, _ in calls)
        assert "ghu_tok" not in str(result)

    def test_existing_clone_is_reused(self, tmp_path: Path) -> None:
        (tmp_path / VALID_SESSION / "private-clone").mkdir(parents=True)
        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run") as mock_run,
        ):
            result = clone_repo_into_session(
                github_repo="octo/private", session_id=VALID_SESSION, user_github_token="t"
            )

        assert result["status"] == "granted"
        assert "already exists" in result["note"]
        mock_run.assert_not_called()

    def test_clone_failure_returns_error(self, tmp_path: Path) -> None:
        err = subprocess.CalledProcessError(1, ["gh"], stderr=b"repository not found")
        with (
            patch("ypl.agent_harness_service.tools.repo_manager.AHS_SESSIONS_DIR", str(tmp_path)),
            patch("ypl.agent_harness_service.tools.repo_manager.subprocess.run", side_effect=err),
        ):
            result = clone_repo_into_session(
                github_repo="octo/private", session_id=VALID_SESSION, user_github_token="t"
            )

        assert result["status"] == "error"
        assert "repository not found" in result["error"]
