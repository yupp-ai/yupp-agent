"""Unit tests for ypl/utils.py utility functions.

Covers: maybe_truncate, maybe_truncate_list, concatenate_after_maybe_truncate,
extract_json_dict_from_text, deep_merge_dicts, validate_all_enums_are_defined_in_dict,
tiktoken_trim, is_short_prompt, coalesce, not_empty, parse_float, parse_int, ifnull.
"""

import enum

import pytest
from ypl.utils import (
    coalesce,
    concatenate_after_maybe_truncate,
    deep_merge_dicts,
    extract_json_dict_from_text,
    ifnull,
    is_short_prompt,
    maybe_truncate,
    maybe_truncate_list,
    not_empty,
    parse_float,
    parse_int,
    tiktoken_trim,
    validate_all_enums_are_defined_in_dict,
)

# ---------------------------------------------------------------------------
# maybe_truncate
# ---------------------------------------------------------------------------


class TestMaybeTruncate:
    def test_short_string_unchanged(self) -> None:
        assert maybe_truncate("hello", 100) == "hello"

    def test_exact_length_unchanged(self) -> None:
        assert maybe_truncate("hello", 5) == "hello"

    def test_long_string_truncated(self) -> None:
        result = maybe_truncate("a" * 100, 30)
        assert len(result) == 30
        assert result.endswith("... (truncated)")

    def test_empty_string(self) -> None:
        assert maybe_truncate("", 10) == ""

    def test_very_short_max_length(self) -> None:
        # When max_length < len(truncate_with), just hard-cut
        result = maybe_truncate("abcdefghij", 3)
        assert result == "abc"

    def test_custom_truncate_suffix(self) -> None:
        result = maybe_truncate("a" * 20, 10, truncate_with="...")
        assert result.endswith("...")
        assert len(result) == 10

    def test_none_passthrough(self) -> None:
        # maybe_truncate checks `if input`, None is falsy
        assert maybe_truncate(None, 10) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# maybe_truncate_list
# ---------------------------------------------------------------------------


class TestMaybeTruncateList:
    def test_empty_list(self) -> None:
        assert maybe_truncate_list([], 100) == []

    def test_short_list_unchanged(self) -> None:
        result = maybe_truncate_list(["a", "b"], 100)
        assert result == ["a", "b"]

    def test_excess_messages_trimmed_to_last_n(self) -> None:
        msgs = ["m1", "m2", "m3", "m4", "m5", "m6"]
        result = maybe_truncate_list(msgs, 10000, max_num_messages=3)
        assert len(result) == 3
        assert result == ["m4", "m5", "m6"]

    def test_long_messages_truncated(self) -> None:
        msgs = ["x" * 100, "y" * 100]
        result = maybe_truncate_list(msgs, 50)
        for msg in result:
            assert len(msg) <= 25  # 50 / 2 messages

    def test_raises_on_impossibly_short_max(self) -> None:
        # max_total_length must be so small that per_message_max_length <= len(truncate_with)
        # truncate_with is "... (truncated)" = 15 chars. With 2 messages, per_message = max/2.
        # So max_total_length of 20 gives per_message=10 < 15, triggering ValueError.
        msgs = ["hello world this is long", "another long message here"]
        with pytest.raises(ValueError, match="too short"):
            maybe_truncate_list(msgs, 20)


# ---------------------------------------------------------------------------
# concatenate_after_maybe_truncate
# ---------------------------------------------------------------------------


class TestConcatenateAfterMaybeTruncate:
    def test_single_message(self) -> None:
        result = concatenate_after_maybe_truncate(["hello"], max_turns=1)
        assert result == "hello"

    def test_multiple_messages_with_turn_tags(self) -> None:
        result = concatenate_after_maybe_truncate(["a", "b"], max_turns=5)
        assert "<User Turn 1>" in result
        assert "<User Turn 2>" in result

    def test_excess_turns_show_truncation_notice(self) -> None:
        msgs = ["m1", "m2", "m3", "m4"]
        result = concatenate_after_maybe_truncate(msgs, max_turns=2)
        assert "truncated prior" in result
        # Only last 2 messages should be included
        assert "<User Turn 3>" in result
        assert "<User Turn 4>" in result
        assert "<User Turn 1>" not in result


# ---------------------------------------------------------------------------
# extract_json_dict_from_text
# ---------------------------------------------------------------------------


class TestExtractJsonDictFromText:
    def test_clean_json(self) -> None:
        assert extract_json_dict_from_text('{"key": "value"}') == '{"key": "value"}'

    def test_json_with_surrounding_text(self) -> None:
        text = 'Here is the result: {"answer": 42} and some trailing text'
        assert extract_json_dict_from_text(text) == '{"answer": 42}'

    def test_nested_braces(self) -> None:
        text = '{"outer": {"inner": true}}'
        assert extract_json_dict_from_text(text) == '{"outer": {"inner": true}}'

    def test_no_braces(self) -> None:
        assert extract_json_dict_from_text("no json here") == "no json here"

    def test_only_opening_brace(self) -> None:
        assert extract_json_dict_from_text("text { more") == "text { more"

    def test_only_closing_brace(self) -> None:
        assert extract_json_dict_from_text("text } more") == "text } more"

    def test_reversed_braces(self) -> None:
        assert extract_json_dict_from_text("} text {") == "} text {"

    def test_empty_dict(self) -> None:
        assert extract_json_dict_from_text("prefix {} suffix") == "{}"

    def test_llm_markdown_wrapped(self) -> None:
        text = '```json\n{"model": "gpt-4"}\n```'
        result = extract_json_dict_from_text(text)
        assert result == '{"model": "gpt-4"}'


# ---------------------------------------------------------------------------
# deep_merge_dicts
# ---------------------------------------------------------------------------


class TestDeepMergeDicts:
    def test_simple_merge(self) -> None:
        result = deep_merge_dicts({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_override_value(self) -> None:
        result = deep_merge_dicts({"a": 1}, {"a": 2})
        assert result == {"a": 2}

    def test_nested_merge(self) -> None:
        d1 = {"a": {"x": 1, "y": 2}}
        d2 = {"a": {"y": 3, "z": 4}}
        result = deep_merge_dicts(d1, d2)
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_does_not_mutate_originals(self) -> None:
        d1 = {"a": {"x": 1}}
        d2 = {"a": {"y": 2}}
        deep_merge_dicts(d1, d2)
        assert d1 == {"a": {"x": 1}}
        assert d2 == {"a": {"y": 2}}

    def test_empty_dicts(self) -> None:
        assert deep_merge_dicts({}, {}) == {}
        assert deep_merge_dicts({"a": 1}, {}) == {"a": 1}
        assert deep_merge_dicts({}, {"a": 1}) == {"a": 1}

    def test_deeply_nested(self) -> None:
        d1 = {"a": {"b": {"c": 1}}}
        d2 = {"a": {"b": {"d": 2}}}
        result = deep_merge_dicts(d1, d2)
        assert result == {"a": {"b": {"c": 1, "d": 2}}}

    def test_dict_overridden_by_non_dict(self) -> None:
        result = deep_merge_dicts({"a": {"nested": True}}, {"a": "flat"})
        assert result == {"a": "flat"}


# ---------------------------------------------------------------------------
# validate_all_enums_are_defined_in_dict
# ---------------------------------------------------------------------------


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class TestValidateAllEnumsAreDefinedInDict:
    def test_complete_mapping_passes(self) -> None:
        mapping = {Color.RED: "#f00", Color.GREEN: "#0f0", Color.BLUE: "#00f"}
        result = validate_all_enums_are_defined_in_dict(Color, mapping, "color_map")
        assert result is mapping

    def test_missing_enum_raises(self) -> None:
        mapping = {Color.RED: "#f00", Color.GREEN: "#0f0"}
        with pytest.raises(ValueError, match="color_map is missing"):
            validate_all_enums_are_defined_in_dict(Color, mapping, "color_map")

    def test_empty_mapping_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_all_enums_are_defined_in_dict(Color, {}, "color_map")


# ---------------------------------------------------------------------------
# tiktoken_trim
# ---------------------------------------------------------------------------


class TestTiktokenTrim:
    def test_left_trim(self) -> None:
        text = "The quick brown fox jumps over the lazy dog"
        result = tiktoken_trim(text, 5, direction="left")
        # Should only contain the first 5 tokens
        assert len(result) < len(text)
        assert text.startswith(result)

    def test_right_trim(self) -> None:
        text = "The quick brown fox jumps over the lazy dog"
        result = tiktoken_trim(text, 5, direction="right")
        assert len(result) < len(text)
        assert text.endswith(result)

    def test_no_trim_needed(self) -> None:
        text = "hello"
        result = tiktoken_trim(text, 1000, direction="left")
        assert result == text

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid direction"):
            tiktoken_trim("test", 5, direction="center")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_short_prompt
# ---------------------------------------------------------------------------


class TestIsShortPrompt:
    def test_empty_string_is_short(self) -> None:
        is_short, words, chars = is_short_prompt("")
        assert is_short is True
        assert words == 0
        assert chars == 0

    def test_single_word_is_short(self) -> None:
        is_short, words, chars = is_short_prompt("hi")
        assert is_short is True
        assert words == 1
        assert chars == 2

    def test_long_prompt_is_not_short(self) -> None:
        is_short, words, chars = is_short_prompt("Please write a comprehensive essay about AI safety")
        assert is_short is False

    def test_respects_custom_thresholds(self) -> None:
        is_short, _, _ = is_short_prompt("hello world", max_words=10, max_chars=50)
        assert is_short is True


# ---------------------------------------------------------------------------
# coalesce, not_empty, parse_float, parse_int, ifnull
# ---------------------------------------------------------------------------


class TestCoalesce:
    def test_first_non_none(self) -> None:
        assert coalesce(None, True) is True
        assert coalesce(None, False) is False

    def test_all_none_returns_false(self) -> None:
        assert coalesce(None, None) is False

    def test_first_value_wins(self) -> None:
        assert coalesce(True, False) is True


class TestNotEmpty:
    def test_none(self) -> None:
        assert not_empty(None) is False

    def test_empty_string(self) -> None:
        assert not_empty("") is False

    def test_whitespace_only(self) -> None:
        assert not_empty("   ") is False

    def test_non_empty(self) -> None:
        assert not_empty("hello") is True


class TestParseFloat:
    def test_valid_float(self) -> None:
        assert parse_float("3.14") == 3.14

    def test_none(self) -> None:
        assert parse_float(None) is None

    def test_empty(self) -> None:
        assert parse_float("") is None

    def test_invalid(self) -> None:
        assert parse_float("abc") is None


class TestParseInt:
    def test_valid_int(self) -> None:
        assert parse_int("42") == 42

    def test_none(self) -> None:
        assert parse_int(None) is None

    def test_empty(self) -> None:
        assert parse_int("") is None

    def test_invalid(self) -> None:
        assert parse_int("xyz") is None

    def test_float_string_invalid(self) -> None:
        assert parse_int("3.14") is None


class TestIfnull:
    def test_non_none_value(self) -> None:
        assert ifnull(42, 0) == 42

    def test_none_returns_default(self) -> None:
        assert ifnull(None, "default") == "default"

    def test_false_is_not_none(self) -> None:
        assert ifnull(False, True) is False
