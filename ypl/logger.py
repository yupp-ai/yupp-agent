import logging
import os
import re
import time
import traceback
from datetime import UTC, datetime
from typing import Any, cast

import orjson
from dotenv import load_dotenv
from google.cloud import logging as google_logging
from google.cloud.logging.handlers import CloudLoggingHandler
from google.cloud.logging_v2.handlers.transports.background_thread import BackgroundThreadTransport

from ypl.backend.utils.json import json_dumps, orjson_dumps
from ypl.loggers.filters import ConnectionTerminationErrorFilter
from ypl.loggers.gcloud_utils import LargeLogDetector, get_trace_and_process_logging_filter
from ypl.loggers.processors import normalize_error_message_string

MAX_LOGGED_FIELD_LENGTH_CHARS = 32000
MAX_LOG_ENTRY_SIZE_BYTES = 250 * 1024

load_dotenv()

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "default")


def redact_sensitive_data(text: str) -> str:
    if os.environ.get("ENVIRONMENT") == "local":
        return text

    # Email pattern
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"

    # Redact email addresses
    redacted = re.sub(email_pattern, "[REDACTED EMAIL]", text)

    # Redact last4 if it appears as a key in JSON
    try:
        if isinstance(text, str) and text.strip().startswith("{"):
            data = orjson.loads(text)

            def redact_last4(obj: Any) -> Any:
                if isinstance(obj, dict):
                    if "last4" in obj:
                        obj["last4"] = "[REDACTED]"
                    for value in obj.values():
                        redact_last4(value)
                elif isinstance(obj, list):
                    for item in obj:
                        redact_last4(item)
                return obj

            data = redact_last4(data)
            return orjson_dumps(data).decode()
    except (orjson.JSONDecodeError, TypeError):
        pass

    # TODO: Add more sensitive data patterns to redact

    return redacted


class RedactingMixin:
    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_data(value)
        if isinstance(value, dict):
            # recursive redact the value in dict, we don't process the key though.
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            # recursive redact the value in list or tuple
            return type(value)(self._redact_value(item) for item in value)
        return value

    def redact_record(self, record: logging.LogRecord) -> None:
        record.msg = self._redact_value(record.msg)

        # Handle extra parameters
        if hasattr(record, "extra"):
            for k, v in record.extra.items():
                setattr(record, k, self._redact_value(v))


class TruncatingMixin:
    def truncate_value(self, value: Any) -> Any:
        if isinstance(value, str):
            # if the log entry is a json, this limit applies to every leaf level string value.
            if len(value) > MAX_LOGGED_FIELD_LENGTH_CHARS:
                return value[:MAX_LOGGED_FIELD_LENGTH_CHARS] + "... (truncated)"
            return value
        if isinstance(value, dict):
            return {k: self.truncate_value(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(self.truncate_value(item) for item in value)
        return value

    def _needs_truncation(self, value: Any) -> bool:
        if isinstance(value, str):
            return len(value) > MAX_LOGGED_FIELD_LENGTH_CHARS
        if isinstance(value, dict):
            return any(self._needs_truncation(v) for v in value.values())
        if isinstance(value, list | tuple):
            return any(self._needs_truncation(item) for item in value)
        return False

    def truncate_record(self, record: logging.LogRecord) -> None:
        if self._needs_truncation(record.msg):
            record.msg = self.truncate_value(record.msg)

        # Handle extra parameters
        if hasattr(record, "extra"):
            for k, v in record.extra.items():
                if self._needs_truncation(v):
                    setattr(record, k, self.truncate_value(v))


class ConsolidatedMixin:
    def _maybe_parse_json(self, msg: str) -> dict | None:
        """Parse a string as JSON and return the dict if successful, None otherwise."""
        msg = msg.strip()
        if not msg.startswith("{"):
            return None
        try:
            msg_to_parse = self._remove_traceback_from_record(msg)
            parsed = orjson.loads(msg_to_parse)
            return parsed if isinstance(parsed, dict) else None
        except (orjson.JSONDecodeError, TypeError):
            return None

    def _remove_traceback_from_record(self, msg: str) -> str:
        """Remove the traceback from the message if it exists.
        This is to avoid logging the traceback twice, especially in the message field.
        Not fully foolproof (e.g. if the traceback is in the middle of the message with multiple lines),
        but should work for most cases.

        The output is a string that can be parsed as JSON depending on the context.
        """
        lines = msg.split("\n")
        if len(lines) > 1 and lines[1].startswith("Traceback"):
            return lines[0]
        return msg

    def _format_message(
        self, record: logging.LogRecord, formatter: logging.Handler, for_console: bool = False
    ) -> str | dict:
        """Format the log message with module information."""
        msg_dict = None
        traceback_dict = (
            {
                "traceback": traceback.format_exc(),
                "exc_type": record.exc_info[0].__name__,  # type: ignore
            }
            if record.exc_info
            else {}
        )

        if isinstance(record.msg, dict):
            # Do a round trip through json_dumps and orjson.loads to ensure the dict is valid.
            # This ensures all the objects are serializable inside GCP logger.
            msg_dict = cast(dict, orjson.loads(json_dumps(record.msg)))

        if isinstance(record.msg, str):
            record.msg = formatter.format(record)
            record.args = None  # These are already processed in format()
            msg_dict = self._maybe_parse_json(record.msg) or None

        module_info = {"module": record.module}

        if msg_dict:
            return msg_dict | traceback_dict | module_info
        if "traceback" in traceback_dict and not for_console:
            # At this stage, we know that record.msg is not a valid JSON string
            # (at least unparseable by our python code).
            # So if there is a traceback that needs to be logged, we force record.msg into a dict,
            # so that we do not get the traceback logged as a string along with the message.
            return {"message": self._remove_traceback_from_record(str(record.msg))} | traceback_dict | module_info
        if "traceback" in traceback_dict and for_console:
            return str(record.msg) + "\n" + traceback_dict["traceback"]
        return str(record.msg)

    def normalize_error_message_string(self, record: logging.LogRecord) -> None:
        """
        Normalize the error message string by removing variable values.
        E.g. "Connector error: 0x12345678" ->
             {"message": "Connector error: 0x[NORMALIZED]", "variable_values": ["0x12345678"]}
             "Error for user: 0123-uuid-1234" ->
             {"message": "Error for user: <NORMALIZED-UUID>", "variable_values": ["0123-uuid-1234"]}
        """
        if record.levelname not in ("ERROR", "CRITICAL"):
            return

        if isinstance(record.msg, dict) and "message" in record.msg:
            original_message = record.msg["message"]
            normalized_message = normalize_error_message_string(record.msg["message"])
            if normalized_message != original_message:
                record.msg["message"] = normalized_message
                record.msg["_normalized_message"] = True
                record.msg["_original_message"] = original_message
        elif isinstance(record.msg, str):
            # This should not happen, since we're already making the string record.msg as a dict in _format_message().
            original_message = record.msg
            normalized_message = normalize_error_message_string(record.msg)
            if normalized_message != original_message:
                record.msg = {
                    "message": normalized_message,
                    "_normalized_message": True,
                    "_original_message": original_message,
                }


class ConsolidatedHandler(RedactingMixin, TruncatingMixin, CloudLoggingHandler, ConsolidatedMixin):
    def _get_log_entry_size(self, record: logging.LogRecord) -> int:
        try:
            log_dict = {
                "message": record.msg,
                "level": record.levelname,
                "timestamp": record.created,
                "module": record.module,
                "funcName": record.funcName,
                "lineno": record.lineno,
                "extra": getattr(record, "extra", {}),
            }
            return len(orjson_dumps(log_dict).decode())
        except Exception:
            return MAX_LOG_ENTRY_SIZE_BYTES + 1

    def _truncate_large_log_entry(self, record: logging.LogRecord) -> None:
        def get_item_size(item: Any) -> int:
            """Calculate the size of a single item when serialized to JSON."""
            try:
                return len(orjson_dumps(item).decode())
            except Exception:
                return MAX_LOG_ENTRY_SIZE_BYTES + 1

        def truncate_dict(d: dict, max_size: int, min_value_logged_size: int = 100) -> dict:
            """Recursively truncate a dictionary to fit within max_size."""
            result = {}
            current_size = 0

            for k, v in d.items():
                if isinstance(v, dict):
                    truncated_v = truncate_dict(v, max_size - current_size)
                    item_size = get_item_size({k: truncated_v})
                else:
                    item_size = get_item_size({k: v})

                if current_size + item_size <= max_size:
                    result[k] = truncated_v if isinstance(v, dict) else v
                    current_size += item_size
                else:
                    truncated_items = []
                    for tk, tv in d.items():
                        if tk not in result:
                            truncated_items.append(
                                {
                                    tk: str(tv)[:min_value_logged_size] + "..."
                                    if len(str(tv)) > min_value_logged_size
                                    else tv
                                }
                            )
                    result["_truncated_items"] = truncated_items
                    break

            return result

        if self._get_log_entry_size(record) > MAX_LOG_ENTRY_SIZE_BYTES:
            if isinstance(record.msg, dict):
                metadata_size = get_item_size({"message": "", "original_size": 0, "_truncated_items": []})
                max_content_size = MAX_LOG_ENTRY_SIZE_BYTES - metadata_size

                truncated_content = truncate_dict(record.msg, max_content_size)

                truncated_dict = {
                    "message": "Log entry partially truncated",
                    "original_size": self._get_log_entry_size(record),
                    "truncated_content": dict(truncated_content),
                }

                record.msg = truncated_dict
            else:
                max_msg_length = MAX_LOG_ENTRY_SIZE_BYTES // 2
                if len(str(record.msg)) > max_msg_length:
                    record.msg = str(record.msg)[:max_msg_length] + "... (truncated due to size)"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatted_msg = self._format_message(record, self)
            record.msg = (
                {
                    "module": record.module,
                    "message": formatted_msg,
                }
                if isinstance(formatted_msg, str)
                else formatted_msg
            )
            self.normalize_error_message_string(record)
            self.redact_record(record)
            self._truncate_large_log_entry(record)
            super().emit(record)  # type: ignore[no-untyped-call]

            if os.environ.get("ENVIRONMENT") == "local" and record.levelno >= logging.ERROR:
                print(f"{record}")
        except Exception as e:
            print(f"Error in ConsolidatedHandler: {e}")


class ConsolidatedStreamHandler(RedactingMixin, TruncatingMixin, logging.StreamHandler, ConsolidatedMixin):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            record.msg = self._format_message(record, self, for_console=True)
            self.normalize_error_message_string(record)
            self.truncate_record(record)
            self.redact_record(record)

            # Add timestamp and module to the message meant for the console.
            message = orjson_dumps(record.msg).decode() if isinstance(record.msg, dict) else str(record.msg)
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            record.msg = f"[{timestamp} {record.levelname}] [{record.module}] {message}"

            record.exc_info = None  # Already handled in _format_message().
            super().emit(record)
        except Exception as e:
            print(f"Error in ConsolidatedStreamHandler: {e}")


GOOGLE_CLOUD_LOGGING_CLIENT: google_logging.Client | None = None


def setup_google_cloud_logging() -> None:
    try:
        global GOOGLE_CLOUD_LOGGING_CLIENT
        if GOOGLE_CLOUD_LOGGING_CLIENT is None:
            GOOGLE_CLOUD_LOGGING_CLIENT = google_logging.Client()  # type: ignore[no-untyped-call]
        name = GCP_PROJECT_ID
        consolidated_handler = ConsolidatedHandler(
            GOOGLE_CLOUD_LOGGING_CLIENT,
            name=name,
            transport=BackgroundThreadTransport,
        )
        consolidated_handler.addFilter(get_trace_and_process_logging_filter())
        consolidated_handler.addFilter(LargeLogDetector())
        consolidated_handler.addFilter(ConnectionTerminationErrorFilter())

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(consolidated_handler)
        root_logger.propagate = False
    except Exception as e:
        logging.basicConfig(level=logging.INFO)
        logging.error(f"Google Cloud Logging setup failed: {e}")


if os.environ.get("USE_GOOGLE_CLOUD_LOGGING", "").lower() in ("true", "1", "yes", "on"):
    setup_google_cloud_logging()
    logging.info(
        f"Legacy logger is set up. Build git SHA: {os.environ.get('BUILD_GIT_SHA')}, "
        f"build ref: {os.environ.get('GITHUB_REF')}, build date: {os.environ.get('BUILD_DATE')}"
    )
else:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(ConsolidatedStreamHandler())
    root_logger.propagate = False


def init_worker_logging() -> None:
    """Initialize logging in ProcessPoolExecutor worker processes.

    Worker processes spawned by ProcessPoolExecutor don't inherit the parent's logging configuration.
    Pass this function as the `initializer` argument to ProcessPoolExecutor to ensure workers use
    our configured logger with filters.

    Example:
        executor = ProcessPoolExecutor(max_workers=4, initializer=init_worker_logging)
    """
    import ypl.logger  # noqa: F401 - import for side effects (configures root logger)


def flush_google_cloud_logging() -> None:
    """Flush logs without closing - safe to call from asyncio cleanup."""
    global GOOGLE_CLOUD_LOGGING_CLIENT
    if GOOGLE_CLOUD_LOGGING_CLIENT is not None:
        GOOGLE_CLOUD_LOGGING_CLIENT.flush_handlers()  # type: ignore[no-untyped-call]
        # Wait for background thread to complete sending logs to GCP
        # BackgroundThreadTransport needs time to send all queued logs
        time.sleep(3.0)


def close_google_cloud_logging() -> None:
    """Close logging client - only call from atexit handler after flushing."""
    global GOOGLE_CLOUD_LOGGING_CLIENT
    if GOOGLE_CLOUD_LOGGING_CLIENT is not None:
        GOOGLE_CLOUD_LOGGING_CLIENT.close()  # type: ignore[no-untyped-call]


def flush_and_close_google_cloud_logging() -> None:
    """Flush and close logging client - for server shutdown (not CLI)."""
    flush_google_cloud_logging()
    close_google_cloud_logging()
