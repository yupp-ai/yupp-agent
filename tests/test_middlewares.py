"""Unit tests for ypl/middlewares/.

Builds a minimal Starlette app with the middleware applied and inspects the
context-variable values returned in the response body.
"""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from ypl.middlewares.trace_context_middleware import (
    TraceContextMiddleware,
    gcp_cloud_trace_context,
)


def _make_trace_app() -> Starlette:
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({"trace": gcp_cloud_trace_context.get()})

    app = Starlette(routes=[Route("/", handler)])
    app.add_middleware(TraceContextMiddleware)
    return app


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
        response = client.get("/", headers={"x-cloud-trace-context": ""})
        assert response.status_code == 200
        # Header present → context var is set (even to "")
        assert response.json()["trace"] == ""

    def test_different_trace_values_independent(self) -> None:
        """Two sequential requests each set the correct trace context from their respective headers."""
        client = TestClient(_make_trace_app(), raise_server_exceptions=True)
        r1 = client.get("/", headers={"x-cloud-trace-context": "trace1"})
        r2 = client.get("/", headers={"x-cloud-trace-context": "trace2"})
        assert r1.json()["trace"] == "trace1"
        assert r2.json()["trace"] == "trace2"
