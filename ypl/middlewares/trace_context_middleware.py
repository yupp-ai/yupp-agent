import contextvars

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

gcp_cloud_trace_context = contextvars.ContextVar("gcp_cloud_trace_context", default="")


class TraceContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if "x-cloud-trace-context" in request.headers:
            gcp_cloud_trace_context.set(request.headers.get("x-cloud-trace-context", ""))

        return await call_next(request)
