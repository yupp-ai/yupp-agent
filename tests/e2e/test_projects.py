"""E2E tests for AHS project and task lifecycle via MCP tools + REST API.

Projects and tasks are created via MCP tools (add_project, add_tasks) called over
JSON-RPC 2.0 at /mcp/. Auth uses YUPPSTER_MCP_TOKEN (Bearer yupp_dev_*).
CRUD, status transitions, and dependency cascade are tested via REST API.
"""

from __future__ import annotations
import json
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import E2E_PREFIX, call_mcp_tool

pytestmark = [pytest.mark.e2e]


async def _create_project(
    client: httpx.AsyncClient,
    mcp_headers: dict[str, str],
    name: str,
) -> str:
    """Create a project via MCP and return the project_id."""
    result = await call_mcp_tool(client, mcp_headers, "add_project", {"name": name})
    # MCP JSON-RPC response: {"jsonrpc": "2.0", "id": "...", "result": {"content": [...]}}
    inner = result.get("result", result)
    content = inner.get("content", [])
    assert len(content) >= 1, f"add_project returned no content: {result}"
    text = content[0].get("text", "")
    data = json.loads(text)
    assert data.get("success"), f"add_project failed: {data}"
    pid: str = data["agent_project_id"]
    return pid


async def _create_tasks(
    client: httpx.AsyncClient,
    mcp_headers: dict[str, str],
    project_id: str,
    tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create tasks via MCP add_tasks and return task list."""
    result = await call_mcp_tool(
        client,
        mcp_headers,
        "add_tasks",
        {"project_id": project_id, "tasks": json.dumps(tasks)},
    )
    inner = result.get("result", result)
    content = inner.get("content", [])
    assert len(content) >= 1, f"add_tasks returned no content: {result}"
    text = content[0].get("text", "")
    data = json.loads(text)
    assert data.get("success"), f"add_tasks failed: {data}"
    task_list: list[dict[str, Any]] = data.get("tasks", [])
    return task_list


class TestProjectLifecycle:
    """Test project creation, detail, update, and status changes."""

    async def test_create_and_get_project(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Create a project via MCP, then GET its detail via REST."""
        name = f"{E2E_PREFIX}detail-{tag}"
        project_id = await _create_project(client, mcp_headers, name)

        resp = await client.get(f"/ahs/projects/{project_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == name

    async def test_update_project(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Update a project's name and description."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}update-{tag}")

        resp = await client.patch(
            f"/ahs/projects/{project_id}",
            json={"name": f"{E2E_PREFIX}updated-{tag}", "description": "Updated by e2e test"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == f"{E2E_PREFIX}updated-{tag}"

    async def test_change_project_status(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Change project status from PAUSED to ACTIVE."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}status-{tag}")

        resp = await client.post(
            f"/ahs/projects/{project_id}/status",
            json={"status": "ACTIVE"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ACTIVE"

    async def test_list_projects(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """List projects and verify at least one exists."""
        await _create_project(client, mcp_headers, f"{E2E_PREFIX}list-{tag}")

        resp = await client.get("/ahs/projects", params={"limit": 10}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1


class TestTaskLifecycle:
    """Test task creation, detail, update, and status transitions."""

    async def test_create_tasks_and_list(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Create tasks via MCP and list them via REST."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}tasks-{tag}")
        await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"task-a-{tag}", "title": f"{E2E_PREFIX}task-a-{tag}"},
                {"name": f"task-b-{tag}", "title": f"{E2E_PREFIX}task-b-{tag}"},
            ],
        )

        resp = await client.get(f"/ahs/projects/{project_id}/tasks", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] >= 2

    async def test_update_task(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Update a task's title and priority."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}tupd-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"update-me-{tag}", "title": f"{E2E_PREFIX}update-me-{tag}"},
            ],
        )
        task_id = tasks[0]["agent_task_id"]

        resp = await client.patch(
            f"/ahs/projects/{project_id}/tasks/{task_id}",
            json={"title": f"{E2E_PREFIX}updated-{tag}", "priority": "HIGH"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    async def test_status_ready_to_completed(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Transition a task: READY → IN_PROGRESS → COMPLETED."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}status-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"complete-{tag}", "title": f"{E2E_PREFIX}complete-me-{tag}"},
            ],
        )
        task_id = tasks[0]["agent_task_id"]

        # READY → IN_PROGRESS
        resp1 = await client.post(
            f"/ahs/projects/{project_id}/tasks/{task_id}/status",
            json={"status": "IN_PROGRESS"},
            headers=auth_headers,
        )
        assert resp1.status_code == 200

        # IN_PROGRESS → COMPLETED
        resp2 = await client.post(
            f"/ahs/projects/{project_id}/tasks/{task_id}/status",
            json={"status": "COMPLETED", "result": {"summary": "Done by e2e test"}},
            headers=auth_headers,
        )
        assert resp2.status_code == 200

    async def test_invalid_status_transition(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Invalid status transition should return 400."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}invalid-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"invalid-{tag}", "title": f"{E2E_PREFIX}invalid-{tag}"},
            ],
        )
        task_id = tasks[0]["agent_task_id"]

        # READY → COMPLETED directly (should fail — must go through IN_PROGRESS)
        resp = await client.post(
            f"/ahs/projects/{project_id}/tasks/{task_id}/status",
            json={"status": "COMPLETED"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestTaskDependencies:
    """Test task dependency management and cascade behavior."""

    async def test_dependency_cascade(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Complete task A → dependent task B auto-promotes to READY."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}deps-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"dep-a-{tag}", "title": f"{E2E_PREFIX}dep-a-{tag}"},
                {"name": f"dep-b-{tag}", "title": f"{E2E_PREFIX}dep-b-{tag}", "depends_on": [f"dep-a-{tag}"]},
            ],
        )

        task_a = next(t for t in tasks if f"dep-a-{tag}" in t["title"])
        task_b = next(t for t in tasks if f"dep-b-{tag}" in t["title"])

        # B should be BLOCKED (has unmet dependency)
        b_detail = await client.get(
            f"/ahs/projects/{project_id}/tasks/{task_b['agent_task_id']}",
            headers=auth_headers,
        )
        assert b_detail.json()["status"] in ("BLOCKED", "PENDING")

        # Complete A: READY → IN_PROGRESS → COMPLETED
        a_id = task_a["agent_task_id"]
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{a_id}/status",
            json={"status": "IN_PROGRESS"},
            headers=auth_headers,
        )
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{a_id}/status",
            json={"status": "COMPLETED", "result": {"summary": "Done"}},
            headers=auth_headers,
        )

        # B should now be READY (dependency cascade)
        b_detail2 = await client.get(
            f"/ahs/projects/{project_id}/tasks/{task_b['agent_task_id']}",
            headers=auth_headers,
        )
        assert b_detail2.json()["status"] == "READY"

    async def test_multi_dependency_all_must_complete(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Task C depends on both A and B — only becomes READY when both complete."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}multidep-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"ma-{tag}", "title": f"{E2E_PREFIX}multi-a-{tag}"},
                {"name": f"mb-{tag}", "title": f"{E2E_PREFIX}multi-b-{tag}"},
                {"name": f"mc-{tag}", "title": f"{E2E_PREFIX}multi-c-{tag}", "depends_on": [f"ma-{tag}", f"mb-{tag}"]},
            ],
        )

        task_a = next(t for t in tasks if f"multi-a-{tag}" in t["title"])
        task_b = next(t for t in tasks if f"multi-b-{tag}" in t["title"])
        task_c = next(t for t in tasks if f"multi-c-{tag}" in t["title"])

        # C should be BLOCKED
        assert task_c["status"] == "BLOCKED"

        # Complete A only
        a_id = task_a["agent_task_id"]
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{a_id}/status", json={"status": "IN_PROGRESS"}, headers=auth_headers
        )
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{a_id}/status",
            json={"status": "COMPLETED", "result": {"summary": "Done"}},
            headers=auth_headers,
        )

        # C should STILL be BLOCKED (B not complete yet)
        c_resp = await client.get(f"/ahs/projects/{project_id}/tasks/{task_c['agent_task_id']}", headers=auth_headers)
        assert c_resp.json()["status"] in ("BLOCKED", "PENDING")

        # Complete B
        b_id = task_b["agent_task_id"]
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{b_id}/status", json={"status": "IN_PROGRESS"}, headers=auth_headers
        )
        await client.post(
            f"/ahs/projects/{project_id}/tasks/{b_id}/status",
            json={"status": "COMPLETED", "result": {"summary": "Done"}},
            headers=auth_headers,
        )

        # Now C should be READY
        c_resp2 = await client.get(f"/ahs/projects/{project_id}/tasks/{task_c['agent_task_id']}", headers=auth_headers)
        assert c_resp2.json()["status"] == "READY"

    async def test_cycle_detection(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """Setting circular dependencies should return 400."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}cycle-{tag}")
        tasks = await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"cycle-a-{tag}", "title": f"{E2E_PREFIX}cycle-a-{tag}"},
                {"name": f"cycle-b-{tag}", "title": f"{E2E_PREFIX}cycle-b-{tag}"},
            ],
        )

        task_a = tasks[0]["agent_task_id"]
        task_b = tasks[1]["agent_task_id"]

        # Set A depends on B
        resp1 = await client.put(
            f"/ahs/projects/{project_id}/tasks/{task_a}/dependencies",
            json={"depends_on": [task_b]},
            headers=auth_headers,
        )
        assert resp1.status_code == 200

        # Set B depends on A → cycle
        resp2 = await client.put(
            f"/ahs/projects/{project_id}/tasks/{task_b}/dependencies",
            json={"depends_on": [task_a]},
            headers=auth_headers,
        )
        assert resp2.status_code == 400


class TestTaskPickup:
    """Test get_ready_tasks and claim_task MCP tools."""

    async def test_get_ready_tasks(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """get_ready_tasks returns READY tasks in priority order."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}ready-{tag}")
        await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"low-{tag}", "title": f"{E2E_PREFIX}low-{tag}", "priority": "LOW"},
                {"name": f"high-{tag}", "title": f"{E2E_PREFIX}high-{tag}", "priority": "HIGH"},
            ],
        )

        result = await call_mcp_tool(client, mcp_headers, "get_ready_tasks", {"project_id": project_id})
        inner = result.get("result", result)
        text = inner["content"][0]["text"]
        data = json.loads(text)
        assert data.get("success"), f"get_ready_tasks failed: {data}"
        tasks = data["tasks"]
        assert len(tasks) >= 2
        # HIGH priority should come before LOW
        priorities = [t["priority"] for t in tasks]
        assert priorities.index("HIGH") < priorities.index("LOW")

    async def test_claim_task(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """claim_task atomically transitions a READY task to IN_PROGRESS."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}claim-{tag}")
        await _create_tasks(
            client,
            mcp_headers,
            project_id,
            [
                {"name": f"claimme-{tag}", "title": f"{E2E_PREFIX}claimme-{tag}"},
            ],
        )

        result = await call_mcp_tool(client, mcp_headers, "claim_task", {"project_id": project_id})
        inner = result.get("result", result)
        text = inner["content"][0]["text"]
        data = json.loads(text)
        assert data.get("success"), f"claim_task failed: {data}"
        assert data["task"]["status"] == "IN_PROGRESS"
        assert f"claimme-{tag}" in data["task"]["title"]

    async def test_claim_task_no_ready_tasks(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        mcp_headers: dict[str, str],
        tag: str,
    ) -> None:
        """claim_task with no READY tasks returns an error."""
        project_id = await _create_project(client, mcp_headers, f"{E2E_PREFIX}empty-{tag}")
        # No tasks created — nothing to claim

        result = await call_mcp_tool(client, mcp_headers, "claim_task", {"project_id": project_id})
        inner = result.get("result", result)
        text = inner["content"][0]["text"]
        data = json.loads(text)
        assert not data.get("success") or "no" in data.get("error", "").lower() or data.get("task") is None
