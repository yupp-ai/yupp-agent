"""E2E auth boundary tests — verify API key enforcement."""

import httpx
import pytest

pytestmark = pytest.mark.e2e


class TestAuth:
    async def test_valid_api_key_accepted(self, client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
        resp = await client.get("/ahs/agents", headers=auth_headers)
        assert resp.status_code == 200

    async def test_missing_api_key_rejected(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/ahs/agents")
        assert resp.status_code in (401, 403)

    async def test_wrong_api_key_rejected(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/ahs/agents", headers={"X-API-Key": "wrong-key-12345"})
        assert resp.status_code in (401, 403)
