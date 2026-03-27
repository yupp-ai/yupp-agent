from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind

from ypl.backend.config import settings
from ypl.structured_logger import get_logger

logger = get_logger()


class SQLQuerySpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Any | None = None) -> None:
        attributes = getattr(span, "attributes", {})
        if attributes.get("db.system") == "postgresql":
            sql = attributes.get("db.statement")
            if sql:
                span.set_attribute("sql.query", sql)
                db_name = attributes.get("db.name")
                if db_name is not None:
                    span.set_attribute("db.name", db_name)
                db_operation = attributes.get("db.operation")
                if db_operation is not None:
                    span.set_attribute("db.operation", db_operation)
                span.set_attribute("db.type", "sql")
                if db_operation is not None:
                    span.set_attribute("db.statement.type", db_operation)

    def on_end(self, span: ReadableSpan) -> None:
        pass


def setup_telemetry(app: FastAPI) -> None:
    if settings.ENVIRONMENT == "test":
        return
    try:
        tracer_provider = TracerProvider()
        trace.set_tracer_provider(tracer_provider)

        if settings.ENVIRONMENT != "local":
            gcp_exporter = CloudTraceSpanExporter()  # type: ignore[no-untyped-call]
            tracer_provider.add_span_processor(BatchSpanProcessor(gcp_exporter))

        tracer_provider.add_span_processor(SQLQuerySpanProcessor())

        SQLAlchemyInstrumentor().instrument(
            trace_parent_span=True,
            capture_parameters=True,
            capture_statement=True,
            span_name_prefix="db.query",
            span_kind=SpanKind.CLIENT,
            enable_commenter=True,
            commenter_options={
                "include_db_name": True,
                "include_operation": True,
            },
        )

        FastAPIInstrumentor.instrument_app(
            app=app,
            tracer_provider=tracer_provider,
            excluded_urls="health,metrics",
        )
        logger.info("OpenTelemetry initialized successfully")
    except Exception:
        logger.error("Failed to initialize OpenTelemetry", exc_info=True)
