"""Unit tests for ypl/mcp_server/tools/agent_memory.py.

Focus on pure-logic helper functions that don't require GCS connections.
"""

from __future__ import annotations

from ypl.mcp_server.tools.agent_memory import (
    _sanitize_metadata_value,
    _validate_memory_topic,
)

# ---------------------------------------------------------------------------
# _validate_memory_topic
# ---------------------------------------------------------------------------


class TestValidateMemoryTopic:
    def test_simple_valid_topic(self) -> None:
        assert _validate_memory_topic("my-topic") is None

    def test_alphanumeric_valid(self) -> None:
        assert _validate_memory_topic("topic123") is None

    def test_underscore_valid(self) -> None:
        assert _validate_memory_topic("my_topic") is None

    def test_nested_slash_valid(self) -> None:
        assert _validate_memory_topic("bookkeeper/routing-overview") is None

    def test_deeply_nested_valid(self) -> None:
        assert _validate_memory_topic("a/b/c") is None

    def test_empty_topic_invalid(self) -> None:
        error = _validate_memory_topic("")
        assert error is not None
        assert "empty" in error.lower()

    def test_too_long_topic(self) -> None:
        error = _validate_memory_topic("a" * 101)
        assert error is not None
        assert "100 characters" in error

    def test_exactly_100_chars_valid(self) -> None:
        assert _validate_memory_topic("a" * 100) is None

    def test_leading_slash_invalid(self) -> None:
        error = _validate_memory_topic("/my-topic")
        assert error is not None

    def test_trailing_slash_invalid(self) -> None:
        error = _validate_memory_topic("my-topic/")
        assert error is not None

    def test_double_slash_invalid(self) -> None:
        error = _validate_memory_topic("a//b")
        assert error is not None

    def test_segment_starting_with_hyphen_invalid(self) -> None:
        error = _validate_memory_topic("-bad-segment")
        assert error is not None

    def test_segment_starting_with_underscore_invalid(self) -> None:
        error = _validate_memory_topic("_bad-segment")
        assert error is not None

    def test_spaces_invalid(self) -> None:
        error = _validate_memory_topic("my topic")
        assert error is not None

    def test_special_chars_invalid(self) -> None:
        error = _validate_memory_topic("my!topic")
        assert error is not None

    def test_single_char_valid(self) -> None:
        assert _validate_memory_topic("a") is None

    def test_nested_with_numbers_valid(self) -> None:
        assert _validate_memory_topic("agent1/memory2") is None

    def test_segment_starting_with_number_valid(self) -> None:
        # Numbers are OK as segment starts per the regex
        assert _validate_memory_topic("2024-report") is None


# ---------------------------------------------------------------------------
# _sanitize_metadata_value
# ---------------------------------------------------------------------------


class TestSanitizeMetadataValue:
    def test_plain_alphanumeric_unchanged(self) -> None:
        assert _sanitize_metadata_value("hello123") == "hello123"

    def test_allowed_special_chars_kept(self) -> None:
        val = "sre-agent_1.0 user@yupp.ai/path"
        result = _sanitize_metadata_value(val)
        # All chars in the original are allowed
        assert result == val

    def test_strips_yaml_breaking_chars(self) -> None:
        # Colons, brackets, quotes, etc should be removed
        val = 'key: "value" [bad]'
        result = _sanitize_metadata_value(val)
        assert ":" not in result
        assert '"' not in result
        assert "[" not in result

    def test_strips_html_comment_breaking_chars(self) -> None:
        # < and > would break HTML comments
        val = "value<injection>attack"
        result = _sanitize_metadata_value(val)
        assert "<" not in result
        assert ">" not in result

    def test_empty_string_unchanged(self) -> None:
        assert _sanitize_metadata_value("") == ""

    def test_newlines_stripped(self) -> None:
        val = "line1\nline2"
        result = _sanitize_metadata_value(val)
        assert "\n" not in result

    def test_tabs_stripped(self) -> None:
        val = "col1\tcol2"
        result = _sanitize_metadata_value(val)
        assert "\t" not in result
