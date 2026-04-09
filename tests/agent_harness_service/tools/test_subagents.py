"""Unit tests for ypl/agent_harness_service/tools/subagents.py.

Tests permission checks, agent resolution, and subagent spawning.
"""

from __future__ import annotations
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.agent_harness_service.tools.subagents import (
    _check_tool_explicitly_allowed,
    _resolve_current_personal_agent,
)
from ypl.agent_harness_service.tools.subagents import list_agents as _list_agents
from ypl.agent_harness_service.tools.subagents import new_task as _new_task
from ypl.agent_harness_service.tools.subagents import route_model as _route_model

# Unwrap MCP FunctionTool wrappers to get raw callables
list_agents = _list_agents.fn
new_task = _new_task.fn
route_model = _route_model.fn

VALID_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


# ---------------------------------------------------------------------------
# Tests: _check_tool_explicitly_allowed
# ---------------------------------------------------------------------------


class TestCheckToolExplicitlyAllowed:
    async def test_returns_true_when_on_disk_config_allows(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.tool_permissions = {"bash": "allow"}

        with patch(
            "ypl.agent_harness_service.tools.subagents.load_agent_config",
            return_value=mock_cfg,
        ):
            result = await _check_tool_explicitly_allowed("reviewer", "bash")

        assert result is True

    async def test_returns_false_when_on_disk_config_denies(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.tool_permissions = {"bash": "deny"}

        with patch(
            "ypl.agent_harness_service.tools.subagents.load_agent_config",
            return_value=mock_cfg,
        ):
            result = await _check_tool_explicitly_allowed("reviewer", "bash")

        assert result is False

    async def test_returns_false_when_tool_not_in_config(self) -> None:
        mock_cfg = MagicMock()
        mock_cfg.tool_permissions = {"other_tool": "allow"}

        with patch(
            "ypl.agent_harness_service.tools.subagents.load_agent_config",
            return_value=mock_cfg,
        ):
            result = await _check_tool_explicitly_allowed("reviewer", "bash")

        assert result is False

    async def test_falls_back_to_db_when_no_disk_config(self) -> None:
        mock_db = AsyncMock()
        db_result = MagicMock()
        db_result.first.return_value = ({"tool_permissions": {"my_tool": "allow"}},)
        mock_db.execute = AsyncMock(return_value=db_result)

        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.load_agent_config",
                return_value=None,
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.common.models.expand_tool_permissions",
                return_value={"my_tool": "allow"},
            ),
        ):
            result = await _check_tool_explicitly_allowed("bizbot", "my_tool")

        assert result is True

    async def test_returns_false_when_db_agent_not_found(self) -> None:
        mock_db = AsyncMock()
        db_result = MagicMock()
        db_result.first.return_value = None
        mock_db.execute = AsyncMock(return_value=db_result)

        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.load_agent_config",
                return_value=None,
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents.get_async_session_read_replica",
                _make_mock_session_factory(mock_db),
            ),
        ):
            result = await _check_tool_explicitly_allowed("unknown-agent", "tool")

        assert result is False


# ---------------------------------------------------------------------------
# Tests: _resolve_current_personal_agent
# ---------------------------------------------------------------------------


class TestResolveCurrentPersonalAgent:
    async def test_returns_error_when_no_session_context(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
            MagicMock(get=MagicMock(return_value=None)),
        ):
            agent_name, error = await _resolve_current_personal_agent(None, "my_tool")

        assert agent_name is None
        assert "No session context" in (error or "")

    async def test_returns_error_when_agent_name_not_resolved(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                MagicMock(get=MagicMock(return_value=VALID_UUID)),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                AsyncMock(return_value={"agent_name": None}),
            ),
        ):
            agent_name, error = await _resolve_current_personal_agent(VALID_UUID, "my_tool")

        assert agent_name is None
        assert "Could not resolve" in (error or "")

    async def test_returns_agent_name_for_personal_agent(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                MagicMock(get=MagicMock(return_value=VALID_UUID)),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                AsyncMock(return_value={"agent_name": "yuppclaw-alice"}),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents.is_personal_agent",
                return_value=True,
            ),
        ):
            agent_name, error = await _resolve_current_personal_agent(VALID_UUID, "my_tool")

        assert agent_name == "yuppclaw-alice"
        assert error is None

    async def test_returns_agent_name_when_tool_explicitly_allowed(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                MagicMock(get=MagicMock(return_value=VALID_UUID)),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                AsyncMock(return_value={"agent_name": "custom-agent"}),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents.is_personal_agent",
                return_value=False,
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._check_tool_explicitly_allowed",
                AsyncMock(return_value=True),
            ),
        ):
            agent_name, error = await _resolve_current_personal_agent(VALID_UUID, "allowed_tool")

        assert agent_name == "custom-agent"
        assert error is None

    async def test_returns_error_when_agent_not_allowed(self) -> None:
        with (
            patch(
                "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                MagicMock(get=MagicMock(return_value=VALID_UUID)),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                AsyncMock(return_value={"agent_name": "restricted-agent"}),
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents.is_personal_agent",
                return_value=False,
            ),
            patch(
                "ypl.agent_harness_service.tools.subagents._check_tool_explicitly_allowed",
                AsyncMock(return_value=False),
            ),
        ):
            agent_name, error = await _resolve_current_personal_agent(VALID_UUID, "restricted_tool")

        assert agent_name is None
        assert "not available" in (error or "").lower() or "not 'restricted-agent'" in (error or "")


# ---------------------------------------------------------------------------
# Tests: list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def test_returns_list_of_agents(self) -> None:
        mock_config = MagicMock()
        mock_config.name = "reviewer"
        mock_config.description = "reviews code"
        mock_config.executor.type = "harnessed"
        mock_config.executor.model = "claude-code-cli"

        with patch(
            "ypl.agent_harness_service.tools.subagents.list_predefined_agents",
            return_value=[mock_config],
        ):
            result = list_agents()

        assert len(result) == 1
        assert result[0]["name"] == "reviewer"
        assert result[0]["description"] == "reviews code"

    def test_returns_empty_list_when_no_agents(self) -> None:
        with patch(
            "ypl.agent_harness_service.tools.subagents.list_predefined_agents",
            return_value=[],
        ):
            result = list_agents()

        assert result == []

    def test_handles_none_description(self) -> None:
        mock_config = MagicMock()
        mock_config.name = "reviewer"
        mock_config.description = None
        mock_config.executor.type = "harnessed"
        mock_config.executor.model = None

        with patch(
            "ypl.agent_harness_service.tools.subagents.list_predefined_agents",
            return_value=[mock_config],
        ):
            result = list_agents()

        assert result[0]["description"] == ""
        assert "(assigned at spawn time)" in result[0]["model"]


# ---------------------------------------------------------------------------
# Tests: route_model
# ---------------------------------------------------------------------------


class TestRouteModel:
    def test_returns_error_when_no_callbacks_registered(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._route_model_stub_fn
        _mcp_instance._route_model_stub_fn = None
        try:
            result = route_model("code review")
            assert len(result) == 1
            assert "ERROR" in result[0]
        finally:
            _mcp_instance._route_model_stub_fn = original

    def test_calls_registered_stub(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._route_model_stub_fn
        stub = MagicMock(return_value=["anthropic/claude-sonnet-4-6"])
        _mcp_instance._route_model_stub_fn = stub
        try:
            result = route_model("code review", count=1)
            assert result == ["anthropic/claude-sonnet-4-6"]
            stub.assert_called_once_with("code review", 1, None)
        finally:
            _mcp_instance._route_model_stub_fn = original


# ---------------------------------------------------------------------------
# Tests: new_task
# ---------------------------------------------------------------------------


class TestNewTask:
    async def test_returns_error_when_no_run_subagent_fn(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._run_subagent_fn
        _mcp_instance._run_subagent_fn = None
        try:
            with (
                patch(
                    "ypl.agent_harness_service.tools.subagents._validate_session_id",
                    return_value=VALID_UUID,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                    AsyncMock(
                        return_value={
                            "agent_name": None,
                            "model": None,
                            "workspace": None,
                            "permissions": None,
                            "subagent_depth": 0,
                        }
                    ),
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                    MagicMock(get=MagicMock(return_value=None)),
                ),
            ):
                result = await new_task("reviewer", "review this PR", session_id=VALID_UUID)
            assert result["status"] == "error"
            assert "not registered" in result["error"]
        finally:
            _mcp_instance._run_subagent_fn = original

    async def test_spawns_subagent_successfully(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._run_subagent_fn
        mock_run = AsyncMock(return_value=None)
        _mcp_instance._run_subagent_fn = mock_run

        try:
            with (
                patch(
                    "ypl.agent_harness_service.tools.subagents._validate_session_id",
                    return_value=VALID_UUID,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                    AsyncMock(
                        return_value={
                            "agent_name": "orchestrator",
                            "model": "anthropic/claude-sonnet-4-6",
                            "workspace": "/workspace",
                            "permissions": None,
                            "subagent_depth": 0,
                        }
                    ),
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.get_agent_spec",
                    return_value=None,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.load_agent_config",
                    return_value=None,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                    MagicMock(get=MagicMock(return_value=None)),
                ),
            ):
                result = await new_task("reviewer", "review this", session_id=VALID_UUID)

            assert result["status"] == "spawned"
            assert result["agent_name"] == "reviewer"
            assert "session_id" in result
        finally:
            _mcp_instance._run_subagent_fn = original

    async def test_returns_error_when_agent_not_in_allowed_subagents(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._run_subagent_fn
        mock_run = AsyncMock(return_value=None)
        _mcp_instance._run_subagent_fn = mock_run

        try:
            mock_spec = MagicMock()
            mock_spec.allowed_subagents = ["allowed-agent"]

            with (
                patch(
                    "ypl.agent_harness_service.tools.subagents._validate_session_id",
                    return_value=VALID_UUID,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                    AsyncMock(
                        return_value={
                            "agent_name": "restricted-parent",
                            "model": None,
                            "workspace": None,
                            "permissions": None,
                            "subagent_depth": 0,
                        }
                    ),
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.get_agent_spec",
                    return_value=mock_spec,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                    MagicMock(get=MagicMock(return_value=None)),
                ),
            ):
                result = await new_task("forbidden-agent", "do something", session_id=VALID_UUID)

            assert result["status"] == "error"
            assert "not allowed" in result["error"].lower()
        finally:
            _mcp_instance._run_subagent_fn = original

    async def test_allows_wildcard_subagent(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mcp_instance

        original = _mcp_instance._run_subagent_fn
        mock_run = AsyncMock(return_value=None)
        _mcp_instance._run_subagent_fn = mock_run

        try:
            mock_spec = MagicMock()
            mock_spec.allowed_subagents = ["*"]

            with (
                patch(
                    "ypl.agent_harness_service.tools.subagents._validate_session_id",
                    return_value=VALID_UUID,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents._resolve_parent_session",
                    AsyncMock(
                        return_value={
                            "agent_name": "parent",
                            "model": None,
                            "workspace": None,
                            "permissions": None,
                            "subagent_depth": 0,
                        }
                    ),
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.get_agent_spec",
                    return_value=mock_spec,
                ),
                patch(
                    "ypl.agent_harness_service.tools.subagents.mcp_session_id_var",
                    MagicMock(get=MagicMock(return_value=None)),
                ),
            ):
                result = await new_task("any-agent", "do something", session_id=VALID_UUID)

            assert result["status"] == "spawned"
        finally:
            _mcp_instance._run_subagent_fn = original
