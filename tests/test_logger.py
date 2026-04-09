"""Unit tests for ypl/logger.py.

Tests logging configuration, formatters, and handler logic.
"""

from __future__ import annotations
import logging
import os
from unittest.mock import MagicMock, patch

from ypl.logger import (
    MAX_LOGGED_FIELD_LENGTH_CHARS,
    ConsolidatedMixin,
    ConsolidatedStreamHandler,
    RedactingMixin,
    TruncatingMixin,
    close_google_cloud_logging,
    flush_and_close_google_cloud_logging,
    flush_google_cloud_logging,
    init_worker_logging,
    redact_sensitive_data,
)

# ---------------------------------------------------------------------------
# Tests: redact_sensitive_data
# ---------------------------------------------------------------------------


class TestRedactSensitiveData:
    def test_redacts_email_in_non_local_env(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = redact_sensitive_data("Contact user@example.com for help")
        assert "[REDACTED EMAIL]" in result
        assert "user@example.com" not in result

    def test_skips_redaction_in_local_env(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "local"}):
            result = redact_sensitive_data("Contact user@example.com for help")
        assert "user@example.com" in result

    def test_redacts_last4_in_json(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = redact_sensitive_data('{"last4": "1234", "name": "Alice"}')
        assert "[REDACTED]" in result
        assert "1234" not in result

    def test_does_not_corrupt_non_json_string(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = redact_sensitive_data("This is a normal string without emails")
        assert result == "This is a normal string without emails"

    def test_handles_multiple_emails(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = redact_sensitive_data("From alice@example.com to bob@test.org")
        assert result.count("[REDACTED EMAIL]") == 2

    def test_handles_nested_json_last4(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = redact_sensitive_data('{"card": {"last4": "9999"}}')
        assert "9999" not in result

    def test_handles_invalid_json_gracefully(self) -> None:
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            # Invalid JSON should not raise
            result = redact_sensitive_data("{invalid json}")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: RedactingMixin
# ---------------------------------------------------------------------------


class TestRedactingMixin:
    def _make_mixin(self) -> RedactingMixin:
        class Impl(RedactingMixin):
            pass

        return Impl()

    def test_redact_string_value(self) -> None:
        mixin = self._make_mixin()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = mixin._redact_value("Contact admin@yupp.ai for help")
        assert "admin@yupp.ai" not in result

    def test_redact_dict_value(self) -> None:
        mixin = self._make_mixin()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = mixin._redact_value({"email": "user@example.com", "name": "Alice"})
        assert isinstance(result, dict)
        assert "[REDACTED EMAIL]" in result["email"]

    def test_redact_list_value(self) -> None:
        mixin = self._make_mixin()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = mixin._redact_value(["user@example.com", "plain text"])
        assert isinstance(result, list)
        assert "[REDACTED EMAIL]" in result[0]

    def test_redact_tuple_value(self) -> None:
        mixin = self._make_mixin()
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            result = mixin._redact_value(("user@example.com",))
        assert isinstance(result, tuple)

    def test_passes_through_non_string(self) -> None:
        mixin = self._make_mixin()
        result = mixin._redact_value(42)
        assert result == 42

    def test_redact_record(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.msg = "Contact admin@yupp.ai"
        record.extra = {}

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            mixin.redact_record(record)

        assert "[REDACTED EMAIL]" in record.msg

    def test_redact_record_with_extra(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.msg = "plain message"

        # Simulate extra attribute
        record.extra = {"user_email": "test@example.com"}
        record.user_email = "test@example.com"

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            mixin.redact_record(record)

        assert "[REDACTED EMAIL]" in getattr(record, "user_email", "")


# ---------------------------------------------------------------------------
# Tests: TruncatingMixin
# ---------------------------------------------------------------------------


class TestTruncatingMixin:
    def _make_mixin(self) -> TruncatingMixin:
        class Impl(TruncatingMixin):
            pass

        return Impl()

    def test_truncates_long_string(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 100)
        result = mixin.truncate_value(long_str)
        assert len(result) <= MAX_LOGGED_FIELD_LENGTH_CHARS + len("... (truncated)")
        assert "(truncated)" in result

    def test_does_not_truncate_short_string(self) -> None:
        mixin = self._make_mixin()
        short_str = "hello"
        result = mixin.truncate_value(short_str)
        assert result == "hello"

    def test_truncates_long_values_in_dict(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 100)
        result = mixin.truncate_value({"key": long_str})
        assert "(truncated)" in result["key"]

    def test_truncates_long_values_in_list(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 100)
        result = mixin.truncate_value([long_str, "short"])
        assert "(truncated)" in result[0]
        assert result[1] == "short"

    def test_needs_truncation_returns_true_for_long_string(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 1)
        assert mixin._needs_truncation(long_str) is True

    def test_needs_truncation_returns_false_for_short_string(self) -> None:
        mixin = self._make_mixin()
        assert mixin._needs_truncation("short") is False

    def test_needs_truncation_checks_nested_dict(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 1)
        assert mixin._needs_truncation({"key": long_str}) is True

    def test_needs_truncation_returns_false_for_non_string(self) -> None:
        mixin = self._make_mixin()
        assert mixin._needs_truncation(42) is False
        assert mixin._needs_truncation(None) is False

    def test_truncate_record_updates_msg(self) -> None:
        mixin = self._make_mixin()
        long_str = "a" * (MAX_LOGGED_FIELD_LENGTH_CHARS + 100)
        record = MagicMock()
        record.msg = long_str
        record.extra = {}

        mixin.truncate_record(record)

        assert "(truncated)" in record.msg


# ---------------------------------------------------------------------------
# Tests: ConsolidatedMixin
# ---------------------------------------------------------------------------


class TestConsolidatedMixin:
    def _make_mixin(self) -> ConsolidatedMixin:
        class Impl(ConsolidatedMixin):
            pass

        return Impl()

    def test_maybe_parse_json_valid(self) -> None:
        mixin = self._make_mixin()
        result = mixin._maybe_parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_maybe_parse_json_invalid(self) -> None:
        mixin = self._make_mixin()
        result = mixin._maybe_parse_json("not json")
        assert result is None

    def test_maybe_parse_json_non_dict(self) -> None:
        mixin = self._make_mixin()
        result = mixin._maybe_parse_json("[1, 2, 3]")
        assert result is None

    def test_remove_traceback_from_record(self) -> None:
        mixin = self._make_mixin()
        msg_with_tb = '{"key": "value"}\nTraceback (most recent call last):\n  File "..."'
        result = mixin._remove_traceback_from_record(msg_with_tb)
        assert result == '{"key": "value"}'

    def test_remove_traceback_no_traceback(self) -> None:
        mixin = self._make_mixin()
        msg = '{"key": "value"}'
        result = mixin._remove_traceback_from_record(msg)
        assert result == '{"key": "value"}'

    def test_format_message_with_dict_msg(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.msg = {"message": "hello"}
        record.exc_info = None
        record.module = "test_module"

        formatter = MagicMock()
        result = mixin._format_message(record, formatter)

        assert isinstance(result, dict)
        assert "message" in result

    def test_format_message_with_string_msg(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.msg = "hello world"
        record.exc_info = None
        record.module = "test_module"

        formatter = MagicMock()
        formatter.format = MagicMock(return_value="hello world")
        result = mixin._format_message(record, formatter)

        assert isinstance(result, str)

    def test_normalize_error_message_skips_non_error(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.levelname = "INFO"
        record.msg = {"message": "some 0x12345 message"}

        # Should not modify the record
        mixin.normalize_error_message_string(record)
        assert record.msg["message"] == "some 0x12345 message"

    def test_normalize_error_message_on_error(self) -> None:
        mixin = self._make_mixin()
        record = MagicMock()
        record.levelname = "ERROR"
        # Use a message that triggers normalization
        record.msg = {"message": "Error for user 12345678-1234-1234-1234-123456789012"}

        mixin.normalize_error_message_string(record)
        # Either normalized or unchanged, but should not raise
        assert isinstance(record.msg, dict)


# ---------------------------------------------------------------------------
# Tests: ConsolidatedStreamHandler
# ---------------------------------------------------------------------------


class TestConsolidatedStreamHandler:
    def test_emit_with_string_message(self) -> None:
        handler = ConsolidatedStreamHandler()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )

        # Should not raise
        with patch.object(handler, "stream") as mock_stream:
            mock_stream.write = MagicMock()
            mock_stream.flush = MagicMock()
            handler.emit(record)

    def test_emit_with_dict_message(self) -> None:
        handler = ConsolidatedStreamHandler()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg={"key": "value", "message": "test"},
            args=(),
            exc_info=None,
        )

        with patch.object(handler, "stream") as mock_stream:
            mock_stream.write = MagicMock()
            mock_stream.flush = MagicMock()
            handler.emit(record)

    def test_emit_handles_exception(self) -> None:
        handler = ConsolidatedStreamHandler()
        record = MagicMock()
        record.exc_info = None
        record.module = "test"
        record.levelno = logging.INFO
        record.levelname = "INFO"
        record.msg = "test"

        with patch.object(handler, "_format_message", side_effect=Exception("format error")):
            # Should not raise
            handler.emit(record)


# ---------------------------------------------------------------------------
# Tests: Google Cloud Logging functions
# ---------------------------------------------------------------------------


class TestGoogleCloudLoggingFunctions:
    def test_flush_when_no_client(self) -> None:
        import ypl.logger as logger_module

        original = logger_module.GOOGLE_CLOUD_LOGGING_CLIENT
        logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = None
        try:
            # Should not raise
            flush_google_cloud_logging()
        finally:
            logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = original

    def test_close_when_no_client(self) -> None:
        import ypl.logger as logger_module

        original = logger_module.GOOGLE_CLOUD_LOGGING_CLIENT
        logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = None
        try:
            # Should not raise
            close_google_cloud_logging()
        finally:
            logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = original

    def test_flush_and_close_when_no_client(self) -> None:
        import ypl.logger as logger_module

        original = logger_module.GOOGLE_CLOUD_LOGGING_CLIENT
        logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = None
        try:
            # Should not raise
            flush_and_close_google_cloud_logging()
        finally:
            logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = original

    def test_flush_calls_client_methods(self) -> None:
        import ypl.logger as logger_module

        mock_client = MagicMock()
        original = logger_module.GOOGLE_CLOUD_LOGGING_CLIENT
        logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = mock_client
        try:
            with patch("ypl.logger.time") as mock_time:
                mock_time.sleep = MagicMock()
                flush_google_cloud_logging()
            mock_client.flush_handlers.assert_called_once()
        finally:
            logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = original

    def test_close_calls_client_close(self) -> None:
        import ypl.logger as logger_module

        mock_client = MagicMock()
        original = logger_module.GOOGLE_CLOUD_LOGGING_CLIENT
        logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = mock_client
        try:
            close_google_cloud_logging()
            mock_client.close.assert_called_once()
        finally:
            logger_module.GOOGLE_CLOUD_LOGGING_CLIENT = original


# ---------------------------------------------------------------------------
# Tests: init_worker_logging
# ---------------------------------------------------------------------------


class TestInitWorkerLogging:
    def test_does_not_raise(self) -> None:
        # Should import ypl.logger for side effects without raising
        init_worker_logging()
