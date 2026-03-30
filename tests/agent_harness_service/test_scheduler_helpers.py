"""Unit tests for scheduler helper functions.

Covers _parse_env_int and configuration constants.
"""

import os
from unittest.mock import patch

from ypl.agent_harness_service.scheduler import _parse_env_int


class TestParseEnvInt:
    """Test _parse_env_int with various env var values."""

    def test_valid_int(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "42"}):
            assert _parse_env_int("TEST_VAR", 10) == 42

    def test_missing_env_var_uses_default(self) -> None:
        # Ensure the var doesn't exist
        with patch.dict(os.environ, {}, clear=True):
            assert _parse_env_int("NONEXISTENT_VAR", 10) == 10

    def test_empty_string_uses_default(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": ""}):
            # Empty string will fail int() conversion
            assert _parse_env_int("TEST_VAR", 10) == 10

    def test_non_numeric_uses_default(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "abc"}):
            assert _parse_env_int("TEST_VAR", 10) == 10

    def test_zero_uses_default(self) -> None:
        """Zero is not > 0, so should fall back to default."""
        with patch.dict(os.environ, {"TEST_VAR": "0"}):
            assert _parse_env_int("TEST_VAR", 10) == 10

    def test_negative_uses_default(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "-5"}):
            assert _parse_env_int("TEST_VAR", 10) == 10

    def test_positive_int_accepted(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "1"}):
            assert _parse_env_int("TEST_VAR", 10) == 1

    def test_large_int(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "99999"}):
            assert _parse_env_int("TEST_VAR", 10) == 99999

    def test_float_string_uses_default(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR": "3.14"}):
            assert _parse_env_int("TEST_VAR", 10) == 10
