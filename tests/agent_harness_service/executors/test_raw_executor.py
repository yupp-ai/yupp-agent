"""Tests for raw executor context management utilities."""

from pathlib import Path

import pytest
from ypl.agent_harness_service.executors.raw_executor import (
    _estimate_cost,
    estimate_messages_tokens,
    estimate_tokens,
    spill_tool_result_to_file,
    truncate_tool_result,
)


class TestTruncateToolResult:
    def test_no_truncation_needed(self) -> None:
        text = "short result"
        assert truncate_tool_result(text, max_chars=1000, max_lines=100) == text

    def test_truncate_by_lines(self) -> None:
        lines = [f"line {i}" for i in range(20)]
        text = "\n".join(lines)
        result = truncate_tool_result(text, max_chars=100_000, max_lines=10)
        assert result.endswith("...[truncated 10 lines]")
        # Should contain exactly 10 kept lines + the truncation marker
        result_lines = result.split("\n")
        assert result_lines[0] == "line 0"
        assert result_lines[9] == "line 9"

    def test_truncate_by_chars(self) -> None:
        text = "a" * 500
        result = truncate_tool_result(text, max_chars=100, max_lines=10_000)
        assert result.startswith("a" * 100)
        assert "truncated to 100 chars (500 total)" in result

    def test_truncate_lines_then_chars(self) -> None:
        """When both limits apply, lines are truncated first, then chars."""
        lines = ["x" * 100] * 50  # 50 lines of 100 chars each
        text = "\n".join(lines)
        original_len = len(text)
        result = truncate_tool_result(text, max_chars=200, max_lines=10)
        # Lines truncated first, then chars
        assert "truncated" in result
        assert len(result.split("\n...[truncated to")[0]) <= 200
        # The char truncation message reports the original text length
        assert f"({original_len} total)" in result

    def test_empty_text(self) -> None:
        assert truncate_tool_result("", max_chars=100, max_lines=10) == ""

    def test_exact_line_limit(self) -> None:
        lines = [f"line {i}" for i in range(10)]
        text = "\n".join(lines)
        result = truncate_tool_result(text, max_chars=100_000, max_lines=10)
        assert result == text  # Exactly at limit, no truncation

    def test_exact_char_limit(self) -> None:
        text = "a" * 100
        result = truncate_tool_result(text, max_chars=100, max_lines=10_000)
        assert result == text  # Exactly at limit, no truncation


class TestEstimateTokens:
    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 0

    def test_short_text(self) -> None:
        result = estimate_tokens("hello")
        # 5 chars / 4 * 1.2 = 1.5, ceil = 2
        assert result == 2

    def test_longer_text(self) -> None:
        text = "a" * 400
        result = estimate_tokens(text)
        # 400 / 4 * 1.2 = 120
        assert result == 120

    def test_returns_int(self) -> None:
        assert isinstance(estimate_tokens("test string"), int)


class TestEstimateMessagesTokens:
    def test_system_prompt_only(self) -> None:
        result = estimate_messages_tokens([], "You are a helpful assistant.")
        assert result == estimate_tokens("You are a helpful assistant.")

    def test_string_content_messages(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = estimate_messages_tokens(messages, "System")
        expected = estimate_tokens("System") + estimate_tokens("Hello") + 4 + estimate_tokens("Hi there") + 4
        assert result == expected

    def test_list_content_messages(self) -> None:
        """Anthropic-format messages with list content blocks."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "123", "content": "result text"},
                ],
            },
        ]
        result = estimate_messages_tokens(messages, "System")
        assert result > estimate_tokens("System")  # Should include block tokens + overhead

    def test_missing_content(self) -> None:
        """Messages with no content field (e.g., OpenAI assistant with only tool_calls)."""
        messages = [{"role": "assistant"}]
        result = estimate_messages_tokens(messages, "System")
        # Should still count the 4-token overhead + system prompt
        assert result == estimate_tokens("System") + estimate_tokens("") + 4

    def test_openai_tool_calls_counted(self) -> None:
        """OpenAI assistant messages with tool_calls are counted."""
        messages_without = [{"role": "assistant", "content": None}]
        messages_with = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}},
                ],
            }
        ]
        without = estimate_messages_tokens(messages_without, "System")
        with_calls = estimate_messages_tokens(messages_with, "System")
        assert with_calls > without

    def test_tool_schemas_counted(self) -> None:
        """Tool schemas add to the total token estimate."""
        messages = [{"role": "user", "content": "Hello"}]
        schemas = [{"name": "bash", "description": "Run a command", "input_schema": {"type": "object"}}]
        without = estimate_messages_tokens(messages, "System")
        with_schemas = estimate_messages_tokens(messages, "System", tool_schemas=schemas)
        assert with_schemas > without


class TestEstimateCost:
    """Tests for cache-aware and reasoning-aware cost estimation."""

    def test_basic_cost_known_model(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        cost = _estimate_cost("claude-sonnet-4-6", usage)
        # 1M * 3.0 + 1M * 15.0 = $18.0
        assert abs(cost - 18.0) < 0.001

    def test_basic_cost_unknown_model(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        cost = _estimate_cost("unknown-model", usage)
        # fallback: 1M * 3.0 + 1M * 15.0 = $18.0
        assert abs(cost - 18.0) < 0.001

    def test_anthropic_cache_read(self) -> None:
        """Cache reads should be billed at 0.1x input rate."""
        usage = {
            "input_tokens": 500,  # uncached suffix
            "output_tokens": 1000,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 0,
        }
        cost = _estimate_cost("claude-sonnet-4-6", usage)
        # Anthropic: input_tokens IS the uncached portion (additive buckets)
        # uncached: 500 * 3.0/1M, cache_read: 10000 * 0.30/1M, output: 1000 * 15.0/1M
        cost_no_cache = _estimate_cost("claude-sonnet-4-6", {"input_tokens": 10500, "output_tokens": 1000})
        assert cost < cost_no_cache

    def test_anthropic_cache_write(self) -> None:
        """Cache writes should be billed at 1.25x input rate."""
        usage = {
            "input_tokens": 500,
            "output_tokens": 1000,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 0,
        }
        cost = _estimate_cost("claude-sonnet-4-6", usage)
        # uncached: 500 * 3.0, cache_write: 10000 * 3.75, output: 1000 * 15.0
        expected_cost = (500 * 3.0 + 1000 * 15.0 + 10_000 * 3.75) / 1_000_000
        assert abs(cost - expected_cost) < 0.0001

    def test_openai_cached_tokens(self) -> None:
        """OpenAI cached_tokens should reduce effective cost."""
        usage_no_cache = {"input_tokens": 10_000, "output_tokens": 1_000}
        usage_with_cache = {"input_tokens": 10_000, "output_tokens": 1_000, "cached_tokens": 8_000}
        cost_no_cache = _estimate_cost("gpt-4o", usage_no_cache)
        cost_with_cache = _estimate_cost("gpt-4o", usage_with_cache)
        assert cost_with_cache < cost_no_cache

    def test_o3_reasoning_tokens(self) -> None:
        """o3 reasoning tokens should be priced at the reasoning rate."""
        usage = {"input_tokens": 1000, "output_tokens": 5000, "reasoning_tokens": 4000}
        cost = _estimate_cost("o3", usage)
        # reasoning is 4000 at $40/M, regular output is 1000 at $40/M, input 1000 at $10/M
        expected = (1000 * 10.0 + 1000 * 40.0 + 4000 * 40.0) / 1_000_000
        assert abs(cost - expected) < 0.0001

    def test_zero_tokens(self) -> None:
        usage = {"input_tokens": 0, "output_tokens": 0}
        assert _estimate_cost("claude-sonnet-4-6", usage) == 0.0

    def test_missing_optional_fields(self) -> None:
        """Missing cache/reasoning fields should be treated as 0."""
        usage = {"input_tokens": 1000, "output_tokens": 1000}
        cost = _estimate_cost("claude-sonnet-4-6", usage)
        assert cost > 0  # should not raise


class TestSpillToolResultToFile:
    def test_writes_full_result_to_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import ypl.agent_harness_service.common.constants as const_mod

        session_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        monkeypatch.setattr(const_mod, "AHS_SESSIONS_DIR", str(tmp_path))

        result = "x" * 200_000
        msg = spill_tool_result_to_file(result, "search_gcp_logs", "tc-abc", session_id)

        # Message must contain the file path and size hint
        assert "200,000 characters" in msg
        assert "tool-results" in msg
        assert "search_gcp_logs" in msg
        assert "Read tool" in msg

        # Verify the file exists and contains the full result
        spill_dir = tmp_path / session_id / "tool-results"
        files = list(spill_dir.glob("search_gcp_logs-tc-abc-*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == result

    def test_creates_directory_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import ypl.agent_harness_service.common.constants as const_mod

        session_id = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
        monkeypatch.setattr(const_mod, "AHS_SESSIONS_DIR", str(tmp_path))

        spill_tool_result_to_file("some result", "my_tool", "id-1", session_id)
        assert (tmp_path / session_id / "tool-results").is_dir()
