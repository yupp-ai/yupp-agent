"""Integration tests for CommandHandlerManager — exercises the real Go binary.

These tests are skipped automatically when:
  - bwrap is not available on the host (CI / macOS)
  - The compiled Go binary is not present

To build the binary before running:
  cd services/command-handler && ./build.sh

Run with:
  pytest tests/agent_harness_service/executors/test_command_handler_integration.py -v
"""

from __future__ import annotations
import os

import pytest
from ypl.agent_harness_service.executors.command_handler import (
    _DEFAULT_BINARY_PATH,
    CommandHandlerManager,
)
from ypl.agent_harness_service.executors.sandbox import bwrap_available

# Skip the entire module if bwrap or the binary aren't available.
pytestmark = [
    pytest.mark.skipif(
        not bwrap_available(),
        reason="bwrap not available on this host",
    ),
    pytest.mark.skipif(
        not os.path.isfile(_DEFAULT_BINARY_PATH),
        reason=f"BCH binary not found at {_DEFAULT_BINARY_PATH!r}; run services/command-handler/build.sh",
    ),
]


@pytest.fixture
async def live_manager(tmp_path):
    """Start a real CommandHandlerManager against the compiled Go binary."""
    ws = str(tmp_path / "workspace")
    os.makedirs(ws, exist_ok=True)
    manager = CommandHandlerManager(
        workspace=ws,
        binary_path=_DEFAULT_BINARY_PATH,
        idle_timeout=60,
        call_timeout=30.0,
    )
    await manager.ensure_started()
    yield manager
    await manager.stop()


# ---------------------------------------------------------------------------
# Bash tool
# ---------------------------------------------------------------------------


class TestBashIntegration:
    @pytest.mark.asyncio
    async def test_echo(self, live_manager: CommandHandlerManager) -> None:
        result = await live_manager.call_tool("Bash", {"command": "echo hello"})
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_exit_code_in_output(self, live_manager: CommandHandlerManager) -> None:
        result = await live_manager.call_tool("Bash", {"command": "exit 1"})
        assert "exit code" in result.lower() or result == ""

    @pytest.mark.asyncio
    async def test_pwd_is_workspace(self, live_manager: CommandHandlerManager) -> None:
        result = await live_manager.call_tool("Bash", {"command": "pwd"})
        assert live_manager._workspace in result

    @pytest.mark.asyncio
    async def test_bash_timeout(self, live_manager: CommandHandlerManager) -> None:
        """A command that runs too long should be killed and return a timeout message."""
        result = await live_manager.call_tool("Bash", {"command": "sleep 10", "timeout": 1})
        assert "timeout" in result.lower() or "killed" in result.lower() or result == ""

    @pytest.mark.asyncio
    async def test_concurrent_bash(self, live_manager: CommandHandlerManager) -> None:
        """Multiple concurrent Bash calls complete without mixing up responses."""
        import asyncio

        tasks = [live_manager.call_tool("Bash", {"command": f"echo msg{i}"}) for i in range(5)]
        results = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            assert f"msg{i}" in r, f"Expected msg{i} in result {i}: {r!r}"


# ---------------------------------------------------------------------------
# Read tool
# ---------------------------------------------------------------------------


class TestReadIntegration:
    @pytest.mark.asyncio
    async def test_read_file(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        test_file = tmp_path / "workspace" / "hello.txt"
        test_file.write_text("line one\nline two\nline three\n")

        result = await live_manager.call_tool("Read", {"file_path": str(test_file), "offset": 0, "limit": 10})
        assert "line one" in result
        assert "line two" in result

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        missing = str(tmp_path / "workspace" / "does_not_exist.txt")
        with pytest.raises(RuntimeError):
            await live_manager.call_tool("Read", {"file_path": missing})


# ---------------------------------------------------------------------------
# Write tool
# ---------------------------------------------------------------------------


class TestWriteIntegration:
    @pytest.mark.asyncio
    async def test_write_and_read_back(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        target = str(tmp_path / "workspace" / "output.txt")
        content = "written by BCH proxy\n"

        await live_manager.call_tool("Write", {"file_path": target, "content": content})
        assert os.path.isfile(target)
        with open(target) as f:
            assert f.read() == content


# ---------------------------------------------------------------------------
# Edit tool
# ---------------------------------------------------------------------------


class TestEditIntegration:
    @pytest.mark.asyncio
    async def test_edit_replaces_string(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        target = str(tmp_path / "workspace" / "edit_me.txt")
        with open(target, "w") as f:
            f.write("foo bar baz\n")

        await live_manager.call_tool(
            "Edit",
            {"file_path": target, "old_string": "bar", "new_string": "qux", "replace_all": False},
        )
        with open(target) as f:
            assert f.read() == "foo qux baz\n"


# ---------------------------------------------------------------------------
# Glob tool
# ---------------------------------------------------------------------------


class TestGlobIntegration:
    @pytest.mark.asyncio
    async def test_glob_matches_files(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        ws = tmp_path / "workspace"
        (ws / "a.py").write_text("# a")
        (ws / "b.py").write_text("# b")
        (ws / "c.txt").write_text("hello")

        result = await live_manager.call_tool("Glob", {"pattern": "*.py", "path": str(ws)})
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result


# ---------------------------------------------------------------------------
# Grep tool
# ---------------------------------------------------------------------------


class TestGrepIntegration:
    @pytest.mark.asyncio
    async def test_grep_finds_pattern(self, live_manager: CommandHandlerManager, tmp_path) -> None:
        ws = tmp_path / "workspace"
        (ws / "source.py").write_text("def hello():\n    return 'world'\n")

        result = await live_manager.call_tool("Grep", {"pattern": "def hello", "path": str(ws)})
        assert "source.py" in result
        assert "def hello" in result


# ---------------------------------------------------------------------------
# Restart after idle (short timeout)
# ---------------------------------------------------------------------------


class TestIdleRestartIntegration:
    @pytest.mark.asyncio
    async def test_transparent_restart_after_idle(self, tmp_path) -> None:
        """After idle timeout kills the process, next call_tool restarts it."""
        ws = str(tmp_path / "workspace")
        os.makedirs(ws, exist_ok=True)
        manager = CommandHandlerManager(
            workspace=ws,
            binary_path=_DEFAULT_BINARY_PATH,
            idle_timeout=0.2,  # 200 ms — fast idle for testing
            call_timeout=10.0,
        )
        await manager.ensure_started()
        first_pid = manager._proc.pid if manager._proc else None
        assert first_pid is not None

        # Wait for idle timer to fire
        import asyncio

        await asyncio.sleep(0.5)

        # Manager should have killed the process
        assert manager._proc is None

        # Next call should transparently restart
        result = await manager.call_tool("Bash", {"command": "echo restarted"})
        assert "restarted" in result

        await manager.stop()
