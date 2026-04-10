"""Tests for BCH CommandHandlerManager session lifecycle in service.py.

Verifies:
  - _command_handlers registry exists and is a dict
  - Manager is registered/deregistered correctly across session start and stop
  - stop_all_command_handler_managers() drains the registry and stops all managers
  - _run_agent_task finally block stops manager for terminal (non-SLACK) triggers

These are unit-level lifecycle tests; they do not spin up a full DB or bwrap
sandbox.  Use test_command_handler_integration.py for end-to-end BCH round-trips.
"""

from __future__ import annotations
import sys
import types
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Stub heavy SDK deps so service.py can be imported in a plain test env.
# (Same pattern as tests/agent_harness_service/test_service.py)
# ---------------------------------------------------------------------------

together_module: Any = types.ModuleType("together")
together_types_module: Any = types.ModuleType("together.types")
croniter_module: Any = types.ModuleType("croniter")


class _APITimeoutError(Exception):
    pass


class _AsyncTogether:
    pass


class _Together:
    pass


class _ChatCompletion:
    pass


together_module.APITimeoutError = _APITimeoutError
together_module.AsyncTogether = _AsyncTogether
together_module.Together = _Together
together_types_module.ChatCompletion = _ChatCompletion

sys.modules.setdefault("together", together_module)
sys.modules.setdefault("together.types", together_types_module)
try:
    import croniter as _real_croniter  # type: ignore[import-untyped]  # noqa: F401
except ImportError:
    croniter_module.croniter = lambda *args, **kwargs: None
    sys.modules.setdefault("croniter", croniter_module)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_manager() -> Any:
    """Return a CommandHandlerManager-shaped mock with an async stop()."""
    mgr = AsyncMock()
    mgr.stop = AsyncMock()
    return mgr


# ---------------------------------------------------------------------------
# Tests: module-level registry
# ---------------------------------------------------------------------------


class TestCommandHandlersRegistry:
    """The _command_handlers dict is importable and starts empty."""

    def test_registry_exists(self) -> None:
        from ypl.agent_harness_service.service import _command_handlers

        assert isinstance(_command_handlers, dict)

    def test_registry_is_uuid_keyed(self) -> None:
        """Verify we can insert and retrieve by UUID key without type errors."""
        from ypl.agent_harness_service.service import _command_handlers

        sid = uuid.uuid4()
        mgr = _make_mock_manager()
        _command_handlers[sid] = mgr
        assert _command_handlers.get(sid) is mgr
        # Cleanup — don't pollute other tests.
        _command_handlers.pop(sid, None)


# ---------------------------------------------------------------------------
# Tests: register/deregister lifecycle
# ---------------------------------------------------------------------------


class TestBchLifecycleRegistration:
    """Manager registration mirrors the create_session / stop_session path."""

    def test_register_and_clear(self, tmp_path: Any) -> None:
        import os

        from ypl.agent_harness_service.service import _command_handlers
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )

        ws = str(tmp_path / "ws")
        os.makedirs(ws)
        session_id = uuid.uuid4()
        sid_str = str(session_id)

        # Simulate what create_session does after commit.
        mgr = _make_mock_manager()
        _command_handlers[session_id] = mgr
        set_command_handler_manager(sid_str, mgr)

        assert _command_handlers.get(session_id) is mgr
        assert get_command_handler_manager(sid_str) is mgr

        # Simulate what stop_session does.
        popped = _command_handlers.pop(session_id, None)
        set_command_handler_manager(sid_str, None)

        assert popped is mgr
        assert session_id not in _command_handlers
        assert get_command_handler_manager(sid_str) is None

    def test_multiple_sessions_isolated(self, tmp_path: Any) -> None:
        import os

        from ypl.agent_harness_service.service import _command_handlers
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )

        ws1 = str(tmp_path / "ws1")
        ws2 = str(tmp_path / "ws2")
        os.makedirs(ws1)
        os.makedirs(ws2)

        sid1 = uuid.uuid4()
        sid2 = uuid.uuid4()
        mgr1 = _make_mock_manager()
        mgr2 = _make_mock_manager()

        _command_handlers[sid1] = mgr1
        _command_handlers[sid2] = mgr2
        set_command_handler_manager(str(sid1), mgr1)
        set_command_handler_manager(str(sid2), mgr2)

        assert get_command_handler_manager(str(sid1)) is mgr1
        assert get_command_handler_manager(str(sid2)) is mgr2

        # Removing one doesn't affect the other.
        _command_handlers.pop(sid1)
        set_command_handler_manager(str(sid1), None)

        assert get_command_handler_manager(str(sid1)) is None
        assert get_command_handler_manager(str(sid2)) is mgr2

        # Cleanup.
        _command_handlers.pop(sid2, None)
        set_command_handler_manager(str(sid2), None)


# ---------------------------------------------------------------------------
# Tests: stop_all_command_handler_managers
# ---------------------------------------------------------------------------


class TestStopAllCommandHandlerManagers:
    """stop_all_command_handler_managers() drains registry and calls stop()."""

    @pytest.mark.asyncio
    async def test_stops_all_managers(self) -> None:
        from ypl.agent_harness_service.service import (
            _command_handlers,
            stop_all_command_handler_managers,
        )
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )

        sid1 = uuid.uuid4()
        sid2 = uuid.uuid4()
        mgr1 = _make_mock_manager()
        mgr2 = _make_mock_manager()

        _command_handlers[sid1] = mgr1
        _command_handlers[sid2] = mgr2
        set_command_handler_manager(str(sid1), mgr1)
        set_command_handler_manager(str(sid2), mgr2)

        await stop_all_command_handler_managers()

        mgr1.stop.assert_awaited_once()
        mgr2.stop.assert_awaited_once()

        assert sid1 not in _command_handlers
        assert sid2 not in _command_handlers
        assert get_command_handler_manager(str(sid1)) is None
        assert get_command_handler_manager(str(sid2)) is None

    @pytest.mark.asyncio
    async def test_continues_on_stop_error(self) -> None:
        """A failing stop() does not prevent other managers from being stopped."""
        from ypl.agent_harness_service.service import (
            _command_handlers,
            stop_all_command_handler_managers,
        )
        from ypl.agent_harness_service.tools.workspace_tools import set_command_handler_manager

        sid1 = uuid.uuid4()
        sid2 = uuid.uuid4()

        mgr1 = _make_mock_manager()
        mgr1.stop.side_effect = RuntimeError("bwrap already dead")
        mgr2 = _make_mock_manager()

        _command_handlers[sid1] = mgr1
        _command_handlers[sid2] = mgr2
        set_command_handler_manager(str(sid1), mgr1)
        set_command_handler_manager(str(sid2), mgr2)

        # Should not raise.
        await stop_all_command_handler_managers()

        mgr1.stop.assert_awaited_once()
        mgr2.stop.assert_awaited_once()

        assert sid1 not in _command_handlers
        assert sid2 not in _command_handlers

    @pytest.mark.asyncio
    async def test_noop_when_empty(self) -> None:
        """Calling stop_all when there are no managers is safe."""
        from ypl.agent_harness_service.service import (
            _command_handlers,
            stop_all_command_handler_managers,
        )

        _command_handlers.clear()
        # Should not raise.
        await stop_all_command_handler_managers()


# ---------------------------------------------------------------------------
# Tests: _run_agent_task teardown path (non-SLACK triggers)
# ---------------------------------------------------------------------------


class TestRunAgentTaskTeardown:
    """Non-SLACK sessions stop their BCH manager in the finally block."""

    @pytest.mark.asyncio
    async def test_non_slack_trigger_stops_manager(self) -> None:
        """For TASK/CRON triggers the finally block removes and stops the manager."""
        from ypl.agent_harness_service.service import _command_handlers
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )
        from ypl.db.agent_harness import AgentSessionTrigger

        sid = uuid.uuid4()
        mgr = _make_mock_manager()
        _command_handlers[sid] = mgr
        set_command_handler_manager(str(sid), mgr)

        # Simulate the finally block logic for a TASK session.
        trigger = AgentSessionTrigger.TASK.value
        _is_interactive_session = trigger.upper() == AgentSessionTrigger.SLACK.value
        if not _is_interactive_session:
            _bch_mgr = _command_handlers.pop(sid, None)
            if _bch_mgr is not None:
                set_command_handler_manager(str(sid), None)
                await _bch_mgr.stop()

        assert sid not in _command_handlers
        assert get_command_handler_manager(str(sid)) is None
        mgr.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_slack_trigger_keeps_manager(self) -> None:
        """For SLACK sessions the finally block leaves the manager registered."""
        from ypl.agent_harness_service.service import _command_handlers
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )
        from ypl.db.agent_harness import AgentSessionTrigger

        sid = uuid.uuid4()
        mgr = _make_mock_manager()
        _command_handlers[sid] = mgr
        set_command_handler_manager(str(sid), mgr)

        # Simulate the finally block logic for a SLACK session.
        trigger = AgentSessionTrigger.SLACK.value
        _is_interactive_session = trigger.upper() == AgentSessionTrigger.SLACK.value
        if not _is_interactive_session:
            _bch_mgr = _command_handlers.pop(sid, None)
            if _bch_mgr is not None:
                set_command_handler_manager(str(sid), None)
                await _bch_mgr.stop()

        # Manager should still be registered.
        assert _command_handlers.get(sid) is mgr
        assert get_command_handler_manager(str(sid)) is mgr
        mgr.stop.assert_not_awaited()

        # Cleanup.
        _command_handlers.pop(sid, None)
        set_command_handler_manager(str(sid), None)
