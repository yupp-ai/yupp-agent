"""Unit tests for Slack message buffer utility functions.

Covers _slack_len and _find_slack_truncation_point — the UTF-16 counting
logic that ensures messages don't exceed Slack's character limit.
"""

from ypl.slack_agent_gateway.buffer import _find_slack_truncation_point, _slack_len


class TestSlackLen:
    """Test UTF-16 code unit counting (Slack/JS-compatible)."""

    def test_ascii_string(self) -> None:
        assert _slack_len("hello") == 5

    def test_empty_string(self) -> None:
        assert _slack_len("") == 0

    def test_bmp_characters(self) -> None:
        """Characters in the Basic Multilingual Plane count as 1."""
        # é is U+00E9, within BMP
        assert _slack_len("café") == 4

    def test_emoji_counts_as_two(self) -> None:
        """Emoji (U+1F600+) are outside BMP, count as 2 in UTF-16."""
        # 😀 is U+1F600 (astral plane)
        assert _slack_len("😀") == 2

    def test_mixed_ascii_and_emoji(self) -> None:
        # "hi 😀" = 2 (h,i) + 1 (space) + 2 (emoji) = 5
        assert _slack_len("hi 😀") == 5

    def test_multiple_emoji(self) -> None:
        assert _slack_len("😀😀😀") == 6

    def test_cjk_characters(self) -> None:
        """CJK characters are in BMP, count as 1 each."""
        assert _slack_len("你好") == 2

    def test_flag_emoji(self) -> None:
        """Flag emoji are two regional indicator symbols, each outside BMP."""
        # 🇺🇸 = U+1F1FA U+1F1F8 (two astral-plane chars)
        assert _slack_len("🇺🇸") == 4


class TestFindSlackTruncationPoint:
    """Test finding the safe Python char index for Slack's limit."""

    def test_ascii_within_limit(self) -> None:
        assert _find_slack_truncation_point("hello", 10) == 5

    def test_ascii_at_exact_limit(self) -> None:
        assert _find_slack_truncation_point("hello", 5) == 5

    def test_ascii_truncated(self) -> None:
        assert _find_slack_truncation_point("hello", 3) == 3

    def test_emoji_at_boundary(self) -> None:
        """Emoji that would exceed limit should not be included."""
        # "ab😀cd" — at max_slack_len=3, we can fit "ab" (2 units) but not 😀 (2 units)
        result = _find_slack_truncation_point("ab😀cd", 3)
        assert result == 2  # only "ab" fits

    def test_emoji_fits_exactly(self) -> None:
        # "ab😀" — at max_slack_len=4, "ab" (2) + 😀 (2) = 4, fits exactly
        result = _find_slack_truncation_point("ab😀", 4)
        assert result == 3  # all 3 Python chars fit

    def test_empty_string(self) -> None:
        assert _find_slack_truncation_point("", 10) == 0

    def test_zero_limit(self) -> None:
        assert _find_slack_truncation_point("hello", 0) == 0

    def test_all_emoji(self) -> None:
        # "😀😀😀" — each emoji is 2 units
        # max_slack_len=5: first two emoji fit (4 units), third doesn't (6 > 5)
        result = _find_slack_truncation_point("😀😀😀", 5)
        assert result == 2  # 2 Python chars = 2 emoji = 4 UTF-16 units

    def test_consistency_with_slack_len(self) -> None:
        """Truncation point should always satisfy _slack_len <= max."""
        text = "Hello 😀 World 🌍 Test 🎉 Done"
        for max_len in range(_slack_len(text) + 5):
            idx = _find_slack_truncation_point(text, max_len)
            truncated = text[:idx]
            assert _slack_len(truncated) <= max_len, f"_slack_len(text[:{idx}]) = {_slack_len(truncated)} > {max_len}"
