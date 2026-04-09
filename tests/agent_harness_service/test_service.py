"""Tests for AHS service helpers."""

import sys
import types
from typing import Any

# `service.py` imports DB models, which currently pull in the Together SDK at
# import time. Stub the minimal surface we need so these unit tests can stay
# focused on the pure helper under test.
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
    croniter_module.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", croniter_module)

from ypl.agent_harness_service.service import _extract_tool_uses  # noqa: E402


class TestExtractToolUses:
    def test_extracts_raw_executor_tool_events(self) -> None:
        raw_events: list[dict[str, Any]] = [
            {
                "type": "tool_use",
                "name": "bash",
                "input": {"command": "pwd"},
                "tool_use_id": "tu_raw",
                "step": 3,
            },
            {
                "type": "tool_result",
                "name": "bash",
                "tool_use_id": "tu_raw",
                "output": "/tmp/workspace",
                "duration_ms": 87,
                "step": 3,
            },
        ]

        tool_uses = _extract_tool_uses(raw_events)

        assert tool_uses is not None
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_use_id == "tu_raw"
        assert tool_uses[0].name == "bash"
        assert tool_uses[0].input == {"command": "pwd"}
        assert tool_uses[0].output == "/tmp/workspace"
        assert tool_uses[0].duration_ms == 87
        assert tool_uses[0].step == 3

    def test_extracts_codex_tool_events(self) -> None:
        raw_events: list[dict[str, Any]] = [
            {
                "type": "tool_use",
                "id": "item_1",
                "tool_name": "Bash",
                "input": "git status",
            },
            {
                "type": "tool_result",
                "tool_use_id": "item_1",
                "content": "On branch main",
            },
        ]

        tool_uses = _extract_tool_uses(raw_events)

        assert tool_uses is not None
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_use_id == "item_1"
        assert tool_uses[0].name == "Bash"
        assert tool_uses[0].input == "git status"
        assert tool_uses[0].output == "On branch main"

    def test_extracts_embedded_claude_tool_frames(self) -> None:
        raw_events: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Checking the file."},
                        {
                            "type": "tool_use",
                            "id": "tu_claude",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_claude",
                            "content": "# README",
                        }
                    ]
                },
            },
        ]

        tool_uses = _extract_tool_uses(raw_events)

        assert tool_uses is not None
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_use_id == "tu_claude"
        assert tool_uses[0].name == "Read"
        assert tool_uses[0].input == {"file_path": "README.md"}
        assert tool_uses[0].output == "# README"

    def test_preserves_tool_call_order_across_event_shapes(self) -> None:
        raw_events: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "First tool."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        },
                    ]
                },
            },
            {
                "type": "tool_use",
                "tool_use_id": "tu_2",
                "name": "bash",
                "input": {"command": "pwd"},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "# README"}],
                        }
                    ]
                },
            },
            {
                "type": "tool_result",
                "tool_use_id": "tu_2",
                "output": "/tmp/workspace",
            },
        ]

        tool_uses = _extract_tool_uses(raw_events)

        assert tool_uses is not None
        assert [tool_use.name for tool_use in tool_uses] == ["Read", "bash"]
        assert [tool_use.output for tool_use in tool_uses] == ["# README", "/tmp/workspace"]
