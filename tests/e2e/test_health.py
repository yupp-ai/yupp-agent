"""E2E smoke tests — verify all endpoints are reachable."""

import httpx
import pytest


pytestmark = pytest.mark.e2e


class TestHealth:
    async def test_health_endpoint(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_openapi_docs(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/docs")
        assert resp.status_code == 200

    async def test_openapi_json(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "paths" in data
        assert "/ahs/session/create" in data["paths"]
