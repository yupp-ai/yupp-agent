"""Unit tests for ypl/middlewares/.

Covers TraceContextMiddleware and YuppContextMiddleware by building
minimal Starlette apps with the middleware applied and inspecting the
context-variable values returned in the response body.
"""

import base64
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from ypl.middlewares.trace_context_middleware import (
    TraceContextMiddleware,
    gcp_cloud_trace_context,
)
from ypl.middlewares.yupp_context_middleware import (
    YuppContextMiddleware,
    is_yuppster_request_for_metrics_only_contextvar,
    risk_session_key_contextvar,
    user_agent_contextvar,
    yupp_context,
)

# ---------------------------------------------------------------------------
# Helpers — tiny apps that expose context-var values via JSON response
# ---------------------------------------------------------------------------


def _make_trace_app() -> Starlette:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({"trace": gcp_cloud_trace_context.get()})

    app = Starlette(routes=[Route("/", handler)])
    app.add_middleware(TraceContextMiddleware)
    return app


def _make_yupp_app() -> Starlette:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "yupp_context": yupp_context.get(),
                "risk_session_key": risk_session_key_contextvar.get(),
                "user_agent": user_agent_contextvar.get(),
                "is_yuppster": is_yuppster_request_for_metrics_only_contextvar.get(),
            }
        )

    app = Starlette(routes=[Route("/", handler)])
    app.add_middleware(YuppContextMiddleware)
    return app


def _encode_context(data: dict) -> str:
    """Encode a dict to base64 JSON for the X-YUPP-CONTEXT header."""
    return base64.b64encode(json.dumps(data).encode()).decode()


# ---------------------------------------------------------------------------
# TraceContextMiddleware
# ---------------------------------------------------------------------------


class TestTraceContextMiddleware:
    def test_sets_trace_context_when_header_present(self) -> None:
        client = TestClient(_make_trace_app(), raise_server_exceptions=True)
        trace_value = "abc123/1;o=1"
        response = client.get("/", headers={"x-cloud-trace-context": trace_value})
        assert response.status_code == 200
        assert response.json()["trace"] == trace_value

    def test_trace_context_empty_when_header_absent(self) -> None:
        client = TestClient(_make_trace_app(), raise_server_exceptions=True)
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["trace"] == ""

    def test_empty_trace_header_value(self) -> None:
        client = TestClient(_make_trace_app(), raise_server_exceptions=True)
        # An empty string header is present but blank
        response = client.get("/", headers={"x-cloud-trace-context": ""})
        assert response.status_code == 200
        # Header is present → context var is set (even to "")
        assert response.json()["trace"] == ""

    def test_different_trace_values_independent(self) -> None:
        """Ensure two sequential requests each set the correct trace context from their respective headers."""
        client = TestClient(_make_trace_app(), raise_server_exceptions=True)
        r1 = client.get("/", headers={"x-cloud-trace-context": "trace1"})
        r2 = client.get("/", headers={"x-cloud-trace-context": "trace2"})
        assert r1.json()["trace"] == "trace1"
        assert r2.json()["trace"] == "trace2"


# ---------------------------------------------------------------------------
# YuppContextMiddleware
# ---------------------------------------------------------------------------


class TestYuppContextMiddlewareNoHeader:
    def test_no_header_defaults_preserved(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["yupp_context"] == ""
        assert data["risk_session_key"] == ""
        assert data["user_agent"] is None
        assert data["is_yuppster"] is False

    def test_context_cleared_between_requests(self) -> None:
        """A header-present request followed by a no-header request should reset context vars."""
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        # First request sets context values
        ctx = _encode_context({"sardineSessionKey": "should-not-persist", "userAgent": "Agent/1"})
        client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        # Second request has NO header — values should be reset, not stale
        response = client.get("/")
        data = response.json()
        assert data["risk_session_key"] == ""
        assert data["user_agent"] is None


class TestYuppContextMiddlewareWithHeader:
    def test_sardine_session_key_extracted(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"sardineSessionKey": "key-xyz"})
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.json()["risk_session_key"] == "key-xyz"

    def test_yupp_context_raw_value_stored(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"sardineSessionKey": "abc"})
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.json()["yupp_context"] == ctx

    def test_is_yuppster_flag_set(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"isYuppsterRequest": True})
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.json()["is_yuppster"] is True

    def test_is_yuppster_false_by_default_in_context(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"sardineSessionKey": "key"})  # no isYuppsterRequest
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.json()["is_yuppster"] is False

    def test_user_agent_extracted(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"userAgent": "Mozilla/5.0"})
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.json()["user_agent"] == "Mozilla/5.0"

    def test_full_context_all_fields(self) -> None:
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context(
            {
                "sardineSessionKey": "sess-123",
                "isYuppsterRequest": True,
                "userAgent": "TestAgent/1.0",
            }
        )
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        data = response.json()
        assert data["risk_session_key"] == "sess-123"
        assert data["is_yuppster"] is True
        assert data["user_agent"] == "TestAgent/1.0"

    def test_missing_sardine_key_no_error(self) -> None:
        """Context without sardineSessionKey should still work."""
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        ctx = _encode_context({"isYuppsterRequest": True})
        response = client.get("/", headers={"X-YUPP-CONTEXT": ctx})
        assert response.status_code == 200
        assert response.json()["risk_session_key"] == ""

    def test_invalid_base64_returns_200(self) -> None:
        """Malformed header should not crash the middleware — it logs and continues."""
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        response = client.get("/", headers={"X-YUPP-CONTEXT": "!!not-valid-base64!!"})
        assert response.status_code == 200

    def test_invalid_json_after_decode_returns_200(self) -> None:
        """Valid base64 but invalid JSON inside should log and continue."""
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        bad_json = base64.b64encode(b"not json at all").decode()
        response = client.get("/", headers={"X-YUPP-CONTEXT": bad_json})
        assert response.status_code == 200

    def test_call_next_always_invoked(self) -> None:
        """Middleware must always call call_next — even on errors."""
        client = TestClient(_make_yupp_app(), raise_server_exceptions=True)
        # Any request should get a response body from the inner handler
        response = client.get("/", headers={"X-YUPP-CONTEXT": "garbage"})
        assert "yupp_context" in response.json()
