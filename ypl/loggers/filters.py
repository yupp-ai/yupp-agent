"""
Logging filters for downgrades and severity adjustments.

These filters modify log record severity based on specific patterns to prevent
alerting on known benign errors from third-party libraries.
"""

import logging
import traceback


class ConnectionTerminationErrorFilter(logging.Filter):
    """
    Filter that downgrades benign connection termination errors from ERROR to WARNING.

    SQLAlchemy logs at ERROR level when terminating connections that are already closed,
    which happens normally when:
    - PgBouncer or the database server closes connections
    - Connections time out
    - Network interruptions occur

    These errors are not actionable - the connection is being cleaned up regardless.
    This filter converts them to WARNING to avoid triggering alerts.

    Note: This filter MUST be added to the handler (not the logger) to work correctly,
    because the error comes from sqlalchemy.pool.impl.AsyncAdaptedQueuePool (a child
    logger), and filters on parent loggers don't apply to child logger records.
    """

    # Exceptions in traceback that indicate benign disconnections
    BENIGN_EXCEPTION_MARKERS = [
        "asyncpg.exceptions._base.InternalClientError: not connected",
        "asyncio.exceptions.CancelledError",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        # Check if this is a connection termination error we want to downgrade
        if record.levelno == logging.ERROR and "Exception terminating connection" in record.getMessage():
            # Only downgrade if the traceback contains a benign asyncpg disconnection error
            if record.exc_info:
                tb_text = "".join(traceback.format_exception(*record.exc_info))
                if any(marker in tb_text for marker in self.BENIGN_EXCEPTION_MARKERS):
                    # Downgrade from ERROR to WARNING
                    record.levelno = logging.WARNING
                    record.levelname = "WARNING"
        return True
