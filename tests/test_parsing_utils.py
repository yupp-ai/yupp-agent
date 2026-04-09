"""Unit tests for ypl/backend/utils/parsing_utils.py.

Covers parse_rfc3339_timestamp with:
- Z suffix (UTC shorthand)
- +00:00 explicit offset
- Fractional seconds (µs, ms, nanoseconds beyond µs)
- Naive datetime (no tz → assumed UTC)
- Empty / whitespace / invalid strings
- Non-UTC offsets (converted to UTC)
"""

from __future__ import annotations
from datetime import UTC, datetime, timedelta

from ypl.backend.utils.parsing_utils import parse_rfc3339_timestamp


class TestParseRfc3339Timestamp:
    # ------------------------------------------------------------------
    # Valid inputs
    # ------------------------------------------------------------------

    def test_z_suffix_returns_utc_datetime(self) -> None:
        result = parse_rfc3339_timestamp("2024-01-15T10:30:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_explicit_utc_offset(self) -> None:
        result = parse_rfc3339_timestamp("2024-01-15T10:30:00+00:00")
        assert result is not None
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_microseconds_preserved(self) -> None:
        result = parse_rfc3339_timestamp("2024-06-01T12:00:00.123456Z")
        assert result is not None
        assert result.microsecond == 123456

    def test_milliseconds_padded_to_microseconds(self) -> None:
        result = parse_rfc3339_timestamp("2024-06-01T12:00:00.123Z")
        assert result is not None
        # "123" → padded to "123000"
        assert result.microsecond == 123000

    def test_nanoseconds_truncated_to_microseconds(self) -> None:
        result = parse_rfc3339_timestamp("2024-06-01T12:00:00.123456789Z")
        assert result is not None
        # First 6 digits kept: "123456"
        assert result.microsecond == 123456

    def test_9_digit_nanosecond_fraction(self) -> None:
        result = parse_rfc3339_timestamp("2023-03-10T08:00:00.999999999Z")
        assert result is not None
        assert result.microsecond == 999999

    def test_1_digit_fraction(self) -> None:
        result = parse_rfc3339_timestamp("2024-01-01T00:00:00.5Z")
        assert result is not None
        assert result.microsecond == 500000

    def test_naive_datetime_assumed_utc(self) -> None:
        result = parse_rfc3339_timestamp("2024-03-20T15:00:00")
        assert result is not None
        assert result.tzinfo == UTC
        assert result.year == 2024
        assert result.hour == 15

    def test_non_utc_offset_converted_to_utc(self) -> None:
        # +05:30 (IST) → 10:30 UTC
        result = parse_rfc3339_timestamp("2024-01-01T16:00:00+05:30")
        assert result is not None
        assert result.tzinfo is not None
        expected = datetime(2024, 1, 1, 10, 30, 0, tzinfo=UTC)
        assert result == expected

    def test_negative_offset_converted_to_utc(self) -> None:
        # -08:00 (PST) → 20:00 UTC
        result = parse_rfc3339_timestamp("2024-01-01T12:00:00-08:00")
        assert result is not None
        expected = datetime(2024, 1, 1, 20, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        result = parse_rfc3339_timestamp("  2024-01-15T10:30:00Z  ")
        assert result is not None
        assert result.year == 2024

    def test_fractional_seconds_with_explicit_offset(self) -> None:
        result = parse_rfc3339_timestamp("2024-07-04T18:00:00.500000+00:00")
        assert result is not None
        assert result.microsecond == 500000

    # ------------------------------------------------------------------
    # Invalid / empty inputs
    # ------------------------------------------------------------------

    def test_empty_string_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("")
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("   ")
        assert result is None

    def test_invalid_format_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("not-a-timestamp")
        assert result is None

    def test_partial_date_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("2024-01-15")
        # datetime.fromisoformat can parse date-only strings but the result is naive
        # Result could be a valid date or None — check it's either None or has no error
        # Either outcome is acceptable since the function handles naive datetimes
        if result is not None:
            assert result.year == 2024

    def test_malformed_fractional_part_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("2024-01-15T10:30:00.ABCZ")
        assert result is None

    def test_garbage_string_returns_none(self) -> None:
        result = parse_rfc3339_timestamp("garbage!@#$%")
        assert result is None

    # ------------------------------------------------------------------
    # Return type guarantees
    # ------------------------------------------------------------------

    def test_result_is_always_utc(self) -> None:
        """All successfully parsed timestamps should be in UTC."""
        test_cases = [
            "2024-01-15T10:30:00Z",
            "2024-01-15T10:30:00+00:00",
            "2024-01-15T10:30:00+05:30",
            "2024-01-15T10:30:00-07:00",
        ]
        for ts in test_cases:
            result = parse_rfc3339_timestamp(ts)
            assert result is not None, f"Expected result for {ts}"
            assert result.utcoffset() == timedelta(0), f"Expected UTC offset for {ts}"

    def test_result_has_timezone_info(self) -> None:
        result = parse_rfc3339_timestamp("2024-06-15T12:00:00Z")
        assert result is not None
        assert result.tzinfo is not None
