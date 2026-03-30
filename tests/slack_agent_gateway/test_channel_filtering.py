"""Tests for Slack Agent Gateway channel filtering (allowlist/denylist)."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from ypl.backend.utils.dynamic_app_settings import SlackAgentGatewaySettings
from ypl.slack_agent_gateway.events import (
    _check_channel_allowed,
    _matches_any_pattern,
)


@pytest.fixture(autouse=True)
def clear_cache() -> Generator[None, None, None]:
    """Clear the _check_channel_allowed cache before each test."""
    _check_channel_allowed.cache_clear()
    yield
    _check_channel_allowed.cache_clear()


class TestMatchesAnyPattern:
    """Tests for _matches_any_pattern function."""

    def test_exact_match(self) -> None:
        """Exact channel name should match (fullmatch, not prefix match)."""
        assert _matches_any_pattern("general", ["general"]) is True
        assert _matches_any_pattern("random", ["general"]) is False
        # fullmatch ensures "general" doesn't match "general-discussion"
        assert _matches_any_pattern("general-discussion", ["general"]) is False

    def test_regex_prefix_match(self) -> None:
        """Regex prefix patterns should work."""
        assert _matches_any_pattern("alert-backend", ["^alert-.*"]) is True
        assert _matches_any_pattern("alert-frontend", ["^alert-.*"]) is True
        assert _matches_any_pattern("backend-alert", ["^alert-.*"]) is False

    def test_regex_suffix_match(self) -> None:
        """Regex suffix patterns should work."""
        assert _matches_any_pattern("team-engineering", [".*-engineering$"]) is True
        assert _matches_any_pattern("engineering-team", [".*-engineering$"]) is False

    def test_multiple_patterns(self) -> None:
        """Should match if any pattern matches."""
        patterns = ["general", "^alert-.*", "random"]
        assert _matches_any_pattern("general", patterns) is True
        assert _matches_any_pattern("alert-oncall", patterns) is True
        assert _matches_any_pattern("random", patterns) is True
        assert _matches_any_pattern("other-channel", patterns) is False

    def test_empty_patterns(self) -> None:
        """Empty pattern list should not match anything."""
        assert _matches_any_pattern("general", []) is False

    def test_invalid_regex_is_skipped(self) -> None:
        """Invalid regex patterns should be skipped without raising."""
        # "[" is invalid regex - unclosed bracket
        assert _matches_any_pattern("general", ["[", "general"]) is True
        assert _matches_any_pattern("other", ["["]) is False

    def test_case_sensitive(self) -> None:
        """Pattern matching should be case-sensitive."""
        assert _matches_any_pattern("General", ["general"]) is False
        assert _matches_any_pattern("general", ["General"]) is False
        assert _matches_any_pattern("ALERT-backend", ["^alert-.*"]) is False


class TestCheckChannelAllowed:
    """Tests for _check_channel_allowed function."""

    @pytest.fixture
    def mock_get_channel_name(self) -> Generator[AsyncMock, None, None]:
        """Mock get_channel_name_by_id to return predictable channel names."""
        with patch("ypl.slack_agent_gateway.events.get_channel_name_by_id") as mock:
            # By default, return channel name based on a simple mapping
            async def get_name(bot_token: str, channel_id: str) -> str | None:
                # Simple convention: C_general -> general
                if channel_id.startswith("C_"):
                    return channel_id[2:]
                return None

            mock.side_effect = get_name
            yield mock

    @pytest.fixture
    def mock_settings(self) -> Generator[AsyncMock, None, None]:
        """Mock get_slack_agent_gateway_settings."""
        with patch("ypl.slack_agent_gateway.events.get_slack_agent_gateway_settings") as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_empty_allowlist_denies_all(self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock) -> None:
        """With empty allowlist, all channels are denied by default."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=[],
            channel_allowlist=[],
        )

        is_allowed, allowlist, channel_name = await _check_channel_allowed("bot_token", "C_general")

        assert is_allowed is False
        assert allowlist == []
        assert channel_name == "general"

    @pytest.mark.asyncio
    async def test_wildcard_allowlist_allows_all(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """With '.*' in allowlist, all channels are allowed."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=[],
            channel_allowlist=[".*"],
        )

        is_allowed, _, channel_name = await _check_channel_allowed("bot_token", "C_general")
        assert is_allowed is True
        assert channel_name == "general"

        is_allowed, _, _ = await _check_channel_allowed("bot_token", "C_random")
        assert is_allowed is True

    @pytest.mark.asyncio
    async def test_denylist_blocks_matching_channel(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """Channels matching denylist patterns should be blocked."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=["random", "^test-.*"],
            channel_allowlist=[".*"],  # Allow all except denylist
        )

        # Exact match
        is_allowed, _, channel_name = await _check_channel_allowed("bot_token", "C_random")
        assert is_allowed is False
        assert channel_name == "random"

        # Regex match
        is_allowed, _, channel_name = await _check_channel_allowed("bot_token", "C_test-playground")
        assert is_allowed is False
        assert channel_name == "test-playground"

    @pytest.mark.asyncio
    async def test_denylist_allows_non_matching_channel(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """Channels not matching denylist should be allowed (with wildcard allowlist)."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=["random"],
            channel_allowlist=[".*"],  # Allow all except denylist
        )

        is_allowed, _, channel_name = await _check_channel_allowed("bot_token", "C_general")
        assert is_allowed is True
        assert channel_name == "general"

    @pytest.mark.asyncio
    async def test_allowlist_only_allows_matching_channels(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """Only channels matching allowlist should be allowed."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=[],
            channel_allowlist=["^alert-.*", "general"],
        )

        # Matching channels
        is_allowed, _, _ = await _check_channel_allowed("bot_token", "C_general")
        assert is_allowed is True

        is_allowed, _, _ = await _check_channel_allowed("bot_token", "C_alert-backend")
        assert is_allowed is True

        # Non-matching channel
        is_allowed, allowlist, _ = await _check_channel_allowed("bot_token", "C_random")
        assert is_allowed is False
        assert allowlist == ["^alert-.*", "general"]

    @pytest.mark.asyncio
    async def test_denylist_checked_before_allowlist(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """Denylist should be checked first - denied even if in allowlist."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=["^test-.*"],
            channel_allowlist=["^test-.*", "general"],  # test-* is in both!
        )

        # test-playground matches denylist, should be blocked even though it's in allowlist
        is_allowed, _, channel_name = await _check_channel_allowed("bot_token", "C_test-playground")
        assert is_allowed is False
        assert channel_name == "test-playground"

        # general is only in allowlist, should be allowed
        is_allowed, _, _ = await _check_channel_allowed("bot_token", "C_general")
        assert is_allowed is True

    @pytest.mark.asyncio
    async def test_channel_name_lookup_failure_denies_by_default(self, mock_settings: AsyncMock) -> None:
        """If channel name lookup fails, deny by default for safety."""
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=["random"],
            channel_allowlist=["general"],
        )

        with patch("ypl.slack_agent_gateway.events.get_channel_name_by_id") as mock_get_name:
            mock_get_name.return_value = None  # Lookup failed

            is_allowed, allowlist, channel_name = await _check_channel_allowed("bot_token", "C_unknown")

            assert is_allowed is False  # Deny by default for safety
            assert allowlist == ["general"]
            assert channel_name is None

    @pytest.mark.asyncio
    async def test_returns_allowlist_patterns_for_denial_message(
        self, mock_get_channel_name: AsyncMock, mock_settings: AsyncMock
    ) -> None:
        """Should return allowlist patterns for constructing denial message."""
        allowlist_patterns = ["^alert-.*", "^oncall-.*", "general"]
        mock_settings.return_value = SlackAgentGatewaySettings(
            channel_denylist=[],
            channel_allowlist=allowlist_patterns,
        )

        is_allowed, returned_allowlist, _ = await _check_channel_allowed("bot_token", "C_random")

        assert is_allowed is False
        assert returned_allowlist == allowlist_patterns
