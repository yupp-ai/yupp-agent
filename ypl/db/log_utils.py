import traceback
from typing import Any

from opentelemetry import trace
from sqlalchemy import event
from sqlalchemy.engine import Engine

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


def log_sql_query(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    """Log SQL queries using the structured logger while preserving flow context.

    This function is called by SQLAlchemy before cursor execution.
    """
    # Create a very short version for the log message itself
    short_statement = statement[:200] + "..." if len(statement) > 200 else statement

    if settings.ENVIRONMENT != "production":
        truncated_statement = statement
        truncated_parameters = parameters
    else:
        # Truncate long statements and parameters
        truncated_statement = statement[:500] + "... (truncated)" if len(statement) > 500 else statement
        truncated_parameters = None
        if parameters:
            truncated_parameters = [
                p[:100] + "... (truncated)" if isinstance(p, str) and len(p) > 100 else p for p in parameters
            ]

    # Extract user_id from parameters when possible
    user_id = None
    if parameters:
        # Check common patterns for user_id in parameters
        for param in parameters:
            if isinstance(param, str) and len(param) == 36 and "-" in param:  # Likely a UUID
                user_id = param
                break

    # Create log data with additional context
    log_data: dict[str, Any] = {"statement": truncated_statement, "parameters": truncated_parameters}

    # Add user_id if found
    if user_id:
        log_data["user_id"] = user_id

    # Add traceback to the log
    log_data["traceback"] = traceback.format_stack()

    # Add trace context if available
    current_span = trace.get_current_span()
    if current_span:
        span_context = current_span.get_span_context()
        log_data["trace_context"] = {
            "traceparent": (
                f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-{int(span_context.trace_flags):02x}"
            ),
            "tracestate": span_context.trace_state.to_header() if span_context.trace_state else "[]",
        }

    logger.info(f"SQL Query: {short_statement}", **log_data)


def setup_sql_logging(engine: Engine) -> None:
    """Set up SQL query logging for the given engine."""
    event.listen(engine, "before_cursor_execute", log_sql_query)
