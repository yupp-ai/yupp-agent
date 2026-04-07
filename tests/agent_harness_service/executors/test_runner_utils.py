"""Tests for runner.py utility functions — extract_excerpt, retry logic, bottleneck detection."""

from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ypl.agent_harness_service.common.models import RetryConfig
from ypl.agent_harness_service.common.types import StreamEvent
from ypl.agent_harness_service.executors.runner import (
    AgentRunner,
    RunContext,
    _check_queue_bottleneck,
    _queue_bottleneck_events,
    extract_excerpt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(type_: str, **raw_fields: Any) -> StreamEvent:
    return StreamEvent(type=type_, raw=raw_fields)


def _make_context(**overrides: Any) -> RunContext:
    defaults: dict[str, Any] = {"session_id": "test-session", "workspace": None, "llm_session_id": None}
    defaults.update(overrides)
    return RunContext(**defaults)


# ---------------------------------------------------------------------------
# extract_excerpt
# ---------------------------------------------------------------------------


class TestExtractExcerpt:
    def test_assistant_text_event(self) -> None:
        event = StreamEvent(type="assistant", raw={"message": {"content": [{"type": "text", "text": "Hello world"}]}})
        result = extract_excerpt(event)
        assert "Hello world" in result

    def test_assistant_tool_call_only(self) -> None:
        """Assistant with only tool_use blocks returns 'tool_call: <name>'."""
        event = StreamEvent(
            type="assistant",
            raw={"message": {"content": [{"type": "tool_use", "name": "read_file", "id": "t1", "input": {}}]}},
        )
        result = extract_excerpt(event)
        assert "tool_call" in result
        assert "read_file" in result

    def test_assistant_no_content_returns_placeholder(self) -> None:
        event = StreamEvent(type="assistant", raw={"message": {"content": []}})
        result = extract_excerpt(event)
        assert result == "(no text content)"

    def test_tool_use_event(self) -> None:
        event = _event("tool_use", tool_name="bash", input={"command": "ls -la"})
        result = extract_excerpt(event)
        assert "bash" in result
        assert "ls -la" in result

    def test_tool_result_event(self) -> None:
        event = _event("tool_result", content="file contents here")
        result = extract_excerpt(event)
        assert "file contents here" in result

    def test_result_event_fields(self) -> None:
        event = _event("result", estimated_cost_usd=0.05, num_turns=3, duration_ms=1500)
        result = extract_excerpt(event)
        assert "0.05" in result
        assert "3" in result
        assert "1500" in result

    def test_system_event_session_id(self) -> None:
        event = _event("system", session_id="sess-abc")
        result = extract_excerpt(event)
        assert "sess-abc" in result

    def test_error_event(self) -> None:
        event = _event("error", error="Something went wrong")
        result = extract_excerpt(event)
        assert "Something went wrong" in result

    def test_user_event_with_tool_result_blocks(self) -> None:
        event = StreamEvent(
            type="user",
            raw={
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "tool output here"},
                        {"type": "tool_result", "content": "more output"},
                    ]
                }
            },
        )
        result = extract_excerpt(event)
        assert "tool output here" in result

    def test_max_len_truncates(self) -> None:
        event = _event("tool_result", content="x" * 500)
        result = extract_excerpt(event, max_len=100)
        assert len(result) <= 100

    def test_fallback_to_stderr(self) -> None:
        event = _event("unknown-type", stderr="some stderr output")
        result = extract_excerpt(event)
        assert "some stderr output" in result

    def test_unknown_event_empty_fallback(self) -> None:
        event = _event("unknown-type")
        result = extract_excerpt(event)
        # Returns empty string when no useful content found
        assert isinstance(result, str)

    def test_error_with_cost_usd_field(self) -> None:
        """result event with cost_usd key (not estimated_cost_usd) still works."""
        event = _event("result", cost_usd=0.10, num_turns=1, duration_ms=500)
        result = extract_excerpt(event)
        # Python may format 0.10 as 0.1
        assert "$0.1" in result


# ---------------------------------------------------------------------------
# AgentRunner retry logic
# ---------------------------------------------------------------------------


class _FixedEventsRunner(AgentRunner):
    """Test double that emits a fixed sequence of events on each run."""

    def __init__(self, event_sequences: list[list[StreamEvent]], retry_cfg: RetryConfig | None = None) -> None:
        self._sequences = event_sequences
        self._call_count = 0
        # Build a minimal config-like object so AgentRunner.run() can read retry_cfg
        self.config = MagicMock()
        self.config.executor_config.retry = retry_cfg

    async def _run_once(self, prompt: str, context: RunContext) -> AsyncIterator[StreamEvent]:
        if self._call_count >= len(self._sequences):
            return
            yield  # type: ignore[unreachable]  # makes this an async generator
        events = self._sequences[self._call_count]
        self._call_count += 1
        for event in events:
            yield event


async def _collect(runner: AgentRunner, prompt: str = "test", **ctx_overrides: Any) -> list[StreamEvent]:
    ctx = _make_context(**ctx_overrides)
    return [e async for e in runner.run(prompt, ctx)]


class TestAgentRunnerRetry:
    @pytest.mark.asyncio
    async def test_no_retry_on_success(self) -> None:
        """Successful first run yields events without any retry."""
        events = [_event("system", session_id="s1"), _event("assistant"), _event("result")]
        runner = _FixedEventsRunner([events])
        collected = await _collect(runner)
        assert len(collected) == 3
        assert runner._call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_silent_crash(self) -> None:
        """Empty first run triggers retry; second run succeeds."""
        retry_cfg = RetryConfig(max_retries=1, on_empty_result=True)
        first_run = [_event("error", error="CLI crashed")]
        second_run = [_event("system"), _event("assistant"), _event("result")]
        runner = _FixedEventsRunner([first_run, second_run], retry_cfg=retry_cfg)
        collected = await _collect(runner)
        # First run's error event should be discarded; second run's events returned
        assert runner._call_count == 2
        types = [e.type for e in collected]
        assert "system" in types
        assert "result" in types

    @pytest.mark.asyncio
    async def test_no_retry_when_zero_retries(self) -> None:
        """With max_retries=0, no retry even on silent crash."""
        retry_cfg = RetryConfig(max_retries=0, on_empty_result=True)
        first_run = [_event("error", error="CLI crashed")]
        second_run = [_event("system"), _event("result")]
        runner = _FixedEventsRunner([first_run, second_run], retry_cfg=retry_cfg)
        collected = await _collect(runner)
        # Only first run's error events should appear (forwarded on last attempt)
        assert runner._call_count == 1
        assert all(e.type == "error" for e in collected)

    @pytest.mark.asyncio
    async def test_retries_exhausted_returns_last_error(self) -> None:
        """When all retries fail, the last attempt's error events are forwarded."""
        retry_cfg = RetryConfig(max_retries=2, on_empty_result=True)
        crash = [_event("error", error="crash")]
        runner = _FixedEventsRunner([crash, crash, crash], retry_cfg=retry_cfg)
        collected = await _collect(runner)
        assert runner._call_count == 3
        assert all(e.type == "error" for e in collected)

    @pytest.mark.asyncio
    async def test_non_error_event_stops_buffering(self) -> None:
        """Once a non-error event is seen, all subsequent events stream directly."""
        retry_cfg = RetryConfig(max_retries=1, on_empty_result=True)
        # First run has a mix: error then system — system breaks the "only errors" state
        first_run = [_event("error", error="transient"), _event("system"), _event("result")]
        runner = _FixedEventsRunner([first_run], retry_cfg=retry_cfg)
        collected = await _collect(runner)
        # All events from first run should be returned (system was meaningful)
        assert runner._call_count == 1
        types = [e.type for e in collected]
        assert "error" in types
        assert "system" in types
        assert "result" in types

    @pytest.mark.asyncio
    async def test_no_retry_config_means_zero_retries(self) -> None:
        """Runner without retry config defaults to zero retries."""
        crash = [_event("error", error="crash")]
        runner = _FixedEventsRunner([crash], retry_cfg=None)
        await _collect(runner)
        assert runner._call_count == 1


# ---------------------------------------------------------------------------
# _check_queue_bottleneck
# ---------------------------------------------------------------------------


class TestCheckQueueBottleneck:
    def setup_method(self) -> None:
        """Clear the global bottleneck event deque before each test."""
        _queue_bottleneck_events.clear()

    def test_below_threshold_does_not_log(self) -> None:
        """Wait times below threshold (30s) do not trigger any metric or log."""
        with patch("ypl.agent_harness_service.executors.runner.metric_inc") as mock_metric:
            _check_queue_bottleneck(queue_wait_ms=5_000, session_id="sess-1")
            mock_metric.assert_not_called()

    def test_above_threshold_increments_metric(self) -> None:
        """Wait times above threshold emit the bottleneck event metric."""
        with (
            patch("ypl.agent_harness_service.executors.runner.metric_inc") as mock_metric,
            patch("ypl.agent_harness_service.executors.runner.metric_record_with_labels"),
        ):
            _check_queue_bottleneck(queue_wait_ms=60_000, session_id="sess-1")
            mock_metric.assert_called_once_with("ahs/session_queue_bottleneck_event")

    def test_alert_fires_at_threshold_count(self) -> None:
        """Alert fires exactly when count reaches _QUEUE_BOTTLENECK_MIN_COUNT (3)."""
        calls: list[str] = []
        with (
            patch("ypl.agent_harness_service.executors.runner.metric_inc", side_effect=lambda x: calls.append(x)),
            patch("ypl.agent_harness_service.executors.runner.metric_record_with_labels"),
            patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger,
        ):
            _check_queue_bottleneck(60_000, "s1")  # 1st
            _check_queue_bottleneck(60_000, "s2")  # 2nd
            # Alert not yet fired
            mock_logger.error.assert_not_called()

            _check_queue_bottleneck(60_000, "s3")  # 3rd — alert fires
            mock_logger.error.assert_called_once()
            args = mock_logger.error.call_args
            assert args[0][0] == "session_queue_bottleneck"
            # Alert metric also emitted
            assert "ahs/session_queue_bottleneck_alert" in calls

    def test_alert_fires_only_once_per_window(self) -> None:
        """Additional events beyond the threshold do NOT re-fire the alert."""
        with (
            patch("ypl.agent_harness_service.executors.runner.metric_inc"),
            patch("ypl.agent_harness_service.executors.runner.metric_record_with_labels"),
            patch("ypl.agent_harness_service.executors.runner.logger") as mock_logger,
        ):
            for _ in range(10):
                _check_queue_bottleneck(60_000, "sess")
            # Only one error log should have fired
            assert mock_logger.error.call_count == 1

    def test_exact_threshold_not_triggered(self) -> None:
        """Exactly at the threshold (30_000 ms) — not above — does not trigger."""
        with patch("ypl.agent_harness_service.executors.runner.metric_inc") as mock_metric:
            _check_queue_bottleneck(queue_wait_ms=30_000, session_id="sess-1")
            mock_metric.assert_not_called()
