import logging
import os
import sys
from datetime import UTC, datetime, timedelta

import requests
from google.cloud.logging_v2.handlers import CloudLoggingFilter
from google.cloud.logging_v2.handlers._helpers import (
    _parse_xcloud_trace,
)  # Hack to use the private method, until we have a better way to get the trace context
from pydantic import TypeAdapter

from ypl.middlewares.trace_context_middleware import gcp_cloud_trace_context

GCP_PROJECT_NAME = os.environ.get("GCP_PROJECT_NAME", "yupp-llms")

# Differentiates between multiple instances of the same container on GCP.
# Expected to be different for every run of the container.
CONTAINER_INSTANCE_ID = os.environ.get("CONTAINER_INSTANCE_ID", "unset")


class TraceAndProcessLoggingFilter(CloudLoggingFilter):
    _build_git_sha: str | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        if gcp_cloud_trace_context.get() != "":
            # _parse_xcloud_trace is a private function from google.cloud.logging without type stubs
            trace_id, span_id, trace_sampled = _parse_xcloud_trace(gcp_cloud_trace_context.get())  # type: ignore[no-untyped-call]
            record.trace = f"projects/{GCP_PROJECT_NAME}/traces/{trace_id}"
            record.span_id = span_id
            record.trace_sampled = trace_sampled

        record.labels = {
            # `main-worker`, or `some-other-worker:{id}`. Helps identify the type of workers
            # if there are multiple types of processes running in the same container.
            "processName": record.processName or "unknown",
            # Different for every worker in a container run. Differentiates workers.
            "pid": str(record.process) if record.process else "unknown",
            "service_name": os.environ.get("SERVICE_NAME_FOR_LOGGING", "unknown"),
        }
        # Note: We read BUILD_GIT_SHA at runtime (not module load time) and cache it.
        # This module may be imported before load_dotenv() loads .env from the Docker image.
        if self._build_git_sha is None:
            self._build_git_sha = os.environ.get("BUILD_GIT_SHA")
        if self._build_git_sha:
            record.labels["build_git_sha"] = self._build_git_sha  # type: ignore[attr-defined]

        return super().filter(record)  # type: ignore


# Copied from https://github.com/googleapis/python-logging/issues/53#issuecomment-2442787831
class LargeLogDetector(logging.Filter):
    def __init__(self, limit_size_bytes: int = 200_000):
        self.limit = limit_size_bytes

    def filter(self, record: logging.LogRecord) -> bool:
        record_size = sys.getsizeof(record.msg)
        if record_size > self.limit:
            location = f"{record.filename}:{record.lineno}"
            print(f"Large log detected, dropping record from: {location}")
            return False
        return True


METADATA_INSTANCE_ID = ""


# This is meant to be a blocking call, made only once, and only at the start of the application.
def get_gcp_instance_id() -> str:
    """Get the instance ID of the container from the metadata server."""
    global METADATA_INSTANCE_ID
    if METADATA_INSTANCE_ID == "":
        try:
            METADATA_INSTANCE_ID = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/id",
                headers={"Metadata-Flavor": "Google"},
                timeout=1,
            ).text
            print(f"GCP instance ID: {METADATA_INSTANCE_ID}", file=sys.stdout)
        except Exception:
            # Print to stderr, since this is during the logging setup and logging is not yet configured.
            print("Failed to get GCP instance ID from metadata server, using CONTAINER_INSTANCE_ID", file=sys.stderr)
            METADATA_INSTANCE_ID = CONTAINER_INSTANCE_ID
    return METADATA_INSTANCE_ID


def get_trace_and_process_logging_filter() -> CloudLoggingFilter:
    return TraceAndProcessLoggingFilter(default_labels={"instanceId": get_gcp_instance_id()})  # type: ignore[no-untyped-call]


timedelta_adapter = TypeAdapter(timedelta)


def get_gcp_logs_link(log_search_query: str, lookup_duration: timedelta, around_time: datetime | None = None) -> str:
    if not around_time:
        around_time = datetime.now(UTC)
    duration_str_iso8601 = timedelta_adapter.dump_python(lookup_duration, mode="json")
    return f"https://console.cloud.google.com/logs/query;query={log_search_query};duration={duration_str_iso8601};aroundTime={around_time.isoformat()}?project={GCP_PROJECT_NAME}"
