import base64
import gzip
import io
import re
from collections.abc import AsyncGenerator
from typing import Any

import orjson
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import StreamingResponse
from ypl.backend.config import settings
from ypl.structured_logger import get_logger
from ypl.utils import maybe_truncate

logger = get_logger()


class ApiLoggingConfig(BaseModel):
    max_string_size: int = 1000  # If a string is larger than this, truncate it.
    max_list_size: int = 100  # If a list is larger than this, pick a random subset of it.
    url_regex: re.Pattern[str] = re.compile(settings.API_LOGGING_REGEX)  # Log when url matches this.


global_mutable_api_logging_config = ApiLoggingConfig()  # Update via the /admin/api-logging-config endpoint.

# Regex matching "event: <event_name>\ndata: <json_chunk>"
_STREAMING_RESPONSE_CHUNK_REGEX = re.compile(r"^event: (\w+)\s*data: (.*)", re.MULTILINE)


def _decompress_gzip(compressed_bytes: bytes) -> bytes:
    with (
        io.BytesIO(compressed_bytes) as compressed_stream,
        gzip.GzipFile(fileobj=compressed_stream, mode="rb") as decompressor,
    ):
        # Use gzip to decompress the data
        return decompressor.read()


class ApiLoggingMiddleware(BaseHTTPMiddleware):
    """
    FastAPI Middleware to log request and response bodies. Handles both streaming and non-streaming responses.
    """

    # Coded with the help of Claude

    def __init__(self, app: FastAPI):
        super().__init__(app)

    def _truncate_fields(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return maybe_truncate(obj, global_mutable_api_logging_config.max_string_size)
        if isinstance(obj, list):
            max_list_size = global_mutable_api_logging_config.max_list_size  # Limit to max_list_size.
            return [self._truncate_fields(item) for item in obj[:max_list_size]]
        if isinstance(obj, dict):
            return {k: self._truncate_fields(v) for k, v in obj.items()}
        return obj

    def _maybe_json_trimmed(self, body: bytes | str) -> Any:
        if isinstance(body, bytes):
            try:
                body_str = body.decode("utf-8")
            except Exception:
                # non-text data. Convert to base64.
                base64_obj = {
                    "base64_encoded_content": base64.b64encode(body).decode("utf-8"),
                    "content_size": len(body),
                }
                return self._truncate_fields(base64_obj)
        else:
            body_str = body

        try:
            obj = orjson.loads(body_str)
        except Exception:
            obj = body_str
        return self._truncate_fields(obj)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip logging if the URL doesn't match the regex
        if not global_mutable_api_logging_config.url_regex.search(str(request.url)):
            return await call_next(request)

        # Log request
        log_data: dict[str, Any] = {
            "path": str(request.url.path),
            "message": "API request and response",
            "method": request.method,
            "url": str(request.url),
        }
        if "content-type" in request.headers:
            log_data["request_content_type"] = request.headers["content-type"]

        try:
            request_body = await request.body()
            log_data["request_body"] = self._maybe_json_trimmed(request_body)
        except Exception as e:
            log_data["request_body_error"] = str(e)

        # Process the request
        response = await call_next(request)
        log_data["status_code"] = response.status_code

        # Check if this is a streaming response
        if isinstance(response, StreamingResponse) or hasattr(response, "body_iterator"):
            # For streaming responses, create a wrapper that logs as it streams
            original_iterator = response.body_iterator

            # Create a new async generator that logs chunks as they flow through
            async def logging_iterator() -> AsyncGenerator[bytes | str, None]:
                collected_chunks: list[str] = []
                # The following are for logging chat completions in better format.
                collected_events: list[dict[str, Any]] = []
                chat_completion_content = ""
                all_chunks_are_events = True

                def flush_chat_completion_content() -> None:
                    nonlocal chat_completion_content
                    if chat_completion_content:
                        collected_events.append({"concatenated_content": chat_completion_content.split("\n")})
                        chat_completion_content = ""

                async for chunk in original_iterator:
                    # Log if start of response if there is more than 1 chunk
                    if len(collected_chunks) == 1:
                        logger.info(log_data | {"message": "API Request (streaming response continued)"})

                    if isinstance(chunk, str):
                        chunk_str = chunk
                    else:
                        try:
                            if response.headers.get("content-encoding") == "gzip":
                                chunk_str = _decompress_gzip(chunk).decode("utf-8")
                            else:
                                chunk_str = chunk.decode("utf-8")
                        except Exception:
                            logger.warning("Failed to decode response bytes", chunk_size=len(chunk), exc_info=True)
                            chunk_str = base64.b64encode(chunk).decode("utf-8")

                    # Parse standard StreamingResponse format: "event: status\ndata: json_chunk"
                    # This improves logging of chat completion requests.
                    event_match = _STREAMING_RESPONSE_CHUNK_REGEX.match(chunk_str)
                    if event_match:
                        event = event_match.group(1)
                        data = self._maybe_json_trimmed(event_match.group(2))

                        if event == "content":
                            if isinstance(data, dict) and "content" in data and "model" in data:
                                chat_completion_content += data["content"]
                            else:
                                flush_chat_completion_content()
                        else:  # status or other type of event.
                            flush_chat_completion_content()
                            collected_events.append({"event": event, "data": data})
                    else:
                        all_chunks_are_events = False

                    collected_chunks.append(chunk_str)
                    # Forward the chunk immediately to the client
                    yield chunk

                try:
                    log_data["response_body"] = (
                        collected_events  # These are already truncated in parts.
                        if all_chunks_are_events
                        else self._maybe_json_trimmed("".join(collected_chunks))
                    )
                except Exception as e:
                    log_data["response_body_error"] = str(e)

                logger.info(log_data)

            # Create a new streaming response with our logging iterator
            return StreamingResponse(
                content=logging_iterator(),
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        # Regular response
        try:
            log_data["response_body"] = self._maybe_json_trimmed(response.body)
        except Exception as e:
            log_data["response_body_error"] = str(e)

        logger.info(log_data)

        return response
