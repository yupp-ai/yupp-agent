"""Unit tests for ypl/agent_harness_service/service/agent_crud.py.

Tests create_agent and edit_agent with mocked async DB sessions.
"""

from __future__ import annotations
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy SDK deps
# ---------------------------------------------------------------------------
_together = types.ModuleType("together")
_together_types = types.ModuleType("together.types")
_croniter = types.ModuleType("croniter")


class _APITimeoutError(Exception):
    pass


class _AsyncTogether:
    pass


class _Together:
    pass


class _ChatCompletion:
    pass


_together.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
_together.AsyncTogether = _AsyncTogether  # type: ignore[attr-defined]
_together.Together = _Together  # type: ignore[attr-defined]
_together_types.ChatCompletion = _ChatCompletion  # type: ignore[attr-defined]
sys.modules.setdefault("together", _together)
sys.modules.setdefault("together.types", _together_types)
try:
    import croniter as _real_croniter  # type: ignore[import-untyped,unused-ignore]  # noqa: F401
except ImportError:
    _croniter.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", _croniter)

# ---------------------------------------------------------------------------
# Test imports
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.common.types import (  # noqa: E402
    AgentCreateRequest,
    AgentEditRequest,
    ExecutorConfigRequest,
    SandboxConfigRequest,
)
from ypl.db.agent_harness import Agent  # noqa: E402
from ypl.db.users import User  # noqa: E402


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


def _make_db_agent(
    name: str = "test-agent",
    creator_user_id: str | None = "user-123",
    config: dict[str, Any] | None = None,
) -> Agent:
    return Agent(
        agent_id=uuid.uuid4(),
        name=name,
        display_name="Test Agent",
        creator_user_id=creator_user_id,
        config=config or {},
    )


def _make_create_request(name: str = "my-new-agent", user_id: str = "user-123") -> AgentCreateRequest:
    return AgentCreateRequest(
        name=name,
        user_id=user_id,
        display_name="My New Agent",
        description="A test agent",
        executor_config=ExecutorConfigRequest(type="harnessed", model="claude-code-cli"),
    )


def _make_edit_request(name: str = "test-agent", user_id: str = "user-123") -> AgentEditRequest:
    return AgentEditRequest(name=name, user_id=user_id)


# ===========================================================================
# Tests: create_agent
# ===========================================================================


class TestCreateAgent:
    """Tests for create_agent."""

    async def test_raises_when_agent_already_exists(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import create_agent

        existing = _make_db_agent("my-new-agent")
        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=existing),
            ),
        ):
            req = _make_create_request(name="my-new-agent")
            with pytest.raises(ValueError, match="Agent already exists"):
                await create_agent(req)

    async def test_raises_for_invalid_agent_name(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import create_agent

        mock_db = AsyncMock()

        with patch(
            "ypl.agent_harness_service.service.agent_crud.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            req = _make_create_request(name="INVALID NAME!")
            with pytest.raises((ValueError, Exception)):
                await create_agent(req)

    async def test_creates_new_agent_successfully(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import create_agent

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=None),
            ),
        ):
            req = _make_create_request(name="brand-new-agent", user_id="user-abc")
            result = await create_agent(req)
            assert result.name == "brand-new-agent"
            assert result.status == "created"
            # create_agent adds two rows: the Agent and its User identity record
            assert mock_db.add.call_count == 2
            assert isinstance(mock_db.add.call_args_list[0][0][0], Agent)
            assert isinstance(mock_db.add.call_args_list[1][0][0], User)
            mock_db.commit.assert_called_once()

    async def test_creates_agent_with_role_md(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import create_agent

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=None),
            ),
        ):
            req = _make_create_request(name="role-agent")
            req.role_md = "# My Role"
            req.soul_md = "# Soul"
            result = await create_agent(req)
            assert result.status == "created"
            # Verify the agent added has role_md in its config.
            # call_args_list[0] is the Agent add (first); call_args_list[1] is the User add (second).
            added_agent: Agent = mock_db.add.call_args_list[0][0][0]
            assert added_agent.config is not None
            assert added_agent.config.get("role_md") == "# My Role"
            assert added_agent.config.get("soul_md") == "# Soul"


# ===========================================================================
# Tests: edit_agent
# ===========================================================================


class TestEditAgent:
    """Tests for edit_agent."""

    async def test_raises_when_agent_not_found(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=None),
            ),
        ):
            req = _make_edit_request(name="nonexistent")
            with pytest.raises(ValueError, match="Agent not found"):
                await edit_agent(req)

    async def test_raises_for_global_agent_without_creator(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        global_agent = _make_db_agent(creator_user_id=None)
        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=global_agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            with pytest.raises(PermissionError, match="global agent"):
                await edit_agent(req)

    async def test_raises_for_wrong_user(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="owner-123")
        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="different-user")
            with pytest.raises(PermissionError, match="Only the agent creator"):
                await edit_agent(req)

    async def test_updates_display_name(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="user-123")
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            req.display_name = "New Display Name"
            result = await edit_agent(req)
            assert result.status == "updated"
            assert agent.display_name == "New Display Name"

    async def test_updates_executor_config(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="user-123", config={"executor_config": {"type": "harnessed"}})
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            req.executor_config = ExecutorConfigRequest(type="raw", model="anthropic/claude-sonnet-4-6")
            result = await edit_agent(req)
            assert result.status == "updated"
            assert agent.config is not None
            assert agent.config["executor_config"]["type"] == "raw"
            assert agent.config["executor_config"]["model"] == "anthropic/claude-sonnet-4-6"

    async def test_updates_sandbox_config(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="user-123", config={})
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            req.sandbox = SandboxConfigRequest(enabled=False, auto_allow_bash_if_sandboxed=False, bwrap_enabled=True)
            result = await edit_agent(req)
            assert result.status == "updated"
            assert agent.config is not None
            assert agent.config["sandbox"]["enabled"] is False
            assert agent.config["sandbox"]["bwrapEnabled"] is True

    async def test_updates_tool_permissions_and_max_turns(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="user-123", config={})
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            req.tool_permissions = {"bash": "deny"}
            req.max_turns = 50
            req.max_budget_usd = 5.0
            req.timeout_s = 600
            result = await edit_agent(req)
            assert result.status == "updated"
            assert agent.config is not None
            assert agent.config["tool_permissions"] == {"bash": "deny"}
            assert agent.config["max_turns"] == 50
            assert agent.config["max_budget_usd"] == 5.0
            assert agent.config["timeout_s"] == 600

    async def test_updates_role_md_and_soul_md(self) -> None:
        from ypl.agent_harness_service.service.agent_crud import edit_agent

        agent = _make_db_agent(creator_user_id="user-123", config={})
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.agent_crud.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.agent_crud._resolve_agent",
                AsyncMock(return_value=agent),
            ),
        ):
            req = _make_edit_request(user_id="user-123")
            req.role_md = "# Updated Role"
            req.soul_md = "# Updated Soul"
            result = await edit_agent(req)
            assert result.status == "updated"
            assert agent.config is not None
            assert agent.config["role_md"] == "# Updated Role"
            assert agent.config["soul_md"] == "# Updated Soul"
