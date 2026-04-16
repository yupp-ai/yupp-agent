"""
Logger configuration constants and initialization functions.
"""

import logging
import os
import time
from typing import Any

import orjson
import structlog

from ypl.backend.utils.json import orjson_dumps
from ypl.loggers.filters import ConnectionTerminationErrorFilter

# Size limits
MAX_FIELD_LENGTH = 32000  # Maximum length for any field value
MAX_MESSAGE_LENGTH = 250 * 1024  # Maximum message size (250KB)

# Environment settings
IS_PRODUCTION = os.environ.get("ENVIRONMENT", "").lower() == "production"
CONTAINER_INSTANCE_ID = os.environ.get("CONTAINER_INSTANCE_ID", "unset")

GITHUB_REPO_URL = "https://github.com/yupp-ai/yupp-agent"
GITHUB_TAG = "latest-production"

# Holds the Google Cloud Logging client when GCL is enabled.
# Typed as Any to avoid a module-level hard dependency on google-cloud-logging:
# the actual runtime type is google.cloud.logging.Client, but that import is
# deferred to init_google_cloud_logger() so that the rest of this module (and
# the console / JSON logging paths) can be loaded even if the package is not
# installed or credentials are unavailable.
GOOGLE_LOGGING_CLIENT: Any = None


def init_google_cloud_logger(base_processors: list[Any]) -> structlog.BoundLogger:
    """Initialize and configure Google Cloud structured logger.

    All google-cloud-logging imports are deferred inside this function so that
    the module (and every non-GCL logging path) can be imported without the
    package being installed.  Any ImportError or credential error is caught and
    the function falls back to init_console_logger().

    Args:
        base_processors: List of base processors to include

    Returns:
        Configured structlog logger
    """
    try:
        # Lazy imports — only required when USE_GOOGLE_CLOUD_LOGGING=true.
        # Keeping them here (rather than at module level) removes the hard
        # runtime dependency on google-cloud-logging for non-GCL deployments
        # (MacBook dev, systemd on bare metal, etc.).
        from google.cloud import logging as google_logging
        from google.cloud.logging_v2.handlers.transports.background_thread import (
            BackgroundThreadTransport,
        )

        from ypl.loggers.gcloud_utils import (
            LargeLogDetector,
            get_trace_and_process_logging_filter,
        )

        # Set up Google Cloud client
        global GOOGLE_LOGGING_CLIENT
        if GOOGLE_LOGGING_CLIENT is None:
            GOOGLE_LOGGING_CLIENT = google_logging.Client()  # type: ignore[no-untyped-call]
        project_id = os.environ.get("GCP_PROJECT_ID") or "default"

        stdlib_logger = logging.getLogger("yupp.structured")
        stdlib_logger.setLevel(logging.INFO)
        stdlib_logger.handlers = []

        cloud_handler = google_logging.handlers.CloudLoggingHandler(
            GOOGLE_LOGGING_CLIENT,
            name=project_id,
            transport=BackgroundThreadTransport,
        )
        cloud_handler.addFilter(get_trace_and_process_logging_filter())
        cloud_handler.addFilter(LargeLogDetector())
        cloud_handler.addFilter(ConnectionTerminationErrorFilter())
        stdlib_logger.addHandler(cloud_handler)

        # Extend processors for cloud format
        cloud_processors = base_processors.copy()
        cloud_processors.append(
            lambda _, __, event_dict: {
                # Use `or ""` so that an explicit event=None never becomes
                # message=null in GCP — null messages make SRE triage blind.
                "message": event_dict.pop("event", "") or "",
                **event_dict,
            }
        )
        # TODO: Avoid serializing and deserializing.
        cloud_processors.append(lambda _, __, event_dict: orjson.loads(orjson_dumps(event_dict)))
        # TODO: Consider using orjson_dumps instead as a serializer for JSONRenderer.
        cloud_processors.append(structlog.processors.JSONRenderer())

        # Configure structlog
        structlog.configure(
            processors=cloud_processors,
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        # Get logger and confirm initialization
        return structlog.get_logger()  # type: ignore[no-any-return]

    except Exception:
        return init_console_logger(base_processors)


def init_console_logger(base_processors: list[Any]) -> structlog.BoundLogger:
    """Initialize and configure the console (non-GCL) structured logger.

    Two output formats are available, selected by the ``LOG_FORMAT`` env var:

    ``LOG_FORMAT=json`` (or any value starting with ``"json"``)
        One JSON object per line, emitted to stdout.  The ``event`` key is
        renamed to ``message`` for consistency with the GCL path.  Suitable
        for systemd-journal capture (``journalctl -u ahs-mono -o json``),
        Loki, CloudWatch, or any aggregator that ingests NDJSON.

    ``LOG_FORMAT=pretty`` (default / anything else)
        Coloured human-readable output via structlog's ``ConsoleRenderer``.
        Ideal for interactive terminal sessions on MacBook or in CI.

    Args:
        base_processors: List of base processors to include

    Returns:
        Configured structlog logger
    """
    console_processors = base_processors.copy()
    log_format = os.getenv("LOG_FORMAT", "pretty").lower()

    try:
        if log_format.startswith("json"):
            # Machine-readable JSON ------------------------------------------------
            # Rename 'event' → 'message' (consistent with GCL path), then render
            # each log record as a single JSON line.  No colours or padding.
            def _rename_event_to_message(logger: Any, name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
                """Rename 'event' → 'message' for JSON log output.

                Uses setdefault so that a pre-bound 'message' key is preserved
                and we never silently drop a value that was already there.
                Uses ``or ""`` so that an explicit event=None never produces
                message=null — null messages make SRE triage blind.
                """
                event = event_dict.pop("event", "") or ""
                event_dict.setdefault("message", event)
                return event_dict

            console_processors.append(_rename_event_to_message)
            console_processors.append(structlog.processors.JSONRenderer())

            structlog.configure(
                processors=console_processors,
                wrapper_class=structlog.BoundLogger,
                context_class=dict,
                logger_factory=structlog.PrintLoggerFactory(),
                cache_logger_on_first_use=True,
            )
            return structlog.get_logger()  # type: ignore[no-any-return]

        # Pretty / interactive terminal output -------------------------------------
        # Fancy traceback logging is enabled by default.
        # Those who don't want it and want the basic plain-text one, add this in your env.
        use_plain_traceback_in_logs = os.getenv("USE_PLAIN_TRACEBACK_IN_LOGS", False)

        # Add console renderer with reasonable padding
        console_processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                pad_event=20,
                exception_formatter=structlog.dev.plain_traceback
                if use_plain_traceback_in_logs
                else structlog.dev.default_exception_formatter,
            )
        )

        # Configure structlog
        structlog.configure(
            processors=console_processors,
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

        return structlog.get_logger()  # type: ignore[no-any-return]

    except Exception as e:
        # Basic fallback if console setup fails
        logging.basicConfig(level=logging.INFO)
        logging.warning(f"Console logging setup failed: {e}")

        structlog.configure(
            processors=[structlog.processors.TimeStamper(fmt="iso", utc=False)],
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        return structlog.get_logger()  # type: ignore[no-any-return]


def flush_google_logging_client() -> None:
    """Flush logs without closing - safe to call from asyncio cleanup."""
    global GOOGLE_LOGGING_CLIENT
    if GOOGLE_LOGGING_CLIENT is not None:
        GOOGLE_LOGGING_CLIENT.flush_handlers()
        # Wait for background thread to complete sending logs to GCP
        # BackgroundThreadTransport needs time to send all queued logs
        time.sleep(3.0)


def close_google_logging_client() -> None:
    """Close logging client - only call from atexit handler after flushing."""
    global GOOGLE_LOGGING_CLIENT
    if GOOGLE_LOGGING_CLIENT is not None:
        GOOGLE_LOGGING_CLIENT.close()


def flush_and_close_google_logging_client() -> None:
    """Flush and close logging client - for server shutdown (not CLI)."""
    flush_google_logging_client()
    close_google_logging_client()
