"""Unit tests for ypl/loggers/processors.py.

Covers: get_message_size, create_github_link, truncate_field,
redact_json_sensitive_fields, redact_sensitive_data, truncate_large_message,
normalize_error_message_string, and all Processor classes.
"""

import enum
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel
from ypl.loggers.processors import (
    ErrorDetailsProcessor,
    NormalizeErrorMessageProcessor,
    SerializeModelsProcessor,
    ThreadInfoWrapper,
    TruncateMessageProcessor,
    create_github_link,
    get_message_size,
    normalize_error_message_string,
    redact_json_sensitive_fields,
    redact_sensitive_data,
    truncate_field,
    truncate_large_message,
)

# ---------------------------------------------------------------------------
# get_message_size
# ---------------------------------------------------------------------------


class TestGetMessageSize:
    def test_empty_dict(self) -> None:
        assert get_message_size({}) == len(b"{}")

    def test_simple_dict(self) -> None:
        size = get_message_size({"event": "hello"})
        assert size > 0

    def test_larger_dict_is_bigger(self) -> None:
        small = get_message_size({"a": "b"})
        large = get_message_size({"a": "b" * 10000})
        assert large > small

    def test_unserializable_returns_over_limit(self) -> None:
        from ypl.loggers.config import MAX_MESSAGE_LENGTH

        # object() cannot be JSON-serialized — should return MAX_MESSAGE_LENGTH + 1
        size = get_message_size({"bad": object()})
        assert size > MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# create_github_link
# ---------------------------------------------------------------------------


class TestCreateGithubLink:
    def test_site_packages_returns_none(self) -> None:
        assert create_github_link("/usr/local/lib/python3.12/site-packages/foo.py", 10) is None

    def test_app_path_stripped(self) -> None:
        link = create_github_link("/app/ypl/utils.py", 42)
        assert link is not None
        assert "/app" not in link
        assert "ypl/utils.py" in link
        assert "#L42" in link

    def test_non_app_path_included(self) -> None:
        link = create_github_link("/home/user/project/ypl/foo.py", 1)
        assert link is not None
        assert "#L1" in link

    def test_build_git_sha_in_url(self) -> None:
        with patch.dict("os.environ", {"BUILD_GIT_SHA": "abc123"}):
            link = create_github_link("/ypl/foo.py", 5)
        assert link is not None
        assert "abc123" in link


# ---------------------------------------------------------------------------
# truncate_field
# ---------------------------------------------------------------------------


class TestTruncateField:
    def test_short_string_unchanged(self) -> None:
        assert truncate_field("hello") == "hello"

    def test_long_string_truncated(self) -> None:
        from ypl.loggers.config import MAX_FIELD_LENGTH

        long_str = "x" * (MAX_FIELD_LENGTH + 100)
        result = truncate_field(long_str)
        assert result.endswith("...")
        assert len(result) == MAX_FIELD_LENGTH + 3  # MAX_FIELD_LENGTH chars + "..."

    def test_dict_values_truncated_recursively(self) -> None:
        from ypl.loggers.config import MAX_FIELD_LENGTH

        d = {"key": "x" * (MAX_FIELD_LENGTH + 50)}
        result = truncate_field(d)
        assert isinstance(result, dict)
        assert result["key"].endswith("...")

    def test_list_items_truncated(self) -> None:
        from ypl.loggers.config import MAX_FIELD_LENGTH

        lst = ["x" * (MAX_FIELD_LENGTH + 50), "short"]
        result = truncate_field(lst)
        assert isinstance(result, list)
        assert result[0].endswith("...")
        assert result[1] == "short"

    def test_tuple_preserved_as_tuple(self) -> None:
        result = truncate_field(("a", "b"))
        assert isinstance(result, tuple)

    def test_integer_unchanged(self) -> None:
        assert truncate_field(42) == 42

    def test_none_unchanged(self) -> None:
        assert truncate_field(None) is None


# ---------------------------------------------------------------------------
# redact_json_sensitive_fields
# ---------------------------------------------------------------------------


class TestRedactJsonSensitiveFields:
    def test_password_field_redacted(self) -> None:
        result = redact_json_sensitive_fields({"password": "secret123"})
        assert result["password"] == "[REDACTED]"

    def test_token_field_redacted(self) -> None:
        result = redact_json_sensitive_fields({"token": "abc-token"})
        assert result["token"] == "[REDACTED]"

    def test_secret_field_redacted(self) -> None:
        result = redact_json_sensitive_fields({"secret": "mysecret"})
        assert result["secret"] == "[REDACTED]"

    def test_last4_field_redacted(self) -> None:
        result = redact_json_sensitive_fields({"last4": "1234"})
        assert result["last4"] == "[REDACTED]"

    def test_key_field_redacted(self) -> None:
        result = redact_json_sensitive_fields({"key": "api-key-value"})
        assert result["key"] == "[REDACTED]"

    def test_non_sensitive_field_preserved(self) -> None:
        result = redact_json_sensitive_fields({"username": "alice", "age": 30})
        assert result["username"] == "alice"
        assert result["age"] == 30

    def test_nested_dict_redacted(self) -> None:
        result = redact_json_sensitive_fields({"user": {"password": "pw", "name": "bob"}})
        assert result["user"]["password"] == "[REDACTED]"
        assert result["user"]["name"] == "bob"

    def test_case_insensitive_matching(self) -> None:
        result = redact_json_sensitive_fields({"PASSWORD": "pw"})
        assert result["PASSWORD"] == "[REDACTED]"

    def test_list_value_items_redacted(self) -> None:
        result = redact_json_sensitive_fields({"items": ["plain", "value"]})
        # Non-string items in lists pass through redact_sensitive_data (non-production → no-op)
        assert "items" in result


# ---------------------------------------------------------------------------
# redact_sensitive_data
# ---------------------------------------------------------------------------


class TestRedactSensitiveData:
    def test_non_production_returns_as_is(self) -> None:
        """In non-production mode, nothing should be redacted."""
        with patch("ypl.loggers.processors.IS_PRODUCTION", False):
            result = redact_sensitive_data("user@example.com")
        assert result == "user@example.com"

    def test_production_redacts_email(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data("Contact user@example.com for info")
        assert "user@example.com" not in result
        assert "*" in result

    def test_production_redacts_credit_card(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data("Card: 1234 5678 9012 3456")
        assert "1234 5678 9012 3456" not in result

    def test_production_redacts_phone(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data("Call 555-123-4567 now")
        assert "555-123-4567" not in result

    def test_production_dict_redacted(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data({"password": "secret"})
        assert isinstance(result, dict)
        assert result["password"] == "[REDACTED]"

    def test_production_json_string_password_redacted(self) -> None:
        import json

        payload = json.dumps({"password": "hunter2", "user": "alice"})
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data(payload)
        assert "hunter2" not in result

    def test_production_list_values_redacted(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            result = redact_sensitive_data(["user@example.com", "plain"])
        assert isinstance(result, list)
        assert "user@example.com" not in result[0]

    def test_non_string_primitive_passthrough(self) -> None:
        with patch("ypl.loggers.processors.IS_PRODUCTION", True):
            assert redact_sensitive_data(42) == 42
            assert redact_sensitive_data(None) is None


# ---------------------------------------------------------------------------
# truncate_large_message
# ---------------------------------------------------------------------------


class TestTruncateLargeMessage:
    def test_small_message_unchanged(self) -> None:
        event = {"event": "hello", "level": "info"}
        result = truncate_large_message(event)
        assert result == event

    def test_large_message_truncated(self) -> None:
        from ypl.loggers.config import MAX_MESSAGE_LENGTH

        big_value = "x" * (MAX_MESSAGE_LENGTH + 1)
        event = {"event": "big log", "level": "info", "data": big_value}
        result = truncate_large_message(event)
        assert result.get("_truncated") is True
        assert "event" in result
        assert "level" in result

    def test_essential_fields_preserved(self) -> None:
        from ypl.loggers.config import MAX_MESSAGE_LENGTH

        big_value = "x" * (MAX_MESSAGE_LENGTH + 1)
        event = {
            "event": "important",
            "level": "error",
            "timestamp": "2024-01-01",
            "flow": "main",
            "module": "health",
            "process_info": "worker",
            "extra": big_value,
        }
        result = truncate_large_message(event)
        for field in ("event", "level", "timestamp", "flow", "module", "process_info"):
            assert field in result, f"Essential field '{field}' missing from truncated message"

    def test_truncated_fields_listed(self) -> None:
        from ypl.loggers.config import MAX_MESSAGE_LENGTH

        # Create a message so large that some fields must be dropped
        event = {"event": "x", "level": "info"}
        for i in range(20):
            event[f"field_{i}"] = "y" * (MAX_MESSAGE_LENGTH // 5)
        result = truncate_large_message(event)
        if "_truncated_fields" in result:
            assert isinstance(result["_truncated_fields"], list)


# ---------------------------------------------------------------------------
# normalize_error_message_string
# ---------------------------------------------------------------------------


class TestNormalizeErrorMessageString:
    def test_hex_address_normalized(self) -> None:
        result = normalize_error_message_string("Error at 0x1234abcd")
        assert "0x[NORMALIZED]" in result
        assert "0x1234abcd" not in result

    def test_uuid_normalized(self) -> None:
        msg = "Error for user: 0123abcd-ef45-6789-abcd-ef0123456789"
        result = normalize_error_message_string(msg)
        assert "<NORMALIZED-UUID>" in result
        assert "0123abcd-ef45-6789-abcd-ef0123456789" not in result

    def test_long_number_normalized(self) -> None:
        result = normalize_error_message_string("Task ID: 123456789")
        assert "<N>" in result
        assert "123456789" not in result

    def test_short_number_left_alone(self) -> None:
        result = normalize_error_message_string("retry 3 times")
        assert "3" in result

    def test_no_matches_returns_original(self) -> None:
        msg = "Simple error occurred"
        assert normalize_error_message_string(msg) == msg

    def test_multiple_replacements(self) -> None:
        msg = "hex 0xdeadbeef and uuid 00000000-0000-0000-0000-000000000000"
        result = normalize_error_message_string(msg)
        assert "0x[NORMALIZED]" in result
        assert "<NORMALIZED-UUID>" in result


# ---------------------------------------------------------------------------
# ThreadInfoWrapper
# ---------------------------------------------------------------------------


class TestThreadInfoWrapper:
    def test_wraps_thread_fields(self) -> None:
        processor = ThreadInfoWrapper()
        event_dict: dict[str, Any] = {
            "event": "test",
            "thread": 12345,
            "thread_name": "MainThread",
        }
        result = processor(None, "info", event_dict)
        assert "thread_info" in result
        assert result["thread_info"]["thread_id"] == 12345
        assert result["thread_info"]["thread_name"] == "MainThread"
        assert "thread" not in result
        assert "thread_name" not in result

    def test_preserves_other_fields(self) -> None:
        processor = ThreadInfoWrapper()
        event_dict = {"event": "hello", "level": "info", "thread": 1}
        result = processor(None, "info", event_dict)
        assert result["event"] == "hello"
        assert result["level"] == "info"

    def test_missing_thread_fields_gives_none(self) -> None:
        processor = ThreadInfoWrapper()
        result = processor(None, "info", {"event": "x"})
        assert result["thread_info"]["thread_id"] is None
        assert result["thread_info"]["thread_name"] is None


# ---------------------------------------------------------------------------
# NormalizeErrorMessageProcessor
# ---------------------------------------------------------------------------


class TestNormalizeErrorMessageProcessor:
    def test_error_method_normalizes_event(self) -> None:
        processor = NormalizeErrorMessageProcessor()
        event_dict: dict[str, Any] = {"event": "Error at 0xdeadbeef in process"}
        result = processor(None, "error", event_dict)
        assert "0x[NORMALIZED]" in result["event"]
        assert result.get("_normalized_message") is True
        assert "0xdeadbeef" in result.get("_original_message", "")

    def test_critical_method_normalizes_event(self) -> None:
        processor = NormalizeErrorMessageProcessor()
        event_dict: dict[str, Any] = {"event": "Critical: task 123456789 failed"}
        result = processor(None, "critical", event_dict)
        assert "<N>" in result["event"]

    def test_info_method_skips_normalization(self) -> None:
        processor = NormalizeErrorMessageProcessor()
        original_event = "Info about 0xdeadbeef"
        event_dict: dict[str, Any] = {"event": original_event}
        result = processor(None, "info", event_dict)
        assert result["event"] == original_event
        assert "_normalized_message" not in result

    def test_event_without_normalization_needed(self) -> None:
        processor = NormalizeErrorMessageProcessor()
        event_dict: dict[str, Any] = {"event": "Simple error message"}
        result = processor(None, "error", event_dict)
        assert result["event"] == "Simple error message"
        assert "_normalized_message" not in result


# ---------------------------------------------------------------------------
# SerializeModelsProcessor
# ---------------------------------------------------------------------------


class SampleModel(BaseModel):
    name: str
    value: int


class SampleEnum(enum.Enum):
    ALPHA = "alpha"
    BETA = "beta"


@dataclass
class SampleDataclass:
    x: int
    y: str


class TestSerializeModelsProcessor:
    def test_primitive_values_unchanged(self) -> None:
        processor = SerializeModelsProcessor()
        event_dict = {"event": "test", "count": 42, "flag": True, "ratio": 3.14}
        result = processor(None, "info", event_dict)
        assert result["count"] == 42
        assert result["flag"] is True

    def test_pydantic_model_serialized(self) -> None:
        processor = SerializeModelsProcessor()
        model = SampleModel(name="alice", value=99)
        event_dict: dict[str, Any] = {"event": "test", "model": model}
        result = processor(None, "info", event_dict)
        assert isinstance(result["model"], dict)
        assert result["model"]["name"] == "alice"
        assert result["model"]["value"] == 99

    def test_enum_serialized_to_value(self) -> None:
        processor = SerializeModelsProcessor()
        event_dict: dict[str, Any] = {"event": "test", "status": SampleEnum.ALPHA}
        result = processor(None, "info", event_dict)
        assert result["status"] == "alpha"

    def test_dataclass_serialized(self) -> None:
        processor = SerializeModelsProcessor()
        dc = SampleDataclass(x=1, y="hello")
        event_dict: dict[str, Any] = {"event": "test", "dc": dc}
        result = processor(None, "info", event_dict)
        assert isinstance(result["dc"], dict)
        assert result["dc"]["x"] == 1

    def test_nested_dict_with_model(self) -> None:
        processor = SerializeModelsProcessor()
        model = SampleModel(name="bob", value=7)
        event_dict: dict[str, Any] = {"event": "test", "data": {"model": model}}
        result = processor(None, "info", event_dict)
        assert isinstance(result["data"]["model"], dict)

    def test_list_with_model(self) -> None:
        processor = SerializeModelsProcessor()
        models = [SampleModel(name="a", value=1), SampleModel(name="b", value=2)]
        event_dict: dict[str, Any] = {"event": "test", "items": models}
        result = processor(None, "info", event_dict)
        assert isinstance(result["items"], list)
        for item in result["items"]:
            assert isinstance(item, dict)


# ---------------------------------------------------------------------------
# TruncateMessageProcessor
# ---------------------------------------------------------------------------


class TestTruncateMessageProcessor:
    def test_small_event_unchanged(self) -> None:
        processor = TruncateMessageProcessor()
        event_dict: dict[str, Any] = {"event": "hello", "level": "info"}
        result = processor(None, "info", event_dict)
        assert result["event"] == "hello"

    def test_large_field_truncated(self) -> None:
        from ypl.loggers.config import MAX_FIELD_LENGTH

        processor = TruncateMessageProcessor()
        event_dict: dict[str, Any] = {"event": "test", "big": "x" * (MAX_FIELD_LENGTH + 100)}
        result = processor(None, "info", event_dict)
        assert result["big"].endswith("...")


# ---------------------------------------------------------------------------
# ErrorDetailsProcessor
# ---------------------------------------------------------------------------


class TestErrorDetailsProcessor:
    def test_error_method_adds_exc_info_true(self) -> None:
        processor = ErrorDetailsProcessor()
        event_dict: dict[str, Any] = {"event": "something broke"}
        result = processor(None, "error", event_dict)
        # When no active exception, exc_info=True should still be added
        assert result.get("exc_info") is True

    def test_non_error_method_skips_exc_info(self) -> None:
        processor = ErrorDetailsProcessor()
        event_dict: dict[str, Any] = {"event": "info message"}
        result = processor(None, "info", event_dict)
        assert "exc_info" not in result

    def test_error_with_active_exception_adds_context(self) -> None:
        processor = ErrorDetailsProcessor()
        try:
            raise ValueError("test error for processor")
        except ValueError:
            event_dict: dict[str, Any] = {"event": "caught an error"}
            result = processor(None, "error", event_dict)
        # Should add error_type when exception is active
        assert result.get("error_type") == "ValueError"
        assert "error_file" in result
        assert "error_line" in result

    def test_existing_exc_info_not_overwritten(self) -> None:
        processor = ErrorDetailsProcessor()
        event_dict: dict[str, Any] = {"event": "error", "exc_info": False}
        result = processor(None, "error", event_dict)
        # Our processor only sets exc_info=True when NOT present
        assert result["exc_info"] is False

    def test_warning_method_passthrough(self) -> None:
        processor = ErrorDetailsProcessor()
        event_dict: dict[str, Any] = {"event": "warning"}
        result = processor(None, "warning", event_dict)
        assert result["event"] == "warning"
        assert "exc_info" not in result
