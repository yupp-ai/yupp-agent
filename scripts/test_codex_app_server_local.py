#!/usr/bin/env python3
"""Local integration test for codex app-server via WebSocket JSON-RPC.

Tests the full lifecycle: server startup, WS connection, thread management,
multi-turn conversation, MCP tool calls, and error recovery.

Prerequisites:
  - `codex` CLI installed and on PATH
  - YUPPSTER_MCP_TOKEN env var set (for MCP tool tests)

Usage:
  python scripts/test_codex_app_server_local.py
  python scripts/test_codex_app_server_local.py --skip-mcp   # skip MCP tests if no token
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import time
from typing import Any

import aiohttp

# ── config ──────────────────────────────────────────────────────────────────

CODEX_BINARY = "codex"
STARTUP_TIMEOUT_S = 15.0
RPC_TIMEOUT_S = 30.0
MSG_TIMEOUT_S = 60.0  # MCP tool calls can be slow


# ── helpers ─────────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class CodexAppServer:
    """Manages a codex app-server subprocess."""

    def __init__(self, port: int, extra_args: list[str] | None = None):
        self.port = port
        self.extra_args = extra_args or []
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        args = [CODEX_BINARY, "app-server", "--listen", f"ws://127.0.0.1:{self.port}"]
        args += self.extra_args
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Wait for /readyz
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        async with aiohttp.ClientSession() as http:
            while time.monotonic() < deadline:
                try:
                    async with http.get(f"http://127.0.0.1:{self.port}/readyz") as resp:
                        if resp.status == 200:
                            return
                except (aiohttp.ClientError, OSError):
                    pass
                await asyncio.sleep(0.3)
        raise TimeoutError(f"codex app-server did not become ready on port {self.port}")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except TimeoutError:
                pass


class WSClient:
    """WebSocket JSON-RPC client for codex app-server."""

    def __init__(self, port: int):
        self.port = port
        self.http: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.req_id = 0
        self.thread_id: str | None = None

    async def connect(self) -> None:
        self.http = aiohttp.ClientSession()
        self.ws = await self.http.ws_connect(f"ws://127.0.0.1:{self.port}", heartbeat=30)

    async def close(self) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.http and not self.http.closed:
            await self.http.close()

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.ws is not None
        msg = {"method": method, "id": self.req_id, "params": params}
        await self.ws.send_str(json.dumps(msg))
        self.req_id += 1
        while True:
            raw = await asyncio.wait_for(self.ws.receive(), timeout=RPC_TIMEOUT_S)
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data: dict[str, Any] = json.loads(raw.data)
            # RPC response (has id, no method)
            if "id" in data and not data.get("method"):
                return data
            # Skip notifications during RPC

    async def initialize(self) -> dict[str, Any]:
        result = await self.rpc(
            "initialize",
            {
                "clientInfo": {"name": "local-test", "version": "1.0"},
                "capabilities": {"experimentalApi": False, "optOutNotificationMethods": []},
            },
        )
        assert self.ws is not None
        await self.ws.send_str(json.dumps({"method": "initialized"}))
        return result

    async def start_thread(self, **extra_params: Any) -> str:
        params: dict[str, Any] = {"approvalPolicy": "never", "sandbox": "workspace-write"}
        params.update(extra_params)
        result = await self.rpc("thread/start", params)
        thread_id = result.get("result", {}).get("thread", {}).get("id", "")
        assert thread_id, f"No thread_id in response: {result}"
        self.thread_id = thread_id
        # Drain thread/started notification
        await self._drain_until("thread/started")
        return thread_id

    async def start_turn(self, prompt: str) -> dict[str, Any]:
        assert self.thread_id is not None
        return await self.rpc(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )

    async def stream_turn(self) -> list[dict[str, Any]]:
        """Stream notifications until turn/completed. Auto-accepts server requests."""
        assert self.ws is not None
        events: list[dict[str, Any]] = []
        for _ in range(200):
            try:
                raw = await asyncio.wait_for(self.ws.receive(), timeout=MSG_TIMEOUT_S)
            except TimeoutError:
                events.append({"_error": "TIMEOUT"})
                break
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    events.append({"_error": "WS_CLOSED"})
                    break
                continue
            data: dict[str, Any] = json.loads(raw.data)
            method = data.get("method", "")
            # Auto-accept server requests (MCP tool approvals)
            if "id" in data and method:
                await self.ws.send_str(json.dumps({"id": data["id"], "result": {"action": "accept"}}))
                events.append({"_server_request": method, "_auto_accepted": True})
                continue
            if method:
                events.append(data)
            if method == "turn/completed":
                break
        return events

    async def _drain_until(self, target_method: str) -> None:
        assert self.ws is not None
        deadline = time.monotonic() + RPC_TIMEOUT_S
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self.ws.receive(), timeout=RPC_TIMEOUT_S)
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(raw.data)
            if data.get("method") == target_method and "id" not in data:
                return
        raise TimeoutError(f"Timed out waiting for '{target_method}'")


# ── test cases ──────────────────────────────────────────────────────────────


def _extract_agent_text(events: list[dict[str, Any]]) -> str:
    """Extract concatenated agentMessage text from stream events."""
    texts = []
    for e in events:
        params = e.get("params", {})
        item = params.get("item", {})
        if e.get("method") == "item/completed" and item.get("type") == "agentMessage":
            texts.append(item.get("text", ""))
    return "\n".join(texts)


def _has_method(events: list[dict[str, Any]], method: str) -> bool:
    return any(e.get("method") == method for e in events)


def _get_turn_status(events: list[dict[str, Any]]) -> str:
    for e in events:
        if e.get("method") == "turn/completed":
            return e.get("params", {}).get("turn", {}).get("status", "")
    return ""


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error: str | None = None
        self.duration_s: float = 0.0

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        err = f" - {self.error}" if self.error else ""
        return f"  [{status}] {self.name} ({self.duration_s:.1f}s){err}"


async def test_basic_turn(client: WSClient) -> TestResult:
    """Basic turn: send a simple math question, expect a response."""
    r = TestResult("basic_turn")
    t0 = time.monotonic()
    try:
        await client.start_turn("What is 2+2? Answer with just the number.")
        events = await client.stream_turn()
        r.duration_s = time.monotonic() - t0

        assert _get_turn_status(events) == "completed", f"Turn not completed: {_get_turn_status(events)}"
        assert _has_method(events, "turn/started"), "Missing turn/started"
        text = _extract_agent_text(events)
        assert "4" in text, f"Expected '4' in response, got: {text[:100]}"
        r.passed = True
    except Exception as e:
        r.error = str(e)
        r.duration_s = time.monotonic() - t0
    return r


async def test_multi_turn(client: WSClient) -> TestResult:
    """Multi-turn: send two turns on the same thread, verify context is maintained."""
    r = TestResult("multi_turn_same_ws")
    t0 = time.monotonic()
    try:
        # Turn 1: set a context
        await client.start_turn("Remember this number: 42. Just acknowledge.")
        events1 = await client.stream_turn()
        assert _get_turn_status(events1) == "completed", "Turn 1 not completed"

        # Turn 2: reference the context
        await client.start_turn("What number did I ask you to remember? Just the number.")
        events2 = await client.stream_turn()
        assert _get_turn_status(events2) == "completed", "Turn 2 not completed"

        text = _extract_agent_text(events2)
        assert "42" in text, f"Expected '42' in turn 2 response, got: {text[:100]}"
        r.passed = True
    except Exception as e:
        r.error = str(e)
    r.duration_s = time.monotonic() - t0
    return r


async def test_bash_tool(client: WSClient) -> TestResult:
    """Bash tool: verify codex can execute a shell command."""
    r = TestResult("bash_tool_execution")
    t0 = time.monotonic()
    try:
        await client.start_turn("Run `echo hello-from-codex` in the shell and tell me the output.")
        events = await client.stream_turn()
        r.duration_s = time.monotonic() - t0

        assert _get_turn_status(events) == "completed", "Turn not completed"
        # Check for commandExecution item
        has_cmd = any(
            e.get("params", {}).get("item", {}).get("type") == "commandExecution"
            for e in events
            if e.get("method") in ("item/started", "item/completed")
        )
        assert has_cmd, "No commandExecution item found"

        text = _extract_agent_text(events)
        assert "hello-from-codex" in text, f"Expected 'hello-from-codex' in response, got: {text[:200]}"
        r.passed = True
    except Exception as e:
        r.error = str(e)
        r.duration_s = time.monotonic() - t0
    return r


async def test_mcp_tool_call(client: WSClient) -> TestResult:
    """MCP tool call: verify search_agent_memory works via yuppster MCP server."""
    r = TestResult("mcp_tool_call")
    t0 = time.monotonic()
    try:
        await client.start_turn(
            "Use the search_agent_memory tool to search for 'test-integration-check'. Report the raw result."
        )
        events = await client.stream_turn()
        r.duration_s = time.monotonic() - t0

        assert _get_turn_status(events) == "completed", "Turn not completed"

        # Verify MCP tool was called
        mcp_started = any(
            e.get("params", {}).get("item", {}).get("type") == "mcpToolCall"
            for e in events
            if e.get("method") == "item/started"
        )
        assert mcp_started, "No mcpToolCall item found — MCP tool was not invoked"

        # Verify the MCP tool completed (not failed)
        mcp_completed = [
            e
            for e in events
            if e.get("method") == "item/completed" and e.get("params", {}).get("item", {}).get("type") == "mcpToolCall"
        ]
        assert mcp_completed, "No mcpToolCall completion found"
        status = mcp_completed[0].get("params", {}).get("item", {}).get("status", "")
        assert status == "completed", f"MCP tool call status: {status} (expected 'completed')"

        # Verify server request was auto-accepted
        accepted = any(e.get("_server_request") == "mcpServer/elicitation/request" for e in events)
        assert accepted, "No mcpServer/elicitation/request was received — approval flow may have changed"

        r.passed = True
    except Exception as e:
        r.error = str(e)
        r.duration_s = time.monotonic() - t0
    return r


async def test_ws_reconnect(server: CodexAppServer) -> TestResult:
    """WS reconnect: close WS, reconnect, verify server still works."""
    r = TestResult("ws_reconnect")
    t0 = time.monotonic()
    try:
        # First connection
        client1 = WSClient(server.port)
        await client1.connect()
        await client1.initialize()
        await client1.start_thread()
        await client1.start_turn("Say 'first'.")
        events1 = await client1.stream_turn()
        assert _get_turn_status(events1) == "completed", "First turn not completed"
        await client1.close()

        # Second connection to the SAME server process
        client2 = WSClient(server.port)
        await client2.connect()
        await client2.initialize()
        await client2.start_thread()
        await client2.start_turn("Say 'second'.")
        events2 = await client2.stream_turn()
        assert _get_turn_status(events2) == "completed", "Second turn (after reconnect) not completed"

        text = _extract_agent_text(events2)
        assert "second" in text.lower(), f"Expected 'second' in response, got: {text[:100]}"
        await client2.close()

        r.passed = True
    except Exception as e:
        r.error = str(e)
    r.duration_s = time.monotonic() - t0
    return r


# ── main ────────────────────────────────────────────────────────────────────


async def main(skip_mcp: bool = False) -> int:
    port = _find_free_port()
    mcp_args: list[str] = []

    has_mcp_token = bool(os.environ.get("YUPPSTER_MCP_TOKEN"))
    if not skip_mcp and has_mcp_token:
        mcp_args = [
            "-c",
            'mcp_servers.yuppster-mcp-server.url="https://yuppster-mcp.yupp.ai/mcp"',
            "-c",
            'mcp_servers.yuppster-mcp-server.bearer_token_env_var="YUPPSTER_MCP_TOKEN"',
        ]
    elif not skip_mcp and not has_mcp_token:
        print("WARNING: YUPPSTER_MCP_TOKEN not set — skipping MCP tests")
        skip_mcp = True

    server = CodexAppServer(port, extra_args=mcp_args)

    print(f"Starting codex app-server on port {port}...")
    try:
        await server.start()
    except TimeoutError:
        print("FATAL: codex app-server failed to start. Is `codex` installed?")
        return 1
    print(f"Server ready (pid={server.proc.pid if server.proc else '?'})\n")

    results: list[TestResult] = []

    try:
        # Tests that share a single WS connection + thread
        client = WSClient(port)
        await client.connect()
        await client.initialize()
        await client.start_thread()

        print("Running tests...\n")

        # 1. Basic turn
        r = await test_basic_turn(client)
        results.append(r)
        print(r)

        # 2. Multi-turn (same WS, same thread)
        r = await test_multi_turn(client)
        results.append(r)
        print(r)

        # 3. Bash tool execution
        r = await test_bash_tool(client)
        results.append(r)
        print(r)

        # 4. MCP tool call
        if not skip_mcp:
            r = await test_mcp_tool_call(client)
            results.append(r)
            print(r)
        else:
            print("  [SKIP] mcp_tool_call")

        await client.close()

        # 5. WS reconnect (uses its own connections)
        r = await test_ws_reconnect(server)
        results.append(r)
        print(r)

    except Exception as e:
        print(f"\nFATAL: Unhandled error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await server.stop()

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    total_time = sum(r.duration_s for r in results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed ({total_time:.1f}s total)")
    if passed < total:
        print("FAILED tests:")
        for r in results:
            if not r.passed:
                print(f"  - {r.name}: {r.error}")
    print(f"{'=' * 40}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local integration test for codex app-server")
    parser.add_argument("--skip-mcp", action="store_true", help="Skip MCP tool call tests")
    args = parser.parse_args()

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))

    sys.exit(asyncio.run(main(skip_mcp=args.skip_mcp)))
