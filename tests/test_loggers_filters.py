"""Unit tests for ypl/loggers/filters.py.

Covers: ConnectionTerminationErrorFilter — severity downgrade for benign
asyncpg disconnection errors, pass-through for all other records.
"""

import logging
import sys

import pytest
from ypl.loggers.filters import ConnectionTerminationErrorFilter


def _make_record(
    msg: str = "test message",
    level: int = logging.ERROR,
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    """Helper: build a minimal LogRecord."""
    return logging.LogRecord(
        name="sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
        level=level,
        pathname="pool.py",
        lineno=42,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def _capture_exc_info(exc_msg: str) -> tuple:
    """Raise and capture exc_info for a RuntimeError with the given message."""
    try:
        raise RuntimeError(exc_msg)
    except RuntimeError:
        return sys.exc_info()


class TestConnectionTerminationErrorFilterAlwaysReturnsTrue:
    """filter() must always return True — it never drops log records."""

    def test_returns_true_for_normal_record(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        assert flt.filter(_make_record("routine message")) is True

    def test_returns_true_for_connection_termination_error(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        record = _make_record("Exception terminating connection")
        assert flt.filter(record) is True

    def test_returns_true_for_warning_level(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        assert flt.filter(_make_record("warn", level=logging.WARNING)) is True


class TestConnectionTerminationErrorFilterNoDowngrade:
    """Cases where the filter should NOT change the log level."""

    def test_non_error_levels_left_unchanged(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        for level in (logging.DEBUG, logging.INFO, logging.WARNING):
            record = _make_record("Exception terminating connection", level=level)
            flt.filter(record)
            assert record.levelno == level

    def test_error_without_matching_message_unchanged(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        record = _make_record("Some completely different error")
        flt.filter(record)
        assert record.levelno == logging.ERROR

    def test_error_with_connection_msg_but_no_exc_info(self) -> None:
        """No exc_info means there is no traceback to inspect — stay ERROR."""
        flt = ConnectionTerminationErrorFilter()
        record = _make_record("Exception terminating connection")
        record.exc_info = None
        flt.filter(record)
        assert record.levelno == logging.ERROR

    def test_error_with_non_benign_traceback_unchanged(self) -> None:
        """A connection error whose traceback lacks the known benign markers stays ERROR."""
        flt = ConnectionTerminationErrorFilter()
        exc_info = _capture_exc_info("Some unknown connection problem")
        record = _make_record("Exception terminating connection", exc_info=exc_info)
        flt.filter(record)
        assert record.levelno == logging.ERROR


class TestConnectionTerminationErrorFilterDowngrade:
    """Cases where the filter SHOULD downgrade ERROR → WARNING."""

    def test_downgrade_with_internal_client_error_marker(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        exc_info = _capture_exc_info("asyncpg.exceptions._base.InternalClientError: not connected")
        record = _make_record("Exception terminating connection", exc_info=exc_info)
        flt.filter(record)
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"

    def test_downgrade_with_cancelled_error_marker(self) -> None:
        flt = ConnectionTerminationErrorFilter()
        exc_info = _capture_exc_info("asyncio.exceptions.CancelledError raised here")
        record = _make_record("Exception terminating connection", exc_info=exc_info)
        flt.filter(record)
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"

    def test_downgrade_preserves_all_other_fields(self) -> None:
        """Downgrade must only change levelno and levelname."""
        flt = ConnectionTerminationErrorFilter()
        exc_info = _capture_exc_info("asyncpg.exceptions._base.InternalClientError: not connected")
        record = _make_record("Exception terminating connection", exc_info=exc_info)
        original_name = record.name
        original_msg = record.getMessage()
        flt.filter(record)
        assert record.name == original_name
        assert record.getMessage() == original_msg

    @pytest.mark.parametrize(
        "marker",
        [
            "asyncpg.exceptions._base.InternalClientError: not connected",
            "asyncio.exceptions.CancelledError",
        ],
    )
    def test_all_benign_markers_trigger_downgrade(self, marker: str) -> None:
        flt = ConnectionTerminationErrorFilter()
        exc_info = _capture_exc_info(marker)
        record = _make_record("Exception terminating connection", exc_info=exc_info)
        flt.filter(record)
        assert record.levelno == logging.WARNING
