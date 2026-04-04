"""Tests for self_management.py — read_self_system_prompt and update_self_system_prompt."""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.agent_harness_service.tools.self_management import (
    read_self_system_prompt_tool as _read_self_system_prompt_tool,
)
from ypl.agent_harness_service.tools.self_management import (
    update_self_system_prompt_tool as _update_self_system_prompt_tool,
)

# Unwrap FunctionTool to get raw callables
read_self_system_prompt_tool = _read_self_system_prompt_tool.fn
update_self_system_prompt_tool = _update_self_system_prompt_tool.fn

AGENT_NAME = "yuppclaw-alice"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_resolve_agent(agent_name: str | None = AGENT_NAME, error: str | None = None) -> Any:
    return patch(
        "ypl.agent_harness_service.tools.self_management._resolve_current_personal_agent",
        new=AsyncMock(return_value=(agent_name, error)),
    )


def _make_db_session(row: Any = None) -> tuple[AsyncMock, AsyncMock]:
    """Create a mock async session context manager."""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = row
    session.execute.return_value = result
    session.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm, session


# ---------------------------------------------------------------------------
# read_self_system_prompt_tool
# ---------------------------------------------------------------------------


class TestReadSelfSystemPromptTool:
    async def test_success(self) -> None:
        row = ("Hello, I am Alice.",)

        cm, _ = _make_db_session(row=row)

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await read_self_system_prompt_tool()

        assert result["agent_name"] == AGENT_NAME
        assert result["additional_system_prompt"] == "Hello, I am Alice."

    async def test_agent_not_found_in_db(self) -> None:
        cm, _ = _make_db_session(row=None)  # No row → agent not found

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await read_self_system_prompt_tool()

        assert "error" in result
        assert AGENT_NAME in result["error"]

    async def test_resolve_agent_fails(self) -> None:
        with _patch_resolve_agent(agent_name=None, error="No session context available"):
            result = await read_self_system_prompt_tool()

        assert result == {"error": "No session context available"}

    async def test_empty_system_prompt_returns_empty_string(self) -> None:
        row = (None,)  # NULL in DB → should return empty string

        cm, _ = _make_db_session(row=row)

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await read_self_system_prompt_tool()

        assert result["additional_system_prompt"] == ""


# ---------------------------------------------------------------------------
# update_self_system_prompt_tool
# ---------------------------------------------------------------------------


class TestUpdateSelfSystemPromptTool:
    async def test_success(self) -> None:
        row = (AGENT_NAME,)

        cm, session = _make_db_session(row=row)
        # make execute return a result that has .first() returning the row
        execute_result = MagicMock()
        execute_result.first.return_value = row
        session.execute.return_value = execute_result

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await update_self_system_prompt_tool("I am a helpful assistant.")

        assert result["success"] is True
        assert result["agent_name"] == AGENT_NAME

    async def test_empty_prompt_rejected(self) -> None:
        with _patch_resolve_agent():
            result = await update_self_system_prompt_tool("")

        assert "error" in result
        assert "empty" in result["error"]

    async def test_whitespace_only_prompt_rejected(self) -> None:
        with _patch_resolve_agent():
            result = await update_self_system_prompt_tool("   \n\t  ")

        assert "error" in result
        assert "empty" in result["error"]

    async def test_prompt_too_long_rejected(self) -> None:
        long_prompt = "x" * 10_001  # Exceeds 10_000 char limit

        with _patch_resolve_agent():
            result = await update_self_system_prompt_tool(long_prompt)

        assert "error" in result
        assert "exceeds maximum" in result["error"]
        assert "10001" in result["error"]

    async def test_prompt_at_max_length_accepted(self) -> None:
        """Exactly 10,000 characters should be accepted."""
        max_prompt = "x" * 10_000
        row = (AGENT_NAME,)

        cm, session = _make_db_session()
        execute_result = MagicMock()
        execute_result.first.return_value = row
        session.execute.return_value = execute_result

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await update_self_system_prompt_tool(max_prompt)

        assert result.get("success") is True

    async def test_resolve_agent_fails(self) -> None:
        with _patch_resolve_agent(agent_name=None, error="Could not resolve agent"):
            result = await update_self_system_prompt_tool("Hello world")

        assert result == {"error": "Could not resolve agent"}

    async def test_agent_not_found_in_db(self) -> None:
        cm, session = _make_db_session()
        execute_result = MagicMock()
        execute_result.first.return_value = None  # UPDATE returned no rows
        session.execute.return_value = execute_result

        with (
            _patch_resolve_agent(),
            patch("ypl.agent_harness_service.tools.self_management.get_async_session", return_value=cm),
        ):
            result = await update_self_system_prompt_tool("New prompt content")

        assert "error" in result
        assert AGENT_NAME in result["error"]
