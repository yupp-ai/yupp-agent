"""Tests for mcp_instance.py — session state management, validation helpers, DI callbacks."""

from __future__ import annotations
import asyncio
from collections.abc import Callable

import pytest
from ypl.agent_harness_service.tools.mcp_instance import (
    _session_auth_terminal_states,
    _session_current_user,
    _session_sandbox,
    _session_websearch_count,
    _session_websearch_locks,
    _turn_websearch_count,
    _validate_session_id,
    clear_session_sandbox,
    clear_session_state,
    clear_session_websearch_count,
    register_orchestration_callbacks,
    reset_turn_websearch_count,
    set_session_current_user,
    set_session_sandbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_UUID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# _validate_session_id
# ---------------------------------------------------------------------------


class TestValidateSessionId:
    def test_valid_uuid_passes(self) -> None:
        result = _validate_session_id(VALID_UUID)
        assert result == VALID_UUID

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="session_id is required"):
            _validate_session_id("")

    def test_non_uuid_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            _validate_session_id("not-a-uuid")

    def test_arbitrary_string_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid UUID"):
            _validate_session_id("hello world")

    def test_uppercase_uuid_passes(self) -> None:
        upper = VALID_UUID.upper()
        result = _validate_session_id(upper)
        assert result == upper


# ---------------------------------------------------------------------------
# Sandbox stack management
# ---------------------------------------------------------------------------


class TestSessionSandbox:
    def setup_method(self) -> None:
        """Clean up sandbox state before each test."""
        _session_sandbox.pop(VALID_UUID, None)
        _session_sandbox.pop(OTHER_UUID, None)

    def test_set_and_read(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        assert _session_sandbox[VALID_UUID] == [True]

    def test_push_multiple(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        set_session_sandbox(VALID_UUID, False)
        assert _session_sandbox[VALID_UUID] == [True, False]

    def test_clear_pops_top(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        set_session_sandbox(VALID_UUID, False)
        clear_session_sandbox(VALID_UUID)
        assert _session_sandbox[VALID_UUID] == [True]

    def test_clear_removes_key_when_empty(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        clear_session_sandbox(VALID_UUID)
        assert VALID_UUID not in _session_sandbox

    def test_clear_missing_session_is_noop(self) -> None:
        # Should not raise when clearing a session that doesn't exist
        clear_session_sandbox("nonexistent-session")

    def test_independent_sessions(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        set_session_sandbox(OTHER_UUID, False)
        assert _session_sandbox[VALID_UUID] == [True]
        assert _session_sandbox[OTHER_UUID] == [False]


# ---------------------------------------------------------------------------
# Websearch counters
# ---------------------------------------------------------------------------


class TestWebsearchCounters:
    def setup_method(self) -> None:
        _turn_websearch_count.pop(VALID_UUID, None)
        _session_websearch_count.pop(VALID_UUID, None)
        _session_websearch_locks.pop(VALID_UUID, None)

    def test_reset_turn_count(self) -> None:
        _turn_websearch_count[VALID_UUID] = 5
        reset_turn_websearch_count(VALID_UUID)
        assert VALID_UUID not in _turn_websearch_count

    def test_reset_turn_count_missing_is_noop(self) -> None:
        reset_turn_websearch_count(VALID_UUID)  # Should not raise

    def test_clear_session_websearch_count_removes_all(self) -> None:
        _turn_websearch_count[VALID_UUID] = 3
        _session_websearch_count[VALID_UUID] = 10
        _session_websearch_locks[VALID_UUID] = asyncio.Lock()
        clear_session_websearch_count(VALID_UUID)
        assert VALID_UUID not in _turn_websearch_count
        assert VALID_UUID not in _session_websearch_count
        assert VALID_UUID not in _session_websearch_locks

    def test_clear_missing_is_noop(self) -> None:
        clear_session_websearch_count("nonexistent")


# ---------------------------------------------------------------------------
# Current user tracking
# ---------------------------------------------------------------------------


class TestSessionCurrentUser:
    def setup_method(self) -> None:
        _session_current_user.pop(VALID_UUID, None)

    def test_set_and_read(self) -> None:
        set_session_current_user(VALID_UUID, "user-123")
        assert _session_current_user[VALID_UUID] == "user-123"

    def test_overwrite(self) -> None:
        set_session_current_user(VALID_UUID, "user-123")
        set_session_current_user(VALID_UUID, "user-456")
        assert _session_current_user[VALID_UUID] == "user-456"


# ---------------------------------------------------------------------------
# clear_session_state
# ---------------------------------------------------------------------------


class TestClearSessionState:
    def setup_method(self) -> None:
        from ypl.agent_harness_service.tools.mcp_instance import _session_polling_tasks

        _session_sandbox.pop(VALID_UUID, None)
        _turn_websearch_count.pop(VALID_UUID, None)
        _session_websearch_count.pop(VALID_UUID, None)
        _session_websearch_locks.pop(VALID_UUID, None)
        _session_auth_terminal_states.pop(VALID_UUID, None)
        _session_current_user.pop(VALID_UUID, None)
        _session_polling_tasks.pop(VALID_UUID, None)

    def test_clears_all_state(self) -> None:
        set_session_sandbox(VALID_UUID, True)
        _turn_websearch_count[VALID_UUID] = 1
        _session_websearch_count[VALID_UUID] = 2
        _session_websearch_locks[VALID_UUID] = asyncio.Lock()
        _session_auth_terminal_states[VALID_UUID] = "expired"
        set_session_current_user(VALID_UUID, "user-abc")

        clear_session_state(VALID_UUID)

        assert VALID_UUID not in _session_sandbox
        assert VALID_UUID not in _turn_websearch_count
        assert VALID_UUID not in _session_websearch_count
        assert VALID_UUID not in _session_websearch_locks
        assert VALID_UUID not in _session_auth_terminal_states
        assert VALID_UUID not in _session_current_user

    async def test_clears_polling_task(self) -> None:
        """Cancels polling task when session is cleared."""
        from ypl.agent_harness_service.tools.mcp_instance import _session_polling_tasks

        async def _fake_task() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(_fake_task())
        _session_polling_tasks[VALID_UUID] = task
        clear_session_state(VALID_UUID)
        assert VALID_UUID not in _session_polling_tasks
        # Task should have a pending cancellation request
        assert task.cancelling() > 0 or task.cancelled()
        # Clean up: await the cancelled task to avoid ResourceWarning
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_clears_missing_session_is_noop(self) -> None:
        clear_session_state("nonexistent-session-uuid")  # Should not raise


# ---------------------------------------------------------------------------
# register_orchestration_callbacks
# ---------------------------------------------------------------------------


class TestRegisterOrchestrationCallbacks:
    def test_register_and_retrieve(self) -> None:
        """Registered callbacks are stored and accessible."""
        import ypl.agent_harness_service.tools.mcp_instance as _mod

        original_run = _mod._run_subagent_fn
        original_route = _mod._route_model_stub_fn
        original_create = _mod._create_agent_fn

        try:
            stub_run = lambda *a, **k: None  # noqa: E731
            stub_route: Callable[..., list[str]] = lambda *a, **k: []  # noqa: E731
            stub_create = lambda *a, **k: None  # noqa: E731

            register_orchestration_callbacks(
                run_subagent=stub_run,
                route_model_stub=stub_route,
                create_agent=stub_create,
            )

            assert _mod._run_subagent_fn is stub_run
            assert _mod._route_model_stub_fn is stub_route
            assert _mod._create_agent_fn is stub_create
        finally:
            # Restore originals
            _mod._run_subagent_fn = original_run
            _mod._route_model_stub_fn = original_route
            _mod._create_agent_fn = original_create

    def test_register_without_create_agent(self) -> None:
        import ypl.agent_harness_service.tools.mcp_instance as _mod

        original_run = _mod._run_subagent_fn
        original_route = _mod._route_model_stub_fn
        original_create = _mod._create_agent_fn

        try:
            stub_run = lambda *a, **k: None  # noqa: E731
            stub_route: Callable[..., list[str]] = lambda *a, **k: []  # noqa: E731
            register_orchestration_callbacks(run_subagent=stub_run, route_model_stub=stub_route)
            assert _mod._create_agent_fn is None
        finally:
            _mod._run_subagent_fn = original_run
            _mod._route_model_stub_fn = original_route
            _mod._create_agent_fn = original_create
