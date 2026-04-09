"""Unit tests for backend feature_flags module.

Covers:
- _maybe_add_prefix
- strtobool
- get_feature_value (redis hit / miss / error / fallback to yaml)
- is_feature_enabled (various value types)
- set_feature_value / set_serialized_feature_value
- get_feature_flags_from_yml (valid / invalid / missing file)
- get_default_feature_flags
- is_feature_flag_critical
"""

from __future__ import annotations
import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from ypl.backend.feature_flags import (
    _maybe_add_prefix,
    get_default_feature_flags,
    get_feature_flags_from_yml,
    get_feature_value_from_redis_no_cache,
    is_feature_enabled,
    is_feature_flag_critical,
    set_feature_value,
    strtobool,
)

# ---------------------------------------------------------------------------
# _maybe_add_prefix
# ---------------------------------------------------------------------------


class TestMaybeAddPrefix:
    def test_adds_prefix_when_missing(self) -> None:
        result = _maybe_add_prefix("my_flag")
        assert result == "feature_flag:my_flag"

    def test_no_double_prefix(self) -> None:
        result = _maybe_add_prefix("feature_flag:my_flag")
        assert result == "feature_flag:my_flag"

    def test_empty_name(self) -> None:
        result = _maybe_add_prefix("")
        assert result == "feature_flag:"


# ---------------------------------------------------------------------------
# strtobool
# ---------------------------------------------------------------------------


class TestStrtobool:
    def test_true_values(self) -> None:
        for val in ["y", "yes", "yupp", "t", "true", "on", "1", "True", "YES", " 1 "]:
            assert strtobool(val) is True

    def test_false_values(self) -> None:
        for val in ["n", "no", "nopef", "false", "off", "0"]:
            assert strtobool(val) is False

    def test_invalid_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid truth value"):
            strtobool("maybe")

    def test_case_insensitive(self) -> None:
        assert strtobool("TRUE") is True
        assert strtobool("FALSE") is False


# ---------------------------------------------------------------------------
# get_feature_value_from_redis_no_cache
# ---------------------------------------------------------------------------


class TestGetFeatureValueFromRedisNoCache:
    @pytest.mark.asyncio
    async def test_returns_none_when_key_missing(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            result = await get_feature_value_from_redis_no_cache("nonexistent_flag")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_parsed_value(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(True))

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            result = await get_feature_value_from_redis_no_cache("some_flag")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_integer_value(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(42))

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            result = await get_feature_value_from_redis_no_cache("int_flag")

        assert result == 42


# ---------------------------------------------------------------------------
# get_feature_value
# ---------------------------------------------------------------------------


class TestGetFeatureValue:
    @pytest.mark.asyncio
    async def test_returns_redis_value_when_present(self) -> None:
        import json

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(123))

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            result = await get_feature_value_from_redis_no_cache("some_flag")
        assert result == 123

    @pytest.mark.asyncio
    async def test_falls_back_to_yaml_when_redis_returns_none(self) -> None:
        defaults = {"my_flag": "yaml_default"}

        with (
            patch(
                "ypl.backend.feature_flags.get_feature_value_from_redis_no_cache",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("ypl.backend.feature_flags.get_default_feature_flags", return_value=defaults),
        ):
            from ypl.backend.feature_flags import get_feature_value as _gfv

            # Clear cache before calling
            _gfv.cache_clear()
            result = await _gfv("my_flag")

        assert result == "yaml_default"

    @pytest.mark.asyncio
    async def test_returns_default_param_when_not_in_yaml(self) -> None:
        with (
            patch(
                "ypl.backend.feature_flags.get_feature_value_from_redis_no_cache",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("ypl.backend.feature_flags.get_default_feature_flags", return_value={}),
        ):
            from ypl.backend.feature_flags import get_feature_value as _gfv

            _gfv.cache_clear()
            result = await _gfv("missing_flag", default="my_default")

        assert result == "my_default"

    @pytest.mark.asyncio
    async def test_redis_error_falls_back_to_yaml(self) -> None:
        defaults = {"fallback_flag": "fallback_value"}

        with (
            patch(
                "ypl.backend.feature_flags.get_feature_value_from_redis_no_cache",
                new_callable=AsyncMock,
                side_effect=Exception("Redis unavailable"),
            ),
            patch("ypl.backend.feature_flags.get_default_feature_flags", return_value=defaults),
        ):
            from ypl.backend.feature_flags import get_feature_value as _gfv

            _gfv.cache_clear()
            result = await _gfv("fallback_flag")

        assert result == "fallback_value"


# ---------------------------------------------------------------------------
# is_feature_enabled
# ---------------------------------------------------------------------------


class TestIsFeatureEnabled:
    @pytest.mark.asyncio
    async def test_true_boolean(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value=True,
        ):
            assert await is_feature_enabled("flag") is True

    @pytest.mark.asyncio
    async def test_false_boolean(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await is_feature_enabled("flag") is False

    @pytest.mark.asyncio
    async def test_none_returns_false(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await is_feature_enabled("flag") is False

    @pytest.mark.asyncio
    async def test_string_true(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value="true",
        ):
            assert await is_feature_enabled("flag") is True

    @pytest.mark.asyncio
    async def test_string_false(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value="false",
        ):
            assert await is_feature_enabled("flag") is False

    @pytest.mark.asyncio
    async def test_invalid_string_returns_false(self) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_value",
            new_callable=AsyncMock,
            return_value="maybe",
        ):
            assert await is_feature_enabled("flag") is False


# ---------------------------------------------------------------------------
# set_feature_value
# ---------------------------------------------------------------------------


class TestSetFeatureValue:
    @pytest.mark.asyncio
    async def test_serializes_and_stores(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            await set_feature_value("my_flag", True)

        mock_redis.set.assert_awaited_once()
        call_args = mock_redis.set.call_args[0]
        assert "feature_flag:my_flag" in call_args[0]
        assert json.loads(call_args[1]) is True

    @pytest.mark.asyncio
    async def test_redis_error_logged_not_raised(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=Exception("Redis down"))

        with patch("ypl.backend.feature_flags.get_redis_client", new_callable=AsyncMock, return_value=mock_redis):
            # Should not raise
            await set_feature_value("my_flag", 42)


# ---------------------------------------------------------------------------
# get_feature_flags_from_yml
# ---------------------------------------------------------------------------


class TestGetFeatureFlagsFromYml:
    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with patch(
            "ypl.backend.feature_flags.get_feature_flags_path",
            return_value=tmp_path / "nonexistent.yml",
        ):
            result = get_feature_flags_from_yml()
        assert result == []

    def test_parses_valid_flags(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""
            feature_flags:
              - name: test_flag
                description: "A test flag"
                default_state: true
                critical: false
              - name: another_flag
                description: "Another flag"
                default_state: 100
        """)
        yml_file = tmp_path / "feature_flags.yml"
        yml_file.write_text(yaml_content)

        with patch("ypl.backend.feature_flags.get_feature_flags_path", return_value=yml_file):
            result = get_feature_flags_from_yml()

        assert len(result) == 2
        assert result[0]["name"] == "test_flag"
        assert result[1]["name"] == "another_flag"

    def test_sets_critical_default_to_false(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""
            feature_flags:
              - name: no_critical_field
                description: "Missing critical"
                default_state: true
        """)
        yml_file = tmp_path / "feature_flags.yml"
        yml_file.write_text(yaml_content)

        with patch("ypl.backend.feature_flags.get_feature_flags_path", return_value=yml_file):
            result = get_feature_flags_from_yml()

        assert result[0]["critical"] is False

    def test_skips_invalid_flags(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""
            feature_flags:
              - name: valid_flag
                description: "Valid"
                default_state: true
              - description: "Missing name"
                default_state: false
        """)
        yml_file = tmp_path / "feature_flags.yml"
        yml_file.write_text(yaml_content)

        with patch("ypl.backend.feature_flags.get_feature_flags_path", return_value=yml_file):
            result = get_feature_flags_from_yml()

        assert len(result) == 1
        assert result[0]["name"] == "valid_flag"

    def test_returns_empty_on_invalid_yaml(self, tmp_path: Path) -> None:
        yml_file = tmp_path / "bad.yml"
        yml_file.write_text("{ invalid: yaml: [")

        with patch("ypl.backend.feature_flags.get_feature_flags_path", return_value=yml_file):
            result = get_feature_flags_from_yml()

        assert result == []

    def test_returns_empty_when_feature_flags_key_missing(self, tmp_path: Path) -> None:
        yml_file = tmp_path / "wrong.yml"
        yml_file.write_text("other_key:\n  - foo")

        with patch("ypl.backend.feature_flags.get_feature_flags_path", return_value=yml_file):
            result = get_feature_flags_from_yml()

        assert result == []


# ---------------------------------------------------------------------------
# get_default_feature_flags
# ---------------------------------------------------------------------------


class TestGetDefaultFeatureFlags:
    def test_returns_name_to_default_state_mapping(self) -> None:
        flags = [
            {"name": "flag_a", "description": "A", "default_state": True, "critical": False},
            {"name": "flag_b", "description": "B", "default_state": 42, "critical": True},
        ]
        with patch("ypl.backend.feature_flags.get_feature_flags_from_yml", return_value=flags):
            get_default_feature_flags.cache_clear()
            result = get_default_feature_flags()

        assert result == {"flag_a": True, "flag_b": 42}


# ---------------------------------------------------------------------------
# is_feature_flag_critical
# ---------------------------------------------------------------------------


class TestIsFeatureFlagCritical:
    def test_critical_flag(self) -> None:
        flags = [{"name": "critical_flag", "description": "X", "default_state": True, "critical": True}]
        with patch("ypl.backend.feature_flags.get_feature_flags_from_yml", return_value=flags):
            assert is_feature_flag_critical("critical_flag") is True

    def test_non_critical_flag(self) -> None:
        flags = [{"name": "normal_flag", "description": "X", "default_state": True, "critical": False}]
        with patch("ypl.backend.feature_flags.get_feature_flags_from_yml", return_value=flags):
            assert is_feature_flag_critical("normal_flag") is False

    def test_missing_flag_returns_false(self) -> None:
        with patch("ypl.backend.feature_flags.get_feature_flags_from_yml", return_value=[]):
            assert is_feature_flag_critical("nonexistent") is False

    def test_flag_without_critical_key_defaults_false(self) -> None:
        flags = [{"name": "no_critical", "description": "X", "default_state": True}]
        with patch("ypl.backend.feature_flags.get_feature_flags_from_yml", return_value=flags):
            assert is_feature_flag_critical("no_critical") is False
