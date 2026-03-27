import base64
import contextvars
import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Context variable to store the Sardine session key
risk_session_key_contextvar = contextvars.ContextVar("risk_session_key", default="")
yupp_context = contextvars.ContextVar("yupp_context", default="")
user_agent_contextvar = contextvars.ContextVar("user_agent", default=None)
# Context variable to store if the request is sent by yuppster.
# Do not use this for access control without thorough security discussion and review, as it's easily spoofable.
is_yuppster_request_for_metrics_only_contextvar = contextvars.ContextVar("is_yuppster_request", default=False)


class YuppContextMiddleware(BaseHTTPMiddleware):
    """Middleware to extract Sardine session key from X-YUPP-CONTEXT header."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Extract X-YUPP-CONTEXT header
        yupp_context_header = request.headers.get("X-YUPP-CONTEXT", "")
        yupp_context.set(yupp_context_header)

        if yupp_context_header:
            try:
                # Decode base64 encoded header
                decoded_bytes = base64.b64decode(yupp_context_header)
                decoded_str = decoded_bytes.decode("utf-8")

                # Parse JSON to get the context map
                context_map: dict[str, Any] = json.loads(decoded_str)

                # Extract sardineSessionKey if it exists
                sardine_session_key = context_map.get("sardineSessionKey", "")

                if sardine_session_key:
                    # Store in context variable
                    risk_session_key_contextvar.set(sardine_session_key)

                is_yuppster = context_map.get("isYuppsterRequest", False)
                if is_yuppster:
                    is_yuppster_request_for_metrics_only_contextvar.set(True)

                user_agent = context_map.get("userAgent")
                if user_agent:
                    user_agent_contextvar.set(user_agent)

            except Exception:
                logging.error("Unexpected error processing X-YUPP-CONTEXT header", exc_info=True)

        return await call_next(request)
