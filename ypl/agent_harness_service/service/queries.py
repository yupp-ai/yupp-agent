"""Read-only query endpoints and feedback."""

import os
import uuid
from collections import deque
from datetime import datetime

from sqlalchemy import func
from sqlmodel import col, select

from ypl.agent_harness_service.common.config import (
    AgentConfig,
    discover_agents,
)
from ypl.agent_harness_service.common.constants import AHS_AGENTS_DIR
from ypl.agent_harness_service.common.types import (
    AgentDetailResponse,
    AgentInfo,
    AgentListResponse,
    FeedbackResponse,
    MessageHistoryItem,
    SessionDetailResponse,
    SessionFeedbackRequest,
    SessionHistoryResponse,
    SessionInfo,
    SessionListResponse,
    SessionPermissions,
)
from ypl.agent_harness_service.service._resolvers import (
    _load_agent_config_with_db_fallback,
    _resolve_session,
)
from ypl.agent_harness_service.service.message_helpers import _extract_tool_uses
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentFeedback,
    AgentFeedbackRating,
    AgentSession,
    AgentSessionMessage,
    AgentSessionStatus,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()


async def send_feedback(request: SessionFeedbackRequest) -> FeedbackResponse:
    """Record feedback on a session or message."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {request.session_id}")

        message_uuid = None
        if request.message_id:
            try:
                message_uuid = uuid.UUID(request.message_id)
            except ValueError:
                raise ValueError(f"Invalid message_id: {request.message_id}") from None
            # Verify the message exists and belongs to this session
            msg_result = await session.exec(
                select(AgentSessionMessage).where(
                    AgentSessionMessage.agent_session_message_id == message_uuid,
                    AgentSessionMessage.agent_session_id == agent_session.agent_session_id,
                )
            )
            if not msg_result.one_or_none():
                raise ValueError(f"Message {request.message_id} not found in session {request.session_id}")

        # Convert rating string to enum
        rating_enum: AgentFeedbackRating | None = None
        if request.rating:
            try:
                rating_enum = AgentFeedbackRating(request.rating.upper())
            except ValueError:
                raise ValueError(f"Invalid rating: {request.rating}. Must be POSITIVE or NEGATIVE.") from None

        user_id = request.user_id or "SYSTEM"

        feedback = AgentFeedback(
            agent_session_id=agent_session.agent_session_id,
            agent_session_message_id=message_uuid,
            user_id=user_id,
            rating=rating_enum,
            structured=request.structured,
            comment=request.comment,
            slack_ts=request.slack_ts,
        )
        session.add(feedback)
        await session.commit()

        logger.info(
            "Recorded feedback",
            session_id=str(agent_session.agent_session_id),
            message_id=request.message_id,
            rating=request.rating,
        )

    return FeedbackResponse(status="recorded")


async def get_session_history(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
) -> SessionHistoryResponse:
    """Get message history for a session with pagination."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        agent = await session.get(Agent, agent_session.agent_id)

        # Total count
        count_result = await session.exec(
            select(func.count())
            .select_from(AgentSessionMessage)
            .where(AgentSessionMessage.agent_session_id == agent_session.agent_session_id)
        )
        total = count_result.one()

        # Paginated messages
        result = await session.exec(
            select(AgentSessionMessage)
            .where(AgentSessionMessage.agent_session_id == agent_session.agent_session_id)
            .order_by(AgentSessionMessage.turn_number, AgentSessionMessage.created_at)  # type: ignore[arg-type]
            .offset(offset)
            .limit(limit)
        )
        messages = result.all()

        return SessionHistoryResponse(
            session_id=str(agent_session.agent_session_id),
            agent_id=agent.name if agent else str(agent_session.agent_id),
            status=agent_session.status.value,
            messages=[
                MessageHistoryItem(
                    message_id=str(msg.agent_session_message_id),
                    turn_number=msg.turn_number,
                    role=msg.role.value,
                    content=msg.content,
                    tool_uses=_extract_tool_uses(msg.raw_events),
                    cost_usd=msg.cost_usd,
                    duration_ms=msg.duration_ms,
                    num_agent_turns=msg.num_agent_turns,
                    slack_ts=msg.slack_ts,
                    created_at=msg.created_at,
                )
                for msg in messages
            ],
            total_messages=total,
            limit=limit,
            offset=offset,
        )


def _build_agent_info(cfg: AgentConfig) -> AgentInfo:
    """Build an AgentInfo from an AgentConfig."""
    return AgentInfo(
        name=cfg.name,
        display_name=cfg.display_name,
        description=cfg.description,
        executor_type=cfg.executor_config.type,
        executor_model=cfg.executor_config.model,
        llm_model=cfg.llm_model,
        tool_permissions={k: str(v) for k, v in cfg.tool_permissions.items()},
        allowed_subagents=cfg.allowed_subagents,
        default_repo=cfg.default_repo,
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        timeout_s=cfg.timeout_s,
        sandbox_enabled=cfg.sandbox.enabled,
        allowed_gateways=cfg.allowed_gateways,
    )


def _build_agent_info_from_db(agent: Agent) -> AgentInfo:
    """Build an AgentInfo from a DB Agent row."""
    cfg = agent.config or {}
    executor_cfg = cfg.get("executor_config", {})
    sandbox_cfg = cfg.get("sandbox", {})
    return AgentInfo(
        name=agent.name,
        display_name=agent.display_name,
        description=agent.description,
        executor_type=executor_cfg.get("type", agent.executor_type or "harnessed"),
        executor_model=executor_cfg.get("model", agent.executor_model),
        llm_model=executor_cfg.get("model") if executor_cfg.get("type") == "raw" else None,
        tool_permissions=cfg.get("tool_permissions", {"*": "allow"}),
        allowed_subagents=cfg.get("allowed_subagents", []),
        default_repo=cfg.get("default_repo", "yupp-mind"),
        max_turns=cfg.get("max_turns", 20),
        max_budget_usd=cfg.get("max_budget_usd", 2.0),
        timeout_s=cfg.get("timeout_s", 300),
        sandbox_enabled=sandbox_cfg.get("enabled", True),
        allowed_gateways=cfg.get("allowed_gateways", ["*"]),
        creator_user_id=agent.creator_user_id,
    )


async def list_agents(
    *,
    user_id: str | None = None,
    include_all: bool = False,
) -> AgentListResponse:
    """List agents with optional user filtering.

    When include_all=False (default), only returns agents created by user_id (DB agents).
    When include_all=True, returns all filesystem agents + all DB agents.
    """
    result_agents: list[AgentInfo] = []

    if include_all:
        # Include all filesystem agents
        fs_agents = discover_agents()
        result_agents.extend(_build_agent_info(cfg) for cfg in fs_agents.values())

    # Query DB agents with user_id filter when not including all.
    # When include_all=False and user_id is missing, return no DB agents
    # to avoid leaking cross-user agent metadata.
    async with get_async_session() as session:
        query = select(Agent)
        if not include_all:
            if not user_id:
                return AgentListResponse(agents=result_agents)
            query = query.where(Agent.creator_user_id == user_id)
        db_result = await session.exec(query)
        db_agents = db_result.all()

        # Track names already added from filesystem to avoid duplicates
        seen_names = {a.name for a in result_agents}
        for agent in db_agents:
            if agent.name in seen_names:
                continue
            seen_names.add(agent.name)
            result_agents.append(_build_agent_info_from_db(agent))

    if user_id:
        for agent_info in result_agents:
            agent_info.is_owner = agent_info.creator_user_id == user_id

    return AgentListResponse(agents=result_agents)


async def get_agent_detail(
    agent_name: str,
    include_system_prompts: bool = False,
) -> AgentDetailResponse:
    """Get a single agent's config, optionally including system prompt files."""
    cfg = await _load_agent_config_with_db_fallback(agent_name)
    if not cfg:
        raise ValueError(f"Agent not found: {agent_name}")

    prompts: dict[str, str] | None = None
    if include_system_prompts:
        from ypl.agent_harness_service.common.config import read_file_if_exists
        from ypl.agent_harness_service.common.constants import AHS_SHARED_DIR

        prompts = {}
        # Shared files: SOUL.md first, then rest alphabetically
        import glob as glob_mod

        soul_path = os.path.join(AHS_SHARED_DIR, "SOUL.md")
        soul_content = read_file_if_exists(soul_path)
        if soul_content:
            prompts["shared/SOUL.md"] = soul_content

        for md_path in sorted(glob_mod.glob(os.path.join(AHS_SHARED_DIR, "*.md"))):
            if md_path == soul_path:
                continue
            content = read_file_if_exists(md_path)
            if content:
                prompts[f"shared/{os.path.basename(md_path)}"] = content

        # Agent-specific files (with legacy core/ fallback)
        role_content = read_file_if_exists(os.path.join(AHS_AGENTS_DIR, agent_name, "ROLE.md"))
        if role_content:
            prompts[f"{agent_name}/ROLE.md"] = role_content
        else:
            for fname in ["ROLE.md", "SOUL.md"]:
                content = read_file_if_exists(os.path.join(AHS_AGENTS_DIR, agent_name, "core", fname))
                if content:
                    prompts[f"{agent_name}/{fname}"] = content

    additional_system_prompt = cfg.additional_system_prompt if include_system_prompts else None
    return AgentDetailResponse(
        agent=_build_agent_info(cfg),
        system_prompts=prompts,
        additional_system_prompt=additional_system_prompt,
    )


def _build_session_info(
    row: AgentSession,
    agent_name: str,
    message_count: int = 0,
) -> SessionInfo:
    """Build a SessionInfo from a DB row."""
    ctx = row.context or {}
    perms = None
    has_full = None
    if "permissions" in ctx:
        sp = SessionPermissions.from_context(ctx)
        perms = {
            "allowed_servers": ",".join(sp.allowed_servers),
            "allowed_harness_tools": ",".join(sp.allowed_harness_tools),
        }
        has_full = sp.has_full_tool_access

    return SessionInfo(
        session_id=str(row.agent_session_id),
        agent_name=agent_name,
        status=row.status.value,
        trigger=row.trigger.value,
        model=row.model,
        created_at=row.created_at,
        slack_channel_name=ctx.get("slack_channel_name"),
        slack_user_id=ctx.get("slack_user_id"),
        tool_permissions=perms,
        has_full_tool_access=has_full,
        parent_session_id=str(row.parent_session_id) if row.parent_session_id else None,
        title=row.title,
        message_count=message_count,
    )


async def list_sessions(
    *,
    status: str | None = None,
    agent_name: str | None = None,
    trigger: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    root_sessions_only: bool = True,
    user_id: str | None = None,
    include_all: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> SessionListResponse:
    """List sessions with optional filters and pagination.

    When include_all=False (default), only returns sessions created by user_id.
    When include_all=True, returns all sessions regardless of creator.
    """
    async with get_async_session() as session:
        # Build base query
        query = select(AgentSession).join(Agent, AgentSession.agent_id == Agent.agent_id)  # type: ignore[arg-type]

        if not include_all:
            if not user_id:
                return SessionListResponse(sessions=[], total=0, limit=limit, offset=offset)
            query = query.where(AgentSession.creator_user_id == user_id)
        if root_sessions_only:
            query = query.where(AgentSession.parent_session_id.is_(None))  # type: ignore[union-attr]
        if status:
            try:
                query = query.where(AgentSession.status == AgentSessionStatus(status.upper()))
            except ValueError:
                raise ValueError(
                    f"Invalid status filter: {status!r}. Valid values: {[s.value for s in AgentSessionStatus]}"
                ) from None
        if agent_name:
            query = query.where(Agent.name == agent_name)
        if trigger:
            try:
                query = query.where(AgentSession.trigger == AgentSessionTrigger(trigger.upper()))
            except ValueError:
                raise ValueError(
                    f"Invalid trigger filter: {trigger!r}. Valid values: {[t.value for t in AgentSessionTrigger]}"
                ) from None
        if since:
            query = query.where(col(AgentSession.created_at) >= since)
        if until:
            query = query.where(col(AgentSession.created_at) <= until)

        # Total count (same filters, no pagination)
        count_q = select(func.count()).select_from(query.subquery())
        count_result = await session.exec(count_q)
        total = count_result.one()

        # Paginated results
        query = query.order_by(col(AgentSession.created_at).desc()).offset(offset).limit(limit)
        result = await session.exec(query)
        rows = result.all()

        # Message counts: batch query for all session IDs in this page
        session_ids = [r.agent_session_id for r in rows]
        msg_counts: dict[uuid.UUID, int] = {}
        if session_ids:
            mc_q = (
                select(AgentSessionMessage.agent_session_id, func.count())
                .where(col(AgentSessionMessage.agent_session_id).in_(session_ids))
                .group_by(col(AgentSessionMessage.agent_session_id))
            )
            mc_result = await session.exec(mc_q)
            msg_counts = dict(mc_result.all())

        # Resolve agent names via batch query
        agent_ids = list({r.agent_id for r in rows})
        agent_map: dict[uuid.UUID, str] = {}
        if agent_ids:
            agent_result = await session.exec(select(Agent).where(col(Agent.agent_id).in_(agent_ids)))
            agent_map = {a.agent_id: a.name for a in agent_result.all()}

        items = [
            _build_session_info(
                r,
                agent_name=agent_map.get(r.agent_id, str(r.agent_id)),
                message_count=msg_counts.get(r.agent_session_id, 0),
            )
            for r in rows
        ]

        return SessionListResponse(sessions=items, total=total, limit=limit, offset=offset)


async def get_session_detail(session_id: str) -> SessionDetailResponse:
    """Get a single session with a flat list of all descendant subsessions."""
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        # Collect all descendant subsessions via iterative BFS
        max_depth = 20
        all_rows: list[AgentSession] = [agent_session]
        queue: deque[tuple[uuid.UUID, int]] = deque([(agent_session.agent_session_id, 0)])
        seen: set[uuid.UUID] = {agent_session.agent_session_id}
        while queue:
            parent_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            result = await session.exec(select(AgentSession).where(AgentSession.parent_session_id == parent_id))
            children = result.all()
            for child in children:
                if child.agent_session_id not in seen:
                    seen.add(child.agent_session_id)
                    all_rows.append(child)
                    queue.append((child.agent_session_id, depth + 1))

        # Batch-resolve agent names
        agent_ids = list({r.agent_id for r in all_rows})
        agent_map: dict[uuid.UUID, str] = {}
        if agent_ids:
            agent_result = await session.exec(select(Agent).where(col(Agent.agent_id).in_(agent_ids)))
            agent_map = {a.agent_id: a.name for a in agent_result.all()}

        # Batch-resolve message counts
        all_session_ids = [r.agent_session_id for r in all_rows]
        msg_counts: dict[uuid.UUID, int] = {}
        if all_session_ids:
            mc_q = (
                select(AgentSessionMessage.agent_session_id, func.count())
                .where(col(AgentSessionMessage.agent_session_id).in_(all_session_ids))
                .group_by(col(AgentSessionMessage.agent_session_id))
            )
            mc_result = await session.exec(mc_q)
            msg_counts = dict(mc_result.all())

        # Build SessionInfo list
        infos = [
            _build_session_info(
                r,
                agent_name=agent_map.get(r.agent_id, str(r.agent_id)),
                message_count=msg_counts.get(r.agent_session_id, 0),
            )
            for r in all_rows
        ]

        return SessionDetailResponse(
            session=infos[0],
            subsessions=infos[1:],
        )
