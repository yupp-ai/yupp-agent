"""
Processors for structlog to handle redaction, truncation, and error details.
"""

import enum
import os
import re
import sys
import traceback
from collections.abc import MutableMapping
from typing import Any

import orjson
from pydantic import BaseModel

from ypl.backend.utils.json import orjson_dumps
from ypl.loggers.config import (
    GITHUB_REPO_URL,
    IS_PRODUCTION,
    MAX_FIELD_LENGTH,
    MAX_MESSAGE_LENGTH,
)

SENSITIVE_PATTERNS = {
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b": lambda m: "*" * len(m.group(0)),  # Email
    r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b": lambda m: "*" * len(m.group(0)),  # Credit card
    r"\b\d{3}[-\s]?\d{3}[-\s]?\d{4}\b": lambda m: "*" * len(m.group(0)),  # Phone
}


def get_message_size(event_dict: dict[str, Any]) -> int:
    """Calculate the size of the log message when serialized to JSON."""
    try:
        return len(orjson_dumps(event_dict))
    except Exception:
        return MAX_MESSAGE_LENGTH + 1


def create_github_link(file_path: str, line_no: int) -> str | None:
    """Generate a GitHub URL for the given file and line number."""
    if "site-packages" in file_path:
        return None  # Skip third-party packages

    try:
        file_path = file_path.removeprefix("/app")
        github_tag = os.environ.get("BUILD_GIT_SHA", "latest-production" if IS_PRODUCTION else "latest-development")
        return f"{GITHUB_REPO_URL}/blob/{github_tag}{file_path}#L{line_no}"
    except Exception:
        return None


def truncate_field(value: Any) -> Any:
    """Truncate field values that exceed the maximum length."""
    if isinstance(value, str) and len(value) > MAX_FIELD_LENGTH:
        return value[:MAX_FIELD_LENGTH] + "..."
    if isinstance(value, dict):
        return {k: truncate_field(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(truncate_field(item) for item in value)
    return value


def redact_json_sensitive_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive fields in JSON data."""
    sensitive_keys = ["last4", "password", "token", "secret", "key"]
    result: dict[str, Any] = {}

    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = redact_json_sensitive_fields(v)
        elif isinstance(v, list | tuple):
            result[k] = type(v)(redact_sensitive_data(item) for item in v)
        elif k.lower() in sensitive_keys:
            result[k] = "[REDACTED]"
        else:
            result[k] = v
    return result


def redact_sensitive_data(value: Any) -> Any:
    """Redact sensitive information from log messages."""
    if not IS_PRODUCTION:
        return value

    try:
        if isinstance(value, str):
            # Apply regex patterns
            try:
                for pattern, replacement in SENSITIVE_PATTERNS.items():
                    value = re.sub(pattern, replacement, value)
            except Exception:
                pass

            # Handle JSON strings
            if value.strip().startswith("{"):
                try:
                    data = orjson.loads(value)
                    if isinstance(data, dict):
                        data = redact_json_sensitive_fields(data)
                        return orjson_dumps(data).decode()
                except (orjson.JSONDecodeError, TypeError):
                    pass
            return value
        if isinstance(value, dict):
            return redact_json_sensitive_fields(value)
        if isinstance(value, list | tuple):
            try:
                return type(value)(redact_sensitive_data(item) for item in value)
            except Exception:
                return value
        return value
    except Exception:
        return value


def truncate_large_message(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Intelligently truncate a large log message."""
    essential_fields = {"event", "level", "timestamp", "flow", "module", "process_info"}

    if get_message_size(event_dict) <= MAX_MESSAGE_LENGTH:
        return event_dict

    # Create new dict with essential fields
    result: dict[str, Any] = {k: v for k, v in event_dict.items() if k in essential_fields}
    result["_truncated"] = True
    result["_original_size"] = get_message_size(event_dict)

    # Add as many non-essential fields as will fit
    remaining_size = MAX_MESSAGE_LENGTH - get_message_size(result)
    truncated_fields: list[str] = []

    for k, v in event_dict.items():
        if k in essential_fields:
            continue

        item_size = get_message_size({k: v})
        if remaining_size >= item_size:
            result[k] = v
            remaining_size -= item_size
        elif isinstance(v, dict | list | tuple) and remaining_size > 100:
            result[k] = f"[TRUNCATED: {item_size} bytes]"
            remaining_size -= len(result[k])
        else:
            truncated_fields.append(k)

    if truncated_fields:
        result["_truncated_fields"] = truncated_fields

    return result


class ThreadInfoWrapper:
    """Processor that wraps callsite parameters into a thread_info field."""

    def __call__(self, _: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Wrap callsite parameters into a thread_info field."""
        result_dict = dict(event_dict)

        # Extract thread-related fields
        thread_info = {
            "thread_id": result_dict.pop("thread", None),
            "thread_name": result_dict.pop("thread_name", None),
        }

        # Add thread_info to the event dict
        result_dict["thread_info"] = thread_info

        return result_dict


class RedactSensitiveProcessor:
    """Processor that redacts sensitive data."""

    def __call__(self, _: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Process the event dictionary to redact sensitive data."""
        result: dict[str, Any] = redact_sensitive_data(dict(event_dict))
        return result


HEX_OR_UUID_REGEX = re.compile(
    r"(0x[0-9a-fA-F]{6,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Matches any number with 5 or more digits (e.g., task IDs, line numbers, PIDs)
LONG_NUMBER_REGEX = re.compile(r"\d{5,}")


def normalize_error_message_string(message: str) -> str:
    """
    Normalize the error message string by removing variable values.
    This is used to avoid alerting multiple times for the same error due to different variable values.

    E.g. "Connector error: 0x12345678" ->
         ("Connector error: 0x[NORMALIZED]")
        "Error for user: 0123-uuid-1234" ->
        ("Error for user: <NORMALIZED-UUID>")
    """

    def replacement_func(match: re.Match) -> str:
        matched_text = match.group(0)

        if matched_text.startswith("0x"):
            return "0x[NORMALIZED]"
        return "<NORMALIZED-UUID>"

    message = HEX_OR_UUID_REGEX.sub(replacement_func, message)
    return LONG_NUMBER_REGEX.sub("<N>", message)


class NormalizeErrorMessageProcessor:
    """Processor that normalizes error messages by removing variable values."""

    def __call__(self, _: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Process the event dictionary to normalize error messages."""

        if method_name not in ("error", "critical"):
            return dict(event_dict)

        if "event" in event_dict:
            original_message = event_dict["event"]
            normalized_message = normalize_error_message_string(event_dict["event"])
            if normalized_message != original_message:
                event_dict["event"] = normalized_message
                event_dict["_normalized_message"] = True
                event_dict["_original_message"] = original_message

        return dict(event_dict)


class SerializeModelsProcessor:
    """Processor that serializes model objects to dict to avoid schema overhead."""

    def __call__(self, _: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Convert model objects to dict representation."""
        # Early exit for simple messages with only primitive types
        if not self._needs_processing(event_dict):
            return dict(event_dict)

        result_dict = dict(event_dict)
        for key, value in result_dict.items():
            result_dict[key] = self._serialize_object(value)

        return result_dict

    def _needs_processing(self, event_dict: MutableMapping[str, Any]) -> bool:
        """Quick check if event_dict contains objects that need serialization."""
        return any(self._is_complex_object(value) for value in event_dict.values())

    def _is_complex_object(self, value: Any) -> bool:
        """Check if value is a complex object that needs serialization."""
        # Quick primitive type check
        if isinstance(value, str | int | float | bool | type(None)):
            return False

        # Check for Pydantic models (most common case)
        if isinstance(value, BaseModel):
            return True

        # Check for objects with __dict__ (but not built-in types)
        if hasattr(value, "__dict__") and not isinstance(value, str | int | float | bool | type(None)):
            return True

        # Check collections recursively
        if isinstance(value, dict):
            return any(self._is_complex_object(v) for v in value.values())
        if isinstance(value, list | tuple | set):
            return any(self._is_complex_object(item) for item in value)

        return False

    def _serialize_object(self, value: Any, depth: int = 0) -> Any:
        """Recursively serialize objects to simple types."""
        if depth > 5:
            return "<Object recursion depth exceeded>"

        # Handle enums - serialize to their values
        if isinstance(value, enum.Enum):
            return value.value

        # Handle Pydantic models
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")

        # Handle other objects with common serialization methods
        if hasattr(value, "__dict__") and not isinstance(value, str | int | float | bool | type(None)):
            # Check if it's a SQLAlchemy model or similar ORM object
            if hasattr(value, "__table__") or hasattr(value, "__mapper__"):
                # SQLAlchemy model - get only loaded column values
                try:
                    state = value.__dict__
                    return {col: state.get(col) for col in value.__table__.columns}
                except Exception:
                    return f"<{type(value).__name__} object at {id(value)}>"

            # Handle dataclasses
            if hasattr(value, "__dataclass_fields__"):
                try:
                    return {
                        k: self._serialize_object(v, depth + 1)
                        for k, v in value.__dict__.items()
                        if not k.startswith("_")
                    }
                except Exception:
                    return f"<{type(value).__name__} object at {id(value)}>"

            # Handle regular objects with __dict__
            try:
                obj_dict = {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
                return {k: self._serialize_object(v, depth + 1) for k, v in obj_dict.items()}
            except Exception:
                return f"<{type(value).__name__} object at {id(value)}>"

        # Handle collections
        elif isinstance(value, dict):
            return {k: self._serialize_object(v, depth + 1) for k, v in value.items()}
        elif isinstance(value, list | tuple | set):
            return type(value)(self._serialize_object(item, depth + 1) for item in value)

        # Return primitive types as-is
        else:
            return value


class TruncateMessageProcessor:
    """Processor that truncates large field values and messages."""

    def __call__(self, _: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Process the event dictionary to truncate large values."""
        event_dict_as_dict = dict(event_dict)

        # Truncate individual fields
        for key, value in event_dict_as_dict.items():
            event_dict_as_dict[key] = truncate_field(value)

        # Truncate entire message if needed
        return truncate_large_message(event_dict_as_dict)


class ErrorDetailsProcessor:
    """Processor that adds error details to logs when exceptions are present."""

    def __call__(self, _: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        """Add error details if exc_info is present or if this is an error log."""
        result_dict = dict(event_dict)

        # If this is an error log and no exc_info is present, add it
        if method_name == "error" and not result_dict.get("exc_info"):
            result_dict["exc_info"] = True

        # Get exception info for metadata extraction
        exc_info = sys.exc_info()
        if not exc_info or not exc_info[0]:
            return result_dict

        try:
            # Extract traceback information
            tb = traceback.extract_tb(exc_info[2])
            if not tb:
                return result_dict

            # Get details from the last frame
            last_frame = tb[-1]
            line_no = last_frame.lineno if last_frame.lineno is not None else 0
            func_name = last_frame.name if hasattr(last_frame, "name") else "unknown_function"
            code_context = last_frame.line if hasattr(last_frame, "line") else None
            github_url = create_github_link(last_frame.filename, line_no)

            # Build error context
            error_context = {
                "error_type": exc_info[0].__name__,
                "error_file": last_frame.filename,
                "error_line": line_no,
                "error_function": func_name,
            }

            if code_context:
                error_context["error_code_context"] = code_context

            if github_url:
                error_context["github_url"] = github_url

            result_dict.update(error_context)
        except Exception:
            pass  # Silently continue on errors

        return result_dict
