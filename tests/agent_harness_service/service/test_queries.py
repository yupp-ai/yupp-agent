"""Unit tests for ypl/agent_harness_service/service/queries.py.

Tests read-only DB queries and builder helpers with mocked async sessions.
"""

from __future__ import annotations
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy SDK deps so service modules can be imported in a plain test env.
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
    import croniter as _real_croniter  # type: ignore[import-untyped]  # noqa: F401
except ImportError:
    _croniter.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", _croniter)

# ---------------------------------------------------------------------------
# Imports from the module under test
# ---------------------------------------------------------------------------
from ypl.agent_harness_service.common.config import AgentConfig  # noqa: E402
from ypl.agent_harness_service.service.queries import (  # noqa: E402
    _build_agent_info,
    _build_agent_info_from_db,
    _build_session_info,
)
from ypl.db.agent_harness import (  # noqa: E402
    Agent,
    AgentSession,
    AgentSessionStatus,
    AgentSessionTrigger,
)


def _make_mock_session_factory(mock_db: AsyncMock) -> Any:
    """Return an asynccontextmanager factory that yields the provided mock DB session."""

    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[Any, None]:
        yield mock_db

    return _ctx


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_agent(
    name: str = "test-agent",
    display_name: str = "Test Agent",
    creator_user_id: str | None = "user-123",
    config: dict[str, Any] | None = None,
) -> Agent:
    return Agent(
        agent_id=uuid.uuid4(),
        name=name,
        display_name=display_name,
        creator_user_id=creator_user_id,
        config=config or {},
    )


def _make_session(
    agent_id: uuid.UUID | None = None,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    trigger: AgentSessionTrigger = AgentSessionTrigger.API,
    parent_session_id: uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
) -> AgentSession:
    return AgentSession(
        agent_session_id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        status=status,
        trigger=trigger,
        parent_session_id=parent_session_id,
        context=context,
        model="claude-code-cli",
        created_at=datetime.now(UTC),
    )


def _make_agent_config(name: str = "test-agent") -> AgentConfig:
    """Build a minimal AgentConfig suitable for _build_agent_info."""
    from ypl.agent_harness_service.common.config import (
        ExecutorConfig,
        SandboxConfig,
    )

    return AgentConfig(
        name=name,
        config_dir=f"/fake/agents/{name}",
        display_name="Test Agent",
        description="A test agent",
        executor_config=ExecutorConfig(type="harnessed", model="claude-code-cli"),
        tool_permissions={"*": "allow"},
        allowed_subagents=[],
        allowed_gateways=["*"],
        default_repo="yupp-mind",
        max_turns=20,
        max_budget_usd=2.0,
        timeout_s=300,
        sandbox=SandboxConfig(enabled=True),
    )


# ===========================================================================
# Tests: _build_agent_info
# ===========================================================================


class TestBuildAgentInfo:
    """Tests for _build_agent_info (config → AgentInfo)."""

    def test_basic_fields_populated(self) -> None:
        cfg = _make_agent_config("my-agent")
        info = _build_agent_info(cfg)
        assert info.name == "my-agent"
        assert info.display_name == "Test Agent"
        assert info.executor_type == "harnessed"
        assert info.executor_model == "claude-code-cli"

    def test_tool_permissions_stringified(self) -> None:
        cfg = _make_agent_config()
        cfg.tool_permissions = {"*": "allow"}
        info = _build_agent_info(cfg)
        assert info.tool_permissions == {"*": "allow"}

    def test_allowed_gateways_passed_through(self) -> None:
        cfg = _make_agent_config()
        cfg.allowed_gateways = ["slack"]
        info = _build_agent_info(cfg)
        assert info.allowed_gateways == ["slack"]

    def test_max_budget_and_timeout_present(self) -> None:
        cfg = _make_agent_config()
        info = _build_agent_info(cfg)
        assert info.max_budget_usd == 2.0
        assert info.timeout_s == 300


# ===========================================================================
# Tests: _build_agent_info_from_db
# ===========================================================================


class TestBuildAgentInfoFromDb:
    """Tests for _build_agent_info_from_db (Agent DB row → AgentInfo)."""

    def test_uses_agent_fields_when_no_config(self) -> None:
        agent = _make_agent(name="db-agent", config=None)
        info = _build_agent_info_from_db(agent)
        assert info.name == "db-agent"
        assert info.display_name == "Test Agent"

    def test_executor_type_from_config(self) -> None:
        agent = _make_agent(config={"executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"}})
        info = _build_agent_info_from_db(agent)
        assert info.executor_type == "raw"
        assert info.executor_model == "anthropic/claude-sonnet-4-6"

    def test_llm_model_only_set_for_raw_executor(self) -> None:
        agent = _make_agent(config={"executor_config": {"type": "raw", "model": "anthropic/claude-sonnet-4-6"}})
        info = _build_agent_info_from_db(agent)
        assert info.llm_model == "anthropic/claude-sonnet-4-6"

    def test_llm_model_none_for_harnessed_executor(self) -> None:
        agent = _make_agent(config={"executor_config": {"type": "harnessed", "model": "claude-code-cli"}})
        info = _build_agent_info_from_db(agent)
        assert info.llm_model is None

    def test_defaults_when_config_empty(self) -> None:
        agent = _make_agent(config={})
        info = _build_agent_info_from_db(agent)
        assert info.max_turns == 20
        assert info.max_budget_usd == 2.0
        assert info.timeout_s == 300
        assert info.sandbox_enabled is True
        assert info.allowed_gateways == ["*"]

    def test_creator_user_id_propagated(self) -> None:
        agent = _make_agent(creator_user_id="user-abc")
        info = _build_agent_info_from_db(agent)
        assert info.creator_user_id == "user-abc"

    def test_tool_permissions_from_config(self) -> None:
        agent = _make_agent(config={"tool_permissions": {"bash": "deny"}})
        info = _build_agent_info_from_db(agent)
        assert info.tool_permissions == {"bash": "deny"}

    def test_sandbox_enabled_false_from_config(self) -> None:
        agent = _make_agent(config={"sandbox": {"enabled": False}})
        info = _build_agent_info_from_db(agent)
        assert info.sandbox_enabled is False


# ===========================================================================
# Tests: _build_session_info
# ===========================================================================


class TestBuildSessionInfo:
    """Tests for _build_session_info (AgentSession DB row → SessionInfo)."""

    def test_basic_fields(self) -> None:
        session = _make_session()
        info = _build_session_info(session, agent_name="my-agent")
        assert info.session_id == str(session.agent_session_id)
        assert info.agent_name == "my-agent"
        assert info.status == "ACTIVE"
        assert info.trigger == "API"

    def test_message_count_passed_through(self) -> None:
        session = _make_session()
        info = _build_session_info(session, agent_name="x", message_count=7)
        assert info.message_count == 7

    def test_parent_session_id_string(self) -> None:
        parent_id = uuid.uuid4()
        session = _make_session(parent_session_id=parent_id)
        info = _build_session_info(session, agent_name="x")
        assert info.parent_session_id == str(parent_id)

    def test_parent_session_id_none_when_root(self) -> None:
        session = _make_session()
        info = _build_session_info(session, agent_name="x")
        assert info.parent_session_id is None

    def test_context_slack_fields_extracted(self) -> None:
        ctx = {"slack_channel_name": "#eng", "slack_user_id": "U123"}
        session = _make_session(context=ctx)
        info = _build_session_info(session, agent_name="x")
        assert info.slack_channel_name == "#eng"
        assert info.slack_user_id == "U123"

    def test_permissions_extracted_when_present(self) -> None:
        ctx = {
            "permissions": {
                "allowed_servers": ["harness"],
                "allowed_harness_tools": ["read", "write"],
            }
        }
        session = _make_session(context=ctx)
        info = _build_session_info(session, agent_name="x")
        assert info.tool_permissions is not None
        assert info.has_full_tool_access is False

    def test_full_tool_access_detected(self) -> None:
        ctx = {
            "permissions": {
                "allowed_servers": ["harness"],
                "allowed_harness_tools": ["*"],
            }
        }
        session = _make_session(context=ctx)
        info = _build_session_info(session, agent_name="x")
        assert info.has_full_tool_access is True


# ===========================================================================
# Tests: send_feedback
# ===========================================================================


class TestSendFeedback:
    """Tests for send_feedback with mocked DB session."""

    async def test_raises_when_session_not_found(self) -> None:
        from ypl.agent_harness_service.common.types import SessionFeedbackRequest
        from ypl.agent_harness_service.service.queries import send_feedback

        mock_db = AsyncMock()
        mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=lambda: None))

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=None),
            ),
        ):
            req = SessionFeedbackRequest(session_id="nonexistent-session")
            with pytest.raises(ValueError, match="Session not found"):
                await send_feedback(req)

    async def test_raises_for_invalid_message_id(self) -> None:
        from ypl.agent_harness_service.common.types import SessionFeedbackRequest
        from ypl.agent_harness_service.service.queries import send_feedback

        agent_session = _make_session()
        mock_db = AsyncMock()
        mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=lambda: None))

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=agent_session),
            ),
        ):
            req = SessionFeedbackRequest(session_id=str(agent_session.agent_session_id), message_id="not-a-uuid")
            with pytest.raises(ValueError, match="Invalid message_id"):
                await send_feedback(req)

    async def test_raises_for_invalid_rating(self) -> None:
        from ypl.agent_harness_service.common.types import SessionFeedbackRequest
        from ypl.agent_harness_service.service.queries import send_feedback

        agent_session = _make_session()
        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=agent_session),
            ),
        ):
            req = SessionFeedbackRequest(session_id=str(agent_session.agent_session_id), rating="INVALID")
            with pytest.raises(ValueError, match="Invalid rating"):
                await send_feedback(req)

    async def test_records_feedback_successfully(self) -> None:
        from ypl.agent_harness_service.common.types import SessionFeedbackRequest
        from ypl.agent_harness_service.service.queries import send_feedback

        agent_session = _make_session()
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=agent_session),
            ),
        ):
            req = SessionFeedbackRequest(
                session_id=str(agent_session.agent_session_id),
                rating="POSITIVE",
                comment="great",
            )
            result = await send_feedback(req)
            assert result.status == "recorded"
            mock_db.add.assert_called_once()
            mock_db.commit.assert_called_once()


# ===========================================================================
# Tests: list_agents
# ===========================================================================


class TestListAgents:
    """Tests for list_agents with mocked DB."""

    async def test_returns_empty_when_no_user_and_not_all(self) -> None:
        from ypl.agent_harness_service.service.queries import list_agents

        mock_db = AsyncMock()

        with patch(
            "ypl.agent_harness_service.service.queries.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_agents(include_all=False, user_id=None)
            assert result.agents == []

    async def test_returns_db_agents_for_user(self) -> None:
        from ypl.agent_harness_service.service.queries import list_agents

        agent = _make_agent(name="my-agent", creator_user_id="user-xyz")
        mock_db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = [agent]
        mock_db.exec = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.agent_harness_service.service.queries.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_agents(include_all=False, user_id="user-xyz")
            assert len(result.agents) == 1
            assert result.agents[0].name == "my-agent"

    async def test_marks_is_owner_for_matching_user(self) -> None:
        from ypl.agent_harness_service.service.queries import list_agents

        agent = _make_agent(name="mine", creator_user_id="user-xyz")
        mock_db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = [agent]
        mock_db.exec = AsyncMock(return_value=exec_result)

        with patch(
            "ypl.agent_harness_service.service.queries.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_agents(include_all=False, user_id="user-xyz")
            assert result.agents[0].is_owner is True

    async def test_include_all_adds_filesystem_agents(self) -> None:
        from ypl.agent_harness_service.service.queries import list_agents

        mock_db = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = []
        mock_db.exec = AsyncMock(return_value=exec_result)

        fake_cfg = _make_agent_config("fs-agent")
        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries.discover_agents",
                return_value={"fs-agent": fake_cfg},
            ),
        ):
            result = await list_agents(include_all=True)
            assert any(a.name == "fs-agent" for a in result.agents)


# ===========================================================================
# Tests: get_session_detail (subsession BFS)
# ===========================================================================


class TestGetSessionDetail:
    """Tests for get_session_detail — BFS traversal of subsessions."""

    async def test_raises_when_session_not_found(self) -> None:
        from ypl.agent_harness_service.service.queries import get_session_detail

        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ValueError, match="Session not found"),
        ):
            await get_session_detail("nonexistent")

    async def test_returns_session_with_no_subsessions(self) -> None:
        from ypl.agent_harness_service.service.queries import get_session_detail

        agent = _make_agent()
        session = _make_session(agent_id=agent.agent_id)
        mock_db = AsyncMock()

        # BFS child query returns no children
        children_result = MagicMock()
        children_result.all.return_value = []

        # Agent lookup
        agent_result = MagicMock()
        agent_result.all.return_value = [agent]

        # Message count
        msg_count_result = MagicMock()
        msg_count_result.all.return_value = []

        mock_db.exec = AsyncMock(side_effect=[children_result, agent_result, msg_count_result])

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            patch(
                "ypl.agent_harness_service.service.queries._resolve_session",
                AsyncMock(return_value=session),
            ),
        ):
            result = await get_session_detail(str(session.agent_session_id))
            assert result.session.session_id == str(session.agent_session_id)
            assert result.subsessions == []


# ===========================================================================
# Tests: list_sessions
# ===========================================================================


class TestListSessions:
    """Tests for list_sessions filter/pagination."""

    async def test_returns_empty_when_no_user_and_not_all(self) -> None:
        from ypl.agent_harness_service.service.queries import list_sessions

        mock_db = AsyncMock()

        with patch(
            "ypl.agent_harness_service.service.queries.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_sessions(include_all=False, user_id=None)
            assert result.sessions == []
            assert result.total == 0

    async def test_raises_for_invalid_status_filter(self) -> None:
        from ypl.agent_harness_service.service.queries import list_sessions

        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(ValueError, match="Invalid status filter"),
        ):
            await list_sessions(include_all=True, status="INVALID_STATUS")

    async def test_raises_for_invalid_trigger_filter(self) -> None:
        from ypl.agent_harness_service.service.queries import list_sessions

        mock_db = AsyncMock()

        with (
            patch(
                "ypl.agent_harness_service.service.queries.get_async_session",
                _make_mock_session_factory(mock_db),
            ),
            pytest.raises(ValueError, match="Invalid trigger filter"),
        ):
            await list_sessions(include_all=True, trigger="INVALID_TRIGGER")

    async def test_returns_sessions_for_user(self) -> None:
        from ypl.agent_harness_service.service.queries import list_sessions

        agent = _make_agent()
        session = _make_session(agent_id=agent.agent_id)
        mock_db = AsyncMock()

        count_result = MagicMock()
        count_result.one.return_value = 1

        sessions_result = MagicMock()
        sessions_result.all.return_value = [session]

        msg_count_result = MagicMock()
        msg_count_result.all.return_value = []

        agent_result = MagicMock()
        agent_result.all.return_value = [agent]

        mock_db.exec = AsyncMock(side_effect=[count_result, sessions_result, msg_count_result, agent_result])

        with patch(
            "ypl.agent_harness_service.service.queries.get_async_session",
            _make_mock_session_factory(mock_db),
        ):
            result = await list_sessions(include_all=True, limit=10, offset=0)
            assert result.total == 1
            assert len(result.sessions) == 1
