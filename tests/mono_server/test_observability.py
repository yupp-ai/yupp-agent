"""Observability tests for the monolith: /metrics endpoint and structured logging.

Covers:
- GET /metrics returns 200 with a valid Prometheus text payload
- _service_field_processor injects the ``service`` key correctly
- LOG_FORMAT=json switches the console renderer to JSONRenderer
"""

from __future__ import annotations
import os
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """/metrics returns a valid Prometheus text exposition payload."""

    def test_metrics_returns_200(self) -> None:
        from ypl.mono_server.server import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/metrics")
        assert response.status_code == 200

    def test_metrics_content_type_is_prometheus_text(self) -> None:
        """Content-Type must start with text/plain so Prometheus accepts it."""
        from ypl.mono_server.server import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/metrics")
        assert "text/plain" in response.headers["content-type"]

    def test_metrics_body_is_valid_prometheus_text(self) -> None:
        """Response body is non-empty and starts with a Prometheus comment line.

        The stub returns a comment-only payload.  When real instrumentation is
        wired in, this test should be updated to assert specific metric names.
        """
        from ypl.mono_server.server import app

        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/metrics")
        body = response.text
        # A valid Prometheus text payload (even an empty stub) must be non-empty
        # and must not contain any bare metric values — comment lines only for now.
        assert body.strip(), "Metrics body must not be empty"
        assert body.startswith("#"), f"Expected Prometheus comment, got: {body[:80]!r}"

    def test_metrics_route_registered(self) -> None:
        """The /metrics path must appear in the application's route table."""
        from ypl.mono_server.server import app

        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/metrics" in paths, f"/metrics not in routes: {paths}"


# ---------------------------------------------------------------------------
# _service_field_processor
# ---------------------------------------------------------------------------


class TestServiceFieldProcessor:
    """_service_field_processor injects ``service`` into every log record."""

    def test_service_field_added_from_env(self) -> None:
        with patch.dict(os.environ, {"SERVICE_NAME": "ahs"}, clear=False):
            from ypl.structured_logger import _service_field_processor

            result = _service_field_processor(None, "info", {"event": "hello"})  # type: ignore[arg-type]
        assert result["service"] == "ahs"

    def test_service_field_defaults_to_yupp_agent(self) -> None:
        # Build an environment that definitely has no SERVICE_NAME key.
        env_without = {k: v for k, v in os.environ.items() if k != "SERVICE_NAME"}
        with patch.dict(os.environ, env_without, clear=True):
            from ypl.structured_logger import _service_field_processor

            result = _service_field_processor(None, "info", {"event": "hello"})  # type: ignore[arg-type]
        assert result["service"] == "yupp-agent"

    def test_explicit_service_field_not_overwritten(self) -> None:
        """setdefault semantics: a pre-bound ``service`` value is preserved."""
        with patch.dict(os.environ, {"SERVICE_NAME": "ahs"}, clear=False):
            from ypl.structured_logger import _service_field_processor

            result = _service_field_processor(  # type: ignore[arg-type]
                None, "info", {"event": "hello", "service": "my-custom-service"}
            )
        assert result["service"] == "my-custom-service"

    def test_other_fields_untouched(self) -> None:
        with patch.dict(os.environ, {"SERVICE_NAME": "sag"}, clear=False):
            from ypl.structured_logger import _service_field_processor

            event_dict: dict[str, Any] = {"event": "msg", "session_id": "abc", "level": "info"}
            result = _service_field_processor(None, "info", event_dict)  # type: ignore[arg-type]
        assert result["event"] == "msg"
        assert result["session_id"] == "abc"
        assert result["service"] == "sag"


# ---------------------------------------------------------------------------
# JSON log format
# ---------------------------------------------------------------------------


class TestJsonLogFormat:
    """LOG_FORMAT=json switches the console renderer to JSONRenderer."""

    def test_json_format_does_not_raise(self) -> None:
        """init_console_logger with LOG_FORMAT=json must not raise."""
        from ypl.loggers.config import init_console_logger

        with patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=False):
            logger = init_console_logger([])
        assert logger is not None

    def test_pretty_format_does_not_raise(self) -> None:
        """init_console_logger with default LOG_FORMAT must not raise."""
        from ypl.loggers.config import init_console_logger

        with patch.dict(os.environ, {"LOG_FORMAT": "pretty"}, clear=False):
            logger = init_console_logger([])
        assert logger is not None

    def test_json_format_configures_json_renderer(self) -> None:
        """After calling init_console_logger(LOG_FORMAT=json), JSONRenderer is present."""
        import structlog
        from ypl.loggers.config import init_console_logger

        with patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=False):
            init_console_logger([])

        config = structlog.get_config()
        processor_names = [type(p).__name__ for p in config["processors"]]
        assert "JSONRenderer" in processor_names, f"JSONRenderer not found in processor chain: {processor_names}"

    def test_pretty_format_configures_console_renderer(self) -> None:
        """After calling init_console_logger(LOG_FORMAT=pretty), ConsoleRenderer is present."""
        import structlog
        from ypl.loggers.config import init_console_logger

        with patch.dict(os.environ, {"LOG_FORMAT": "pretty"}, clear=False):
            init_console_logger([])

        config = structlog.get_config()
        processor_names = [type(p).__name__ for p in config["processors"]]
        assert "ConsoleRenderer" in processor_names, f"ConsoleRenderer not found in processor chain: {processor_names}"
