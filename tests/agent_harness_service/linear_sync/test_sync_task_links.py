"""Tests for sync_task_links_to_linear and deduplication logic."""

from __future__ import annotations
from typing import Any
from unittest.mock import MagicMock, patch

from tests.agent_harness_service.linear_sync.conftest import make_task


def _make_mock_client(
    existing_urls: list[str] | None = None,
    attach_success: bool = True,
) -> MagicMock:
    """Create a mock LinearClient with configurable existing attachments."""
    client = MagicMock()
    nodes = [{"id": f"att-{i}", "title": "existing", "url": u} for i, u in enumerate(existing_urls or [])]
    client.list_attachments.return_value = {"data": {"issue": {"attachments": {"nodes": nodes}}}}
    attachment = {"id": "new", "title": "t", "url": "u"}
    client.attach_link.return_value = {
        "data": {"attachmentLinkURL": {"success": attach_success, "attachment": attachment}}
    }
    return client


async def _run_sync(task: Any, issue_id: str, mock_client: MagicMock) -> int:
    """Run sync_task_links_to_linear with mocked LinearClient and asyncio.to_thread."""
    from ypl.agent_harness_service.tools.linear_sync.export_to_linear import sync_task_links_to_linear

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    with (
        patch("ypl.agent_harness_service.tools.linear_sync.export_to_linear.LinearClient", return_value=mock_client),
        patch(
            "ypl.agent_harness_service.tools.linear_sync.export_to_linear.asyncio.to_thread",
            side_effect=fake_to_thread,
        ),
    ):
        return await sync_task_links_to_linear(task, issue_id)


class TestSyncTaskLinksToLinear:
    """Tests for sync_task_links_to_linear."""

    async def test_no_links_returns_zero(self) -> None:
        task = make_task()
        task.result = None
        task.assigned_session_ids = None
        client = _make_mock_client()

        count = await _run_sync(task, "issue-123", client)

        assert count == 0
        client.attach_link.assert_not_called()
        # Should not even fetch attachments when there are no links
        client.list_attachments.assert_not_called()

    async def test_attaches_pr_link(self) -> None:
        task = make_task()
        task.result = {"pr_url": "https://github.com/org/repo/pull/42"}
        task.assigned_session_ids = None
        client = _make_mock_client()

        count = await _run_sync(task, "issue-123", client)

        assert count == 1
        client.attach_link.assert_called_once_with("issue-123", "https://github.com/org/repo/pull/42", "PR #42")

    async def test_attaches_session_links(self) -> None:
        task = make_task()
        task.result = None
        task.assigned_session_ids = ["abc12345-6789-0000-0000-000000000000"]
        client = _make_mock_client()

        count = await _run_sync(task, "issue-123", client)

        assert count == 1
        call_args = client.attach_link.call_args
        assert "abc12345" in call_args[0][2]  # title contains session ID prefix

    async def test_skips_duplicate_urls(self) -> None:
        pr_url = "https://github.com/org/repo/pull/42"
        task = make_task()
        task.result = {"pr_url": pr_url}
        task.assigned_session_ids = None
        client = _make_mock_client(existing_urls=[pr_url])

        count = await _run_sync(task, "issue-123", client)

        assert count == 0
        client.attach_link.assert_not_called()

    async def test_attaches_both_pr_and_sessions(self) -> None:
        task = make_task()
        task.result = {"pr_url": "https://github.com/org/repo/pull/10"}
        task.assigned_session_ids = ["sess1111-0000-0000-0000-000000000000"]
        client = _make_mock_client()

        count = await _run_sync(task, "issue-123", client)

        assert count == 2
        assert client.attach_link.call_count == 2

    async def test_partial_dedup_only_new_links_attached(self) -> None:
        """If one link already exists but another is new, only attach the new one."""
        pr_url = "https://github.com/org/repo/pull/5"
        task = make_task()
        task.result = {"pr_url": pr_url}
        task.assigned_session_ids = ["newsess1-0000-0000-0000-000000000000"]
        client = _make_mock_client(existing_urls=[pr_url])

        count = await _run_sync(task, "issue-123", client)

        assert count == 1  # Only the session link
        assert client.attach_link.call_count == 1
        call_url = client.attach_link.call_args[0][1]
        assert "newsess1" in call_url
