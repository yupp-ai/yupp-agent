"""Unit tests for ypl/agent_harness_service/routes.py.

Tests route handlers with mocked service layer functions.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, WebSocketException
from fastapi.testclient import TestClient
from ypl.agent_harness_service.routes import _verify_ws_api_key, router

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    """Build a minimal app with the AHS router and bypass auth dependency."""
    from ypl.agent_harness_service.common.auth import verify_api_key

    _app = FastAPI()
    # Override api key auth to always pass
    _app.dependency_overrides[verify_api_key] = lambda: None
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper factories — return actual Pydantic model instances
# ---------------------------------------------------------------------------


def _make_session_create_response() -> object:
    from ypl.agent_harness_service.common.types import SessionCreateResponse

    return SessionCreateResponse(session_id="sess-abc", status="created")


def _make_session_message_response() -> object:
    from ypl.agent_harness_service.common.types import SessionMessageResponse

    return SessionMessageResponse(session_id="sess-abc", turn_number=1, status="processing")


def _make_session_stop_response() -> object:
    from ypl.agent_harness_service.common.types import SessionStopResponse

    return SessionStopResponse(session_id="sess-abc", status="stopped")


def _make_feedback_response() -> object:
    from ypl.agent_harness_service.common.types import FeedbackResponse

    return FeedbackResponse(status="recorded")


def _make_session_history_response() -> object:
    from ypl.agent_harness_service.common.types import SessionHistoryResponse

    return SessionHistoryResponse(
        session_id="sess-abc",
        agent_id="agent-1",
        status="ACTIVE",
        messages=[],
    )


def _make_session_detail_response() -> object:
    from ypl.agent_harness_service.common.types import SessionDetailResponse, SessionInfo

    info = SessionInfo(
        session_id="sess-abc",
        agent_name="reviewer",
        status="ACTIVE",
        trigger="api",
    )
    return SessionDetailResponse(session=info, subsessions=[])


def _make_agent_list_response() -> object:
    from ypl.agent_harness_service.common.types import AgentListResponse

    return AgentListResponse(agents=[])


def _make_agent_detail_response() -> object:
    from ypl.agent_harness_service.common.types import AgentDetailResponse, AgentInfo

    info = AgentInfo(
        name="reviewer",
        display_name="Reviewer",
        description="reviews code",
        executor_type="harnessed",
        tool_permissions={},
        allowed_subagents=[],
        default_repo="yupp-mind",
        max_turns=20,
        max_budget_usd=2.0,
        timeout_s=300,
        sandbox_enabled=True,
        allowed_gateways=["*"],
    )
    return AgentDetailResponse(agent=info)


def _make_session_list_response() -> object:
    from ypl.agent_harness_service.common.types import SessionListResponse

    return SessionListResponse(sessions=[], total=0, limit=50, offset=0)


def _make_agent_create_response() -> object:
    from ypl.agent_harness_service.common.types import AgentCreateResponse

    return AgentCreateResponse(name="new-agent", status="created")


def _make_agent_edit_response() -> object:
    from ypl.agent_harness_service.common.types import AgentEditResponse

    return AgentEditResponse(name="my-agent", status="updated")


def _make_session_attach_slack_response() -> object:
    from ypl.agent_harness_service.common.types import SessionAttachSlackResponse

    return SessionAttachSlackResponse(session_id="sess-abc", status="attached")


# ---------------------------------------------------------------------------
# Valid request bodies matching actual Pydantic models
# ---------------------------------------------------------------------------


def _session_create_body() -> dict:
    return {"agent_id": "reviewer", "trigger": "api"}


def _session_message_body() -> dict:
    return {"session_id": "sess-abc", "message": "hello"}


def _session_stop_body() -> dict:
    return {"session_id": "sess-abc"}


def _session_feedback_body() -> dict:
    return {"session_id": "sess-abc", "rating": "POSITIVE"}


def _session_attach_slack_body() -> dict:
    return {"session_id": "sess-abc", "slack_session_id": "C123:12345.0:A123"}


def _schedule_create_body() -> dict:
    return {
        "agent_name": "scheduler",
        "message": "do stuff",
        "execute_at": "2025-01-01T00:00:00Z",
        "user_id": "user-1",
    }


def _recurring_schedule_create_body() -> dict:
    return {
        "agent_name": "scheduler",
        "message": "do stuff",
        "cron_expression": "0 9 * * *",
        "user_id": "user-1",
    }


# ---------------------------------------------------------------------------
# Tests: _verify_ws_api_key
# ---------------------------------------------------------------------------


class TestVerifyWsApiKey:
    def test_raises_when_no_expected_key(self) -> None:
        with (
            patch("ypl.agent_harness_service.routes.settings") as mock_settings,
            pytest.raises(WebSocketException),
        ):
            mock_settings.AGENT_HARNESS_SERVICE_API_KEY = None
            _verify_ws_api_key("any-key")

    def test_raises_when_key_mismatch(self) -> None:
        with (
            patch("ypl.agent_harness_service.routes.settings") as mock_settings,
            pytest.raises(WebSocketException),
        ):
            mock_settings.AGENT_HARNESS_SERVICE_API_KEY = "correct-key"
            _verify_ws_api_key("wrong-key")

    def test_passes_with_correct_key(self) -> None:
        with patch("ypl.agent_harness_service.routes.settings") as mock_settings:
            mock_settings.AGENT_HARNESS_SERVICE_API_KEY = "correct-key"
            # Should not raise
            _verify_ws_api_key("correct-key")

    def test_raises_when_api_key_is_none(self) -> None:
        with (
            patch("ypl.agent_harness_service.routes.settings") as mock_settings,
            pytest.raises(WebSocketException),
        ):
            mock_settings.AGENT_HARNESS_SERVICE_API_KEY = "correct-key"
            _verify_ws_api_key(None)


# ---------------------------------------------------------------------------
# Tests: POST /ahs/session/create
# ---------------------------------------------------------------------------


class TestSessionCreate:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_session",
            AsyncMock(return_value=_make_session_create_response()),
        ):
            resp = client.post("/ahs/session/create", json=_session_create_body())
        assert resp.status_code == 200

    def test_returns_400_on_validation_error(self, client: TestClient) -> None:
        from ypl.agent_harness_service.common.types import AHSValidationError

        with patch(
            "ypl.agent_harness_service.routes.create_session",
            AsyncMock(side_effect=AHSValidationError("bad input")),
        ):
            resp = client.post("/ahs/session/create", json=_session_create_body())
        assert resp.status_code == 400

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_session",
            AsyncMock(side_effect=ValueError("Agent not found")),
        ):
            resp = client.post("/ahs/session/create", json=_session_create_body())
        assert resp.status_code == 404

    def test_returns_409_on_runtime_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_session",
            AsyncMock(side_effect=RuntimeError("Queue full")),
        ):
            resp = client.post("/ahs/session/create", json=_session_create_body())
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: POST /ahs/session/message
# ---------------------------------------------------------------------------


class TestSessionMessage:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.send_message",
            AsyncMock(return_value=_make_session_message_response()),
        ):
            resp = client.post("/ahs/session/message", json=_session_message_body())
        assert resp.status_code == 200

    def test_returns_409_on_runtime_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.send_message",
            AsyncMock(side_effect=RuntimeError("Queue full")),
        ):
            resp = client.post("/ahs/session/message", json=_session_message_body())
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: POST /ahs/session/stop
# ---------------------------------------------------------------------------


class TestSessionStop:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.stop_session",
            AsyncMock(return_value=_make_session_stop_response()),
        ):
            resp = client.post("/ahs/session/stop", json=_session_stop_body())
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.stop_session",
            AsyncMock(side_effect=ValueError("Session not found")),
        ):
            resp = client.post("/ahs/session/stop", json=_session_stop_body())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: POST /ahs/session/feedback
# ---------------------------------------------------------------------------


class TestSessionFeedback:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.send_feedback",
            AsyncMock(return_value=_make_feedback_response()),
        ):
            resp = client.post("/ahs/session/feedback", json=_session_feedback_body())
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.send_feedback",
            AsyncMock(side_effect=ValueError("Session not found")),
        ):
            resp = client.post("/ahs/session/feedback", json={"session_id": "sess-xyz"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /ahs/session/{session_id}/history
# ---------------------------------------------------------------------------


class TestSessionHistory:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_session_history",
            AsyncMock(return_value=_make_session_history_response()),
        ):
            resp = client.get("/ahs/session/sess-abc/history")
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_session_history",
            AsyncMock(side_effect=ValueError("Session not found")),
        ):
            resp = client.get("/ahs/session/sess-xyz/history")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /ahs/session/{session_id}
# ---------------------------------------------------------------------------


class TestGetSessionDetail:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_session_detail",
            AsyncMock(return_value=_make_session_detail_response()),
        ):
            resp = client.get("/ahs/session/sess-abc")
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_session_detail",
            AsyncMock(side_effect=ValueError("Not found")),
        ):
            resp = client.get("/ahs/session/bad-session")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /ahs/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.list_sessions",
            AsyncMock(return_value=_make_session_list_response()),
        ):
            resp = client.get("/ahs/sessions")
        assert resp.status_code == 200

    def test_returns_422_on_invalid_datetime(self, client: TestClient) -> None:
        resp = client.get("/ahs/sessions?since=not-a-date")
        assert resp.status_code == 422

    def test_returns_422_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.list_sessions",
            AsyncMock(side_effect=ValueError("invalid filter")),
        ):
            resp = client.get("/ahs/sessions")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /ahs/agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def test_returns_200(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.list_agents",
            AsyncMock(return_value=_make_agent_list_response()),
        ):
            resp = client.get("/ahs/agents")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: GET /ahs/agent/{agent_name}
# ---------------------------------------------------------------------------


class TestGetAgentDetail:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_agent_detail",
            AsyncMock(return_value=_make_agent_detail_response()),
        ):
            resp = client.get("/ahs/agent/reviewer")
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_agent_detail",
            AsyncMock(side_effect=ValueError("Agent not found")),
        ):
            resp = client.get("/ahs/agent/unknown")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: POST /ahs/agent/create
# ---------------------------------------------------------------------------


class TestCreateAgent:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_agent",
            AsyncMock(return_value=_make_agent_create_response()),
        ):
            resp = client.post(
                "/ahs/agent/create",
                json={
                    "name": "new-agent",
                    "user_id": "user-1",
                    "display_name": "New Agent",
                    "executor_config": {"type": "harnessed"},
                },
            )
        assert resp.status_code == 200

    def test_returns_409_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_agent",
            AsyncMock(side_effect=ValueError("Agent already exists")),
        ):
            resp = client.post(
                "/ahs/agent/create",
                json={
                    "name": "existing",
                    "user_id": "user-1",
                    "display_name": "Existing Agent",
                    "executor_config": {"type": "harnessed"},
                },
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests: POST /ahs/agent/edit
# ---------------------------------------------------------------------------


class TestEditAgent:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_agent",
            AsyncMock(return_value=_make_agent_edit_response()),
        ):
            resp = client.post(
                "/ahs/agent/edit",
                json={"name": "my-agent", "user_id": "user-1"},
            )
        assert resp.status_code == 200

    def test_returns_403_on_permission_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_agent",
            AsyncMock(side_effect=PermissionError("Not your agent")),
        ):
            resp = client.post(
                "/ahs/agent/edit",
                json={"name": "other-agent", "user_id": "user-1"},
            )
        assert resp.status_code == 403

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_agent",
            AsyncMock(side_effect=ValueError("Agent not found")),
        ):
            resp = client.post(
                "/ahs/agent/edit",
                json={"name": "none", "user_id": "user-1"},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /ahs/models
# ---------------------------------------------------------------------------


class TestListModels:
    def test_returns_200(self, client: TestClient) -> None:
        with (
            patch("ypl.agent_harness_service.routes.HARNESSED_MODELS", ["claude-code-cli"], create=True),
            patch("ypl.agent_harness_service.routes.KNOWN_MODELS", ["anthropic/claude-sonnet-4-6"], create=True),
        ):
            resp = client.get("/ahs/models")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: POST /ahs/resolve_user
# ---------------------------------------------------------------------------


class TestResolveUser:
    def test_returns_400_for_non_yupp_email(self, client: TestClient) -> None:
        resp = client.post("/ahs/resolve_user", json={"email": "user@gmail.com"})
        assert resp.status_code == 400

    def test_returns_404_when_user_not_found(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_user_id_by_email",
            AsyncMock(return_value=None),
        ):
            resp = client.post("/ahs/resolve_user", json={"email": "nobody@yupp.ai"})
        assert resp.status_code == 404

    def test_returns_200_when_user_found(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_user_id_by_email",
            AsyncMock(return_value="user-uuid-123"),
        ):
            resp = client.post("/ahs/resolve_user", json={"email": "alice@yupp.ai"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "user-uuid-123"
        assert data["email"] == "alice@yupp.ai"


# ---------------------------------------------------------------------------
# Tests: Schedule endpoints
# ---------------------------------------------------------------------------


class TestScheduleCreate:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_one_time_schedule",
            AsyncMock(
                return_value={
                    "success": True,
                    "agent_schedule_id": "sched-1",
                    "agent_name": "scheduler",
                    "schedule_type": "SCHEDULED",
                    "status": "PENDING",
                }
            ),
        ):
            resp = client.post("/ahs/schedules/create", json=_schedule_create_body())
        assert resp.status_code == 200

    def test_returns_400_on_failure(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_one_time_schedule",
            AsyncMock(return_value={"success": False, "error": "Bad request"}),
        ):
            resp = client.post("/ahs/schedules/create", json=_schedule_create_body())
        assert resp.status_code == 400

    def test_returns_400_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_one_time_schedule",
            AsyncMock(side_effect=ValueError("invalid args")),
        ):
            resp = client.post("/ahs/schedules/create", json=_schedule_create_body())
        assert resp.status_code == 400


class TestListSchedules:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.list_schedules",
            AsyncMock(return_value={"success": True, "agent_schedules": [], "count": 0}),
        ):
            resp = client.get("/ahs/schedules")
        assert resp.status_code == 200

    def test_returns_400_on_failure(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.list_schedules",
            AsyncMock(return_value={"success": False, "error": "error"}),
        ):
            resp = client.get("/ahs/schedules")
        assert resp.status_code == 400


class TestGetScheduleDetail:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        from ypl.agent_harness_service.common.types import ScheduleInfo

        schedule = ScheduleInfo(
            agent_schedule_id="sched-1",
            agent_name="scheduler",
            schedule_type="SCHEDULED",
            status="PENDING",
            message="do stuff",
        )
        with patch(
            "ypl.agent_harness_service.routes.get_schedule_detail",
            AsyncMock(return_value={"success": True, "schedule": schedule}),
        ):
            resp = client.get("/ahs/schedule/sched-1")
        assert resp.status_code == 200

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_schedule_detail",
            AsyncMock(return_value={"success": False, "error": "Not found", "error_code": "NOT_FOUND"}),
        ):
            resp = client.get("/ahs/schedule/bad-id")
        assert resp.status_code == 404

    def test_returns_400_on_other_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.get_schedule_detail",
            AsyncMock(return_value={"success": False, "error": "DB error"}),
        ):
            resp = client.get("/ahs/schedule/sched-1")
        assert resp.status_code == 400


class TestDeleteSchedule:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.cancel_schedule",
            AsyncMock(return_value={"success": True}),
        ):
            resp = client.delete("/ahs/schedule/sched-1?user_id=user-1")
        assert resp.status_code == 200

    def test_returns_403_on_forbidden(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.cancel_schedule",
            AsyncMock(return_value={"success": False, "error": "Forbidden", "error_code": "FORBIDDEN"}),
        ):
            resp = client.delete("/ahs/schedule/sched-1?user_id=user-2")
        assert resp.status_code == 403

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.cancel_schedule",
            AsyncMock(return_value={"success": False, "error": "Not found", "error_code": "NOT_FOUND"}),
        ):
            resp = client.delete("/ahs/schedule/bad-id?user_id=user-1")
        assert resp.status_code == 404


class TestTriggerSchedule:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.trigger_schedule",
            AsyncMock(return_value={"success": True, "session_id": "sess-1", "run_number": 1}),
        ):
            resp = client.post("/ahs/schedule/sched-1/trigger", json={"user_id": "user-1"})
        assert resp.status_code == 200

    def test_returns_502_on_execution_failed(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.trigger_schedule",
            AsyncMock(return_value={"success": False, "error": "Execution failed", "error_code": "EXECUTION_FAILED"}),
        ):
            resp = client.post("/ahs/schedule/sched-1/trigger", json={"user_id": "user-1"})
        assert resp.status_code == 502

    def test_returns_403_on_forbidden(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.trigger_schedule",
            AsyncMock(return_value={"success": False, "error": "Forbidden", "error_code": "FORBIDDEN"}),
        ):
            resp = client.post("/ahs/schedule/sched-1/trigger", json={"user_id": "user-2"})
        assert resp.status_code == 403


class TestEditSchedule:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_schedule",
            AsyncMock(return_value={"success": True}),
        ):
            resp = client.post(
                "/ahs/schedule/edit",
                json={"agent_schedule_id": "sched-1", "user_id": "user-1"},
            )
        assert resp.status_code == 200

    def test_returns_403_on_forbidden(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_schedule",
            AsyncMock(return_value={"success": False, "error": "Forbidden", "error_code": "FORBIDDEN"}),
        ):
            resp = client.post(
                "/ahs/schedule/edit",
                json={"agent_schedule_id": "sched-1", "user_id": "user-2"},
            )
        assert resp.status_code == 403

    def test_returns_404_when_not_found(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.edit_schedule",
            AsyncMock(return_value={"success": False, "error": "Not found", "error_code": "NOT_FOUND"}),
        ):
            resp = client.post(
                "/ahs/schedule/edit",
                json={"agent_schedule_id": "bad-id", "user_id": "user-1"},
            )
        assert resp.status_code == 404


class TestListScheduleRuns:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        from ypl.agent_harness_service.common.types import ScheduleRunInfo

        runs = [
            ScheduleRunInfo(agent_schedule_run_id="r1", run_number=1, status="COMPLETED"),
            ScheduleRunInfo(agent_schedule_run_id="r2", run_number=2, status="COMPLETED"),
        ]
        with patch(
            "ypl.agent_harness_service.routes.list_schedule_runs",
            AsyncMock(return_value=runs),
        ):
            resp = client.get("/ahs/schedule/sched-1/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2


class TestSessionAttachSlack:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.attach_slack_to_session",
            AsyncMock(return_value=_make_session_attach_slack_response()),
        ):
            resp = client.post("/ahs/session/attach-slack", json=_session_attach_slack_body())
        assert resp.status_code == 200

    def test_returns_404_on_value_error(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.attach_slack_to_session",
            AsyncMock(side_effect=ValueError("Session not found")),
        ):
            resp = client.post("/ahs/session/attach-slack", json=_session_attach_slack_body())
        assert resp.status_code == 404


class TestRecurringScheduleCreate:
    def test_returns_200_on_success(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.routes.create_recurring_schedule",
            AsyncMock(
                return_value={
                    "success": True,
                    "agent_schedule_id": "sched-2",
                    "agent_name": "scheduler",
                    "schedule_type": "RECURRING",
                    "status": "PENDING",
                }
            ),
        ):
            resp = client.post("/ahs/schedules/create-recurring", json=_recurring_schedule_create_body())
        assert resp.status_code == 200
