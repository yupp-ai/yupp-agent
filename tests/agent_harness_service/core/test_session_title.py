"""Tests for ypl.agent_harness_service.core.session_title.

Covers pure-logic aspects only (no DB or LLM API calls):
- TITLE_GENERATE_TURNS constant
- _TRIGGER_PREFIX mapping
- maybe_generate_session_title: skips on wrong turns, skips when no API key
- _generate_title_text: skips when CEREBRAS_API_KEY is absent
"""

from __future__ import annotations
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.core.session_title import (
    _TRIGGER_PREFIX,
    TITLE_GENERATE_TURNS,
    _generate_title_text,
    maybe_generate_session_title,
)

# ===========================================================================
# TITLE_GENERATE_TURNS
# ===========================================================================


class TestTitleGenerateTurns:
    def test_is_a_set(self) -> None:
        assert isinstance(TITLE_GENERATE_TURNS, set)

    def test_contains_turn_1(self) -> None:
        assert 1 in TITLE_GENERATE_TURNS

    def test_contains_turn_4(self) -> None:
        assert 4 in TITLE_GENERATE_TURNS

    def test_does_not_contain_turn_2(self) -> None:
        assert 2 not in TITLE_GENERATE_TURNS

    def test_does_not_contain_turn_3(self) -> None:
        assert 3 not in TITLE_GENERATE_TURNS

    def test_does_not_contain_turn_5(self) -> None:
        assert 5 not in TITLE_GENERATE_TURNS


# ===========================================================================
# _TRIGGER_PREFIX
# ===========================================================================


class TestTriggerPrefix:
    def test_cron_trigger_has_prefix(self) -> None:
        from ypl.db.agent_harness import AgentSessionTrigger

        prefix = _TRIGGER_PREFIX.get(AgentSessionTrigger.CRON.value)
        assert prefix is not None
        assert "[CRON]" in prefix

    def test_task_trigger_has_prefix(self) -> None:
        from ypl.db.agent_harness import AgentSessionTrigger

        prefix = _TRIGGER_PREFIX.get(AgentSessionTrigger.TASK.value)
        assert prefix is not None
        assert "[TASK]" in prefix

    def test_cron_prefix_ends_with_space(self) -> None:
        from ypl.db.agent_harness import AgentSessionTrigger

        prefix = _TRIGGER_PREFIX.get(AgentSessionTrigger.CRON.value, "")
        assert prefix.endswith(" ")

    def test_all_prefix_values_are_strings(self) -> None:
        for key, val in _TRIGGER_PREFIX.items():
            assert isinstance(key, str)
            assert isinstance(val, str)


# ===========================================================================
# _generate_title_text (no API key)
# ===========================================================================


def _make_mock_response(text: str | None, *, no_choices: bool = False) -> MagicMock:
    """Build a mock OpenAI ChatCompletion response with the given message content."""
    mock_response = MagicMock()
    if no_choices:
        mock_response.choices = []
    else:
        choice = MagicMock()
        choice.message.content = text
        mock_response.choices = [choice]
    return mock_response


class TestGenerateTitleText:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        result = await _generate_title_text(["Fix the login bug"])
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty messages list should not crash; no title generated."""
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        result = await _generate_title_text([])
        assert result is None

    @pytest.mark.asyncio
    async def test_calls_llm_api_when_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When API key is set, the OpenAI-compatible client is called."""
        monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")

        mock_response = _make_mock_response("Fix Login Bug")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch(
            "ypl.agent_harness_service.core.session_title.openai.AsyncOpenAI",
            return_value=mock_client,
        ):
            result = await _generate_title_text(["Fix the login bug in auth service"])

        assert result == "Fix Login Bug"

    @pytest.mark.asyncio
    async def test_returns_none_for_no_title_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When LLM returns [NO TITLE], result should be None."""
        monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")

        mock_response = _make_mock_response("[NO TITLE]")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch(
            "ypl.agent_harness_service.core.session_title.openai.AsyncOpenAI",
            return_value=mock_client,
        ):
            result = await _generate_title_text([""])

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When LLM returns no choices, result should be None."""
        monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")

        mock_response = _make_mock_response(None, no_choices=True)

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch(
            "ypl.agent_harness_service.core.session_title.openai.AsyncOpenAI",
            return_value=mock_client,
        ):
            result = await _generate_title_text(["Hello"])

        assert result is None

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CEREBRAS_API_KEY", "test-key-123")

        mock_response = _make_mock_response("  Fix Auth Bug  ")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch(
            "ypl.agent_harness_service.core.session_title.openai.AsyncOpenAI",
            return_value=mock_client,
        ):
            result = await _generate_title_text(["Fix the auth bug"])

        assert result == "Fix Auth Bug"


# ===========================================================================
# maybe_generate_session_title (pure logic paths)
# ===========================================================================


class TestMaybeGenerateSessionTitle:
    @pytest.mark.asyncio
    async def test_skips_on_non_generate_turn(self) -> None:
        """Turns not in TITLE_GENERATE_TURNS should return immediately without any DB call."""
        session_id = uuid.uuid4()
        # Turn 2 is not in TITLE_GENERATE_TURNS
        with patch("ypl.agent_harness_service.core.session_title.get_async_session") as mock_session:
            await maybe_generate_session_title(session_id, turn_number=2)
            mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_on_turn_3(self) -> None:
        session_id = uuid.uuid4()
        with patch("ypl.agent_harness_service.core.session_title.get_async_session") as mock_session:
            await maybe_generate_session_title(session_id, turn_number=3)
            mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_raise_on_db_exception(self) -> None:
        """DB errors are caught and logged, never propagated."""
        session_id = uuid.uuid4()
        with patch(
            "ypl.agent_harness_service.core.session_title.get_async_session",
            side_effect=Exception("DB connection failed"),
        ):
            # Should not raise
            await maybe_generate_session_title(session_id, turn_number=1)

    @pytest.mark.asyncio
    async def test_trigger_prefix_applied_to_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When trigger is CRON, the [CRON] prefix is prepended to the title."""
        from ypl.db.agent_harness import AgentSessionTrigger

        session_id = uuid.uuid4()

        mock_db_session = AsyncMock()
        mock_db_session.exec = AsyncMock(return_value=AsyncMock(all=MagicMock(return_value=["Fix bug"])))

        mock_agent_session = MagicMock()

        async def fake_get_async_session() -> Any:
            return mock_db_session

        class FakeCtxMgr:
            async def __aenter__(self) -> MagicMock:
                mock_db_session.get = AsyncMock(return_value=mock_agent_session)
                return mock_db_session

            async def __aexit__(self, *args: object) -> None:
                pass

        call_count = 0

        def fake_session_factory() -> FakeCtxMgr:
            nonlocal call_count
            call_count += 1
            return FakeCtxMgr()

        with (
            patch("ypl.agent_harness_service.core.session_title.get_async_session", fake_session_factory),
            patch(
                "ypl.agent_harness_service.core.session_title._generate_title_text",
                new=AsyncMock(return_value="Fix Auth Bug"),
            ),
        ):
            await maybe_generate_session_title(session_id, turn_number=1, trigger=AgentSessionTrigger.CRON)

        # The title stored on the mock session object should have the CRON prefix
        assert isinstance(mock_agent_session.title, str)
        assert mock_agent_session.title.startswith("[CRON]")

    @pytest.mark.asyncio
    async def test_no_title_generated_when_no_user_messages(self) -> None:
        """If there are no user messages, no title is generated."""
        session_id = uuid.uuid4()

        class FakeCtxMgr:
            async def __aenter__(self) -> MagicMock:
                mock = MagicMock()
                mock.exec = AsyncMock(return_value=AsyncMock(all=MagicMock(return_value=[])))
                return mock

            async def __aexit__(self, *args: object) -> None:
                pass

        with (
            patch("ypl.agent_harness_service.core.session_title.get_async_session", FakeCtxMgr),
            patch(
                "ypl.agent_harness_service.core.session_title._generate_title_text",
                new=AsyncMock(return_value="Should not be called"),
            ) as mock_gen,
        ):
            await maybe_generate_session_title(session_id, turn_number=1)
            mock_gen.assert_not_called()
