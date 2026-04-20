"""Integration test: verifies HOST_PATH_GUARD wiring in create_app().

Uses env-var monkeypatching to confirm the middleware is registered and reads
MonoConfig. Does not re-test the middleware's own dispatch logic (covered in
``test_host_path_guard.py``).
"""

from __future__ import annotations
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mono_app_with_guard(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "HOST_PATH_GUARD",
        json.dumps({"mcp.agcouch.com": ["/mcp", "/health"]}),
    )
    # Import lazily so the env var is read at config construction time.
    from ypl.mono_server.server import create_app

    app = create_app()
    return TestClient(app)


def test_mcp_host_allows_health(mono_app_with_guard: TestClient) -> None:
    resp = mono_app_with_guard.get("/health", headers={"Host": "mcp.agcouch.com"})
    assert resp.status_code == 200


def test_mcp_host_blocks_non_mcp_path(mono_app_with_guard: TestClient) -> None:
    # Any path outside the allowlist must 404 regardless of whether the route exists.
    resp = mono_app_with_guard.get("/ahs/sessions", headers={"Host": "mcp.agcouch.com"})
    assert resp.status_code == 404


def test_unscoped_host_unrestricted(mono_app_with_guard: TestClient) -> None:
    # A host not in the guard map sees every route; /health always returns 200.
    resp = mono_app_with_guard.get("/health", headers={"Host": "localhost"})
    assert resp.status_code == 200
