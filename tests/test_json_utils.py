"""Unit tests for backend utils/json.py.

Covers:
- CustomJSONEncoder (UUID, datetime, date, time, timedelta, Decimal, Enum,
  Path, BaseModel, set, bytes UTF-8, bytes binary, numpy types, to_json,
  __dict__, unknown type fallback)
- json_dumps
- orjson_dumps
- _convert_dict_keys
"""

from __future__ import annotations
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

import numpy as np
from pydantic import BaseModel
from ypl.backend.utils.json import CustomJSONEncoder, _convert_dict_keys, json_dumps, orjson_dumps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode(obj: object) -> object:
    return CustomJSONEncoder().default(obj)


def _round_trip(obj: object) -> str:
    return json.dumps(obj, cls=CustomJSONEncoder)


# ---------------------------------------------------------------------------
# CustomJSONEncoder
# ---------------------------------------------------------------------------


class TestCustomJSONEncoderUUID:
    def test_uuid_serialized_as_string(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        result = _encode(uid)
        assert result == "12345678-1234-5678-1234-567812345678"


class TestCustomJSONEncoderDatetime:
    def test_datetime_serialized_as_isoformat(self) -> None:
        dt = datetime(2025, 1, 15, 12, 30, 45, tzinfo=UTC)
        result = _encode(dt)
        assert isinstance(result, str)
        assert "2025-01-15" in result
        assert "12:30:45" in result

    def test_date_serialized(self) -> None:
        d = date(2025, 6, 1)
        result = _encode(d)
        assert result == "2025-06-01"

    def test_time_serialized(self) -> None:
        t = time(8, 30, 0)
        result = _encode(t)
        assert result == "08:30:00"

    def test_timedelta_serialized_as_string(self) -> None:
        td = timedelta(days=1, hours=2)
        result = _encode(td)
        assert isinstance(result, str)
        assert "1 day" in result


class TestCustomJSONEncoderDecimal:
    def test_decimal_serialized_as_string(self) -> None:
        d = Decimal("3.14159")
        result = _encode(d)
        assert result == "3.14159"


class TestCustomJSONEncoderEnum:
    def test_enum_serialized_as_value(self) -> None:
        class Color(Enum):
            RED = "red"
            BLUE = 42

        assert _encode(Color.RED) == "red"
        assert _encode(Color.BLUE) == 42


class TestCustomJSONEncoderPath:
    def test_path_serialized_as_string(self) -> None:
        p = Path("/home/user/file.txt")
        result = _encode(p)
        assert result == "/home/user/file.txt"


class TestCustomJSONEncoderBaseModel:
    def test_pydantic_model_serialized(self) -> None:
        class MyModel(BaseModel):
            name: str
            value: int

        obj = MyModel(name="test", value=42)
        result = _encode(obj)
        assert result == {"name": "test", "value": 42}


class TestCustomJSONEncoderSet:
    def test_set_serialized_as_list(self) -> None:
        s = {1, 2, 3}
        result = _encode(s)
        assert isinstance(result, list)
        assert sorted(result) == [1, 2, 3]


class TestCustomJSONEncoderBytes:
    def test_utf8_bytes_decoded_as_string(self) -> None:
        b = b"hello world"
        result = _encode(b)
        assert result == "hello world"

    def test_binary_bytes_encoded_as_base64(self) -> None:
        b = bytes(range(256))
        result = _encode(b)
        assert isinstance(result, str)
        assert result.startswith("base64:")

    def test_non_utf8_bytes_base64(self) -> None:
        b = b"\xff\xfe"
        result = _encode(b)
        assert isinstance(result, str)
        assert result.startswith("base64:")


class TestCustomJSONEncoderNumpy:
    def test_numpy_int(self) -> None:
        result = _encode(np.int64(42))
        assert result == 42
        assert isinstance(result, int)

    def test_numpy_float(self) -> None:
        result = _encode(np.float64(3.14))
        assert isinstance(result, float)
        assert abs(result - 3.14) < 1e-10

    def test_numpy_bool_true(self) -> None:
        result = _encode(np.bool_(True))
        assert result is True

    def test_numpy_bool_false(self) -> None:
        result = _encode(np.bool_(False))
        assert result is False

    def test_numpy_ndarray(self) -> None:
        arr = np.array([1, 2, 3])
        result = _encode(arr)
        assert result == [1, 2, 3]

    def test_numpy_2d_ndarray(self) -> None:
        arr = np.array([[1, 2], [3, 4]])
        result = _encode(arr)
        assert result == [[1, 2], [3, 4]]


class TestCustomJSONEncoderToJson:
    def test_object_with_to_json_method(self) -> None:
        class HasToJson:
            def to_json(self) -> dict:
                return {"key": "value"}

        result = _encode(HasToJson())
        assert result == {"key": "value"}


class TestCustomJSONEncoderDict:
    def test_object_with_dict_attribute(self) -> None:
        class Simple:
            def __init__(self) -> None:
                self.x = 1
                self.y = 2

        result = _encode(Simple())
        assert result == {"x": 1, "y": 2}


class TestCustomJSONEncoderFallback:
    def test_object_with_dict_falls_back_to_dict_repr(self) -> None:
        """Objects with __dict__ are serialized via __dict__ fallback."""

        class Simple:
            def __init__(self) -> None:
                self.x = 42

        result = _round_trip(Simple())
        data = json.loads(result)
        assert data["x"] == 42


# ---------------------------------------------------------------------------
# _convert_dict_keys
# ---------------------------------------------------------------------------


class TestConvertDictKeys:
    def test_string_keys_unchanged(self) -> None:
        d = {"a": 1, "b": 2}
        result = _convert_dict_keys(d)
        assert result == {"a": 1, "b": 2}

    def test_numeric_keys_converted_to_string(self) -> None:
        d = {1: "one", 2: "two"}
        result = _convert_dict_keys(d)
        assert result == {"1": "one", "2": "two"}

    def test_nested_dict(self) -> None:
        d = {1: {2: "nested"}}
        result = _convert_dict_keys(d)
        assert result == {"1": {"2": "nested"}}

    def test_list_values_converted(self) -> None:
        d = {"key": [1, 2, 3]}
        result = _convert_dict_keys(d)
        assert result == {"key": [1, 2, 3]}

    def test_non_dict_returned_as_is(self) -> None:
        assert _convert_dict_keys(42) == 42
        assert _convert_dict_keys("hello") == "hello"
        assert _convert_dict_keys([1, 2, 3]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# json_dumps
# ---------------------------------------------------------------------------


class TestJsonDumps:
    def test_basic_dict(self) -> None:
        result = json_dumps({"a": 1})
        assert json.loads(result) == {"a": 1}

    def test_uuid_in_dict(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        result = json_dumps({"id": uid})
        data = json.loads(result)
        assert data["id"] == "12345678-1234-5678-1234-567812345678"

    def test_datetime_in_dict(self) -> None:
        dt = datetime(2025, 1, 1, tzinfo=UTC)
        result = json_dumps({"ts": dt})
        data = json.loads(result)
        assert "2025-01-01" in data["ts"]

    def test_numeric_keys_converted(self) -> None:
        result = json_dumps({1: "val"})
        data = json.loads(result)
        assert data["1"] == "val"

    def test_kwargs_passed_through(self) -> None:
        result = json_dumps({"b": 2, "a": 1}, sort_keys=True)
        assert result.index('"a"') < result.index('"b"')


# ---------------------------------------------------------------------------
# orjson_dumps
# ---------------------------------------------------------------------------


class TestOrjsonDumps:
    def test_basic_dict(self) -> None:
        result = orjson_dumps({"a": 1})
        assert json.loads(result) == {"a": 1}

    def test_returns_bytes(self) -> None:
        result = orjson_dumps({"x": 42})
        assert isinstance(result, bytes)

    def test_uuid_serialized(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        result = orjson_dumps({"id": uid})
        data = json.loads(result)
        assert data["id"] == "12345678-1234-5678-1234-567812345678"

    def test_default_kwarg_ignored(self) -> None:
        """orjson_dumps pops 'default' from kwargs."""

        def my_default(obj: object) -> str:
            return "custom"

        # Should not raise even with default kwarg
        result = orjson_dumps({"a": 1}, default=my_default)
        assert json.loads(result) == {"a": 1}
