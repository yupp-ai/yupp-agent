"""Tests for HostPathGuardMiddleware.

Verifies that requests arriving on a "scoped" Host header (e.g. mcp.example.com)
are restricted to the configured path allowlist, while unscoped hosts
(e.g. direct IP access, other subdomains) pass through untouched.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from ypl.agent_harness_service.host_path_guard import HostPathGuardMiddleware


def _make_app(host_map: dict[str, list[str]]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(HostPathGuardMiddleware, host_allowlist=host_map)

    @app.get("/mcp/ping")
    async def mcp_ping() -> dict[str, str]:
        return {"ok": "mcp"}

    @app.get("/ahs/ping")
    async def ahs_ping() -> dict[str, str]:
        return {"ok": "ahs"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"ok": "health"}

    return app


def test_scoped_host_allows_listed_prefix() -> None:
    app = _make_app({"mcp.example.com": ["/mcp", "/health"]})
    client = TestClient(app)

    resp = client.get("/mcp/ping", headers={"Host": "mcp.example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": "mcp"}


def test_scoped_host_allows_health() -> None:
    app = _make_app({"mcp.example.com": ["/mcp", "/health"]})
    client = TestClient(app)

    resp = client.get("/health", headers={"Host": "mcp.example.com"})
    assert resp.status_code == 200


def test_scoped_host_blocks_other_prefix() -> None:
    app = _make_app({"mcp.example.com": ["/mcp", "/health"]})
    client = TestClient(app)

    resp = client.get("/ahs/ping", headers={"Host": "mcp.example.com"})
    assert resp.status_code == 404


def test_unscoped_host_passes_through() -> None:
    app = _make_app({"mcp.example.com": ["/mcp"]})
    client = TestClient(app)

    # Any host not in the map sees all routes.
    for host in ("localhost", "agent.example.com", "127.0.0.1:8090"):
        resp = client.get("/ahs/ping", headers={"Host": host})
        assert resp.status_code == 200, f"host {host!r} unexpectedly blocked"


def test_host_port_stripped_before_match() -> None:
    """Host headers often include a port (e.g. Host: mcp.example.com:443)."""
    app = _make_app({"mcp.example.com": ["/mcp"]})
    client = TestClient(app)

    resp = client.get("/mcp/ping", headers={"Host": "mcp.example.com:443"})
    assert resp.status_code == 200

    resp = client.get("/ahs/ping", headers={"Host": "mcp.example.com:443"})
    assert resp.status_code == 404


def test_empty_allowlist_for_host_blocks_everything() -> None:
    app = _make_app({"mcp.example.com": []})
    client = TestClient(app)

    resp = client.get("/mcp/ping", headers={"Host": "mcp.example.com"})
    assert resp.status_code == 404


def test_no_host_map_is_noop() -> None:
    app = _make_app({})
    client = TestClient(app)

    resp = client.get("/ahs/ping", headers={"Host": "mcp.example.com"})
    assert resp.status_code == 200


def test_prefix_not_substring() -> None:
    """`/mcp` must match `/mcp` and `/mcp/...` but NOT `/mcpother`."""
    app = FastAPI()
    app.add_middleware(
        HostPathGuardMiddleware,
        host_allowlist={"mcp.example.com": ["/mcp"]},
    )

    @app.get("/mcp")
    async def mcp_root() -> dict[str, str]:
        return {"ok": "root"}

    @app.get("/mcpother")
    async def mcp_other() -> dict[str, str]:
        return {"ok": "other"}

    client = TestClient(app)
    assert client.get("/mcp", headers={"Host": "mcp.example.com"}).status_code == 200
    assert client.get("/mcpother", headers={"Host": "mcp.example.com"}).status_code == 404
