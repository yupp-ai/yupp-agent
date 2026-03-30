"""Tests for AHS WebSocket streaming — translation, PubSub, and WebSocket endpoint.

Test categories:
1. Unit tests for translate_stream_event() — pure function with mock StreamEvents
2. Unit tests for InMemoryPubSub — asyncio-based fan-out
3. Integration tests for WebSocket endpoint — real FastAPI test server + WS client
"""

from __future__ import annotations
import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from ypl.agent_harness_service.core.streaming import (
    InMemoryPubSub,
    TranslationState,
    WebSocketManager,
    _classify_tool,
    translate_stream_event,
)
from ypl.agent_harness_service.executors.runner import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str, raw: dict[str, Any] | None = None) -> StreamEvent:
    """Create a StreamEvent for testing."""
    return StreamEvent(type=event_type, raw=raw or {})


def _make_state(session_id: str = "test-session", turn_number: int = 1) -> TranslationState:
    """Create a TranslationState for testing."""
    return TranslationState(session_id=session_id, turn_number=turn_number)


# ===========================================================================
# 1. Tool classification tests
# ===========================================================================


class TestClassifyTool:
    def test_bash_is_command_execution(self) -> None:
        assert _classify_tool("Bash") == "command_execution"

    def test_bash_lowercase_is_command_execution(self) -> None:
        assert _classify_tool("bash") == "command_execution"

    def test_run_command_is_command_execution(self) -> None:
        assert _classify_tool("run_command") == "command_execution"

    def test_write_is_file_change(self) -> None:
        assert _classify_tool("Write") == "file_change"

    def test_edit_is_file_change(self) -> None:
        assert _classify_tool("Edit") == "file_change"

    def test_multi_edit_is_file_change(self) -> None:
        assert _classify_tool("MultiEdit") == "file_change"

    def test_unknown_tool_is_mcp(self) -> None:
        assert _classify_tool("search_gcp_logs") == "mcp_tool_call"

    def test_read_is_mcp(self) -> None:
        assert _classify_tool("Read") == "mcp_tool_call"

    def test_glob_is_mcp(self) -> None:
        assert _classify_tool("Glob") == "mcp_tool_call"


# ===========================================================================
# 2. Translation layer tests
# ===========================================================================


class TestTranslateStreamEvent:
    """Unit tests for translate_stream_event()."""

    def test_system_event_produces_nothing(self) -> None:
        """System events are handled at the WS level, not translation."""
        event = _make_event("system", {"session_id": "abc"})
        state = _make_state()
        result = translate_stream_event(event, state)
        assert result == []

    def test_assistant_text_starts_message_item(self) -> None:
        """First assistant text block should produce item.started + delta."""
        event = _make_event(
            "assistant",
            {
                "message": {
                    "content": [{"type": "text", "text": "Hello world"}],
                },
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 2
        assert result[0]["type"] == "item/started"
        assert result[0]["item"]["type"] == "agent_message"
        assert result[0]["item"]["status"] == "in_progress"
        assert result[1]["type"] == "item/agentMessage/delta"
        assert result[1]["delta"] == "Hello world"

    def test_assistant_text_continues_message_item(self) -> None:
        """Second assistant text should only produce a delta (no new item.started)."""
        state = _make_state()
        event1 = _make_event(
            "assistant",
            {
                "message": {"content": [{"type": "text", "text": "Hello "}]},
            },
        )
        event2 = _make_event(
            "assistant",
            {
                "message": {"content": [{"type": "text", "text": "world"}]},
            },
        )
        translate_stream_event(event1, state)
        result = translate_stream_event(event2, state)
        assert len(result) == 1
        assert result[0]["type"] == "item/agentMessage/delta"
        assert result[0]["delta"] == "world"

    def test_assistant_empty_text_is_skipped(self) -> None:
        event = _make_event(
            "assistant",
            {
                "message": {"content": [{"type": "text", "text": ""}]},
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert result == []

    def test_tool_use_bash_produces_command_execution(self) -> None:
        event = _make_event(
            "tool_use",
            {
                "name": "Bash",
                "id": "tu_1",
                "input": {"command": "ls -la"},
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 1
        assert result[0]["type"] == "item/started"
        assert result[0]["item"]["type"] == "command_execution"
        assert result[0]["item"]["command"] == "ls -la"

    def test_tool_use_write_produces_file_change(self) -> None:
        event = _make_event(
            "tool_use",
            {
                "name": "Write",
                "id": "tu_2",
                "input": {"file_path": "/tmp/test.py"},
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 1
        assert result[0]["type"] == "item/started"
        assert result[0]["item"]["type"] == "file_change"
        assert result[0]["item"]["changes"][0]["path"] == "/tmp/test.py"
        assert result[0]["item"]["changes"][0]["kind"] == "add"

    def test_tool_use_edit_produces_file_change_update(self) -> None:
        event = _make_event(
            "tool_use",
            {
                "name": "Edit",
                "id": "tu_3",
                "input": {"file_path": "/tmp/test.py"},
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert result[0]["item"]["changes"][0]["kind"] == "update"

    def test_tool_use_mcp_produces_mcp_tool_call(self) -> None:
        event = _make_event(
            "tool_use",
            {
                "name": "search_gcp_logs",
                "id": "tu_4",
                "input": {"query": "severity>=ERROR"},
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 1
        assert result[0]["type"] == "item/started"
        assert result[0]["item"]["type"] == "mcp_tool_call"
        assert result[0]["item"]["tool"] == "search_gcp_logs"

    def test_tool_result_matches_tool_use(self) -> None:
        """tool_result should produce item.completed for the matching tool_use."""
        state = _make_state()
        # First, emit tool_use
        translate_stream_event(
            _make_event("tool_use", {"name": "Bash", "id": "tu_1", "input": {"command": "ls"}}),
            state,
        )
        # Then, emit tool_result
        result = translate_stream_event(
            _make_event("tool_result", {"tool_use_id": "tu_1", "content": "file1.txt\nfile2.txt"}),
            state,
        )
        assert len(result) == 1
        assert result[0]["type"] == "item/completed"
        assert result[0]["item"]["type"] == "command_execution"
        assert result[0]["item"]["aggregated_output"] == "file1.txt\nfile2.txt"
        assert result[0]["item"]["exit_code"] == 0

    def test_tool_result_with_error(self) -> None:
        state = _make_state()
        translate_stream_event(
            _make_event("tool_use", {"name": "Bash", "id": "tu_1", "input": {"command": "bad"}}),
            state,
        )
        result = translate_stream_event(
            _make_event(
                "tool_result",
                {
                    "tool_use_id": "tu_1",
                    "content": "command not found",
                    "is_error": True,
                },
            ),
            state,
        )
        assert result[0]["item"]["status"] == "failed"
        assert result[0]["item"]["exit_code"] == 1

    def test_tool_result_unmatched_is_ignored(self) -> None:
        """tool_result without a matching tool_use should produce nothing."""
        state = _make_state()
        result = translate_stream_event(
            _make_event("tool_result", {"tool_use_id": "unknown_id", "content": "x"}),
            state,
        )
        assert result == []

    def test_user_tool_result_block_matches_tool_use(self) -> None:
        """Claude user events with tool_result blocks should close the tool item."""
        state = _make_state()
        translate_stream_event(
            _make_event(
                "assistant",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ]
                    }
                },
            ),
            state,
        )

        result = translate_stream_event(
            _make_event(
                "user",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "# README",
                            }
                        ]
                    }
                },
            ),
            state,
        )

        assert len(result) == 1
        assert result[0]["type"] == "item/completed"
        assert result[0]["item"]["type"] == "mcp_tool_call"
        assert result[0]["item"]["tool"] == "Read"
        assert result[0]["item"]["result"] == "# README"
        assert result[0]["item"]["status"] == "completed"

    def test_result_event_produces_turn_completed(self) -> None:
        event = _make_event(
            "result",
            {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cached_input_tokens": 0,
                "cost_usd": 0.01,
                "duration_ms": 5000,
                "num_turns": 3,
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 1
        assert result[0]["type"] == "turn/completed"
        assert result[0]["turn_id"] == "test-session:1"
        assert result[0]["usage"]["cost_usd"] == 0.01
        assert result[0]["usage"]["num_agent_turns"] == 3

    def test_result_closes_open_message_item(self) -> None:
        """If there's an open message item when result arrives, it should be closed first."""
        state = _make_state()
        translate_stream_event(
            _make_event(
                "assistant",
                {
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                },
            ),
            state,
        )
        result = translate_stream_event(_make_event("result", {}), state)
        assert len(result) == 2
        assert result[0]["type"] == "item/completed"
        assert result[0]["item"]["type"] == "agent_message"
        assert result[0]["item"]["text"] == "Done."
        assert result[1]["type"] == "turn/completed"

    def test_error_event_produces_turn_failed(self) -> None:
        event = _make_event("error", {"error": "Context window exceeded"})
        state = _make_state()
        result = translate_stream_event(event, state)
        assert len(result) == 1
        assert result[0]["type"] == "turn/completed"
        assert result[0]["status"] == "failed"
        assert result[0]["error"]["message"] == "Context window exceeded"

    def test_tool_use_closes_open_message_item(self) -> None:
        """Tool use should close any open message item first."""
        state = _make_state()
        translate_stream_event(
            _make_event(
                "assistant",
                {
                    "message": {"content": [{"type": "text", "text": "Let me check"}]},
                },
            ),
            state,
        )
        result = translate_stream_event(
            _make_event("tool_use", {"name": "Bash", "id": "tu_1", "input": {"command": "ls"}}),
            state,
        )
        assert len(result) == 2
        assert result[0]["type"] == "item/completed"
        assert result[0]["item"]["type"] == "agent_message"
        assert result[1]["type"] == "item/started"
        assert result[1]["item"]["type"] == "command_execution"

    def test_full_turn_sequence(self) -> None:
        """Test a complete turn: text → tool → result → text → done."""
        state = _make_state()
        all_events: list[dict] = []

        # Agent says something
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "assistant",
                    {
                        "message": {"content": [{"type": "text", "text": "Checking logs..."}]},
                    },
                ),
                state,
            )
        )

        # Agent uses a tool
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "tool_use",
                    {
                        "name": "search_gcp_logs",
                        "id": "tu_1",
                        "input": {"query": "ERROR"},
                    },
                ),
                state,
            )
        )

        # Tool result
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "tool_result",
                    {
                        "tool_use_id": "tu_1",
                        "content": "Found 5 errors",
                    },
                ),
                state,
            )
        )

        # Agent says something more
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "assistant",
                    {
                        "message": {"content": [{"type": "text", "text": "Found 5 errors."}]},
                    },
                ),
                state,
            )
        )

        # Turn completes
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "result",
                    {
                        "cost_usd": 0.02,
                        "duration_ms": 10000,
                        "num_turns": 5,
                    },
                ),
                state,
            )
        )

        # Verify event sequence
        event_types = [e["type"] for e in all_events]
        assert event_types == [
            "item/started",  # agent_message start
            "item/agentMessage/delta",  # text delta
            "item/completed",  # agent_message end (closed by tool_use)
            "item/started",  # mcp_tool_call start
            "item/completed",  # mcp_tool_call end
            "item/started",  # new agent_message start
            "item/agentMessage/delta",  # text delta
            "item/completed",  # agent_message end (closed by result)
            "turn/completed",  # turn done
        ]

    def test_assistant_with_tool_use_content_blocks(self) -> None:
        """Test assistant event that contains both text and tool_use content blocks."""
        event = _make_event(
            "assistant",
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me run that."},
                        {"type": "tool_use", "name": "Bash", "id": "tu_1", "input": {"command": "echo hi"}},
                    ],
                },
            },
        )
        state = _make_state()
        result = translate_stream_event(event, state)
        types = [e["type"] for e in result]
        # text → item.started (msg) + delta, then tool_use → item.completed (msg) + item.started (cmd)
        assert "item/started" in types
        assert "item/agentMessage/delta" in types

    def test_claude_tool_use_then_user_tool_result_sequence(self) -> None:
        """Claude assistant/user tool frames should produce item.completed before turn completion."""
        state = _make_state()
        all_events: list[dict[str, Any]] = []

        all_events.extend(
            translate_stream_event(
                _make_event(
                    "assistant",
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Checking the file."},
                                {
                                    "type": "tool_use",
                                    "id": "tu_1",
                                    "name": "Read",
                                    "input": {"file_path": "README.md"},
                                },
                            ]
                        }
                    },
                ),
                state,
            )
        )
        all_events.extend(
            translate_stream_event(
                _make_event(
                    "user",
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tu_1",
                                    "content": "README contents",
                                }
                            ]
                        }
                    },
                ),
                state,
            )
        )
        all_events.extend(translate_stream_event(_make_event("result", {}), state))

        event_types = [event["type"] for event in all_events]
        assert event_types == [
            "item/started",
            "item/agentMessage/delta",
            "item/completed",
            "item/started",
            "item/completed",
            "turn/completed",
        ]


# ===========================================================================
# 3. InMemoryPubSub tests
# ===========================================================================


class TestInMemoryPubSub:
    @pytest.mark.asyncio
    async def test_publish_subscribe_basic(self) -> None:
        """Single subscriber receives published messages."""
        pubsub = InMemoryPubSub()
        received: list[str] = []

        async def subscriber() -> None:
            async with pubsub.subscribe("ch1") as messages:
                async for msg in messages:
                    received.append(msg)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)  # Let subscriber start

        await pubsub.publish("ch1", "msg1")
        await pubsub.publish("ch1", "msg2")

        await asyncio.wait_for(task, timeout=2.0)
        assert received == ["msg1", "msg2"]

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self) -> None:
        """Multiple subscribers on the same channel all receive messages."""
        pubsub = InMemoryPubSub()
        received_a: list[str] = []
        received_b: list[str] = []

        async def sub_a() -> None:
            async with pubsub.subscribe("ch") as messages:
                async for msg in messages:
                    received_a.append(msg)
                    if len(received_a) >= 1:
                        break

        async def sub_b() -> None:
            async with pubsub.subscribe("ch") as messages:
                async for msg in messages:
                    received_b.append(msg)
                    if len(received_b) >= 1:
                        break

        task_a = asyncio.create_task(sub_a())
        task_b = asyncio.create_task(sub_b())
        await asyncio.sleep(0.01)

        await pubsub.publish("ch", "hello")

        await asyncio.wait_for(task_a, timeout=2.0)
        await asyncio.wait_for(task_b, timeout=2.0)
        assert received_a == ["hello"]
        assert received_b == ["hello"]

    @pytest.mark.asyncio
    async def test_publish_to_empty_channel(self) -> None:
        """Publishing to a channel with no subscribers is a no-op."""
        pubsub = InMemoryPubSub()
        # Should not raise
        await pubsub.publish("empty", "msg")

    @pytest.mark.asyncio
    async def test_close_channel(self) -> None:
        """Closing a channel terminates all subscribers."""
        pubsub = InMemoryPubSub()
        received: list[str] = []

        async def subscriber() -> None:
            async with pubsub.subscribe("ch") as messages:
                received.extend([msg async for msg in messages])

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        await pubsub.publish("ch", "before_close")
        await asyncio.sleep(0.01)
        await pubsub.close_channel("ch")

        await asyncio.wait_for(task, timeout=2.0)
        assert "before_close" in received

    @pytest.mark.asyncio
    async def test_has_subscribers(self) -> None:
        pubsub = InMemoryPubSub()
        assert not pubsub.has_subscribers("ch")

        # Use an event to cleanly exit the subscriber
        stop = asyncio.Event()

        async def subscriber() -> None:
            async with pubsub.subscribe("ch") as messages:
                stop.set()
                async for _ in messages:
                    break

        task = asyncio.create_task(subscriber())
        await stop.wait()
        assert pubsub.has_subscribers("ch")

        await pubsub.close_channel("ch")
        await asyncio.wait_for(task, timeout=2.0)
        assert not pubsub.has_subscribers("ch")

    @pytest.mark.asyncio
    async def test_subscriber_cleanup_on_context_exit(self) -> None:
        """Subscriber is removed when context manager exits."""
        pubsub = InMemoryPubSub()

        async with pubsub.subscribe("ch"):
            assert pubsub.has_subscribers("ch")

        assert not pubsub.has_subscribers("ch")


# ===========================================================================
# 4. WebSocket integration tests
# ===========================================================================


def _make_test_app(heartbeat_interval: float = 300) -> FastAPI:
    """Create a minimal FastAPI app for WebSocket testing.

    Args:
        heartbeat_interval: Heartbeat interval in seconds. Set high by default to avoid
            heartbeat interference in tests.
    """
    from ypl.agent_harness_service.core.streaming import (
        InMemoryPubSub,
    )

    app = FastAPI()
    pubsub = InMemoryPubSub()
    ws_manager = WebSocketManager(pubsub)
    ws_manager.HEARTBEAT_INTERVAL_S = heartbeat_interval

    @app.websocket("/ahs/session/{session_id}/ws")
    async def ws_endpoint(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        await ws_manager.run_connection(websocket, session_id)

    @app.post("/test/publish/{session_id}")
    async def test_publish(session_id: str, events: list[dict[str, Any]]) -> dict:
        """Test-only endpoint to publish events to a session's PubSub channel."""
        channel = f"ahs:stream:{session_id}"
        for event in events:
            await pubsub.publish(channel, json.dumps(event))
        return {"published": len(events)}

    # Expose pubsub for tests to inject events
    app.state.pubsub = pubsub
    app.state.ws_manager = ws_manager
    return app


class TestWebSocketEndpoint:
    """Integration tests using Starlette's TestClient with WebSocket support."""

    def test_receives_thread_started(self) -> None:
        """Connecting to WS should immediately receive thread.started."""
        app = _make_test_app()

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            data = ws.receive_json()
            assert data["type"] == "thread/started"
            assert data["thread_id"] == "sess-1"
            assert "event_id" in data
            assert data["event_id"] == 1

    def test_receives_published_events(self) -> None:
        """Events published to PubSub should be forwarded to the WebSocket client."""
        app = _make_test_app()

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            # Consume thread.started
            ws.receive_json()

            # Publish via the test HTTP endpoint (runs in the app's event loop)
            import threading

            def publish_via_http() -> None:
                import time

                time.sleep(0.05)
                client.post(
                    "/test/publish/sess-1",
                    json=[
                        {"type": "turn/started", "turn_id": "sess-1:1"},
                    ],
                )

            thread = threading.Thread(target=publish_via_http)
            thread.start()

            data = ws.receive_json(mode="text")
            assert data["type"] == "turn/started"
            assert data["turn_id"] == "sess-1:1"
            assert data["event_id"] == 2

            thread.join()

    def test_client_ping_pong(self) -> None:
        """Client sending ping should receive pong."""
        app = _make_test_app()

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            ws.receive_json()  # thread.started

            ws.send_json({"type": "ping"})
            data = ws.receive_json()
            assert data["type"] == "pong"

    def test_invalid_json_returns_error(self) -> None:
        """Invalid JSON from client should receive an error."""
        app = _make_test_app()

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            ws.receive_json()  # thread.started

            ws.send_text("not valid json")
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Invalid JSON" in data["message"]

    def test_full_turn_over_websocket(self) -> None:
        """Simulate a complete agent turn flowing through the WebSocket."""
        app = _make_test_app()

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            # 1. thread.started
            data = ws.receive_json()
            assert data["type"] == "thread/started"

            # Simulate the server publishing a turn sequence via HTTP endpoint
            import threading

            turn_events = [
                {"type": "turn/started", "turn_id": "sess-1:1"},
                {
                    "type": "item/started",
                    "item": {
                        "id": "item_abc",
                        "type": "agent_message",
                        "text": "",
                        "status": "in_progress",
                    },
                },
                {
                    "type": "item/agentMessage/delta",
                    "item_id": "item_abc",
                    "delta": "Hello from the agent!",
                },
                {
                    "type": "item/completed",
                    "item": {
                        "id": "item_abc",
                        "type": "agent_message",
                        "text": "Hello from the agent!",
                        "status": "completed",
                    },
                },
                {
                    "type": "turn/completed",
                    "turn_id": "sess-1:1",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": 0,
                        "cost_usd": 0.001,
                    },
                },
            ]

            def publish_turn() -> None:
                import time

                time.sleep(0.05)
                client.post("/test/publish/sess-1", json=turn_events)

            thread = threading.Thread(target=publish_turn)
            thread.start()

            # Receive all events
            received = []
            for _ in range(len(turn_events)):
                data = ws.receive_json(mode="text")
                received.append(data)

            thread.join()

            types = [e["type"] for e in received]
            assert types == [
                "turn/started",
                "item/started",
                "item/agentMessage/delta",
                "item/completed",
                "turn/completed",
            ]

            # Verify event_ids are sequential
            event_ids = [e["event_id"] for e in received]
            assert event_ids == [2, 3, 4, 5, 6]  # 1 was thread.started

    def test_user_message_callback(self) -> None:
        """Test that user_message from client triggers the callback."""
        from ypl.agent_harness_service.core.streaming import InMemoryPubSub

        app = FastAPI()
        pubsub = InMemoryPubSub()
        ws_manager = WebSocketManager(pubsub)
        ws_manager.HEARTBEAT_INTERVAL_S = 300
        callback_calls: list[tuple[str, str, str | None, str | None]] = []

        async def on_msg(session_id: str, content: str, user_id: str | None = None, source: str | None = None) -> None:
            callback_calls.append((session_id, content, user_id, source))

        @app.websocket("/ahs/session/{session_id}/ws")
        async def ws_with_cb(websocket: WebSocket, session_id: str) -> None:
            await websocket.accept()
            await ws_manager.run_connection(
                websocket,
                session_id,
                on_user_message=on_msg,
            )

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            ws.receive_json()  # thread.started

            ws.send_json({"type": "user_message", "content": "Hello agent"})
            # Give a moment for the async callback to fire
            import time

            time.sleep(0.2)

        assert len(callback_calls) == 1
        assert callback_calls[0] == ("sess-1", "Hello agent", None, None)

    def test_heartbeat_received(self) -> None:
        """Test that heartbeat messages are sent (using a short interval)."""
        app = _make_test_app(heartbeat_interval=0.2)

        with TestClient(app) as client, client.websocket_connect("/ahs/session/sess-1/ws") as ws:
            ws.receive_json()  # thread.started

            # Wait for heartbeat
            import time

            time.sleep(0.4)
            data = ws.receive_json()
            assert data["type"] == "heartbeat"


# ===========================================================================
# 5. TranslationState tests
# ===========================================================================


class TestTranslationState:
    def test_turn_id_format(self) -> None:
        state = _make_state("my-session", 3)
        assert state.turn_id == "my-session:3"

    def test_item_ids_are_unique(self) -> None:
        state = _make_state()
        ids = {state.next_item_id() for _ in range(100)}
        assert len(ids) == 100

    def test_item_id_format(self) -> None:
        state = _make_state()
        item_id = state.next_item_id()
        assert item_id.startswith("item_")
        assert len(item_id) == 13  # "item_" + 8 hex chars
