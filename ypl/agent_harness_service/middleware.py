"""FastAPI middleware for Agent Harness Service.

Houses the two ASGI middleware classes used by the AHS server:

- AHSRequestLoggingMiddleware — logs every inbound AHS request and binds
  ``session_id`` to structlog context vars.
- McpTokenAuthMiddleware      — rejects MCP sub-app requests that don't
  carry the process-local secret token.
"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from structlog.contextvars import bind_contextvars, unbind_contextvars

from ypl.agent_harness_service.common.constants import AHS_MCP_SECRET, mcp_session_id_var
from ypl.structured_logger import get_logger

_logger = get_logger()

# ---------------------------------------------------------------------------
# Helpers for AHSRequestLoggingMiddleware
# ---------------------------------------------------------------------------

_MSG_TRUNCATE_LEN = 50
_TRUNCATE_FIELDS = {"message"}


def _get_media_type(content_type: str | None) -> str | None:
    """Extract and normalize the media type from a Content-Type header."""
    if not content_type:
        return None

    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type:
        return None

    return media_type


def _is_json_content_type(content_type: str | None) -> bool:
    """Return True when Content-Type is JSON (including vendor JSON types)."""
    media_type = _get_media_type(content_type)
    if not media_type:
        return False

    return media_type == "application/json" or media_type.endswith("+json")


def _maybe_truncate(v: Any) -> Any:
    """Truncate a string value if it exceeds the max length."""
    if isinstance(v, str) and len(v) > _MSG_TRUNCATE_LEN:
        return v[:_MSG_TRUNCATE_LEN] + f"... ({len(v)} chars)"
    return v


def _truncate_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a dict payload with long message fields truncated for logging."""
    return {k: _maybe_truncate(v) if k in _TRUNCATE_FIELDS else v for k, v in data.items()}


# Matches /ahs/session/{uuid} and /ahs/session/{uuid}/...
# Uses UUID pattern to avoid matching action paths like /ahs/session/create.
_SESSION_PATH_RE = re.compile(r"^/ahs/session/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


# ---------------------------------------------------------------------------
# Middleware classes
# ---------------------------------------------------------------------------


class AHSRequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every inbound AHS request with all fields (message truncated).

    Also binds ``session_id`` to structlog contextvars so every log emitted
    during the request (including background tasks) automatically includes it.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        path = request.url.path
        # Only log /ahs/ routes (skip /health, /mcp/harness, etc.)
        if not path.startswith("/ahs/"):
            response: Response = await call_next(request)
            return response

        method = request.method
        query_params = dict(request.query_params)

        # Read and log body for POST requests
        body_log: dict[str, Any] | None = None
        body_parsed: dict[str, Any] | None = None
        if method == "POST":
            content_type = request.headers.get("content-type")
            if _is_json_content_type(content_type):
                raw_body = await request.body()
                if raw_body:
                    try:
                        parsed = json.loads(raw_body)
                    except (json.JSONDecodeError, TypeError):
                        body_log = {"_raw": raw_body[:200].decode("utf-8", errors="replace")}
                    else:
                        if isinstance(parsed, dict):
                            body_parsed = parsed
                            body_log = _truncate_payload(parsed)
                        else:
                            body_log = {"_raw": raw_body[:200].decode("utf-8", errors="replace")}
            else:
                body_log = {
                    "_skipped": "request body omitted for non-json content type",
                    "content_type": _get_media_type(content_type),
                }

        # Extract session_id: from body (POST) or path (GET /ahs/session/{id}/...).
        session_id: str | None = None
        if body_parsed:
            session_id = body_parsed.get("session_id")
        if not session_id:
            m = _SESSION_PATH_RE.match(path)
            if m:
                session_id = m.group(1)

        # Bind session_id to structlog contextvars so ALL downstream logs include it.
        if session_id:
            bind_contextvars(session_id=session_id)

        try:
            log_kwargs: dict[str, Any] = {}
            if query_params:
                log_kwargs["query_params"] = query_params
            if body_log is not None:
                log_kwargs["body"] = body_log

            _logger.info(f"{method} {path}", **log_kwargs)

            result: Response = await call_next(request)
            return result
        finally:
            if session_id:
                unbind_contextvars("session_id")


class McpTokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject MCP requests that don't carry the process-local secret token.

    The token is generated at startup (AHS_MCP_SECRET) and injected into the
    per-session MCP config that the runner writes for Claude CLI. External
    callers won't know the token, so they can't hit the MCP endpoints.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        token = request.headers.get("x-ahs-token")
        session_id = request.headers.get("x-ahs-session-id", "")

        if token != AHS_MCP_SECRET:
            # Fallback: Accept Bearer token in format "<secret>:<session_id>".
            # Codex CLI only supports bearer_token_env_var for MCP auth, so we
            # encode both the secret and session ID into a single Bearer token.
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                bearer = auth_header[7:]
                if ":" in bearer:
                    bearer_secret, bearer_session_id = bearer.split(":", 1)
                    if bearer_secret == AHS_MCP_SECRET:
                        token = bearer_secret
                        session_id = bearer_session_id

        if token != AHS_MCP_SECRET:
            return JSONResponse(content={"detail": "Unauthorized"}, status_code=401)
        # Propagate session identity so MCP tools can enforce access control.
        mcp_session_id_var.set(session_id)
        return await call_next(request)
