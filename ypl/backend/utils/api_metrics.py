import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from ypl.backend.utils.monitoring import metric_inc_with_labels, metric_record_with_labels
from ypl.backend.utils.utils import sanitize_request_path
from ypl.middlewares.yupp_context_middleware import is_yuppster_request_for_metrics_only_contextvar


class ApiMetricsMiddleware(BaseHTTPMiddleware):
    """
    A middleware to record status code and latency metrics for all endpoint calls.
    It will replace out the ID-like parts with a placeholder "ID" so we don't create too many metrics.
    Only records metrics for paths starting with /api/v1.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        method = request.method
        path = request.url.path

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            # Only record metrics for /api/v1 paths
            if path.startswith("/api/v1"):
                sanitized_path = sanitize_request_path(path)
                metric_name_base = f"endpoint/{method}/{sanitized_path}"
                latency_ms = int((time.monotonic() - start) * 1000)
                host = request.base_url.hostname or "unknown"
                is_yuppster_request = is_yuppster_request_for_metrics_only_contextvar.get()
                labels = {"host": host, "is_yuppster_request": str(is_yuppster_request).lower()}
                metric_inc_with_labels(f"{metric_name_base}/total", labels)
                metric_inc_with_labels(f"{metric_name_base}/{status_code}", labels)
                metric_record_with_labels(f"{metric_name_base}/latency_ms", latency_ms, labels)
        return response
