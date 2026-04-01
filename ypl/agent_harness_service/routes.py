"""FastAPI routes for Agent Harness Service.

Endpoints:
- POST /session/create — create or resume a session
- POST /session/message — send a message to a session (returns immediately)
- POST /session/stop — stop a running agent task
- POST /session/feedback — record feedback
- GET  /session/{session_id}/history — get message history with pagination (includes tool uses)
- GET  /session/{session_id} — get session detail with descendant subsessions
- GET  /sessions — list sessions with filters, user_id, include_all, and pagination
- GET  /agents — list agents with user_id and include_all filters
- GET  /agent/{agent_name} — get a single agent's config
- POST /agent/create — create a new agent (requires user_id)
- POST /agent/edit — edit an existing agent (requires user_id)
- POST /schedules/create — create a one-time agent schedule
- POST /schedules/create-recurring — create a recurring agent schedule
- GET  /schedules — list schedules with filters
- GET  /schedule/{agent_schedule_id} — get a single schedule
- POST /schedule/edit — edit an existing schedule
- POST /schedule/{agent_schedule_id}/trigger — trigger a recurring schedule immediately
- DELETE /schedule/{agent_schedule_id} — cancel a schedule
- WS   /session/{session_id}/ws — WebSocket streaming (Codex-compatible events)
- POST /search — search sessions, projects, tasks, schedules, and artifacts
"""

import hmac
import json
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.status import WS_1008_POLICY_VIOLATION
from structlog.contextvars import bind_contextvars, unbind_contextvars

from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.common.types import (
    AgentCreateRequest,
    AgentCreateResponse,
    AgentDetailResponse,
    AgentEditRequest,
    AgentEditResponse,
    AgentListResponse,
    AHSValidationError,
    FeedbackResponse,
    ModelsListResponse,
    RecurringScheduleCreateRequest,
    ResolveUserRequest,
    ResolveUserResponse,
    ScheduleCreateRequest,
    ScheduleCreateResponse,
    ScheduleDeleteResponse,
    ScheduleDetailResponse,
    ScheduleEditRequest,
    ScheduleEditResponse,
    ScheduleListResponse,
    ScheduleRunsResponse,
    ScheduleTriggerRequest,
    ScheduleTriggerResponse,
    SessionAttachSlackRequest,
    SessionAttachSlackResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionFeedbackRequest,
    SessionHistoryResponse,
    SessionListResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionStopRequest,
    SessionStopResponse,
)
from ypl.agent_harness_service.core.streaming import get_ws_manager
from ypl.agent_harness_service.projects.schedule_service import (
    cancel_schedule,
    create_one_time_schedule,
    create_recurring_schedule,
    edit_schedule,
    get_schedule_detail,
    list_schedule_runs,
    list_schedules,
    trigger_schedule,
)
from ypl.agent_harness_service.search.search_routes import search_router
from ypl.agent_harness_service.service import (
    attach_slack_to_session,
    create_agent,
    create_session,
    edit_agent,
    get_agent_detail,
    get_session_detail,
    get_session_history,
    list_agents,
    list_sessions,
    send_feedback,
    send_message,
    stop_session,
)
from ypl.backend.config import settings
from ypl.backend.llm.db_helpers import get_user_id_by_email
from ypl.structured_logger import get_logger

_logger = get_logger()

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


router = APIRouter(prefix="/ahs", tags=["agent-harness-service"])


@router.post(
    "/session/create",
    dependencies=[Depends(verify_api_key)],
)
async def session_create(request: SessionCreateRequest) -> SessionCreateResponse:
    """Create a new session or resume an existing one.

    If `message` is provided, the first agent turn is kicked off in the background.
    """
    try:
        return await create_session(request)
    except AHSValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except RuntimeError as e:
        # Raised when the pending message queue is full during session resume
        raise HTTPException(status_code=409, detail=str(e)) from None


@router.post(
    "/session/message",
    dependencies=[Depends(verify_api_key)],
)
async def session_message(request: SessionMessageRequest) -> SessionMessageResponse:
    """Send a message to an existing session. Returns immediately, agent runs in background.

    If the session has an inflight turn, the message is queued and the response
    has ``status="queued"`` (HTTP 200). The queued messages are combined and
    processed as a single turn once the current turn completes.
    """
    try:
        return await send_message(request)
    except AHSValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except RuntimeError as e:
        # Raised when the pending message queue is full
        raise HTTPException(status_code=409, detail=str(e)) from None


@router.post(
    "/session/attach-slack",
    dependencies=[Depends(verify_api_key)],
)
async def session_attach_slack(request: SessionAttachSlackRequest) -> SessionAttachSlackResponse:
    """Attach Slack thread context to a headless session for thread re-attachment."""
    try:
        return await attach_slack_to_session(request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.post(
    "/session/stop",
    dependencies=[Depends(verify_api_key)],
)
async def session_stop(request: SessionStopRequest) -> SessionStopResponse:
    """Stop a running agent task. Idempotent — stopping an idle session is a no-op."""
    try:
        return await stop_session(request.session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.post(
    "/session/feedback",
    dependencies=[Depends(verify_api_key)],
)
async def session_feedback(request: SessionFeedbackRequest) -> FeedbackResponse:
    """Record feedback on a session or specific message."""
    try:
        return await send_feedback(request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.get(
    "/session/{session_id}/history",
    dependencies=[Depends(verify_api_key)],
)
async def session_history(
    session_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> SessionHistoryResponse:
    """Get message history for a session with pagination."""
    try:
        return await get_session_history(session_id, limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.get(
    "/models",
    dependencies=[Depends(verify_api_key)],
)
async def list_models_route() -> ModelsListResponse:
    """List all selectable models, grouped by executor type.

    Harnessed models are CLI wrapper names — the agent process runs inside a managed
    subprocess (e.g., Claude Code CLI, Codex CLI).  Raw models are LLM identifiers in
    ``provider/model_id`` format — the agent calls the provider API directly.

    Use the returned values as the ``force_model`` field in POST /ahs/session/create.
    """
    from ypl.agent_harness_service.common.constants import (
        HARNESS_CLAUDE_CODE_CLI,
        HARNESS_CLAUDE_SDK,
        HARNESS_CODEX_APP_SERVER,
        HARNESS_CODEX_CLI,
    )
    from ypl.agent_harness_service.executors.providers import KNOWN_MODELS

    return ModelsListResponse(
        harnessed=[
            HARNESS_CLAUDE_CODE_CLI,
            HARNESS_CLAUDE_SDK,
            HARNESS_CODEX_CLI,
            HARNESS_CODEX_APP_SERVER,
        ],
        raw=list(KNOWN_MODELS),
    )


@router.get(
    "/agents",
    dependencies=[Depends(verify_api_key)],
)
async def list_agents_route(
    user_id: str | None = Query(None, description="Filter by creator user ID"),
    include_all: bool = Query(False, description="Include all agents (filesystem + all DB agents)"),
) -> AgentListResponse:
    """List agents. By default only returns agents created by user_id. Set include_all=true for all."""
    return await list_agents(user_id=user_id, include_all=include_all)


@router.get(
    "/agent/{agent_name}",
    dependencies=[Depends(verify_api_key)],
)
async def get_agent_detail_route(
    agent_name: str,
    include_system_prompts: bool = Query(False, description="Include system prompt file contents"),
) -> AgentDetailResponse:
    """Get a single agent's config, optionally with system prompt contents."""
    try:
        return await get_agent_detail(agent_name, include_system_prompts=include_system_prompts)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.post(
    "/agent/create",
    dependencies=[Depends(verify_api_key)],
)
async def create_agent_route(request: AgentCreateRequest) -> AgentCreateResponse:
    """Create a new agent with config on disk and registration in DB."""
    try:
        return await create_agent(request)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@router.post(
    "/agent/edit",
    dependencies=[Depends(verify_api_key)],
)
async def edit_agent_route(request: AgentEditRequest) -> AgentEditResponse:
    """Edit an existing agent's configuration. Only updates provided fields."""
    try:
        return await edit_agent(request)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.get(
    "/sessions",
    dependencies=[Depends(verify_api_key)],
)
async def list_sessions_route(
    status: str | None = Query(None, description="Filter by status: ACTIVE, COMPLETED, STALE"),
    agent_name: str | None = Query(None, description="Filter by agent name"),
    trigger: str | None = Query(None, description="Filter by trigger: SLACK, WEBHOOK, CRON, API"),
    since: str | None = Query(None, description="Created after (ISO-8601 datetime)"),
    until: str | None = Query(None, description="Created before (ISO-8601 datetime)"),
    root_sessions_only: bool = Query(True, description="Only return top-level sessions (no subagent sessions)"),
    user_id: str | None = Query(None, description="Filter by session creator user ID"),
    include_all: bool = Query(False, description="Include all sessions regardless of creator"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SessionListResponse:
    """List sessions with optional filters and pagination."""
    from datetime import datetime

    try:
        since_dt = datetime.fromisoformat(since) if since else None
        until_dt = datetime.fromisoformat(until) if until else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid datetime format: {e}") from None

    try:
        return await list_sessions(
            status=status,
            agent_name=agent_name,
            trigger=trigger,
            since=since_dt,
            until=until_dt,
            root_sessions_only=root_sessions_only,
            user_id=user_id,
            include_all=include_all,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.get(
    "/session/{session_id}",
    dependencies=[Depends(verify_api_key)],
)
async def get_session_detail_route(session_id: str) -> SessionDetailResponse:
    """Get a single session with a flat list of all descendant subsessions."""
    try:
        return await get_session_detail(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.post(
    "/resolve_user",
    dependencies=[Depends(verify_api_key)],
)
async def resolve_user_route(request: ResolveUserRequest) -> ResolveUserResponse:
    """Resolve a @yupp.ai email address to a Yupp user ID."""
    email = request.email.strip().lower()
    if not email.endswith("@yupp.ai"):
        raise HTTPException(status_code=400, detail="Only @yupp.ai emails are allowed")

    user_id = await get_user_id_by_email(email)
    if not user_id:
        raise HTTPException(status_code=404, detail=f"User not found: {email}")

    return ResolveUserResponse(user_id=user_id, email=email)


# ---------------------------------------------------------------------------
# Schedule endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/schedules/create",
    dependencies=[Depends(verify_api_key)],
)
async def create_schedule_route(request: ScheduleCreateRequest) -> ScheduleCreateResponse:
    """Create a one-time agent schedule to execute at a specific time."""
    try:
        result = await create_one_time_schedule(
            agent_name=request.agent_name,
            message=request.message,
            execute_at=request.execute_at,
            timezone=request.timezone,
            created_by_user=request.user_id,
            context=request.context,
            name=request.name,
            description=request.description,
            created_by_agent=request.created_by_agent,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return ScheduleCreateResponse(
            agent_schedule_id=result["agent_schedule_id"],
            agent_name=result["agent_name"],
            schedule_type=result["schedule_type"],
            status=result["status"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post(
    "/schedules/create-recurring",
    dependencies=[Depends(verify_api_key)],
)
async def create_recurring_schedule_route(request: RecurringScheduleCreateRequest) -> ScheduleCreateResponse:
    """Create a recurring agent schedule using a cron expression."""
    try:
        result = await create_recurring_schedule(
            agent_name=request.agent_name,
            message=request.message,
            cron_expression=request.cron_expression,
            timezone=request.timezone,
            created_by_user=request.user_id,
            context=request.context,
            name=request.name,
            description=request.description,
            max_runs=request.max_runs,
            created_by_agent=request.created_by_agent,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return ScheduleCreateResponse(
            agent_schedule_id=result["agent_schedule_id"],
            agent_name=result["agent_name"],
            schedule_type=result["schedule_type"],
            status=result["status"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.get(
    "/schedules",
    dependencies=[Depends(verify_api_key)],
)
async def list_schedules_route(
    agent_name: str | None = Query(None, description="Filter by agent name"),
    status: str | None = Query(None, description="Filter by status: PENDING, IN_PROGRESS, COMPLETED, etc."),
    schedule_type: str | None = Query(None, description="Filter by type: SCHEDULED or RECURRING"),
    created_by: str | None = Query(None, description="Filter by creator user ID"),
    limit: int = Query(50, ge=1, le=200),
) -> ScheduleListResponse:
    """List agent schedules with optional filters."""
    result = await list_schedules(
        agent_name=agent_name,
        status=status,
        schedule_type=schedule_type,
        created_by_user=created_by,
        limit=limit,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return ScheduleListResponse(
        schedules=result["agent_schedules"],
        count=result["count"],
    )


@router.get(
    "/schedule/{agent_schedule_id}",
    dependencies=[Depends(verify_api_key)],
)
async def get_schedule_detail_route(agent_schedule_id: str) -> ScheduleDetailResponse:
    """Get a single agent schedule by ID."""
    result = await get_schedule_detail(agent_schedule_id)
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        error_code = result.get("error_code")
        status_code = 404 if error_code == "NOT_FOUND" else 400
        raise HTTPException(status_code=status_code, detail=error)
    return ScheduleDetailResponse(schedule=result["schedule"])


@router.get(
    "/schedule/{agent_schedule_id}/runs",
    dependencies=[Depends(verify_api_key)],
)
async def list_schedule_runs_route(
    agent_schedule_id: str,
    limit: int = Query(20, description="Max runs to return", ge=1, le=100),
) -> ScheduleRunsResponse:
    """List past runs for a schedule, most recent first."""
    runs = await list_schedule_runs(agent_schedule_id, limit=limit)
    return ScheduleRunsResponse(runs=runs, count=len(runs))


@router.post(
    "/schedule/edit",
    dependencies=[Depends(verify_api_key)],
)
async def edit_schedule_route(request: ScheduleEditRequest) -> ScheduleEditResponse:
    """Edit an existing agent schedule. Only the creator can edit PENDING/PAUSED schedules."""
    # TODO: derive caller identity from authenticated context (e.g. signed gateway claim)
    # rather than trusting the request body. Currently any AHS key holder can pass any
    # user_id and bypass the owner check. Requires auth infrastructure work. (see PR #10883)
    result = await edit_schedule(
        agent_schedule_id=request.agent_schedule_id,
        caller_user_id=request.user_id,
        message=request.message,
        cron_expression=request.cron_expression,
        timezone=request.timezone,
        name=request.name,
        description=request.description,
        context=request.context,
        max_runs=request.max_runs,
        execute_at=request.execute_at,
        agent_name=request.agent_name,
    )
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        error_code = result.get("error_code")
        if error_code == "FORBIDDEN":
            status_code = 403
        elif error_code == "NOT_FOUND":
            status_code = 404
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=error)
    return ScheduleEditResponse(agent_schedule_id=request.agent_schedule_id)


@router.post(
    "/schedule/{agent_schedule_id}/trigger",
    dependencies=[Depends(verify_api_key)],
)
async def trigger_schedule_route(
    agent_schedule_id: str,
    request: ScheduleTriggerRequest,
) -> ScheduleTriggerResponse:
    """Trigger a recurring schedule to run immediately without affecting its normal cadence."""
    result = await trigger_schedule(agent_schedule_id, request.user_id)
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        error_code = result.get("error_code")
        if error_code == "FORBIDDEN":
            status_code = 403
        elif error_code == "NOT_FOUND":
            status_code = 404
        elif error_code == "EXECUTION_FAILED":
            status_code = 502
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=error)
    return ScheduleTriggerResponse(
        agent_schedule_id=agent_schedule_id,
        session_id=result["session_id"],
        run_number=result["run_number"],
    )


@router.delete(
    "/schedule/{agent_schedule_id}",
    dependencies=[Depends(verify_api_key)],
)
async def delete_schedule_route(
    agent_schedule_id: str,
    # TODO: derive caller identity from authenticated context rather than request params.
    # See edit_schedule_route for full context. (PR #10883)
    user_id: str = Query(..., description="Yupp user ID of the requester (must be the creator)"),
) -> ScheduleDeleteResponse:
    """Cancel an agent schedule. Only the creator can cancel PENDING/PAUSED schedules."""
    result = await cancel_schedule(agent_schedule_id, user_id)
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        error_code = result.get("error_code")
        if error_code == "FORBIDDEN":
            status_code = 403
        elif error_code == "NOT_FOUND":
            status_code = 404
        else:
            status_code = 400
        raise HTTPException(status_code=status_code, detail=error)
    return ScheduleDeleteResponse(agent_schedule_id=agent_schedule_id)


# ---------------------------------------------------------------------------
# WebSocket streaming endpoint
# ---------------------------------------------------------------------------


def _verify_ws_api_key(api_key: str | None) -> None:
    """Verify API key for WebSocket connections.

    WebSocket doesn't support Depends() the same way, so we do manual auth.
    Raises WebSocketException on failure.
    """
    expected = settings.AGENT_HARNESS_SERVICE_API_KEY
    if not expected:
        raise WebSocketException(code=WS_1008_POLICY_VIOLATION, reason="Service misconfigured")
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise WebSocketException(code=WS_1008_POLICY_VIOLATION, reason="Invalid API key")


@router.websocket("/session/{session_id}/ws")
async def session_ws(
    websocket: WebSocket,
    session_id: str,
    api_key: str | None = Query(None),
) -> None:
    """WebSocket endpoint for streaming Codex-compatible events.

    Bidirectional:
    - Server → Client: Codex events (thread.started, turn.started, agent_message_delta, etc.)
    - Client → Server: user_message, stop, ping

    Auth: Pass API key as query parameter ``?api_key=...``
    """
    _verify_ws_api_key(api_key)
    await websocket.accept()

    _logger.info("WebSocket connection accepted", session_id=session_id)

    async def _on_user_message(sid: str, content: str, user_id: str | None = None, source: str | None = None) -> None:
        """Handle user_message from WebSocket client."""
        try:
            request = SessionMessageRequest(
                session_id=sid, message=content, user_id=user_id, source=source or "websocket"
            )
            await send_message(request)
        except (ValueError, RuntimeError) as e:
            await websocket.send_json({"type": "error", "message": str(e)})

    async def _on_stop(sid: str) -> None:
        """Handle stop request from WebSocket client."""
        try:
            result = await stop_session(sid)
            await websocket.send_json(
                {
                    "type": "stop_ack",
                    "status": result.status,
                    "session_id": result.session_id,
                }
            )
        except ValueError as e:
            await websocket.send_json({"type": "error", "message": str(e)})

    ws_manager = get_ws_manager()
    await ws_manager.run_connection(
        websocket,
        session_id,
        on_user_message=_on_user_message,
        on_stop=_on_stop,
    )


# ---------------------------------------------------------------------------
# Sub-router registration
# ---------------------------------------------------------------------------

router.include_router(search_router)
