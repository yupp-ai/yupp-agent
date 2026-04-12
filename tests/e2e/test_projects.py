"""E2E project CRUD tests."""

import httpx
import pytest


pytestmark = pytest.mark.e2e


class TestProjectCRUD:
    async def test_create_project(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/projects",
            json={
                "name": "e2e-test-project",
                "description": "Created by e2e test",
                "agent_name": "parrot-bubba",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "project_id" in data

    async def test_list_projects(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.get("/ahs/projects", headers=auth_headers)
        assert resp.status_code == 200

    async def test_create_project_with_tasks(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # Create project
        proj_resp = await client.post(
            "/ahs/projects",
            json={
                "name": "e2e-project-with-tasks",
                "description": "Project with task dependencies",
                "agent_name": "parrot-bubba",
            },
            headers=auth_headers,
        )
        if proj_resp.status_code != 200:
            pytest.skip(f"Project creation returned {proj_resp.status_code}")
        project_id = proj_resp.json()["project_id"]

        # Add tasks
        tasks_resp = await client.post(
            "/ahs/projects/tasks",
            json={
                "project_id": project_id,
                "tasks": [
                    {"title": "Task A", "description": "First task"},
                    {"title": "Task B", "description": "Depends on A", "depends_on_names": ["Task A"]},
                ],
            },
            headers=auth_headers,
        )
        assert tasks_resp.status_code == 200


class TestScheduleCRUD:
    async def test_create_one_time_schedule(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": "Scheduled e2e test",
                "execute_at": "2099-01-01T00:00:00Z",
                "timezone": "UTC",
                "user_id": "e2e-test-user",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_schedule_id" in data

    async def test_list_schedules(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.get("/ahs/schedules", headers=auth_headers)
        assert resp.status_code == 200
