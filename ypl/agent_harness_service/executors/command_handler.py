"""Bwrapped Command Handler (BCH) — persistent proxy process for local tool execution.

One long-lived bwrap subprocess per session acts as a multiplexed local tool dispatcher.
All disk/shell tool calls (Bash, Read, Write, Glob, Grep, Edit) can be forwarded to it
over stdin/stdout using a newline-delimited JSON (ndjson) protocol.

Wire protocol:
  Request  (AHS → proxy): {"req_id": "<uuid>", "tool": "Bash", "args": {...}}\\n
  Response (proxy → AHS): {"req_id": "<uuid>", "ok": true,  "result": "..."}\\n
                       or {"req_id": "<uuid>", "ok": false, "error":  "..."}\\n

Concurrent requests are matched by req_id. If the proxy crashes, all pending
futures are resolved with an error and the proxy is transparently restarted on
the next call_tool() invocation.
"""

from __future__ import annotations
import asyncio
import json
import os
import uuid
from asyncio import Future
from typing import Any

from ypl.agent_harness_service.executors.sandbox import bwrap_available
from ypl.structured_logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Binary path
# ---------------------------------------------------------------------------

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_BINARY_PATH = os.path.join(_MODULE_DIR, "bin", "ahs-command-handler")

# ---------------------------------------------------------------------------
# Constants (read from environment in command_handler.py directly;
# AHS_BCH_IDLE_TIMEOUT_SECONDS is also exported from common/constants.py)
# ---------------------------------------------------------------------------

_DEFAULT_IDLE_TIMEOUT = int(os.environ.get("AHS_BCH_IDLE_TIMEOUT_SECONDS", "3600"))
_DEFAULT_CALL_TIMEOUT = float(os.environ.get("AHS_BCH_CALL_TIMEOUT_SECONDS", "120"))


class CommandHandlerManager:
    """One warm bwrapped proxy process per session for local tool execution.

    Spawns the BCH Go binary inside a bubblewrap sandbox configured with the
    same mount set as ``build_bwrap_command()`` in sandbox.py.  Tool calls are
    forwarded over stdin/stdout using ndjson; responses are dispatched back to
    asyncio Futures keyed by req_id.

    Lifecycle:
      - Created alongside the session workspace.
      - ``ensure_started()`` is called lazily on the first ``call_tool()``.
      - Idle timer kills the process after ``idle_timeout`` seconds of inactivity;
        the next call transparently restarts it.
      - ``stop()`` sends SIGTERM and waits for the process to exit gracefully.

    Crash handling:
      - If the proxy exits unexpectedly, all pending futures are resolved with an
        error, ``_proc`` is set to None, and the next ``call_tool()`` restarts the
        process transparently.
    """

    def __init__(
        self,
        workspace: str,
        binary_path: str = _DEFAULT_BINARY_PATH,
        idle_timeout: float | None = None,
        call_timeout: float | None = None,
    ) -> None:
        """Initialise the manager for a session workspace.

        Args:
            workspace: Absolute path to the session workspace directory.
                Used to configure the bwrap sandbox mount set.
            binary_path: Path to the compiled ``ahs-command-handler`` Go binary.
            idle_timeout: Seconds of inactivity before killing the proxy.
                Defaults to ``AHS_BCH_IDLE_TIMEOUT_SECONDS`` (3600).
            call_timeout: Per-call timeout in seconds for awaiting a response.
                Defaults to ``AHS_BCH_CALL_TIMEOUT_SECONDS`` (120).
        """
        self._workspace = workspace
        self._binary_path = binary_path
        self._idle_timeout: float = idle_timeout if idle_timeout is not None else float(_DEFAULT_IDLE_TIMEOUT)
        self._call_timeout: float = call_timeout if call_timeout is not None else _DEFAULT_CALL_TIMEOUT

        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, Future[str]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._idle_timer_task: asyncio.Task[None] | None = None

        # Serialise concurrent ensure_started() calls so only one spawns the process.
        self._start_lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Bwrap command builder
    # -----------------------------------------------------------------------

    def _build_bwrap_command(self) -> list[str]:
        """Build the bwrap command line to launch the BCH proxy binary.

        Mount configuration mirrors ``build_bwrap_command()`` in sandbox.py:
        system binaries (read-only), workspace (read-write — includes the
        materialized ``agent_memories/`` subdir), repo symlink targets
        (read-only), ephemeral /tmp, and namespace isolation (pid/ipc/uts).
        """
        # Local import to avoid circular dependency; sandbox.py is in the
        # same package but doesn't import command_handler.
        from ypl.agent_harness_service.executors.sandbox import (
            _REAL_SKILLS_DIR,
            _bwrap_system_mounts,
            _resolve_workspace_symlinks,
        )

        args: list[str] = ["bwrap"]

        # System mounts (handles merged-usr symlinks on modern distros)
        args += _bwrap_system_mounts()

        # Resolve workspace symlinks — bwrap doesn't follow host symlinks.
        repo_binds = _resolve_workspace_symlinks(self._workspace)
        for real_path, _symlink_path in repo_binds:
            args += ["--ro-bind", real_path, real_path]

        # Skills directory (read-only)
        if os.path.isdir(_REAL_SKILLS_DIR):
            args += ["--ro-bind", _REAL_SKILLS_DIR, _REAL_SKILLS_DIR]

        # Read-write workspace (includes agent_memories/ as a real subdir).
        args += ["--bind", self._workspace, self._workspace]

        # Ephemeral /tmp, minimal /dev and /proc
        args += ["--tmpfs", "/tmp"]
        args += ["--dev", "/dev"]
        args += ["--proc", "/proc"]

        # Namespace isolation (no --unshare-net: bash commands need network)
        args += ["--unshare-pid"]  # PID isolation
        args += ["--unshare-ipc"]  # IPC isolation
        args += ["--unshare-uts"]  # UTS isolation

        # Kill sandbox if parent (harness) dies
        args += ["--die-with-parent"]

        # Working directory
        args += ["--chdir", self._workspace]

        # The BCH Go binary as the sandboxed process
        args += [self._binary_path]

        return args

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """Ensure the BCH proxy is running; spawn it if not.

        Safe to call concurrently — a lock prevents double-spawning.
        """
        async with self._start_lock:
            if self._proc is not None:
                return

            if not bwrap_available():
                raise RuntimeError("bwrap is not available on this host; cannot start CommandHandlerManager")

            if not os.path.isfile(self._binary_path):
                raise RuntimeError(
                    f"BCH binary not found at {self._binary_path!r}. "
                    "Run services/command-handler/build.sh to compile it."
                )

            cmd = self._build_bwrap_command()

            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Background reader — drains stdout and dispatches responses.
            self._reader_task = asyncio.create_task(self._read_responses(), name=f"bch-reader-{self._proc.pid}")

            # Idle timer — kills the process after inactivity.
            self._restart_idle_timer()

            logger.info(
                "CommandHandlerManager started",
                workspace=self._workspace,
                pid=self._proc.pid,
                binary=self._binary_path,
            )

    async def stop(self) -> None:
        """Send SIGTERM to the proxy and await its exit.

        Safe to call if the process is already dead.
        """
        # Cancel timers first so they don't race with shutdown.
        if self._idle_timer_task is not None:
            self._idle_timer_task.cancel()
            self._idle_timer_task = None

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

        proc = self._proc
        self._proc = None

        if proc is None:
            return

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("BCH proxy did not exit after SIGTERM, sending SIGKILL")
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass  # Already dead

        logger.info("CommandHandlerManager stopped", workspace=self._workspace)

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    async def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        """Send a tool call to the BCH proxy and await the result.

        Transparently restarts the proxy if it has been killed by the idle timer
        or crashed unexpectedly.

        Args:
            tool: Tool name (e.g. ``"Bash"``, ``"Read"``, ``"Write"``).
            args: Tool arguments as a dict (e.g. ``{"command": "ls -la"}``).

        Returns:
            The string result from the proxy.

        Raises:
            RuntimeError: If the proxy cannot be started, or if the tool call
                fails (proxies the error message from the Go binary).
            asyncio.TimeoutError: If no response arrives within ``call_timeout``.
        """
        # Ensure the proxy is running — restarts after idle/crash transparently.
        await self.ensure_started()

        # Restart the idle timer on every call.
        self._restart_idle_timer()

        req_id = str(uuid.uuid4())
        request = json.dumps({"req_id": req_id, "tool": tool, "args": args}) + "\n"

        loop = asyncio.get_event_loop()
        future: Future[str] = loop.create_future()
        self._pending[req_id] = future

        # Write the request to stdin.
        assert self._proc is not None
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(request.encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception as exc:
            self._pending.pop(req_id, None)
            if not future.done():
                future.cancel()
            raise RuntimeError(f"Failed to write to BCH proxy stdin: {exc}") from exc

        # Await the response with a per-call timeout.
        try:
            return await asyncio.wait_for(future, timeout=self._call_timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"BCH tool call timed out after {self._call_timeout}s: tool={tool!r}") from exc

    # -----------------------------------------------------------------------
    # Background reader coroutine
    # -----------------------------------------------------------------------

    async def _read_responses(self) -> None:
        """Drain stdout of the proxy and dispatch responses to pending Futures.

        Runs as a background asyncio Task for the lifetime of the process.
        On EOF (process exit), calls _handle_crash() to resolve all pending
        futures with an error.
        """
        if self._proc is None or self._proc.stdout is None:
            # Process was already stopped or never fully started — nothing to read.
            return

        stdout = self._proc.stdout

        try:
            while True:
                line = await stdout.readline()
                if not line:
                    # EOF: process exited (normal or crash).
                    break

                try:
                    response: dict[str, Any] = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "BCH: failed to parse response line",
                        error=str(exc),
                        raw=line[:200],
                    )
                    continue

                req_id = response.get("req_id")
                if not req_id:
                    logger.warning("BCH: response missing req_id", response=str(response)[:200])
                    continue

                future = self._pending.pop(req_id, None)
                if future is None:
                    logger.warning("BCH: no pending future for req_id", req_id=req_id)
                    continue

                if future.done():
                    continue

                if response.get("ok"):
                    future.set_result(str(response.get("result", "")))
                else:
                    error_msg = response.get("error", "BCH proxy returned an error")
                    future.set_exception(RuntimeError(error_msg))

        except asyncio.CancelledError:
            # stop() cancelled this task — normal shutdown path.
            pass
        except Exception as exc:
            logger.error("BCH reader coroutine raised an exception", error=str(exc))
        finally:
            # Resolve all remaining pending futures with a crash error.
            await self._handle_crash()

    # -----------------------------------------------------------------------
    # Crash / idle handling
    # -----------------------------------------------------------------------

    async def _handle_crash(self) -> None:
        """Resolve all pending futures with a crash error and reset state.

        Called from the reader coroutine's finally block when the process exits
        unexpectedly (or is killed by the idle timer).  After this, ``_proc``
        is set to None so the next ``call_tool()`` transparently restarts it.
        """
        pending = dict(self._pending)
        self._pending.clear()

        # Only reset _proc if it wasn't already replaced by a fresh instance
        # (e.g. the idle timer killed the old process and a new one is starting).
        if self._proc is not None:
            try:
                # Non-blocking check: is the process actually dead?
                if self._proc.returncode is not None:
                    self._proc = None
            except Exception:
                self._proc = None

        error = RuntimeError("BCH proxy exited unexpectedly; next call will restart it")
        resolved = 0
        for future in pending.values():
            if not future.done():
                future.set_exception(error)
                resolved += 1

        if resolved:
            logger.warning(
                "BCH crash: resolved pending futures with error",
                count=resolved,
                workspace=self._workspace,
            )

    def _restart_idle_timer(self) -> None:
        """Cancel the existing idle timer and start a fresh one."""
        if self._idle_timer_task is not None and not self._idle_timer_task.done():
            self._idle_timer_task.cancel()

        if self._idle_timeout > 0:
            self._idle_timer_task = asyncio.create_task(self._idle_timeout_runner(), name="bch-idle-timer")

    async def _idle_timeout_runner(self) -> None:
        """Sleep for idle_timeout seconds, then kill the proxy process."""
        try:
            await asyncio.sleep(self._idle_timeout)
        except asyncio.CancelledError:
            return

        if self._proc is None:
            return

        logger.info(
            "BCH idle timeout reached, killing process",
            workspace=self._workspace,
            idle_timeout=self._idle_timeout,
        )
        proc = self._proc
        self._proc = None  # Mark as dead before kill so _handle_crash skips the reset.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
