"""Unit tests for AHS service/ wiring layer — session lifecycle, task execution.

Target: push service/ coverage to ≥ 50%.

Coverage strategy:
- state.py          — pure functions, env-var parsing, semaphore lazy-init
- message_helpers.py — pure data-sanitization helpers, visible-content extraction
- resolvers.py      — _prepend_attachment_paths, _load_agent_config_with_db_fallback,
                       _resolve_session (UUID + slack fallback)
- run_task.py       — EagerPersistState, _determine_result_status, _format_tool_command
- session_lifecycle.py — _validate_force_model, _drain_pending_messages,
                          _maybe_update_task_completion, deliver_subagent_result_to_parent
                          (queue vs inject path)
- queries.py        — _build_agent_info_from_db, _build_session_info

Heavy mocking for all orchestration / DB / GCS / Slack dependencies.
"""

from __future__ import annotations
import asyncio
import os
import sys
import types
import uuid
from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy SDK deps so service modules can be imported in a plain test env.
# Same pattern as test_service.py / test_service_bch_lifecycle.py.
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
    import croniter as _real_croniter  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
except ImportError:
    _croniter.croniter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules.setdefault("croniter", _croniter)


# ===========================================================================
# Tests: state.py
# ===========================================================================


class TestParseEnvInt:
    """_parse_env_int returns sensible defaults and handles bad values."""

    def test_default_returned_when_env_unset(self) -> None:
        from ypl.agent_harness_service.service.state import _parse_env_int

        key = "__AHS_TEST_PARSE_ABSENT__"
        os.environ.pop(key, None)
        assert _parse_env_int(key, 42) == 42

    def test_valid_env_var_parsed(self) -> None:
        from ypl.agent_harness_service.service.state import _parse_env_int

        key = "__AHS_TEST_PARSE_VAR__"
        os.environ[key] = "7"
        try:
            assert _parse_env_int(key, 42) == 7
        finally:
            os.environ.pop(key, None)

    def test_non_integer_env_var_falls_back_to_default(self) -> None:
        from ypl.agent_harness_service.service.state import _parse_env_int

        key = "__AHS_TEST_PARSE_BAD__"
        os.environ[key] = "not-a-number"
        try:
            assert _parse_env_int(key, 99) == 99
        finally:
            os.environ.pop(key, None)

    def test_zero_falls_back_to_default(self) -> None:
        from ypl.agent_harness_service.service.state import _parse_env_int

        key = "__AHS_TEST_PARSE_ZERO__"
        os.environ[key] = "0"
        try:
            assert _parse_env_int(key, 5) == 5
        finally:
            os.environ.pop(key, None)

    def test_negative_falls_back_to_default(self) -> None:
        from ypl.agent_harness_service.service.state import _parse_env_int

        key = "__AHS_TEST_PARSE_NEG__"
        os.environ[key] = "-3"
        try:
            assert _parse_env_int(key, 5) == 5
        finally:
            os.environ.pop(key, None)


class TestCapacityChecks:
    """get_active_turn_count and has_execution_capacity reflect _active_tasks state."""

    def setup_method(self) -> None:
        from ypl.agent_harness_service.service import state

        self._state = state
        state._active_tasks.clear()

    def teardown_method(self) -> None:
        self._state._active_tasks.clear()

    def test_active_turn_count_zero_when_empty(self) -> None:
        from ypl.agent_harness_service.service.state import get_active_turn_count

        assert get_active_turn_count() == 0

    def test_active_turn_count_reflects_tasks(self) -> None:
        from ypl.agent_harness_service.service.state import _active_tasks, get_active_turn_count

        sid = uuid.uuid4()
        _active_tasks[sid] = MagicMock()
        try:
            assert get_active_turn_count() == 1
        finally:
            _active_tasks.pop(sid, None)

    def test_has_execution_capacity_true_when_empty(self) -> None:
        from ypl.agent_harness_service.service.state import has_execution_capacity

        assert has_execution_capacity() is True

    def test_has_execution_capacity_false_when_at_limit(self) -> None:
        from ypl.agent_harness_service.service.state import (
            MAX_CONCURRENT_EXECUTIONS,
            _active_tasks,
            has_execution_capacity,
        )

        added: list[uuid.UUID] = []
        for _ in range(MAX_CONCURRENT_EXECUTIONS):
            sid = uuid.uuid4()
            _active_tasks[sid] = MagicMock()
            added.append(sid)

        try:
            assert has_execution_capacity() is False
        finally:
            for sid in added:
                _active_tasks.pop(sid, None)


class TestSemaphoreLazyInit:
    """Semaphores are created lazily inside the event loop."""

    def setup_method(self) -> None:
        import ypl.agent_harness_service.service.state as _s

        self._orig_cc = _s._claude_code_semaphore
        self._orig_cx = _s._codex_semaphore

    def teardown_method(self) -> None:
        import ypl.agent_harness_service.service.state as _s

        _s._claude_code_semaphore = self._orig_cc
        _s._codex_semaphore = self._orig_cx

    def test_get_claude_code_semaphore_returns_semaphore(self) -> None:
        import ypl.agent_harness_service.service.state as _state_mod

        _state_mod._claude_code_semaphore = None  # reset lazy state
        sem = _state_mod.get_claude_code_semaphore()
        assert isinstance(sem, asyncio.Semaphore)
        # Idempotent — same object returned on second call
        assert _state_mod.get_claude_code_semaphore() is sem

    def test_get_codex_semaphore_returns_semaphore(self) -> None:
        import ypl.agent_harness_service.service.state as _state_mod

        _state_mod._codex_semaphore = None  # reset lazy state
        sem = _state_mod.get_codex_semaphore()
        assert isinstance(sem, asyncio.Semaphore)
        assert _state_mod.get_codex_semaphore() is sem


class TestPendingMessageDataclass:
    """PendingMessage is a simple dataclass with sensible defaults."""

    def test_defaults(self) -> None:
        from ypl.agent_harness_service.service.state import PendingMessage

        pm = PendingMessage(message="hello")
        assert pm.message == "hello"
        assert pm.slack_ts is None
        assert pm.source == "api"
        assert pm.attachments == []
        assert pm.queued_at > 0  # time.time() was called

    def test_custom_fields(self) -> None:
        from ypl.agent_harness_service.service.state import PendingMessage

        pm = PendingMessage(
            message="test",
            slack_ts="1234.5678",
            slack_user_id="U123",
            user_id="user-abc",
            source="slack",
        )
        assert pm.slack_ts == "1234.5678"
        assert pm.slack_user_id == "U123"
        assert pm.user_id == "user-abc"
        assert pm.source == "slack"


class TestStateConstants:
    """State module constants are correct types and values."""

    def test_task_failure_subtypes_is_frozenset(self) -> None:
        from ypl.agent_harness_service.service.state import _TASK_FAILURE_SUBTYPES

        assert isinstance(_TASK_FAILURE_SUBTYPES, frozenset)
        assert "error" in _TASK_FAILURE_SUBTYPES
        assert "error_max_turns" in _TASK_FAILURE_SUBTYPES
        assert "stopped_context_overflow" in _TASK_FAILURE_SUBTYPES

    def test_personal_agent_prefixes_is_frozenset(self) -> None:
        from ypl.agent_harness_service.service.state import PERSONAL_AGENT_PREFIXES

        assert isinstance(PERSONAL_AGENT_PREFIXES, frozenset)
        assert "yuppclaw" in PERSONAL_AGENT_PREFIXES

    def test_max_pending_messages_is_positive_int(self) -> None:
        from ypl.agent_harness_service.service.state import _MAX_PENDING_MESSAGES

        assert isinstance(_MAX_PENDING_MESSAGES, int)
        assert _MAX_PENDING_MESSAGES > 0

    def test_gateway_append_threshold_is_positive_float(self) -> None:
        from ypl.agent_harness_service.service.state import _GATEWAY_APPEND_THRESHOLD_SECONDS

        assert isinstance(_GATEWAY_APPEND_THRESHOLD_SECONDS, float)
        assert _GATEWAY_APPEND_THRESHOLD_SECONDS > 0


# ===========================================================================
# Tests: message_helpers.py
# ===========================================================================


class TestScrubNullBytes:
    """_scrub_null_bytes removes \\x00 from nested structures."""

    def test_strips_null_from_string(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes("hel\x00lo") == "hello"

    def test_strips_null_from_dict_values(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        result = _scrub_null_bytes({"key": "val\x00ue"})
        assert result == {"key": "value"}

    def test_strips_null_from_nested_list(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        result = _scrub_null_bytes(["a\x00b", {"c": "d\x00e"}])
        assert result == ["ab", {"c": "de"}]

    def test_passthrough_non_string_int(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes(42) == 42

    def test_passthrough_none(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes(None) is None

    def test_empty_string_unchanged(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes("") == ""

    def test_no_null_string_unchanged(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes("no nulls here") == "no nulls here"

    def test_multiple_nulls_all_removed(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _scrub_null_bytes

        assert _scrub_null_bytes("a\x00b\x00c") == "abc"


class TestTrimValue:
    """_trim_value truncates long strings in known keys."""

    def test_trims_long_text_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value
        from ypl.agent_harness_service.service.state import _TRIM_MAX_LEN, _TRIM_SUFFIX

        long_str = "x" * 200
        result = _trim_value({"text": long_str})
        assert isinstance(result, dict)
        trimmed = result["text"]
        assert trimmed.endswith(_TRIM_SUFFIX)
        assert len(trimmed) == _TRIM_MAX_LEN + len(_TRIM_SUFFIX)

    def test_trims_content_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        result = _trim_value({"content": "y" * 200})
        assert len(result["content"]) < 200

    def test_trims_prompt_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        result = _trim_value({"prompt": "z" * 200})
        assert len(result["prompt"]) < 200

    def test_does_not_trim_short_value(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        result = _trim_value({"text": "short"})
        assert result["text"] == "short"

    def test_non_trim_key_not_truncated(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        long_str = "y" * 200
        result = _trim_value({"other_key": long_str})
        assert result["other_key"] == long_str

    def test_handles_nested_list_of_dicts(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        nested = [{"text": "x" * 200}]
        result = _trim_value(nested)
        assert isinstance(result, list)
        assert len(result[0]["text"]) < 200

    def test_passthrough_non_collection(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _trim_value

        assert _trim_value(99) == 99


class TestStripThinkingTags:
    """strip_thinking_tags removes <thinking>/<think> blocks."""

    def test_removes_thinking_block(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        result = strip_thinking_tags("<thinking>internal reasoning</thinking>answer")
        assert "internal reasoning" not in result
        assert "answer" in result

    def test_removes_think_block(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        result = strip_thinking_tags("<think>step by step</think>final")
        assert "step by step" not in result
        assert "final" in result

    def test_case_insensitive(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        result = strip_thinking_tags("<THINKING>hidden</THINKING>visible")
        assert "hidden" not in result
        assert "visible" in result

    def test_multiline_thinking_block(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        text = "<thinking>\nline1\nline2\n</thinking>answer"
        result = strip_thinking_tags(text)
        assert "line1" not in result
        assert "answer" in result

    def test_no_tags_unchanged(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        text = "plain text with no tags"
        assert strip_thinking_tags(text) == text

    def test_empty_string(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        assert strip_thinking_tags("") == ""

    def test_only_thinking_tag_yields_empty(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import strip_thinking_tags

        result = strip_thinking_tags("<thinking>all internal</thinking>")
        assert result == ""


class TestExtractVisibleContent:
    """_extract_visible_content extracts text and marks outlet tools correctly.

    StreamEvent.text is a computed property derived from raw["message"]["content"],
    not a constructor arg.  Build raw dicts that mirror the actual event format.
    """

    def _make_assistant_event(self, text: str) -> Any:
        """Build a StreamEvent with type='assistant' carrying the given text."""
        from ypl.agent_harness_service.common.types import StreamEvent

        raw: dict = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
        return StreamEvent(type="assistant", raw=raw)

    def test_assistant_text_not_already_delivered(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        event = self._make_assistant_event("Hello!")
        results = _extract_visible_content(event, set())
        assert len(results) == 1
        assert results[0].text == "Hello!"
        assert results[0].already_delivered is False

    def test_thinking_stripped_from_assistant_text(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        event = self._make_assistant_event("<thinking>private</thinking>public reply")
        results = _extract_visible_content(event, set())
        assert len(results) == 1
        assert "private" not in results[0].text
        assert "public reply" in results[0].text

    def test_pure_thinking_yields_no_content(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        event = self._make_assistant_event("<thinking>all internal</thinking>")
        results = _extract_visible_content(event, set())
        assert results == []

    def test_empty_assistant_text_yields_nothing(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        event = self._make_assistant_event("")
        results = _extract_visible_content(event, set())
        assert results == []

    def test_non_assistant_event_yields_nothing(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        event = StreamEvent(type="tool_result", raw={"type": "tool_result"})
        results = _extract_visible_content(event, set())
        assert results == []

    def test_send_slack_message_marked_already_delivered(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        raw = {
            "type": "tool_use",
            "id": "tu_1",
            "name": "send_slack_message",
            "input": {"text": "hello slack"},
        }
        event = StreamEvent(type="tool_use", raw=raw)
        seen: set[str] = set()
        results = _extract_visible_content(event, seen)
        assert len(results) == 1
        assert results[0].already_delivered is True
        assert results[0].text == "hello slack"
        assert "tu_1" in seen

    def test_outlet_tool_deduplicated_by_seen_ids(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        raw = {
            "type": "tool_use",
            "id": "tu_dup",
            "name": "send_slack_message",
            "input": {"text": "dupe"},
        }
        event = StreamEvent(type="tool_use", raw=raw)
        seen: set[str] = {"tu_dup"}  # already seen
        results = _extract_visible_content(event, seen)
        assert results == []

    def test_outlet_tool_without_text_yields_nothing(self) -> None:
        from ypl.agent_harness_service.common.types import StreamEvent
        from ypl.agent_harness_service.service.message_helpers import _extract_visible_content

        raw = {
            "type": "tool_use",
            "id": "tu_3",
            "name": "send_slack_message",
            "input": {},  # no "text" key
        }
        event = StreamEvent(type="tool_use", raw=raw)
        results = _extract_visible_content(event, set())
        assert results == []


class TestIterToolBlocks:
    """_iter_tool_use_blocks and _iter_tool_result_blocks extract blocks correctly."""

    def test_standalone_tool_use_events(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _iter_tool_use_blocks

        events: list[dict[str, Any]] = [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": "ls"},
        ]
        blocks = _iter_tool_use_blocks(events)
        assert len(blocks) == 1
        assert blocks[0]["name"] == "Bash"

    def test_embedded_tool_use_in_assistant_event(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _iter_tool_use_blocks

        events: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {}},
                    ]
                },
            }
        ]
        blocks = _iter_tool_use_blocks(events)
        assert len(blocks) == 1
        assert blocks[0]["name"] == "Read"

    def test_non_matching_events_skipped(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _iter_tool_use_blocks

        events: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": "tu_1", "output": "done"},
        ]
        blocks = _iter_tool_use_blocks(events)
        assert blocks == []

    def test_standalone_tool_result_events(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _iter_tool_result_blocks

        events: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": "tu_1", "output": "/tmp"},
        ]
        blocks = _iter_tool_result_blocks(events)
        assert len(blocks) == 1
        assert blocks[0]["output"] == "/tmp"

    def test_embedded_tool_result_in_user_event(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _iter_tool_result_blocks

        events: list[dict[str, Any]] = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_3", "content": "ok"},
                    ]
                },
            }
        ]
        blocks = _iter_tool_result_blocks(events)
        assert len(blocks) == 1
        assert blocks[0]["content"] == "ok"

    def test_empty_events_returns_empty_list(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import (
            _iter_tool_result_blocks,
            _iter_tool_use_blocks,
        )

        assert _iter_tool_use_blocks([]) == []
        assert _iter_tool_result_blocks([]) == []


class TestExtractToolHelpers:
    """_extract_tool_use_id/name/input/output normalise across event shapes."""

    def test_tool_use_id_from_tool_use_id_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_use_id

        assert _extract_tool_use_id({"tool_use_id": "tu_a"}) == "tu_a"

    def test_tool_use_id_fallback_to_id_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_use_id

        assert _extract_tool_use_id({"id": "tu_b"}) == "tu_b"

    def test_tool_use_id_empty_when_missing(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_use_id

        assert _extract_tool_use_id({}) == ""

    def test_tool_name_from_name_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_name

        assert _extract_tool_name({"name": "Bash"}) == "Bash"

    def test_tool_name_fallback_to_tool_name_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_name

        assert _extract_tool_name({"tool_name": "Read"}) == "Read"

    def test_tool_name_empty_when_missing(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_name

        assert _extract_tool_name({}) == ""

    def test_tool_input_from_input_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_input

        assert _extract_tool_input({"input": "ls"}) == "ls"

    def test_tool_input_fallback_to_tool_input_key(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_input

        assert _extract_tool_input({"tool_input": {"x": 1}}) == {"x": 1}

    def test_tool_input_none_when_missing(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_input

        assert _extract_tool_input({}) is None

    def test_tool_output_list_joined(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_output

        payload = {"content": [{"text": "hello"}, {"text": "world"}]}
        result = _extract_tool_output(payload)
        assert result is not None
        assert "hello" in result
        assert "world" in result

    def test_tool_output_string_passthrough(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_output

        assert _extract_tool_output({"output": "done"}) == "done"

    def test_tool_output_truncation(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_output
        from ypl.agent_harness_service.service.state import _TOOL_OUTPUT_MAX_CHARS

        long_output = "x" * (_TOOL_OUTPUT_MAX_CHARS + 100)
        result = _extract_tool_output({"output": long_output})
        assert result is not None
        assert "chars" in result  # truncation marker

    def test_tool_output_none_when_no_output_or_content(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _extract_tool_output

        result = _extract_tool_output({})
        # Output is None → _truncate_tool_output(None) = None
        assert result is None


class TestTruncateToolOutput:
    """_truncate_tool_output caps at _TOOL_OUTPUT_MAX_CHARS."""

    def test_short_output_unchanged(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _truncate_tool_output

        assert _truncate_tool_output("hello") == "hello"

    def test_none_returns_none(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _truncate_tool_output

        assert _truncate_tool_output(None) is None

    def test_long_output_truncated(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _truncate_tool_output
        from ypl.agent_harness_service.service.state import _TOOL_OUTPUT_MAX_CHARS

        long_str = "a" * (_TOOL_OUTPUT_MAX_CHARS + 50)
        result = _truncate_tool_output(long_str)
        assert result is not None
        assert len(result) < len(long_str)
        assert "chars" in result

    def test_non_string_coerced(self) -> None:
        from ypl.agent_harness_service.service.message_helpers import _truncate_tool_output

        result = _truncate_tool_output(42)
        assert result == "42"


# ===========================================================================
# Tests: resolvers.py
# ===========================================================================


class TestPrependAttachmentPaths:
    """_prepend_attachment_paths is a pure string helper."""

    def test_no_paths_returns_original_message(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _prepend_attachment_paths

        assert _prepend_attachment_paths("hello", []) == "hello"

    def test_single_path_prepended(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _prepend_attachment_paths

        result = _prepend_attachment_paths("check this", ["/data/ahs/sessions/x/attachments/file.pdf"])
        assert result.startswith("[Attached files:")
        assert "file.pdf" in result
        assert "check this" in result

    def test_multiple_paths_comma_joined(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _prepend_attachment_paths

        result = _prepend_attachment_paths("msg", ["/path/a.txt", "/path/b.jpg"])
        assert "a.txt" in result
        assert "b.jpg" in result

    def test_message_placed_after_header(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _prepend_attachment_paths

        result = _prepend_attachment_paths("the message", ["/p/file.txt"])
        # Header appears before message
        assert result.index("[Attached files:") < result.index("the message")


class TestLoadAgentConfigWithDbFallback:
    """_load_agent_config_with_db_fallback tries disk first then DB."""

    @pytest.mark.asyncio
    async def test_returns_disk_config_when_found(self) -> None:
        """When load_agent_config() hits, DB is never queried."""
        from ypl.agent_harness_service.service.resolvers import _load_agent_config_with_db_fallback

        mock_cfg = MagicMock()
        with patch(
            "ypl.agent_harness_service.service.resolvers.load_agent_config",
            return_value=mock_cfg,
        ):
            result = await _load_agent_config_with_db_fallback("sre")
        assert result is mock_cfg

    @pytest.mark.asyncio
    async def test_returns_none_when_disk_misses_and_db_empty(self) -> None:
        """Both disk and DB miss → returns None without raising."""
        from ypl.agent_harness_service.service.resolvers import _load_agent_config_with_db_fallback

        # Build a full async context manager mock for get_async_session
        db_mock = AsyncMock()
        db_mock.__aenter__ = AsyncMock(return_value=db_mock)
        db_mock.__aexit__ = AsyncMock(return_value=None)
        exec_result = MagicMock()
        exec_result.one_or_none = MagicMock(return_value=None)
        db_mock.exec = AsyncMock(return_value=exec_result)

        with (
            patch(
                "ypl.agent_harness_service.service.resolvers.load_agent_config",
                return_value=None,
            ),
            patch(
                "ypl.agent_harness_service.service.resolvers.get_async_session",
                return_value=db_mock,
            ),
        ):
            result = await _load_agent_config_with_db_fallback("nonexistent-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_db_exception_gracefully(self) -> None:
        """If DB query raises, returns None instead of propagating."""
        from ypl.agent_harness_service.service.resolvers import _load_agent_config_with_db_fallback

        db_mock = AsyncMock()
        db_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("connection refused"))
        db_mock.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "ypl.agent_harness_service.service.resolvers.load_agent_config",
                return_value=None,
            ),
            patch(
                "ypl.agent_harness_service.service.resolvers.get_async_session",
                return_value=db_mock,
            ),
        ):
            result = await _load_agent_config_with_db_fallback("broken-agent")
        assert result is None


class TestResolveSession:
    """_resolve_session tries UUID lookup first, then slack_session_id."""

    @pytest.mark.asyncio
    async def test_valid_uuid_resolved(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _resolve_session

        mock_session = AsyncMock()
        fake_session = MagicMock()
        exec_result = MagicMock()
        exec_result.one_or_none = MagicMock(return_value=fake_session)
        mock_session.exec = AsyncMock(return_value=exec_result)

        sid = str(uuid.uuid4())
        result = await _resolve_session(mock_session, sid)
        assert result is fake_session

    @pytest.mark.asyncio
    async def test_invalid_uuid_falls_back_to_slack(self) -> None:
        """Non-UUID session_id tries the slack_session_id path."""
        from ypl.agent_harness_service.service.resolvers import _resolve_session

        mock_session = AsyncMock()
        fake_slack_session = MagicMock()
        exec_result = MagicMock()
        exec_result.one_or_none = MagicMock(return_value=fake_slack_session)
        mock_session.exec = AsyncMock(return_value=exec_result)

        result = await _resolve_session(mock_session, "not-a-uuid")
        assert result is fake_slack_session

    @pytest.mark.asyncio
    async def test_uuid_not_found_returns_none(self) -> None:
        from ypl.agent_harness_service.service.resolvers import _resolve_session

        mock_session = AsyncMock()
        exec_result = MagicMock()
        exec_result.one_or_none = MagicMock(return_value=None)
        mock_session.exec = AsyncMock(return_value=exec_result)

        sid = str(uuid.uuid4())
        result = await _resolve_session(mock_session, sid)
        assert result is None
        # UUID miss falls through to slack_session_id fallback — exec must be called twice
        assert mock_session.exec.call_count == 2


# ===========================================================================
# Tests: run_task.py — EagerPersistState
# ===========================================================================


class TestEagerPersistState:
    """EagerPersistState tracks incremental content during agent execution."""

    def test_initial_state(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState()
        assert state.msg_id is None
        assert state.tool_call_count == 0
        assert state.last_persisted_tool_count == 0
        assert state.content_parts == []
        assert state.pending_tool_names == []

    def test_build_content_empty_returns_started(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState()
        assert state.build_content() == "[started]"

    def test_build_content_with_single_part(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState(content_parts=["Hello"])
        assert state.build_content() == "Hello"

    def test_build_content_with_multiple_parts(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState(content_parts=["Hello", "World"])
        assert state.build_content() == "Hello\n\nWorld"

    def test_flush_pending_tools_clears_names(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState(pending_tool_names=["Bash", "Read", "Grep"])
        state.flush_pending_tools()
        assert state.pending_tool_names == []

    def test_flush_does_not_affect_other_fields(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState(
            tool_call_count=5,
            last_persisted_tool_count=3,
            pending_tool_names=["Bash"],
        )
        state.flush_pending_tools()
        assert state.tool_call_count == 5
        assert state.last_persisted_tool_count == 3

    def test_flush_idempotent_on_empty(self) -> None:
        from ypl.agent_harness_service.service.run_task import EagerPersistState

        state = EagerPersistState()
        state.flush_pending_tools()  # should not raise
        assert state.pending_tool_names == []


# ===========================================================================
# Tests: run_task.py — _determine_result_status
# ===========================================================================


class TestDetermineResultStatus:
    """_determine_result_status classifies tool results as done/empty/failed."""

    def test_error_string_returns_failed(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status("something went wrong", is_error=True)
        assert status == "failed"
        assert err is not None  # short error summary extracted

    def test_error_list_extracts_text(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        output = [{"text": "file not found"}]
        status, err = _determine_result_status(output, is_error=True)
        assert status == "failed"
        assert err == "file not found"

    def test_error_with_empty_string_returns_none_message(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status("", is_error=True)
        assert status == "failed"
        assert err is None

    def test_none_output_returns_empty(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status(None, is_error=False)
        assert status == "empty"
        assert err is None

    def test_whitespace_string_returns_empty(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status("   ", is_error=False)
        assert status == "empty"
        assert err is None

    def test_empty_list_returns_empty(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status([], is_error=False)
        assert status == "empty"
        assert err is None

    def test_valid_string_returns_done(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status("result text", is_error=False)
        assert status == "done"
        assert err is None

    def test_valid_list_returns_done(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        status, err = _determine_result_status(["item"], is_error=False)
        assert status == "done"
        assert err is None

    def test_error_truncates_message_at_50_chars(self) -> None:
        from ypl.agent_harness_service.service.run_task import _determine_result_status

        long_err = "x" * 200
        status, err = _determine_result_status(long_err, is_error=True)
        assert status == "failed"
        assert err is not None
        assert len(err) <= 50


# ===========================================================================
# Tests: run_task.py — _format_tool_command
# ===========================================================================


class TestFormatToolCommand:
    """_format_tool_command extracts a display string from tool inputs."""

    def test_prefers_command_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Bash", {"command": "ls -la", "other": "x"})
        assert result == "ls -la"

    def test_falls_back_to_pattern_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Grep", {"pattern": "def foo"})
        assert result == "def foo"

    def test_falls_back_to_file_path_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Read", {"file_path": "/foo/bar.py"})
        assert result == "/foo/bar.py"

    def test_falls_back_to_path_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Glob", {"path": "/src/**/*.py"})
        assert result == "/src/**/*.py"

    def test_falls_back_to_query_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Search", {"query": "find bugs"})
        assert result == "find bugs"

    def test_falls_back_to_text_key(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Msg", {"text": "hello world"})
        assert result == "hello world"

    def test_falls_back_to_first_string_value(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        result = _format_tool_command("Custom", {"custom_key": "some value"})
        assert result == "some value"

    def test_empty_dict_returns_empty_string(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        assert _format_tool_command("Tool", {}) == ""

    def test_non_dict_input_returns_empty_string(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        assert _format_tool_command("Tool", "some string") == ""
        assert _format_tool_command("Tool", None) == ""

    def test_long_value_capped_at_200(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        long_cmd = "a" * 300
        result = _format_tool_command("Bash", {"command": long_cmd})
        assert len(result) == 200

    def test_skips_non_string_values_in_fallback(self) -> None:
        from ypl.agent_harness_service.service.run_task import _format_tool_command

        # Only integer values — fallback finds no string → returns ""
        result = _format_tool_command("Tool", {"num": 42})
        assert result == ""


# ===========================================================================
# Tests: session_lifecycle.py — _validate_force_model
# ===========================================================================


class TestValidateForceModel:
    """_validate_force_model raises AHSValidationError for unknown models."""

    def test_valid_harnessed_model_passes(self) -> None:
        from ypl.agent_harness_service.common.constants import HARNESS_CLAUDE_CODE_CLI
        from ypl.agent_harness_service.common.types import AHSValidationError
        from ypl.agent_harness_service.service.session_lifecycle import _validate_force_model

        try:
            _validate_force_model(HARNESS_CLAUDE_CODE_CLI)
        except AHSValidationError:
            pytest.fail("_validate_force_model raised for a valid harnessed model")

    def test_valid_claude_sdk_model_passes(self) -> None:
        from ypl.agent_harness_service.common.constants import HARNESS_CLAUDE_SDK
        from ypl.agent_harness_service.service.session_lifecycle import _validate_force_model

        _validate_force_model(HARNESS_CLAUDE_SDK)  # must not raise

    def test_unknown_model_raises_validation_error(self) -> None:
        from ypl.agent_harness_service.common.types import AHSValidationError
        from ypl.agent_harness_service.service.session_lifecycle import _validate_force_model

        with pytest.raises(AHSValidationError, match="Unknown model"):
            _validate_force_model("definitely-not-a-real-model-xyz")

    def test_error_message_mentions_valid_models(self) -> None:
        from ypl.agent_harness_service.common.types import AHSValidationError
        from ypl.agent_harness_service.service.session_lifecycle import _validate_force_model

        with pytest.raises(AHSValidationError) as exc_info:
            _validate_force_model("garbage-model")
        assert "Valid models" in str(exc_info.value)

    def test_empty_string_raises(self) -> None:
        from ypl.agent_harness_service.common.types import AHSValidationError
        from ypl.agent_harness_service.service.session_lifecycle import _validate_force_model

        with pytest.raises(AHSValidationError):
            _validate_force_model("")


# ===========================================================================
# Tests: session_lifecycle.py — _drain_pending_messages
# ===========================================================================


class TestDrainPendingMessages:
    """_drain_pending_messages combines queued messages and dispatches a turn."""

    def setup_method(self) -> None:
        from ypl.agent_harness_service.service import state

        state._pending_messages.clear()

    def teardown_method(self) -> None:
        from ypl.agent_harness_service.service import state

        state._pending_messages.clear()

    @pytest.mark.asyncio
    async def test_no_pending_messages_is_noop(self) -> None:
        """When no messages are queued, send_message is never called."""
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages

        sid = uuid.uuid4()
        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await _drain_pending_messages(sid)
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_message_sent_as_is(self) -> None:
        """One pending message is forwarded verbatim (no header added)."""
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages
        from ypl.agent_harness_service.service.state import PendingMessage, _pending_messages

        sid = uuid.uuid4()
        _pending_messages[sid] = [PendingMessage(message="hello")]

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await _drain_pending_messages(sid)
            mock_send.assert_awaited_once()
            call_request = mock_send.call_args[0][0]
            assert call_request.message == "hello"

    @pytest.mark.asyncio
    async def test_multiple_messages_combined_with_header(self) -> None:
        """Multiple pending messages get a count header and numbered items."""
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages
        from ypl.agent_harness_service.service.state import PendingMessage, _pending_messages

        sid = uuid.uuid4()
        _pending_messages[sid] = [
            PendingMessage(message="msg one"),
            PendingMessage(message="msg two"),
            PendingMessage(message="msg three"),
        ]

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await _drain_pending_messages(sid)
            mock_send.assert_awaited_once()
            call_request = mock_send.call_args[0][0]
            assert "msg one" in call_request.message
            assert "msg two" in call_request.message
            assert "msg three" in call_request.message
            assert "3 messages" in call_request.message

    @pytest.mark.asyncio
    async def test_queue_cleared_after_drain(self) -> None:
        """Pending messages consumed; queue is empty on success."""
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages
        from ypl.agent_harness_service.service.state import PendingMessage, _pending_messages

        sid = uuid.uuid4()
        _pending_messages[sid] = [PendingMessage(message="x")]

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
        ):
            await _drain_pending_messages(sid)

        assert sid not in _pending_messages

    @pytest.mark.asyncio
    async def test_send_failure_requeues_messages(self) -> None:
        """If send_message raises, messages are put back so they are not dropped."""
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages
        from ypl.agent_harness_service.service.state import PendingMessage, _pending_messages

        sid = uuid.uuid4()
        _pending_messages[sid] = [PendingMessage(message="important")]

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB error"),
        ):
            await _drain_pending_messages(sid)

        # Messages re-queued
        assert sid in _pending_messages
        assert len(_pending_messages[sid]) == 1

    @pytest.mark.asyncio
    async def test_two_messages_attachments_merged(self) -> None:
        """Attachments from all pending messages are merged into combined request."""
        from ypl.agent_harness_service.common.types import AttachmentInfo
        from ypl.agent_harness_service.service.session_lifecycle import _drain_pending_messages
        from ypl.agent_harness_service.service.state import PendingMessage, _pending_messages

        sid = uuid.uuid4()
        att1 = AttachmentInfo(
            filename="a.pdf", blob_path="attachments/sid/a.pdf", content_type="application/pdf", size=100
        )
        att2 = AttachmentInfo(filename="b.png", blob_path="attachments/sid/b.png", content_type="image/png", size=200)
        _pending_messages[sid] = [
            PendingMessage(message="first", attachments=[att1]),
            PendingMessage(message="second", attachments=[att2]),
        ]

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.send_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await _drain_pending_messages(sid)
            call_request = mock_send.call_args[0][0]
            # Both attachments should be present (or None with empty list)
            assert call_request.attachments is not None
            assert len(call_request.attachments) == 2


# ===========================================================================
# Tests: session_lifecycle.py — _maybe_update_task_completion
# ===========================================================================


class TestMaybeUpdateTaskCompletion:
    """_maybe_update_task_completion calls update_task_completion only for TASK trigger."""

    @pytest.mark.asyncio
    async def test_non_task_trigger_is_noop(self) -> None:
        """SLACK trigger → early return before importing task_executor."""
        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

        mock_update = AsyncMock()
        mock_te = MagicMock()
        mock_te.update_task_completion = mock_update

        with patch.dict("sys.modules", {"ypl.agent_harness_service.task_executor": mock_te}):
            await _maybe_update_task_completion(
                trigger="SLACK",
                session_context={"task_id": str(uuid.uuid4())},
                agent_session_id=uuid.uuid4(),
                success=True,
                result=None,
                error=None,
            )
        mock_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_task_trigger_without_task_id_is_noop(self) -> None:
        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

        mock_update = AsyncMock()
        mock_te = MagicMock()
        mock_te.update_task_completion = mock_update

        with patch.dict("sys.modules", {"ypl.agent_harness_service.task_executor": mock_te}):
            await _maybe_update_task_completion(
                trigger="TASK",
                session_context={"some_other_key": "value"},  # no task_id
                agent_session_id=uuid.uuid4(),
                success=True,
                result=None,
                error=None,
            )
        mock_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_task_trigger_with_task_id_calls_update(self) -> None:
        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

        task_id = str(uuid.uuid4())
        session_id = uuid.uuid4()
        mock_update = AsyncMock()
        mock_te = MagicMock()
        mock_te.update_task_completion = mock_update

        with patch.dict("sys.modules", {"ypl.agent_harness_service.task_executor": mock_te}):
            await _maybe_update_task_completion(
                trigger="TASK",
                session_context={"task_id": task_id},
                agent_session_id=session_id,
                success=True,
                result={"summary": "done"},
                error=None,
            )
        mock_update.assert_awaited_once_with(
            task_id=uuid.UUID(task_id),
            session_id=str(session_id),
            success=True,
            result={"summary": "done"},
            error=None,
            error_subtype=None,
        )

    @pytest.mark.asyncio
    async def test_task_trigger_none_context_is_noop(self) -> None:
        """None session_context → no-op regardless of trigger."""
        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

        mock_update = AsyncMock()
        mock_te = MagicMock()
        mock_te.update_task_completion = mock_update

        with patch.dict("sys.modules", {"ypl.agent_harness_service.task_executor": mock_te}):
            await _maybe_update_task_completion(
                trigger="TASK",
                session_context=None,
                agent_session_id=uuid.uuid4(),
                success=False,
                result=None,
                error="error",
            )
        mock_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_exception_swallowed(self) -> None:
        """Exceptions from update_task_completion are caught and logged, not re-raised."""
        from ypl.agent_harness_service.service.session_lifecycle import _maybe_update_task_completion

        task_id = str(uuid.uuid4())
        mock_update = AsyncMock(side_effect=RuntimeError("upstream failure"))
        mock_te = MagicMock()
        mock_te.update_task_completion = mock_update

        with patch.dict("sys.modules", {"ypl.agent_harness_service.task_executor": mock_te}):
            # Must not raise
            await _maybe_update_task_completion(
                trigger="TASK",
                session_context={"task_id": task_id},
                agent_session_id=uuid.uuid4(),
                success=True,
                result=None,
                error=None,
            )


# ===========================================================================
# Tests: session_lifecycle.py — deliver_subagent_result_to_parent
# ===========================================================================


def _make_subagent_result(**kwargs: Any) -> Any:
    """Build a minimal SubagentResult-shaped mock."""
    result = MagicMock()
    result.status = kwargs.get("status", "completed")
    result.agent_type = kwargs.get("agent_type", "sre")
    result.db_session_id = kwargs.get("db_session_id", str(uuid.uuid4()))
    result.duration_ms = kwargs.get("duration_ms", 1000)
    result.cost_usd = kwargs.get("cost_usd", None)
    result.description = kwargs.get("description", None)
    result.text = kwargs.get("text", "Done.")
    return result


def _make_db_context_manager(agent_session_obj: Any, agent_obj: Any) -> Any:
    """Return a context manager that yields an AsyncMock DB session.

    ``db.get(AgentSession, pk)`` returns ``agent_session_obj``;
    ``db.get(Agent, pk)`` returns ``agent_obj``.
    Dispatches by model class to avoid brittle call-order dependencies.
    """
    from ypl.db.agent_harness import Agent, AgentSession

    async def _get_side_effect(model: Any, pk: Any) -> Any:
        if model is AgentSession:
            return agent_session_obj
        if model is Agent:
            return agent_obj
        return None

    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.get = _get_side_effect  # plain async function works as AsyncMock side_effect
    return db


class TestDeliverSubagentResultToParent:
    """deliver_subagent_result_to_parent queues if parent busy, injects if idle."""

    def setup_method(self) -> None:
        from ypl.agent_harness_service.service import state

        state._active_tasks.clear()
        state._pending_messages.clear()

    def teardown_method(self) -> None:
        from ypl.agent_harness_service.service import state

        state._active_tasks.clear()
        state._pending_messages.clear()

    @pytest.mark.asyncio
    async def test_invalid_parent_session_id_ignored(self) -> None:
        """Malformed UUID → logs error, no DB lookup."""
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result()
        # Should not raise
        await deliver_subagent_result_to_parent("not-a-uuid", result)

    @pytest.mark.asyncio
    async def test_parent_not_found_in_db_returns_early(self) -> None:
        """When parent session absent from DB, injection never happens."""
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result()
        parent_id = str(uuid.uuid4())

        db_mock = AsyncMock()
        db_mock.__aenter__ = AsyncMock(return_value=db_mock)
        db_mock.__aexit__ = AsyncMock(return_value=None)
        db_mock.get = AsyncMock(return_value=None)  # session not found

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db_mock,
        ):
            inject_mock = AsyncMock()
            with patch(
                "ypl.agent_harness_service.service.session_lifecycle._inject_internal_message",
                inject_mock,
            ):
                await deliver_subagent_result_to_parent(parent_id, result)
                inject_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_trigger_skips_reinjection(self) -> None:
        """CRON is non-interactive: subagent result is silently dropped."""
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result()
        parent_id = str(uuid.uuid4())

        fake_session = MagicMock()
        trigger_mock = MagicMock()
        trigger_mock.value = "CRON"
        fake_session.trigger = trigger_mock
        fake_session.creator_user_id = "u1"
        fake_session.workspace = "/tmp"
        fake_session.extra_dirs = []
        fake_session.slack_session_id = None
        fake_session.llm_session_id = None
        fake_session.context = {}
        fake_session.status = MagicMock()

        fake_agent = MagicMock()
        fake_agent.name = "sre"

        db = _make_db_context_manager(fake_session, fake_agent)

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db,
        ):
            inject_mock = AsyncMock()
            with patch(
                "ypl.agent_harness_service.service.session_lifecycle._inject_internal_message",
                inject_mock,
            ):
                await deliver_subagent_result_to_parent(parent_id, result)
                inject_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_task_trigger_skips_reinjection(self) -> None:
        """TASK is non-interactive: subagent result is silently dropped."""
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result()
        parent_id = str(uuid.uuid4())

        fake_session = MagicMock()
        trigger_mock = MagicMock()
        trigger_mock.value = "TASK"
        fake_session.trigger = trigger_mock
        fake_session.creator_user_id = "u1"
        fake_session.workspace = "/tmp"
        fake_session.extra_dirs = []
        fake_session.slack_session_id = None
        fake_session.llm_session_id = None
        fake_session.context = {}
        fake_session.status = MagicMock()

        fake_agent = MagicMock()
        fake_agent.name = "planner"

        db = _make_db_context_manager(fake_session, fake_agent)

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db,
        ):
            inject_mock = AsyncMock()
            with patch(
                "ypl.agent_harness_service.service.session_lifecycle._inject_internal_message",
                inject_mock,
            ):
                await deliver_subagent_result_to_parent(parent_id, result)
                inject_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_busy_api_parent_queues_result(self) -> None:
        """API parent with active turn → result queued in _pending_messages."""
        from ypl.agent_harness_service.service import state
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result(text="subagent output", status="completed")
        parent_id = str(uuid.uuid4())
        parent_uuid = uuid.UUID(parent_id)

        # Mark parent as busy
        state._active_tasks[parent_uuid] = MagicMock()

        fake_session = MagicMock()
        trigger_mock = MagicMock()
        trigger_mock.value = "API"  # interactive trigger → eligible for re-injection
        fake_session.trigger = trigger_mock
        fake_session.creator_user_id = "user1"
        fake_session.workspace = "/data/ahs/sessions/x"
        fake_session.extra_dirs = []
        fake_session.slack_session_id = None
        fake_session.llm_session_id = None
        fake_session.context = {}
        fake_session.status = MagicMock()

        fake_agent = MagicMock()
        fake_agent.name = "sre"

        db = _make_db_context_manager(fake_session, fake_agent)

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db,
        ):
            inject_mock = AsyncMock()
            with patch(
                "ypl.agent_harness_service.service.session_lifecycle._inject_internal_message",
                inject_mock,
            ):
                await deliver_subagent_result_to_parent(parent_id, result)

        # inject not called; message was queued
        inject_mock.assert_not_awaited()
        assert parent_uuid in state._pending_messages
        assert len(state._pending_messages[parent_uuid]) == 1

    @pytest.mark.asyncio
    async def test_slack_idle_parent_injects_directly(self) -> None:
        """SLACK parent that is idle → _inject_internal_message called directly."""
        from ypl.agent_harness_service.service import state
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent

        result = _make_subagent_result(text="slack output")
        parent_id = str(uuid.uuid4())
        parent_uuid = uuid.UUID(parent_id)

        # Parent NOT in active tasks → idle
        assert parent_uuid not in state._active_tasks

        fake_session = MagicMock()
        trigger_mock = MagicMock()
        trigger_mock.value = "SLACK"
        fake_session.trigger = trigger_mock
        fake_session.creator_user_id = "user1"
        fake_session.workspace = "/data/ahs/sessions/x"
        fake_session.extra_dirs = []
        fake_session.slack_session_id = "slack-123"
        fake_session.llm_session_id = None
        fake_session.context = {}
        fake_session.status = MagicMock()

        fake_agent = MagicMock()
        fake_agent.name = "yuppclaw"

        db = _make_db_context_manager(fake_session, fake_agent)

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db,
        ):
            inject_mock = AsyncMock()
            with patch(
                "ypl.agent_harness_service.service.session_lifecycle._inject_internal_message",
                inject_mock,
            ):
                await deliver_subagent_result_to_parent(parent_id, result)

        inject_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_queue_full_drops_result(self) -> None:
        """When _pending_messages is at capacity, new result is dropped (logged)."""
        from ypl.agent_harness_service.service import state
        from ypl.agent_harness_service.service.session_lifecycle import deliver_subagent_result_to_parent
        from ypl.agent_harness_service.service.state import _MAX_PENDING_MESSAGES, PendingMessage

        result = _make_subagent_result()
        parent_id = str(uuid.uuid4())
        parent_uuid = uuid.UUID(parent_id)

        # Mark parent as busy
        state._active_tasks[parent_uuid] = MagicMock()
        # Fill queue to capacity
        state._pending_messages[parent_uuid] = [PendingMessage(message=f"msg{i}") for i in range(_MAX_PENDING_MESSAGES)]

        fake_session = MagicMock()
        trigger_mock = MagicMock()
        trigger_mock.value = "API"
        fake_session.trigger = trigger_mock
        fake_session.creator_user_id = "u"
        fake_session.workspace = "/tmp"
        fake_session.extra_dirs = []
        fake_session.slack_session_id = None
        fake_session.llm_session_id = None
        fake_session.context = {}
        fake_session.status = MagicMock()

        fake_agent = MagicMock()
        fake_agent.name = "sre"

        db = _make_db_context_manager(fake_session, fake_agent)

        with patch(
            "ypl.agent_harness_service.service.session_lifecycle.get_async_session",
            return_value=db,
        ):
            await deliver_subagent_result_to_parent(parent_id, result)

        # Queue still at capacity (new message NOT added)
        assert len(state._pending_messages[parent_uuid]) == _MAX_PENDING_MESSAGES


# ===========================================================================
# Tests: queries.py — _build_agent_info_from_db, _build_session_info
# ===========================================================================


class TestBuildAgentInfoFromDb:
    """_build_agent_info_from_db populates AgentInfo from a DB Agent row."""

    def _make_agent(self, config: dict | None = None) -> MagicMock:
        agent = MagicMock()
        agent.name = "my-agent"
        agent.display_name = "My Agent"
        agent.description = "does stuff"
        agent.executor_type = "harnessed"
        agent.executor_model = "claude-code-cli"
        agent.creator_user_id = "user1"
        agent.config = config or {}
        return agent

    def test_minimal_db_agent(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_agent_info_from_db

        info = _build_agent_info_from_db(self._make_agent())
        assert info.name == "my-agent"
        assert info.display_name == "My Agent"

    def test_config_executor_type_overrides_db_field(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_agent_info_from_db

        agent = self._make_agent(config={"executor_config": {"type": "raw", "model": "openai/gpt-4o"}})
        info = _build_agent_info_from_db(agent)
        assert info.executor_type == "raw"
        assert info.executor_model == "openai/gpt-4o"

    def test_config_max_turns_used(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_agent_info_from_db

        agent = self._make_agent(config={"max_turns": 10})
        info = _build_agent_info_from_db(agent)
        assert info.max_turns == 10

    def test_defaults_applied_when_config_empty(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_agent_info_from_db

        info = _build_agent_info_from_db(self._make_agent(config={}))
        assert info.executor_type == "harnessed"  # fallback to agent.executor_type
        assert info.max_turns is not None
        assert info.timeout_s is not None

    def test_raw_executor_sets_llm_model(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_agent_info_from_db

        agent = self._make_agent(config={"executor_config": {"type": "raw", "model": "anthropic/claude-3-5-sonnet"}})
        info = _build_agent_info_from_db(agent)
        assert info.llm_model == "anthropic/claude-3-5-sonnet"


class TestBuildSessionInfo:
    """_build_session_info constructs SessionInfo from a DB AgentSession row."""

    def _make_row(self, **kwargs: Any) -> MagicMock:
        from datetime import datetime

        row = MagicMock()
        row.agent_session_id = kwargs.get("agent_session_id", uuid.uuid4())
        status_mock = MagicMock()
        status_mock.value = kwargs.get("status", "ACTIVE")
        row.status = status_mock
        trigger_mock = MagicMock()
        trigger_mock.value = kwargs.get("trigger", "api")
        row.trigger = trigger_mock
        row.model = kwargs.get("model", "claude-code-cli")
        row.created_at = kwargs.get("created_at", datetime.now(tz=UTC))
        row.context = kwargs.get("context", {})
        row.parent_session_id = kwargs.get("parent_session_id", None)
        row.title = kwargs.get("title", None)
        return row

    def test_basic_session_info(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_session_info

        row = self._make_row(status="COMPLETED", title="My session")
        info = _build_session_info(row, agent_name="sre", message_count=3)
        assert info.agent_name == "sre"
        assert info.status == "COMPLETED"
        assert info.message_count == 3
        assert info.title == "My session"

    def test_session_info_with_parent(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_session_info

        parent_id = uuid.uuid4()
        row = self._make_row(status="COMPLETED", trigger="task", parent_session_id=parent_id)
        info = _build_session_info(row, agent_name="planner")
        assert info.parent_session_id == str(parent_id)

    def test_session_info_no_parent(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_session_info

        row = self._make_row()
        info = _build_session_info(row, agent_name="sre")
        assert info.parent_session_id is None

    def test_session_info_slack_context(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_session_info

        row = self._make_row(
            context={
                "slack_channel_name": "#eng-agents",
                "slack_user_id": "U123",
            }
        )
        info = _build_session_info(row, agent_name="sre")
        assert info.slack_channel_name == "#eng-agents"
        assert info.slack_user_id == "U123"

    def test_session_info_default_message_count(self) -> None:
        from ypl.agent_harness_service.service.queries import _build_session_info

        row = self._make_row()
        info = _build_session_info(row, agent_name="sre")
        assert info.message_count == 0  # default
