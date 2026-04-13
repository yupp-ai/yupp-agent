"""Agent-to-agent (A2A) messaging MCP tool.

Provides ``send_agent_message`` — a fire-and-forget primitive that lets one
agent asynchronously deliver a message to another.  The message is durably
persisted in the ``agent_messages`` table and enqueued to Redis for delivery.

Design reference: http://go/p/a2a-messaging-design@5 (Sections 7, 13)
"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select

from ypl.agent_harness_service.common.agent_messaging_authz import (
    AgentAuthorizationError,
    check_agent_message_authz,
)
from ypl.agent_harness_service.common.config import load_agent_config, load_agent_config_from_db
from ypl.agent_harness_service.common.constants import mcp_session_id_var
from ypl.agent_harness_service.tools.mcp_instance import _validate_session_id, mcp
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    Agent,
    AgentMessage,
    AgentMessageStatus,
    AgentSession,
    AgentSessionStatus,
)
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Maximum allowed size for the message content field (matches DB column limit).
MAX_CONTENT_BYTES = 64 * 1024  # 64 KB


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@mcp.tool(
    name="send_agent_message",
    description=(
        "Send an asynchronous message to another agent. "
        "The message is durably persisted and delivered at-least-once — "
        "this call returns immediately with status='queued' (fire-and-forget). "
        "Use to_session_id to inject into an existing session, or omit it to "
        "let the dispatcher create a new session for the target agent. "
        "The sending agent must list the recipient in its allowed_to_message config."
    ),
)
async def send_agent_message(
    to_agent_name: str,
    content: str,
    to_session_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Send a fire-and-forget message from the current agent to *to_agent_name*.

    Args:
        to_agent_name: Name of the recipient agent (e.g. ``'sre-james'``).
        content: Message body delivered to the recipient.
        to_session_id: If set, inject into this existing session (Scenario B).
            If omitted, the dispatcher creates a new session for the recipient (Scenario A).
        session_id: Harness session ID of the caller.  Normally injected via
            the ``mcp_session_id`` HTTP header; only pass explicitly when calling
            from a context that cannot set the header.

    Returns:
        ``{"agent_message_id": "<uuid>", "status": "queued"}`` on success, or
        ``{"error": "<reason>"}`` on failure.
    """
    # ------------------------------------------------------------------
    # 0. Validate content size
    # ------------------------------------------------------------------
    if len(content.encode()) > MAX_CONTENT_BYTES:
        return {"error": f"content exceeds maximum size ({MAX_CONTENT_BYTES} bytes)"}

    # ------------------------------------------------------------------
    # 1. Resolve and validate the caller's session ID
    # ------------------------------------------------------------------
    effective_session_id = mcp_session_id_var.get() or session_id
    if not effective_session_id:
        return {"error": "No session context — pass session_id or call from within a session."}

    try:
        _validate_session_id(effective_session_id)
    except ValueError as exc:
        return {"error": str(exc)}

    if to_session_id is not None:
        try:
            _validate_session_id(to_session_id)
        except ValueError as exc:
            return {"error": f"Invalid to_session_id: {exc}"}

    logger.info(
        "MCP tool: send_agent_message",
        from_session_id=effective_session_id,
        to_agent_name=to_agent_name,
        to_session_id=to_session_id,
    )

    from_session_uuid = uuid.UUID(effective_session_id)
    to_session_uuid = uuid.UUID(to_session_id) if to_session_id is not None else None

    try:
        async with get_async_session() as db:
            # ------------------------------------------------------------------
            # 2. Look up the calling agent via its session (two queries to avoid
            #    join ON-clause type issues with mypy)
            # ------------------------------------------------------------------
            from_session_result = await db.execute(
                select(AgentSession).where(AgentSession.agent_session_id == from_session_uuid)
            )
            from_session_obj = from_session_result.scalar_one_or_none()
            if from_session_obj is None:
                return {"error": f"Session '{effective_session_id}' not found"}

            from_result = await db.execute(select(Agent).where(Agent.agent_id == from_session_obj.agent_id))
            from_agent = from_result.scalar_one_or_none()
            if from_agent is None:
                return {"error": f"No agent found for session '{effective_session_id}'"}

            # ------------------------------------------------------------------
            # 2b. Look up the recipient agent by name
            # ------------------------------------------------------------------
            to_result = await db.execute(select(Agent).where(Agent.name == to_agent_name))
            to_agent = to_result.scalar_one_or_none()
            if to_agent is None:
                return {"error": f"Agent '{to_agent_name}' not found"}

            # ------------------------------------------------------------------
            # 2c. Build AgentConfig for the sender (filesystem config wins;
            #     fall back to DB config for agents without an on-disk directory)
            # ------------------------------------------------------------------
            from_config = load_agent_config(from_agent.name)
            if from_config is None:
                from_config = load_agent_config_from_db(from_agent)

            # ------------------------------------------------------------------
            # 2d. Authorization check — deny-by-default
            # ------------------------------------------------------------------
            try:
                check_agent_message_authz(from_config, to_agent_name)
            except AgentAuthorizationError as exc:
                logger.warning(
                    "send_agent_message denied by authz",
                    from_agent=from_agent.name,
                    to_agent=to_agent_name,
                    reason=str(exc),
                )
                return {"error": str(exc), "status": "denied"}

            # ------------------------------------------------------------------
            # 3. If to_session_id is set: load session; reopen if COMPLETED/STALE
            # ------------------------------------------------------------------
            if to_session_uuid is not None:
                # with_for_update() makes the status check + update atomic, preventing TOCTOU
                # races where two concurrent senders both read COMPLETED and both reopen.
                to_session_result = await db.execute(
                    select(AgentSession).where(AgentSession.agent_session_id == to_session_uuid).with_for_update()
                )
                to_session = to_session_result.scalar_one_or_none()
                if to_session is None:
                    return {"error": f"Target session '{to_session_id}' not found"}

                # Verify session ownership — prevents injecting a message into another
                # agent's session even when the caller is authorized to message that agent.
                if to_session.agent_id != to_agent.agent_id:
                    return {"error": f"Session '{to_session_id}' does not belong to agent '{to_agent_name}'"}

                if to_session.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
                    logger.info(
                        "Reopening target session for A2A message inject",
                        to_session_id=to_session_id,
                        previous_status=to_session.status,
                    )
                    to_session.status = AgentSessionStatus.ACTIVE
                    db.add(to_session)
                    await db.flush()

            # ------------------------------------------------------------------
            # 4. INSERT AgentMessage row
            # ------------------------------------------------------------------
            msg = AgentMessage(
                from_agent_id=from_agent.agent_id,
                to_agent_id=to_agent.agent_id,
                from_session_id=from_session_uuid,
                to_session_id=to_session_uuid,
                content=content,
                status=AgentMessageStatus.QUEUED,
                queued_at=datetime.now(UTC),
            )
            db.add(msg)
            await db.flush()  # materialise agent_message_id before commit

            msg_id = str(msg.agent_message_id)
            await db.commit()

        # ------------------------------------------------------------------
        # 5. RPUSH to Redis — best-effort fast path.
        #    If Redis is unavailable, the message is still durably queued in
        #    DB and will be re-enqueued by the startup recovery sweep.
        # ------------------------------------------------------------------
        # Use the normalised UUID (lowercase) — not the raw caller-supplied string — so the
        # key matches exactly what every other component in the system produces via str(uuid.UUID(...)).
        redis_key = f"session_inbox:{to_session_uuid}" if to_session_uuid is not None else "a2a_dispatch"
        try:
            redis_client = await get_redis_client()
            await redis_client.rpush(redis_key, msg_id)  # type: ignore[misc]
            logger.info(
                "A2A message queued",
                agent_message_id=msg_id,
                from_agent=from_agent.name,
                to_agent=to_agent_name,
                redis_key=redis_key,
            )
        except Exception:
            logger.warning(
                "Redis RPUSH failed — message durably queued in DB, recovery sweep will re-enqueue",
                agent_message_id=msg_id,
                redis_key=redis_key,
            )

        return {"agent_message_id": msg_id, "status": "queued"}

    except Exception as exc:
        logger.error(
            "send_agent_message failed",
            from_session_id=effective_session_id,
            to_agent_name=to_agent_name,
            error=str(exc),
            exc_info=True,
        )
        return {"error": f"Failed to send message: {exc}"}
