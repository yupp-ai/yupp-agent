"""E2E tests for AHS schedule CRUD (one-time, recurring, edit, trigger, cancel)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.e2e.conftest import E2E_PREFIX, wait_for_assistant_reply

pytestmark = [pytest.mark.e2e]


def _future_iso(minutes: int = 30) -> str:
    """Return an ISO-8601 datetime string `minutes` in the future."""
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


class TestScheduleOneTime:
    """Test one-time schedule creation, detail, and listing."""

    async def test_create_one_time_schedule(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Create a one-time schedule and verify response fields."""
        resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}onetime {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}onetime-{tag}",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule_type"] == "SCHEDULED"
        assert data["status"] == "PENDING"
        assert "agent_schedule_id" in data
        schedule_cleanup.append(data["agent_schedule_id"])

    async def test_get_schedule_detail(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Create a schedule, then GET its detail."""
        create_resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}detail {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}detail-{tag}",
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]
        schedule_cleanup.append(sid)

        detail_resp = await client.get(f"/ahs/schedule/{sid}", headers=auth_headers)
        assert detail_resp.status_code == 200
        sched = detail_resp.json()["schedule"]
        assert sched["agent_name"] == "parrot-bubba"
        assert sched["name"] == f"{E2E_PREFIX}detail-{tag}"
        assert sched["status"] == "PENDING"

    async def test_list_schedules(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Create a schedule, then list schedules filtered by agent_name."""
        create_resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}list {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}list-{tag}",
            },
            headers=auth_headers,
        )
        schedule_cleanup.append(create_resp.json()["agent_schedule_id"])

        list_resp = await client.get(
            "/ahs/schedules",
            params={"agent_name": "parrot-bubba", "status": "PENDING", "limit": 10},
            headers=auth_headers,
        )
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["count"] >= 1
        assert len(data["schedules"]) >= 1


class TestScheduleRecurring:
    """Test recurring schedule creation, triggering, and runs."""

    async def test_create_recurring_schedule(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Create a recurring schedule with a far-future cron expression."""
        resp = await client.post(
            "/ahs/schedules/create-recurring",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}recurring {tag}",
                "cron_expression": "0 0 1 1 *",  # once a year (Jan 1 midnight)
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}recurring-{tag}",
                "max_runs": 3,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule_type"] == "RECURRING"
        schedule_cleanup.append(data["agent_schedule_id"])

    async def test_trigger_recurring_schedule(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Trigger a recurring schedule and verify it creates a session."""
        # Create recurring schedule
        create_resp = await client.post(
            "/ahs/schedules/create-recurring",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}trigger {tag}",
                "cron_expression": "0 0 1 1 *",
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}trigger-{tag}",
                "max_runs": 5,
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]
        schedule_cleanup.append(sid)

        # Trigger it
        trigger_resp = await client.post(
            f"/ahs/schedule/{sid}/trigger",
            json={"user_id": user_id},
            headers=auth_headers,
        )
        assert trigger_resp.status_code == 200
        trigger_data = trigger_resp.json()
        assert "session_id" in trigger_data
        assert trigger_data["run_number"] >= 1

        # Verify the session was created and processed
        session_id = trigger_data["session_id"]
        messages = await wait_for_assistant_reply(client, session_id, auth_headers, timeout=15.0)
        assert len(messages) >= 1, "Triggered schedule should have produced a session with messages"

    async def test_list_schedule_runs(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Trigger a schedule, then list its runs."""
        create_resp = await client.post(
            "/ahs/schedules/create-recurring",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}runs {tag}",
                "cron_expression": "0 0 1 1 *",
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}runs-{tag}",
                "max_runs": 5,
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]
        schedule_cleanup.append(sid)

        # Trigger
        await client.post(f"/ahs/schedule/{sid}/trigger", json={"user_id": user_id}, headers=auth_headers)

        # List runs
        import asyncio

        await asyncio.sleep(3)
        runs_resp = await client.get(f"/ahs/schedule/{sid}/runs", params={"limit": 10}, headers=auth_headers)
        assert runs_resp.status_code == 200
        data = runs_resp.json()
        assert data["count"] >= 1
        assert len(data["runs"]) >= 1
        assert data["runs"][0]["session_id"] is not None


class TestScheduleEdit:
    """Test editing schedules."""

    async def test_edit_schedule(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Edit a schedule's name and message, verify via detail."""
        create_resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}edit-original {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}edit-original-{tag}",
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]
        schedule_cleanup.append(sid)

        # Edit
        edit_resp = await client.post(
            "/ahs/schedule/edit",
            json={
                "agent_schedule_id": sid,
                "user_id": user_id,
                "name": f"{E2E_PREFIX}edit-updated-{tag}",
                "message": f"{E2E_PREFIX}edit-updated {tag}",
            },
            headers=auth_headers,
        )
        assert edit_resp.status_code == 200

        # Verify
        detail_resp = await client.get(f"/ahs/schedule/{sid}", headers=auth_headers)
        sched = detail_resp.json()["schedule"]
        assert sched["name"] == f"{E2E_PREFIX}edit-updated-{tag}"
        assert sched["message"] == f"{E2E_PREFIX}edit-updated {tag}"

    async def test_edit_schedule_wrong_user(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
        schedule_cleanup: list[str],
    ) -> None:
        """Editing a schedule with a different user_id should fail."""
        create_resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}edit-perm {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}edit-perm-{tag}",
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]
        schedule_cleanup.append(sid)

        edit_resp = await client.post(
            "/ahs/schedule/edit",
            json={
                "agent_schedule_id": sid,
                "user_id": "00000000-0000-0000-0000-000000000000",
                "name": "should-not-work",
            },
            headers=auth_headers,
        )
        assert edit_resp.status_code in (403, 400)


class TestScheduleCancel:
    """Test schedule cancellation."""

    async def test_cancel_schedule(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
        tag: str,
    ) -> None:
        """Cancel a schedule and verify status."""
        create_resp = await client.post(
            "/ahs/schedules/create",
            json={
                "agent_name": "parrot-bubba",
                "message": f"{E2E_PREFIX}cancel {tag}",
                "execute_at": _future_iso(30),
                "timezone": "UTC",
                "user_id": user_id,
                "name": f"{E2E_PREFIX}cancel-{tag}",
            },
            headers=auth_headers,
        )
        sid = create_resp.json()["agent_schedule_id"]

        del_resp = await client.delete(
            f"/ahs/schedule/{sid}",
            params={"user_id": user_id},
            headers=auth_headers,
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "CANCELLED"

        # Verify via detail
        detail_resp = await client.get(f"/ahs/schedule/{sid}", headers=auth_headers)
        assert detail_resp.json()["schedule"]["status"] == "CANCELLED"

    async def test_cancel_nonexistent(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
        user_id: str,
    ) -> None:
        """Cancelling a nonexistent schedule returns 404."""
        resp = await client.delete(
            "/ahs/schedule/00000000-0000-0000-0000-000000000000",
            params={"user_id": user_id},
            headers=auth_headers,
        )
        assert resp.status_code in (404, 400)
