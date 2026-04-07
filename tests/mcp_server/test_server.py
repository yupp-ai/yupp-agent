"""Unit tests for ypl/mcp_server/server.py.

Covers:
  - _McpSessionIdFilter.filter() — injects mcp_request_id into log records
  - McpSessionMiddleware.dispatch():
      - Skips session tracking for /health, /healthz, /tools, /tools/<name>
      - Assigns UUID for other paths
      - Resets ContextVar after request completes
  - health_check() — correct JSON response shape
  - invoke_tool() — authentication required, body validation, tool not found,
      validation error, generic exception, success
  - _get_middleware() — DEV_TOKEN vs OAUTH mode
  - Server module imports and exports expected symbols

All tests run without a live database or MCP protocol connection.
"""

from __future__ import annotations
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient
from ypl.mcp_server.context_vars import mcp_request_id_var
from ypl.mcp_server.server import (
    _PATHS_WITHOUT_SESSION_TRACKING,
    McpSessionMiddleware,
    _McpSessionIdFilter,
    health_check,
    invoke_tool,
)

# ---------------------------------------------------------------------------
# _McpSessionIdFilter
# ---------------------------------------------------------------------------


class TestMcpSessionIdFilter:
    def test_injects_none_when_no_request(self) -> None:
        """Outside a request, mcp_request_id is None."""
        mcp_request_id_var.set(None)
        log_filter = _McpSessionIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        result = log_filter.filter(record)
        assert result is True
        assert record.mcp_request_id is None  # type: ignore[attr-defined]

    def test_injects_uuid_when_in_request(self) -> None:
        """Inside a request, mcp_request_id is populated."""
        test_id = "test-uuid-1234"
        mcp_request_id_var.set(test_id)

        log_filter = _McpSessionIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        log_filter.filter(record)
        assert record.mcp_request_id == test_id  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# McpSessionMiddleware
# ---------------------------------------------------------------------------


def _make_request(path: str) -> MagicMock:
    req = MagicMock(spec=Request)
    req.url = MagicMock()
    req.url.path = path
    return req


class TestMcpSessionMiddlewarePaths:
    """Verify which paths skip tracking."""

    def test_paths_without_tracking_constants(self) -> None:
        assert "/health" in _PATHS_WITHOUT_SESSION_TRACKING
        assert "/healthz" in _PATHS_WITHOUT_SESSION_TRACKING
        assert "/tools" in _PATHS_WITHOUT_SESSION_TRACKING

    async def test_health_path_skips_tracking(self) -> None:
        """McpSessionMiddleware.dispatch() skips tracking for /health."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def health(req: Request) -> JSONResponse:
            # If tracking had fired, a UUID would be set; check it's not
            assert mcp_request_id_var.get() is None
            return JSONResponse({"ok": True})

        mcp_request_id_var.set(None)
        app = Starlette(routes=[Route("/health", health)])
        app.add_middleware(McpSessionMiddleware)

        client = TestClient(app, raise_server_exceptions=True)
        r = client.get("/health")
        assert r.status_code == 200

    async def test_tools_subpath_skips_tracking(self) -> None:
        """Paths starting with /tools/ skip session UUID assignment."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        seen_ids: list[str | None] = []

        async def tool_handler(req: Request) -> JSONResponse:
            seen_ids.append(mcp_request_id_var.get())
            return JSONResponse({"ok": True})

        mcp_request_id_var.set(None)
        app = Starlette(routes=[Route("/tools/my_tool", tool_handler)])
        app.add_middleware(McpSessionMiddleware)

        client = TestClient(app, raise_server_exceptions=True)
        r = client.get("/tools/my_tool")
        assert r.status_code == 200
        # UUID should not have been assigned (still None as we set it to None outside)
        assert seen_ids[0] is None

    async def test_non_tracked_path_gets_uuid(self) -> None:
        """Requests to /mcp get a UUID assigned."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        seen_ids: list[str | None] = []

        async def mcp_handler(req: Request) -> JSONResponse:
            seen_ids.append(mcp_request_id_var.get())
            return JSONResponse({"ok": True})

        mcp_request_id_var.set(None)
        app = Starlette(routes=[Route("/mcp", mcp_handler, methods=["POST"])])
        app.add_middleware(McpSessionMiddleware)

        client = TestClient(app, raise_server_exceptions=True)
        r = client.post("/mcp")
        assert r.status_code == 200
        assert seen_ids[0] is not None
        # Should be a valid UUID
        import uuid

        uuid.UUID(seen_ids[0])  # raises if invalid

    async def test_context_var_reset_after_request(self) -> None:
        """ContextVar is reset to its prior value after the request."""
        from httpx import ASGITransport, AsyncClient
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        prior_id = "prior-id"
        mcp_request_id_var.set(prior_id)

        async def handler(req: Request) -> JSONResponse:
            return JSONResponse({})

        app = Starlette(routes=[Route("/other", handler)])
        app.add_middleware(McpSessionMiddleware)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/other")

        assert mcp_request_id_var.get() == prior_id


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_returns_healthy_json(self) -> None:
        mock_request = MagicMock(spec=Request)

        with patch("ypl.mcp_server.server.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            response = await health_check(mock_request)

        assert response.status_code == 200
        import json

        body = json.loads(response.body)
        assert body["status"] == "healthy"
        assert body["service"] == "mcp-server"
        assert body["mode"] == "DEV_TOKEN"
        assert "timestamp" in body

    async def test_response_includes_current_mode(self) -> None:
        mock_request = MagicMock(spec=Request)

        with patch("ypl.mcp_server.server.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            response = await health_check(mock_request)

        import json

        body = json.loads(response.body)
        assert body["mode"] == "OAUTH"


# ---------------------------------------------------------------------------
# invoke_tool
# ---------------------------------------------------------------------------


class TestInvokeTool:
    def _make_request(
        self,
        *,
        tool_name: str = "test_tool",
        has_token: bool = True,
        body: object = None,
        json_error: bool = False,
    ) -> MagicMock:
        req = MagicMock(spec=Request)
        req.path_params = {"tool_name": tool_name}

        if has_token:
            req.state.mcp_token = MagicMock()
            req.state.engineer_email = "dev@yupp.ai"
        else:
            req.state.mcp_token = None
            req.state.engineer_email = None

        if json_error:
            req.json = AsyncMock(side_effect=ValueError("bad json"))
        else:
            req.json = AsyncMock(return_value=body if body is not None else {"arguments": {}})

        return req

    async def test_no_auth_returns_401(self) -> None:
        req = self._make_request(has_token=False)
        response = await invoke_tool(req)
        assert response.status_code == 401

    async def test_invalid_json_returns_400(self) -> None:
        import json

        req = self._make_request()
        req.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
        response = await invoke_tool(req)
        assert response.status_code == 400

    async def test_body_not_dict_returns_400(self) -> None:
        req = self._make_request(body=["not", "a", "dict"])
        response = await invoke_tool(req)
        assert response.status_code == 400
        import json as _json

        body = _json.loads(response.body)
        assert "JSON object" in body["error"]

    async def test_arguments_not_dict_returns_400(self) -> None:
        req = self._make_request(body={"arguments": "not-a-dict"})
        response = await invoke_tool(req)
        assert response.status_code == 400
        import json as _json

        body = _json.loads(response.body)
        assert "arguments" in body["error"]

    async def test_unknown_tool_returns_404(self) -> None:
        from fastmcp.exceptions import NotFoundError

        req = self._make_request()

        with patch("ypl.mcp_server.server.execute_tool", new=AsyncMock(side_effect=NotFoundError("no such tool"))):
            response = await invoke_tool(req)

        assert response.status_code == 404
        import json as _json

        body = _json.loads(response.body)
        assert "Unknown tool" in body["error"]

    async def test_validation_error_returns_422(self) -> None:
        from pydantic import ValidationError, create_model

        # Build a real ValidationError
        Model = create_model("M", x=(int, ...))
        try:
            Model(x="not-int")
        except ValidationError as exc:
            validation_err = exc

        req = self._make_request()

        with patch("ypl.mcp_server.server.execute_tool", new=AsyncMock(side_effect=validation_err)):
            response = await invoke_tool(req)

        assert response.status_code == 422

    async def test_generic_exception_returns_500(self) -> None:
        req = self._make_request()

        with patch("ypl.mcp_server.server.execute_tool", new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = await invoke_tool(req)

        assert response.status_code == 500
        import json as _json

        body = _json.loads(response.body)
        assert "boom" in body["error"]

    async def test_successful_invocation_returns_200(self) -> None:
        mock_result = MagicMock()
        req = self._make_request(body={"arguments": {"q": "hello"}})

        with (
            patch("ypl.mcp_server.server.execute_tool", new=AsyncMock(return_value=mock_result)),
            patch("ypl.mcp_server.server.format_tool_result", return_value='{"answer": "ok"}'),
        ):
            response = await invoke_tool(req)

        assert response.status_code == 200
        import json as _json

        body = _json.loads(response.body)
        assert body["success"] is True
        assert "execution_time_ms" in body
        assert body["result"] == '{"answer": "ok"}'


# ---------------------------------------------------------------------------
# _get_middleware
# ---------------------------------------------------------------------------


class TestGetMiddleware:
    def test_dev_token_mode_includes_dev_token_middleware(self) -> None:
        from ypl.mcp_server.server import _get_middleware

        with patch("ypl.mcp_server.server.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            middlewares = _get_middleware()

        class_names = [getattr(m.cls, "__name__", "") for m in middlewares]
        assert "McpSessionMiddleware" in class_names
        assert "DevTokenAuthMiddleware" in class_names

    def test_oauth_mode_has_only_session_middleware(self) -> None:
        from ypl.mcp_server.server import _get_middleware

        with patch("ypl.mcp_server.server.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "OAUTH"
            middlewares = _get_middleware()

        class_names = [getattr(m.cls, "__name__", "") for m in middlewares]
        assert "McpSessionMiddleware" in class_names
        assert "DevTokenAuthMiddleware" not in class_names

    def test_session_middleware_is_first(self) -> None:
        """McpSessionMiddleware must be outermost (first in list)."""
        from ypl.mcp_server.server import _get_middleware

        with patch("ypl.mcp_server.server.settings") as mock_settings:
            mock_settings.MCP_SERVER_MODE = "DEV_TOKEN"
            middlewares = _get_middleware()

        assert getattr(middlewares[0].cls, "__name__", "") == "McpSessionMiddleware"


# ---------------------------------------------------------------------------
# Module import smoke tests
# ---------------------------------------------------------------------------


class TestServerModuleImports:
    def test_app_is_importable(self) -> None:
        from ypl.mcp_server.server import app

        assert app is not None

    def test_health_check_is_callable(self) -> None:
        from ypl.mcp_server.server import health_check

        assert callable(health_check)

    def test_list_tools_is_callable(self) -> None:
        from ypl.mcp_server.server import list_tools

        assert callable(list_tools)

    def test_invoke_tool_is_callable(self) -> None:
        from ypl.mcp_server.server import invoke_tool

        assert callable(invoke_tool)

    def test_lifespan_is_callable(self) -> None:
        from ypl.mcp_server.server import lifespan

        assert callable(lifespan)
