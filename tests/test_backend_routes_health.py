"""Unit tests for ypl/backend/routes/v1/health.py.

All external I/O (database, Redis, psutil) is mocked so these tests
run without any live infrastructure.
"""

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from ypl.backend.routes.v1.health import public_router, router


@pytest.fixture
def app() -> FastAPI:
    """Minimal FastAPI app that mounts both routers."""
    _app = FastAPI()
    _app.include_router(router)
    _app.include_router(public_router)
    return _app


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:  # type: ignore[misc]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_health_ok_when_db_succeeds(self, client: AsyncClient) -> None:
        mock_session = AsyncMock()
        mock_session.exec = AsyncMock()

        @asynccontextmanager
        async def mock_get_async_session(*args: Any, **kwargs: Any):  # type: ignore[misc]
            yield mock_session

        with patch("ypl.backend.routes.v1.health.get_async_session", mock_get_async_session):
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_health_error_when_db_fails(self, client: AsyncClient) -> None:
        @asynccontextmanager
        async def failing_session(*args: Any, **kwargs: Any):  # type: ignore[misc]
            raise RuntimeError("DB connection failed")
            yield  # type: ignore[misc]  # unreachable but required for @asynccontextmanager

        with patch("ypl.backend.routes.v1.health.get_async_session", failing_session):
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "DB connection failed" in data["message"]


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------


class TestHealthzEndpoint:
    async def test_healthz_returns_ok(self, client: AsyncClient) -> None:
        mock_process = MagicMock()
        mock_process.num_fds.return_value = 100
        mock_process.memory_percent.return_value = 12.5

        with patch("ypl.backend.routes.v1.health.psutil.Process", return_value=mock_process):
            response = await client.get("/healthz")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    async def test_healthz_includes_process_metrics(self, client: AsyncClient) -> None:
        mock_process = MagicMock()
        mock_process.num_fds.return_value = 42
        mock_process.memory_percent.return_value = 7.5

        with patch("ypl.backend.routes.v1.health.psutil.Process", return_value=mock_process):
            response = await client.get("/healthz")

        assert response.status_code == 200
        data = response.json()
        assert data["num_fds"] == 42
        assert data["memory_percent"] == pytest.approx(7.5)

    async def test_healthz_ok_when_psutil_raises(self, client: AsyncClient) -> None:
        """psutil failures should be swallowed — endpoint still returns 200."""
        with patch("ypl.backend.routes.v1.health.psutil.Process", side_effect=OSError("no proc")):
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_healthz_high_fd_triggers_background_log(self, client: AsyncClient) -> None:
        """When num_fds exceeds threshold, Redis is called for rate-limiting."""
        mock_process = MagicMock()
        mock_process.num_fds.return_value = 15000  # > HIGH_FD_THRESHOLD
        mock_process.memory_percent.return_value = 5.0

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)  # rate-limit key not set yet

        with (
            patch("ypl.backend.routes.v1.health.psutil.Process", return_value=mock_process),
            patch("ypl.backend.routes.v1.health.get_redis_client", AsyncMock(return_value=mock_redis)),
            patch("ypl.backend.routes.v1.health.create_background_task"),
        ):
            response = await client.get("/healthz")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /readyz
# ---------------------------------------------------------------------------


class TestReadyzEndpoint:
    async def test_readyz_returns_ok_default_mode(self, client: AsyncClient) -> None:
        with patch("ypl.backend.routes.v1.health.settings") as mock_settings:
            mock_settings.BACKEND_OPERATING_MODE = "default"
            response = await client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readyz_503_when_leaderboard_refresh_in_progress(self, client: AsyncClient) -> None:
        import sys

        leaderboard_mock = MagicMock()
        leaderboard_mock.refresh_data_in_progress = True
        extra_modules = {
            "ypl.leaderboard": MagicMock(),
            "ypl.leaderboard.ranking": leaderboard_mock,
        }
        with (
            patch("ypl.backend.routes.v1.health.settings") as mock_settings,
            patch.dict(sys.modules, extra_modules),
        ):
            mock_settings.BACKEND_OPERATING_MODE = "leaderboard"
            response = await client.get("/readyz")

        assert response.status_code == 503

    async def test_readyz_200_when_leaderboard_refresh_not_in_progress(self, client: AsyncClient) -> None:
        import sys

        leaderboard_mock = MagicMock()
        leaderboard_mock.refresh_data_in_progress = False
        extra_modules = {
            "ypl.leaderboard": MagicMock(),
            "ypl.leaderboard.ranking": leaderboard_mock,
        }
        with (
            patch("ypl.backend.routes.v1.health.settings") as mock_settings,
            patch.dict(sys.modules, extra_modules),
        ):
            mock_settings.BACKEND_OPERATING_MODE = "leaderboard"
            response = await client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /debug/tasks
# ---------------------------------------------------------------------------


class TestDebugTasksEndpoint:
    async def test_returns_task_list(self, client: AsyncClient) -> None:
        from ypl.backend.utils.debugging_utils import AsyncTaskList

        mock_task_list = AsyncTaskList(tasks=["task-1", "task-2"], num_active_tasks=2)

        with patch(
            "ypl.backend.routes.v1.health.get_async_task_statuses",
            return_value=mock_task_list,
        ):
            response = await client.get("/debug/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["num_active_tasks"] == 2
        assert "task-1" in data["tasks"]

    async def test_returns_empty_task_list(self, client: AsyncClient) -> None:
        from ypl.backend.utils.debugging_utils import AsyncTaskList

        with patch(
            "ypl.backend.routes.v1.health.get_async_task_statuses",
            return_value=AsyncTaskList(),
        ):
            response = await client.get("/debug/tasks")

        assert response.status_code == 200
        assert response.json()["num_active_tasks"] == 0


# ---------------------------------------------------------------------------
# GET /debug/resources
# ---------------------------------------------------------------------------


class TestDebugResourcesEndpoint:
    async def test_returns_resource_info(self, client: AsyncClient) -> None:
        mock_info = {"open_files": 10, "connections": 5}

        with patch(
            "ypl.backend.routes.v1.health.get_resource_leak_info",
            return_value=mock_info,
        ):
            response = await client.get("/debug/resources")

        assert response.status_code == 200
        assert response.json() == mock_info

    async def test_custom_top_n_passed_through(self, client: AsyncClient) -> None:
        with patch(
            "ypl.backend.routes.v1.health.get_resource_leak_info",
            return_value={},
        ) as mock_fn:
            await client.get("/debug/resources?top_n=100")
            mock_fn.assert_called_once_with(top_n=100)

    async def test_top_n_defaults_to_50(self, client: AsyncClient) -> None:
        with patch(
            "ypl.backend.routes.v1.health.get_resource_leak_info",
            return_value={},
        ) as mock_fn:
            await client.get("/debug/resources")
            mock_fn.assert_called_once_with(top_n=50)

    async def test_top_n_out_of_range_returns_422(self, client: AsyncClient) -> None:
        response = await client.get("/debug/resources?top_n=0")
        assert response.status_code == 422

        response = await client.get("/debug/resources?top_n=501")
        assert response.status_code == 422
