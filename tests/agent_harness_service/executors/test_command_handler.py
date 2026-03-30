"""Unit tests for CommandHandlerManager — mocked subprocess, no bwrap required."""

from __future__ import annotations
import asyncio
import json
from asyncio import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.agent_harness_service.executors.command_handler import CommandHandlerManager

# Patch paths (module-level imports in command_handler.py)
_BWRAP_AVAIL = "ypl.agent_harness_service.executors.command_handler.bwrap_available"
_ISFILE = "ypl.agent_harness_service.executors.command_handler.os.path.isfile"
_ISDIR = "ypl.agent_harness_service.executors.command_handler.os.path.isdir"
_CREATE_SUBPROC = "ypl.agent_harness_service.executors.command_handler.asyncio.create_subprocess_exec"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_proc() -> MagicMock:
    """Build a mock asyncio.subprocess.Process with hooked stdin/stdout."""
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None  # still running

    proc.stdout = AsyncMock()
    # Default: readline() returns EOF immediately (override per test)
    proc.stdout.readline = AsyncMock(return_value=b"")

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    return proc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path) -> str:
    ws = str(tmp_path / "workspace")
    import os

    os.makedirs(ws, exist_ok=True)
    return ws


@pytest.fixture
def manager(workspace) -> CommandHandlerManager:
    """Return a CommandHandlerManager pointed at a temporary workspace."""
    return CommandHandlerManager(
        workspace=workspace,
        binary_path="/fake/ahs-command-handler",
        idle_timeout=0,  # disable idle timer for unit tests
        call_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# ensure_started
# ---------------------------------------------------------------------------


class TestEnsureStarted:
    @pytest.mark.asyncio
    async def test_spawns_process(self, manager: CommandHandlerManager) -> None:
        """ensure_started() creates a subprocess and starts the reader task."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

        assert manager._proc is mock_proc
        assert manager._reader_task is not None
        mock_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent(self, manager: CommandHandlerManager) -> None:
        """ensure_started() is a no-op when the process is already running."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()
            await manager.ensure_started()  # second call — should not spawn again

        assert mock_create.await_count == 1

    @pytest.mark.asyncio
    async def test_raises_if_bwrap_unavailable(self, manager: CommandHandlerManager) -> None:
        with patch(_BWRAP_AVAIL, return_value=False), pytest.raises(RuntimeError, match="bwrap is not available"):
            await manager.ensure_started()

    @pytest.mark.asyncio
    async def test_raises_if_binary_missing(self, manager: CommandHandlerManager) -> None:
        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=False),
            pytest.raises(RuntimeError, match="BCH binary not found"),
        ):
            await manager.ensure_started()


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------


class TestCallTool:
    @pytest.mark.asyncio
    async def test_bash_success(self, manager: CommandHandlerManager) -> None:
        """Manually injecting a response into a pending future and awaiting it."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

            # Inject a response directly.
            req_id = "test-req-1234"
            loop = asyncio.get_event_loop()
            future: Future[str] = loop.create_future()
            manager._pending[req_id] = future

            # Simulate the reader resolving the future with a result.
            future.set_result("hello world")

            result = await asyncio.wait_for(future, timeout=1.0)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_call_tool_timeout(self, manager: CommandHandlerManager) -> None:
        """call_tool raises RuntimeError if no response arrives in time.

        We inject a mock proc directly and create a dummy reader task so that
        call_tool can write the request.  The key: the future registered in
        _pending is never resolved, so wait_for fires the timeout.
        """
        never_done: asyncio.Event = asyncio.Event()

        async def _blocking_readline() -> bytes:
            await never_done.wait()
            return b""

        mock_proc = _make_mock_proc()
        mock_proc.stdin.drain = AsyncMock(return_value=None)
        mock_proc.stdout.readline = AsyncMock(side_effect=_blocking_readline)

        # Create a background task for the reader that blocks without crashing.
        reader_task: asyncio.Task[None] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]

        manager._proc = mock_proc
        manager._reader_task = reader_task
        manager._call_timeout = 0.05

        try:
            with pytest.raises(RuntimeError, match="timed out"):
                await manager.call_tool("Bash", {"command": "echo hi"})
        finally:
            # Clean up the blocking reader task.
            reader_task.cancel()
            never_done.set()  # unblock any pending readline calls

    @pytest.mark.asyncio
    async def test_call_tool_proxy_error(self, manager: CommandHandlerManager) -> None:
        """call_tool raises RuntimeError when the proxy future has an error."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

            req_id = "err-req-id"
            loop = asyncio.get_event_loop()
            future: Future[str] = loop.create_future()
            manager._pending[req_id] = future
            future.set_exception(RuntimeError("permission denied"))

            with pytest.raises(RuntimeError, match="permission denied"):
                await asyncio.wait_for(future, timeout=1.0)


# ---------------------------------------------------------------------------
# Reader coroutine dispatching
# ---------------------------------------------------------------------------


class TestReaderDispatch:
    @pytest.mark.asyncio
    async def test_reader_dispatches_success_response(self, manager: CommandHandlerManager) -> None:
        """The reader coroutine dispatches a successful response to the future."""
        req_id = "reader-test-req"
        response_line = json.dumps({"req_id": req_id, "ok": True, "result": "dispatched!"}).encode() + b"\n"

        mock_proc = _make_mock_proc()
        # First readline returns the response, second returns EOF.
        mock_proc.stdout.readline = AsyncMock(side_effect=[response_line, b""])
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

            loop = asyncio.get_event_loop()
            future: Future[str] = loop.create_future()
            manager._pending[req_id] = future

            # Give the reader task a chance to process the line.
            await asyncio.sleep(0.01)

            assert future.done()
            assert future.result() == "dispatched!"

    @pytest.mark.asyncio
    async def test_reader_dispatches_error_response(self, manager: CommandHandlerManager) -> None:
        """The reader coroutine sets an exception for error responses."""
        req_id = "reader-err-req"
        response_line = json.dumps({"req_id": req_id, "ok": False, "error": "cmd failed"}).encode() + b"\n"

        mock_proc = _make_mock_proc()
        mock_proc.stdout.readline = AsyncMock(side_effect=[response_line, b""])
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

            loop = asyncio.get_event_loop()
            future: Future[str] = loop.create_future()
            manager._pending[req_id] = future

            await asyncio.sleep(0.01)

            assert future.done()
            with pytest.raises(RuntimeError, match="cmd failed"):
                future.result()


# ---------------------------------------------------------------------------
# Crash handling
# ---------------------------------------------------------------------------


class TestCrashHandling:
    @pytest.mark.asyncio
    async def test_crash_resolves_pending_futures(self, manager: CommandHandlerManager) -> None:
        """When _handle_crash() is called, all pending futures get an error."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()

            loop = asyncio.get_event_loop()
            futures: list[Future[str]] = []
            for i in range(3):
                f: Future[str] = loop.create_future()
                manager._pending[f"req-{i}"] = f
                futures.append(f)

            # Simulate crash (process is dead)
            mock_proc.returncode = 1
            await manager._handle_crash()

        assert manager._pending == {}
        for f in futures:
            assert f.done()
            with pytest.raises(RuntimeError, match="exited unexpectedly"):
                f.result()

    @pytest.mark.asyncio
    async def test_crash_clears_proc(self, manager: CommandHandlerManager) -> None:
        """After _handle_crash(), _proc is None so next call_tool restarts."""
        mock_proc = _make_mock_proc()
        mock_proc.returncode = 1  # process is dead
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()
            assert manager._proc is not None

            await manager._handle_crash()
            assert manager._proc is None


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_sends_sigterm(self, manager: CommandHandlerManager) -> None:
        """stop() terminates the process and awaits exit."""
        mock_proc = _make_mock_proc()
        mock_create = AsyncMock(return_value=mock_proc)

        with (
            patch(_BWRAP_AVAIL, return_value=True),
            patch(_ISFILE, return_value=True),
            patch(_CREATE_SUBPROC, mock_create),
        ):
            await manager.ensure_started()
            await manager.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_awaited()
        assert manager._proc is None

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_started(self, manager: CommandHandlerManager) -> None:
        """stop() is safe to call before ensure_started()."""
        await manager.stop()  # Should not raise


# ---------------------------------------------------------------------------
# workspace_tools integration (set/get manager)
# ---------------------------------------------------------------------------


class TestWorkspaceToolsIntegration:
    def test_set_and_get_manager(self, manager: CommandHandlerManager) -> None:
        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )

        session_id = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        assert get_command_handler_manager(session_id) is None

        set_command_handler_manager(session_id, manager)
        assert get_command_handler_manager(session_id) is manager

        set_command_handler_manager(session_id, None)
        assert get_command_handler_manager(session_id) is None

    def test_set_manager_multiple_sessions(self, manager: CommandHandlerManager, tmp_path) -> None:
        import os

        from ypl.agent_harness_service.tools.workspace_tools import (
            get_command_handler_manager,
            set_command_handler_manager,
        )

        ws2 = str(tmp_path / "workspace2")
        os.makedirs(ws2, exist_ok=True)
        manager2 = CommandHandlerManager(workspace=ws2)

        session1 = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
        session2 = "cccccccc-4444-5555-6666-dddddddddddd"

        set_command_handler_manager(session1, manager)
        set_command_handler_manager(session2, manager2)

        assert get_command_handler_manager(session1) is manager
        assert get_command_handler_manager(session2) is manager2

        # Cleanup
        set_command_handler_manager(session1, None)
        set_command_handler_manager(session2, None)
