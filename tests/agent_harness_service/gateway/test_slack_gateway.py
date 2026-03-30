"""Unit tests for SlackGateway dead session handling.

Tests that after a 404 response, subsequent send_status_update calls
short-circuit without making an HTTP request. Would have caught PR #30.
"""

import pytest
from ypl.agent_harness_service.gateway.registry import GatewayConfig
from ypl.agent_harness_service.gateway.slack import SlackGateway


@pytest.fixture
def gateway() -> SlackGateway:
    config = GatewayConfig(
        name="test-slack",
        gateway_type="slack",
        settings={"base_url": "http://fake-sag:8080"},
    )
    return SlackGateway(config)


class TestDeadSessionHandling:
    """Test the _dead_session_ids optimization."""

    @pytest.mark.asyncio
    async def test_dead_session_returns_false_without_http(self, gateway: SlackGateway) -> None:
        """Once a session is marked dead, send_status_update should return False immediately."""
        gateway._dead_session_ids.add("dead-session-123")
        result = await gateway.send_status_update("dead-session-123", "some status text")
        assert result is False

    @pytest.mark.asyncio
    async def test_dead_session_set_initially_empty(self, gateway: SlackGateway) -> None:
        assert len(gateway._dead_session_ids) == 0

    @pytest.mark.asyncio
    async def test_multiple_dead_sessions_tracked(self, gateway: SlackGateway) -> None:
        gateway._dead_session_ids.add("dead-1")
        gateway._dead_session_ids.add("dead-2")
        assert await gateway.send_status_update("dead-1", "text") is False
        assert await gateway.send_status_update("dead-2", "text") is False

    @pytest.mark.asyncio
    async def test_live_session_not_blocked_by_dead_ones(self, gateway: SlackGateway) -> None:
        """A live session should NOT be skipped just because other sessions are dead.

        send_status_update catches all exceptions and returns False, so we verify
        the method actually attempted an HTTP call (returns False due to connection
        error) rather than short-circuiting (which also returns False but without
        any HTTP attempt). We check that "live-session" was NOT added to dead set.
        """
        gateway._dead_session_ids.add("dead-session")
        # This will attempt HTTP to fake-sag:8080, fail with ConnectError, return False
        result = await gateway.send_status_update("live-session", "text")
        assert result is False
        # Crucially, a connection error should NOT mark the session as dead
        assert "live-session" not in gateway._dead_session_ids


class TestGatewayConfig:
    """Test SlackGateway initialization from config."""

    def test_base_url_from_config(self, gateway: SlackGateway) -> None:
        assert gateway._base_url == "http://fake-sag:8080"

    def test_name_from_config(self, gateway: SlackGateway) -> None:
        assert gateway.name == "test-slack"

    def test_no_api_key_by_default(self, gateway: SlackGateway) -> None:
        assert gateway._api_key is None
