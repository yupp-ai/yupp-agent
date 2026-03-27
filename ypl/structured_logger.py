"""
Structured logger module for application logging.

This module provides a structured logging system with redaction of sensitive data,
automatic error detection, and standardized formatting.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import structlog
from dotenv import load_dotenv
from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars

from ypl.loggers.config import init_console_logger, init_google_cloud_logger
from ypl.loggers.processors import (
    ErrorDetailsProcessor,
    NormalizeErrorMessageProcessor,
    RedactSensitiveProcessor,
    SerializeModelsProcessor,
    TruncateMessageProcessor,
)

# Load environment variables
load_dotenv()


@dataclass
class LoggerConfig:
    timestamp: bool = True
    func_name: bool = True
    module: bool = True
    thread: bool = True
    thread_name: bool = True

    def all_fields_enabled(self) -> bool:
        return all(getattr(self, field) for field in LoggerConfig.__dataclass_fields__)


# Global configuration instance
_config = LoggerConfig()


class FilterFieldsProcessor:
    """Processor that filters out fields depending on the configuration."""

    def __call__(self, logger: Any, name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        # Quick no-op if all fields are enabled (default configuration)
        if _config.all_fields_enabled():
            return event_dict

        for field in LoggerConfig.__dataclass_fields__:
            if not getattr(_config, field):
                event_dict.pop(field, None)
        return event_dict


# Global logger instance
logger: structlog.BoundLogger | None = None


def setup_logger() -> structlog.BoundLogger:
    """Configure and return the structured logger."""
    global logger

    # Silence noisy HTTP client loggers (e.g., Upstash Redis client)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    base_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        ErrorDetailsProcessor(),
        structlog.processors.format_exc_info,
        structlog.processors.CallsiteParameterAdder(
            parameters={
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.THREAD,
                structlog.processors.CallsiteParameter.THREAD_NAME,
            }
        ),
        FilterFieldsProcessor(),
        SerializeModelsProcessor(),
        NormalizeErrorMessageProcessor(),
        RedactSensitiveProcessor(),
        TruncateMessageProcessor(),
    ]

    if os.environ.get("USE_GOOGLE_CLOUD_LOGGING", "").lower() in ("True", "true", "1", "yes", "on"):
        logger = init_google_cloud_logger(base_processors)
        logger.info(
            f"Structlog is set up. Build git SHA: {os.environ.get('BUILD_GIT_SHA')}, "
            f"build ref: {os.environ.get('GITHUB_REF')}, build date: {os.environ.get('BUILD_DATE')}"
        )
    else:
        logger = init_console_logger(base_processors)

    return logger


def get_logger() -> structlog.BoundLogger:
    """Get a logger instance.

    Returns:
        A configured structlog logger instance
    """
    global logger
    if logger is None:
        setup_logger()
    return structlog.get_logger()  # type: ignore


def bind_logger_context(context: dict[str, Any]) -> None:
    """Bind context variables to the logger.

    Common context variables include:
    - user_id: User identifier
    - flow: Flow identifier (e.g., "onboarding", "payment")
    - session_id: Session identifier
    - request_id: Request identifier
    """
    bind_contextvars(**context)


def get_current_context() -> dict[str, Any]:
    """Get the current context variables for debugging purposes.

    Returns:
        Dictionary containing all current context variables
    """
    return get_contextvars()


def clear_logger_context() -> None:
    """Clear all logger context variables.

    This function should be called at the beginning and end of each request
    to prevent context leakage between requests.
    """
    clear_contextvars()


def configure_logger(
    timestamp: bool | None = None,
    func_name: bool | None = None,
    module: bool | None = None,
    thread: bool | None = None,
    thread_name: bool | None = None,
) -> None:
    """Turn on or off certain fields in the logger output."""
    args = locals()
    for key, value in args.items():
        if value is not None:
            setattr(_config, key, value)


def reset_logger_config() -> None:
    """Reset the logger configuration to the default values."""
    global _config
    _config = LoggerConfig()


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Custom asyncio exception handler that properly separates tracebacks from messages.

    By default, asyncio's exception handler embeds the full traceback in the message string,
    which causes issues with structured logging (traceback appears in the message field instead
    of the traceback field). This handler extracts the exception and logs it with exc_info
    so the traceback is properly separated.

    All context keys from asyncio (future, handle, protocol, transport, socket, etc.)
    are preserved in the log output.
    """
    import traceback as tb_mod

    message = context.get("message", "Unhandled exception in asyncio")
    exception = context.get("exception")

    skip_keys = {"message", "exception"}
    log_context: dict[str, Any] = {}

    for key, value in context.items():
        if key in skip_keys:
            continue
        if key == "future":
            log_context["future_name"] = getattr(value, "get_name", lambda v=value: str(v))()
        elif key == "source_traceback":
            log_context["source_traceback"] = "".join(tb_mod.format_list(value))
        else:
            log_context[key] = str(value)

    log = get_logger()
    log.warning(message, exc_info=exception, asyncio_context=True, **log_context)


_asyncio_logging_installed = False


def setup_asyncio_logging() -> None:
    """Set a custom asyncio exception handler on the running event loop.

    Chains with any previously installed handler so it is still called after ours.
    Idempotent — subsequent calls are no-ops.

    Must be called from within a running event loop (e.g., during FastAPI lifespan
    startup, taskiq worker startup, or after asyncio.run() has started the loop).
    """
    global _asyncio_logging_installed
    if _asyncio_logging_installed:
        return
    _asyncio_logging_installed = True

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _chained_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        _asyncio_exception_handler(loop, context)
        if previous_handler is not None:
            previous_handler(loop, context)

    loop.set_exception_handler(_chained_handler)


# Initialize logger on module import
setup_logger()
