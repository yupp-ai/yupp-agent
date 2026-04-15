"""E2E tests for AHS agent listing and detail endpoints."""

from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.e2e]


class TestAgentList:
    """Test agent listing endpoint."""

    async def test_list_agents(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """List all agents — should return a non-empty list."""
        resp = await client.get("/ahs/agents", params={"include_all": "true"}, headers=auth_headers)
        assert resp.status_code == 200
        agents = resp.json().get("agents", [])
        assert len(agents) >= 1

    async def test_parrot_bubba_in_list(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """The parrot-bubba mock agent should be in the agent list."""
        resp = await client.get("/ahs/agents", params={"include_all": "true"}, headers=auth_headers)
        assert resp.status_code == 200
        agents = resp.json().get("agents", [])
        names = [a["name"] for a in agents]
        assert "parrot-bubba" in names


class TestAgentDetail:
    """Test agent detail endpoint."""

    async def test_get_agent_detail(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Get parrot-bubba agent detail."""
        resp = await client.get("/ahs/agent/parrot-bubba", headers=auth_headers)
        assert resp.status_code == 200
        agent = resp.json().get("agent", resp.json())
        assert agent["name"] == "parrot-bubba"

    async def test_get_nonexistent_agent(
        self,
        client: httpx.AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Requesting a nonexistent agent returns 404."""
        resp = await client.get("/ahs/agent/nonexistent-agent-xyz-999", headers=auth_headers)
        assert resp.status_code == 404
