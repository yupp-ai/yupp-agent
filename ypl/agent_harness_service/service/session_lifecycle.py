"""Session lifecycle — create, message, stop, attach, and internal dispatch."""

import asyncio
import json
import os
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ypl.agent_harness_service.core.subagent_queue import SubagentResult

from sqlalchemy import func, text
from sqlmodel import col, select

import ypl.db.all_models  # noqa: F401 — register all tables so FK references resolve
from ypl.agent_harness_service.common.config import (
    load_agent_config,
    load_agent_config_from_db,
)
from ypl.agent_harness_service.common.constants import (
    AHS_LIT_BASE_URL,
    AHS_MEMORIES_DIR,
    AHS_REPOS_DIR,
    AHS_SESSIONS_DIR,
    EXECUTOR_TYPE_RAW,
    HARNESS_CLAUDE_SDK,
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
    HARNESSED_MODELS,
)
from ypl.agent_harness_service.common.types import (
    AHSValidationError,
    AttachmentInfo,
    SessionAttachSlackRequest,
    SessionAttachSlackResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionMessageRequest,
    SessionMessageResponse,
    SessionPermissions,
    SessionStopResponse,
)
from ypl.agent_harness_service.core.session_persistence import sync_session_to_gcs
from ypl.agent_harness_service.core.streaming import (
    build_notice_events,
    get_pubsub,
)
from ypl.agent_harness_service.executors.command_handler import CommandHandlerManager
from ypl.agent_harness_service.executors.providers import KNOWN_MODELS
from ypl.agent_harness_service.executors.runner import (
    ClaudeCodeRunner,
    RunContext,
)
from ypl.agent_harness_service.gateway import TRIGGER_TO_GATEWAY, GatewayRegistry
from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content
from ypl.agent_harness_service.service.agent_messaging import (
    check_agent_message_authz,
)
from ypl.agent_harness_service.service.resolvers import (
    _download_attachments_to_workspace,
    _has_inflight_turn,
    _load_agent_config_with_db_fallback,
    _mark_session_completed,
    _next_turn_number,
    _prepend_attachment_paths,
    _resolve_agent,
    _resolve_personal_agent_for_user,
    _resolve_session,
    _resolve_user_name_from_db,
)
from ypl.agent_harness_service.service.run_task import _run_agent_task
from ypl.agent_harness_service.service.state import (
    _MAX_PENDING_MESSAGES,
    _SLACK_RESTART_COURTESY_MSG,
    _SLACK_SHUTDOWN_COURTESY_MSG,
    PERSONAL_AGENT_PREFIXES,
    PendingMessage,
    _active_tasks,
    _command_handlers,
    _pending_messages,
    _pre_spawn_tasks,
)
from ypl.agent_harness_service.tools.local_mcp_server import (
    clear_session_state,
    set_session_current_user,
)
from ypl.agent_harness_service.tools.workspace_tools import set_command_handler_manager
from ypl.backend.db import get_async_session
from ypl.backend.utils.async_utils import create_background_task
from ypl.backend.utils.slack_utils import resolve_slack_user_to_yupp_user_id
from ypl.backend.utils.soul_utils import has_permission_by_user_id_cached
from ypl.db.agent_harness import (
    Agent,
    AgentMessage,
    AgentMessageStatus,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageErrorType,
    AgentSessionMessageRole,
    AgentSessionStatus,
    AgentSessionTrigger,
)
from ypl.db.redis import get_redis_client
from ypl.db.soul_rbac import SoulPermission
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Slack courtesy helpers
# ---------------------------------------------------------------------------


async def _send_slack_courtesy(session_ids: list[uuid.UUID], text: str, event: str) -> None:
    """Send a courtesy message to the Slack threads for the given session IDs.

    Silently skips sessions that are not Slack-triggered or have no
    ``slack_session_id``.  Errors from individual sends are logged but do not
    propagate — courtesy messages are best-effort.
    """
    if not session_ids:
        return

    registry = GatewayRegistry.get_instance()
    gateway = registry.get("slack")
    if gateway is None:
        logger.warning("Slack gateway unavailable — skipping courtesy messages", event=event)
        return

    async with get_async_session() as db_session:
        result = await db_session.exec(
            select(AgentSession)
            .where(col(AgentSession.agent_session_id).in_(session_ids))
            .where(col(AgentSession.slack_session_id).is_not(None))
        )
        slack_sessions = result.all()

    if not slack_sessions:
        logger.info("No in-scope Slack sessions for courtesy message", event=event)
        return

    logger.info("Sending courtesy messages to Slack sessions", event=event, count=len(slack_sessions))

    results = await asyncio.gather(
        *[gateway.send_reply(s.slack_session_id, text) for s in slack_sessions if s.slack_session_id],
        return_exceptions=True,
    )

    sent = sum(1 for r in results if r is True)
    failed = len(results) - sent
    logger.info("Slack courtesy messages complete", event=event, sent=sent, failed=failed)


async def send_slack_shutdown_courtesy() -> None:
    """Send a courtesy message to all in-flight Slack sessions before shutdown.

    Called during graceful shutdown (SIGTERM) so users know the server is
    restarting and their session will be available again shortly.
    """
    await _send_slack_courtesy(
        list(_active_tasks.keys()),
        _SLACK_SHUTDOWN_COURTESY_MSG,
        "shutdown",
    )


async def send_slack_restart_courtesy(stale_session_ids: list[uuid.UUID]) -> None:
    """Send a courtesy message to stale Slack sessions after the server restarts.

    Called from ``_recover_stale_sessions`` at startup for sessions that were
    interrupted mid-turn by the previous SIGTERM so users know they can continue.
    """
    await _send_slack_courtesy(
        stale_session_ids,
        _SLACK_RESTART_COURTESY_MSG,
        "restart",
    )


async def stop_all_command_handler_managers() -> None:
    """Stop all active BCH managers.

    Called during graceful SIGTERM shutdown to cleanly terminate every
    bwrapped proxy process.  Errors from individual stops are logged but do
    not propagate so one broken manager cannot block the rest.
    """
    managers = dict(_command_handlers)
    _command_handlers.clear()
    for session_id, manager in managers.items():
        set_command_handler_manager(str(session_id), None)
        try:
            await manager.stop()
        except Exception:
            logger.warning(
                "Failed to stop BCH manager during shutdown",
                session_id=str(session_id),
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Message queue / subagent delivery
# ---------------------------------------------------------------------------


async def _drain_pending_messages(agent_session_id: uuid.UUID) -> None:
    """Process queued messages after a turn completes.

    Pops all pending messages for the session, combines them into a single
    message, and dispatches a new turn via send_message(). If only one message
    is queued, it's sent as-is. Multiple messages get a header and numbered list.
    """
    pending = _pending_messages.pop(agent_session_id, [])
    if not pending:
        return

    if len(pending) == 1:
        combined = pending[0].message
    else:
        parts = [f"[{len(pending)} messages arrived while processing the previous turn]\n"]
        for i, p in enumerate(pending, 1):
            parts.append(f"({i}) {p.message}")
        combined = "\n\n".join(parts)

    # Use metadata from the most recent queued message.
    # TODO: if multi-user threads need per-sender permissions, enforce same-sender
    # batching or apply least-privilege when combining (see PR #10645).
    last = pending[-1]

    # Merge attachments from all pending messages (each may carry its own files).
    all_attachments: list[AttachmentInfo] = []
    for p in pending:
        all_attachments.extend(p.attachments)

    logger.info(
        "Draining pending messages into new turn",
        session_id=str(agent_session_id),
        count=len(pending),
        combined_length=len(combined),
    )

    try:
        await send_message(
            SessionMessageRequest(
                session_id=str(agent_session_id),
                message=combined,
                slack_ts=last.slack_ts,
                slack_user_id=last.slack_user_id,
                user_id=last.user_id,
                attachments=all_attachments or None,
                source=last.source,  # preserve original request source
            )
        )
    except Exception:
        logger.error(
            "Failed to drain pending messages, re-queuing",
            session_id=str(agent_session_id),
            count=len(pending),
            exc_info=True,
        )
        # Re-queue so the messages aren't silently dropped after the user was told "queued".
        existing = _pending_messages.get(agent_session_id, [])
        _pending_messages[agent_session_id] = pending + existing


async def _drain_session_inbox(session_id: uuid.UUID) -> None:
    """Drain pending A2A messages from Redis at the turn boundary (Scenario B).

    Called at the end of every turn, immediately after ``_drain_pending_messages``.
    Pops message IDs from ``session_inbox:{session_id}``, atomically claims each
    in its own ``AsyncSession`` (fresh per message to avoid ``PendingRollbackError``
    cascade), constructs a ``FELLOW_AGENT`` ``AgentSessionMessage`` with provenance
    fields, and finalises the ``AgentMessage`` row as delivered — all in a single
    commit.

    When no turn is currently running the function directly fires
    ``_run_agent_task`` and breaks so that subsequent inbox messages are processed
    at the next turn boundary (one direct turn per drain cycle).  When a turn IS
    already running (started by ``_drain_pending_messages``) the message is queued
    via ``_pending_messages`` as a fallback; in that path the ``AgentSessionMessage``
    will be ``USER`` role rather than ``FELLOW_AGENT``, but the ``AgentMessage``
    row retains full provenance.

    On any per-message failure the ``AgentMessage`` row is reset to QUEUED (or
    FAILED if ``attempt_count`` is exhausted) and the ID is re-pushed to the
    session inbox so the next turn boundary retries delivery.
    """
    try:
        redis = await get_redis_client()
    except Exception:
        logger.warning(
            "A2A inbox drain: Redis unavailable — skipping",
            session_id=str(session_id),
        )
        return

    while True:
        # Non-blocking pop — returns None immediately if the list is empty.
        try:
            raw = await redis.lpop(f"session_inbox:{session_id}")  # type: ignore[misc]
        except Exception:
            logger.warning(
                "A2A inbox drain: Redis LPOP failed",
                session_id=str(session_id),
                exc_info=True,
            )
            break

        if not raw:
            break

        msg_id = raw.decode() if isinstance(raw, bytes) else str(raw)

        # ------------------------------------------------------------------
        # Each message gets its own AsyncSession so a failed rollback on one
        # message cannot leave subsequent iterations in PendingRollbackError.
        # ------------------------------------------------------------------
        async with get_async_session() as msg_db:
            # ------------------------------------------------------------------
            # Atomic DB claim — prevents double delivery if startup recovery
            # re-enqueued this message concurrently.
            # ------------------------------------------------------------------
            try:
                claim_result = await msg_db.execute(
                    text("""
                        UPDATE agent_messages
                        SET status        = 'delivering',
                            claimed_at    = now(),
                            attempt_count = attempt_count + 1
                        WHERE agent_message_id = :id
                          AND status = 'queued'
                          AND attempt_count < max_attempts
                        RETURNING agent_message_id, content, from_agent_id, from_session_id,
                                  to_session_id, attempt_count, max_attempts
                    """),
                    {"id": msg_id},
                )
                await msg_db.commit()
            except Exception:
                logger.error(
                    "A2A inbox drain: DB claim failed",
                    agent_message_id=msg_id,
                    session_id=str(session_id),
                    exc_info=True,
                )
                # Re-push the already-LPOP'd ID so the next turn boundary retries.
                try:
                    await redis.rpush(f"session_inbox:{session_id}", msg_id)  # type: ignore[misc]
                except Exception:
                    logger.error(
                        "A2A inbox drain: failed to re-push after claim failure",
                        agent_message_id=msg_id,
                        exc_info=True,
                    )
                continue

            row = claim_result.mappings().one_or_none()
            if row is None:
                # Already claimed by another worker, or max_attempts exhausted.
                logger.debug(
                    "A2A inbox drain: message already claimed or exhausted — skipping",
                    agent_message_id=msg_id,
                    session_id=str(session_id),
                )
                continue

            # ------------------------------------------------------------------
            # Guard: validate that this message was actually addressed to this
            # session.  A bug in the sender, a Redis key collision, or corrupt
            # RPUSH could land a wrong message ID in our inbox.
            # ------------------------------------------------------------------
            claimed_to_session = row["to_session_id"]
            if claimed_to_session is None or str(claimed_to_session) != str(session_id):
                logger.error(
                    "A2A inbox drain: to_session_id mismatch — resetting",
                    agent_message_id=msg_id,
                    expected_session=str(session_id),
                    got_session=str(claimed_to_session),
                )
                try:
                    await msg_db.execute(
                        text("""
                            UPDATE agent_messages
                            SET status = 'queued', claimed_at = NULL
                            WHERE agent_message_id = :id
                        """),
                        {"id": msg_id},
                    )
                    await msg_db.commit()
                    if claimed_to_session is not None:
                        await redis.rpush(f"session_inbox:{claimed_to_session}", msg_id)  # type: ignore[misc]
                except Exception:
                    logger.error(
                        "A2A inbox drain: failed to reset misrouted message",
                        agent_message_id=msg_id,
                        exc_info=True,
                    )
                continue

            try:
                # ------------------------------------------------------------------
                # Load session + agent metadata.
                # ------------------------------------------------------------------
                session_obj = await msg_db.get(AgentSession, session_id)
                if session_obj is None:
                    raise ValueError(f"Session {session_id} not found — cannot inject FELLOW_AGENT turn")

                agent_obj = await msg_db.get(Agent, session_obj.agent_id)
                if agent_obj is None:
                    raise ValueError(f"Agent not found for session {session_id}")

                # ------------------------------------------------------------------
                # Check whether a new turn is already running (started by
                # _drain_pending_messages).  If so, fall back to _pending_messages
                # rather than starting a second concurrent turn.
                # ------------------------------------------------------------------
                current_task = asyncio.current_task()
                has_active_turn = _active_tasks.get(session_id) is not current_task

                if has_active_turn:
                    # Fallback path: queue via _pending_messages so the in-progress
                    # turn's boundary will pick it up.  The AgentSessionMessage in
                    # this path will be USER role (via send_message), not FELLOW_AGENT,
                    # but the AgentMessage row retains provenance.
                    _pending_messages.setdefault(session_id, []).append(
                        PendingMessage(
                            message=row["content"],
                            user_id=session_obj.creator_user_id,
                        )
                    )
                    await msg_db.execute(
                        text("""
                            UPDATE agent_messages
                            SET status              = :delivered,
                                delivered_at        = now(),
                                resolved_session_id = :sid
                            WHERE agent_message_id  = :id
                        """),
                        {
                            "delivered": AgentMessageStatus.DELIVERED.value,
                            "sid": str(session_id),
                            "id": msg_id,
                        },
                    )
                    await msg_db.commit()
                    logger.info(
                        "A2A inbox: queued via _pending_messages (turn already in progress)",
                        session_id=str(session_id),
                        agent_message_id=msg_id,
                    )
                    continue

                # ------------------------------------------------------------------
                # Normal path: write FELLOW_AGENT turn + mark delivered in one
                # commit, then directly fire _run_agent_task.
                # ------------------------------------------------------------------
                # Re-activate session if it is in a terminal-but-resumable state.
                if session_obj.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
                    session_obj.status = AgentSessionStatus.ACTIVE

                turn_number = await _next_turn_number(msg_db, session_id)
                fellow_turn = AgentSessionMessage(
                    agent_session_id=session_id,
                    turn_number=turn_number,
                    role=AgentSessionMessageRole.FELLOW_AGENT,
                    content=row["content"],
                    creator_user_id=session_obj.creator_user_id,
                    from_agent_id=uuid.UUID(str(row["from_agent_id"])),
                    agent_message_id_ref=uuid.UUID(msg_id),
                )
                msg_db.add(fellow_turn)

                # Single commit: FELLOW_AGENT AgentSessionMessage + AgentMessage delivered.
                await msg_db.execute(
                    text("""
                        UPDATE agent_messages
                        SET status              = :delivered,
                            delivered_at        = now(),
                            resolved_session_id = :sid
                        WHERE agent_message_id  = :id
                    """),
                    {
                        "delivered": AgentMessageStatus.DELIVERED.value,
                        "sid": str(session_id),
                        "id": msg_id,
                    },
                )
                await msg_db.commit()

                # Directly fire _run_agent_task — no _pending_messages involved,
                # so no duplicate USER-role AgentSessionMessage is written.
                trigger_val = session_obj.trigger.value if session_obj.trigger else None
                _active_tasks[session_id] = None  # type: ignore[assignment]
                task = create_background_task(
                    _run_agent_task(
                        agent_session_id=session_id,
                        turn_number=turn_number,
                        message=row["content"],
                        agent_config_name=agent_obj.name,
                        workspace=session_obj.workspace,
                        llm_session_id=session_obj.llm_session_id,
                        extra_dirs=session_obj.extra_dirs or [],
                        slack_session_id=session_obj.slack_session_id,
                        is_slack=(session_obj.trigger == AgentSessionTrigger.SLACK),
                        is_task=(session_obj.trigger == AgentSessionTrigger.TASK),
                        session_context=dict(session_obj.context or {}),
                        trigger=trigger_val,
                    )
                )
                _active_tasks[session_id] = task

                logger.info(
                    "A2A inbox: started FELLOW_AGENT turn directly",
                    session_id=str(session_id),
                    agent_message_id=msg_id,
                    turn_number=turn_number,
                    from_agent_id=str(row["from_agent_id"]),
                )
                # Break after firing one direct turn — remaining inbox messages wait
                # for the next turn boundary so we never have two concurrent turns.
                break

            except Exception as exc:
                logger.error(
                    "A2A inbox drain: delivery failed — resetting message",
                    session_id=str(session_id),
                    agent_message_id=msg_id,
                    exc_info=True,
                )
                # Roll back partial writes, reset status, re-push for retry.
                try:
                    await msg_db.rollback()
                    await msg_db.execute(
                        text("""
                            UPDATE agent_messages
                            SET status     = CASE
                                               WHEN attempt_count >= max_attempts THEN 'failed'
                                               ELSE 'queued'
                                             END,
                                error      = :err,
                                claimed_at = NULL
                            WHERE agent_message_id = :id
                        """),
                        {"err": str(exc)[:1000], "id": msg_id},
                    )
                    await msg_db.commit()
                    if int(row["attempt_count"]) < int(row["max_attempts"]):
                        await redis.rpush(f"session_inbox:{session_id}", msg_id)  # type: ignore[misc]
                except Exception:
                    logger.error(
                        "A2A inbox drain: failed to reset message after delivery failure",
                        agent_message_id=msg_id,
                        session_id=str(session_id),
                        exc_info=True,
                    )


async def _inject_internal_message(
    agent_session_id: uuid.UUID,
    message: str,
    agent_session_data: dict[str, Any],
) -> None:
    """Inject a harness-generated message into a session, bypassing user validation.

    Used to deliver subagent results back to the parent session without requiring
    the message to come from a real user. The session's creator_user_id is used
    for DB attribution.

    Args:
        agent_session_id: UUID of the target session.
        message: The message content to inject.
        agent_session_data: Pre-fetched session metadata (from deliver_subagent_result_to_parent).
    """
    agent_config_name: str | None = agent_session_data.get("agent_name")
    creator_user_id: str | None = agent_session_data.get("creator_user_id")

    if not agent_config_name:
        logger.error(
            "Cannot inject internal message: missing agent_name",
            session_id=str(agent_session_id),
        )
        return

    agent_config = await _load_agent_config_with_db_fallback(agent_config_name)
    if not agent_config:
        logger.error(
            "Cannot inject internal message: agent config not found",
            session_id=str(agent_session_id),
            agent_name=agent_config_name,
        )
        return

    # Write USER message and re-activate session in a single DB transaction
    turn_number: int
    async with get_async_session() as session:
        result = await session.exec(select(AgentSession).where(AgentSession.agent_session_id == agent_session_id))
        agent_session = result.one_or_none()
        if not agent_session:
            logger.error(
                "Cannot inject internal message: session not found",
                session_id=str(agent_session_id),
            )
            return

        # Re-activate if in a terminal-but-resumable state
        if agent_session.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
            agent_session.status = AgentSessionStatus.ACTIVE

        turn_number = await _next_turn_number(session, agent_session_id)
        user_msg = AgentSessionMessage(
            agent_session_id=agent_session_id,
            turn_number=turn_number,
            role=AgentSessionMessageRole.USER,
            content=message,
            creator_user_id=creator_user_id,
        )
        session.add(user_msg)
        await session.commit()

    # Capture session fields needed for _run_agent_task (session object is now detached)
    workspace = agent_session_data.get("workspace")
    extra_dirs: list[str] = agent_session_data.get("extra_dirs") or []
    slack_session_id = agent_session_data.get("slack_session_id")
    llm_session_id = agent_session_data.get("llm_session_id")
    trigger = agent_session_data.get("trigger")
    session_context: dict[str, Any] = dict(agent_session_data.get("context") or {})
    is_slack = trigger == AgentSessionTrigger.SLACK.value if trigger else False
    is_task = trigger == AgentSessionTrigger.TASK.value if trigger else False

    # Register sentinel and fire background task
    _active_tasks[agent_session_id] = None  # type: ignore[assignment]
    task = create_background_task(
        _run_agent_task(
            agent_session_id=agent_session_id,
            turn_number=turn_number,
            message=message,
            agent_config_name=agent_config_name,
            workspace=workspace,
            llm_session_id=llm_session_id,
            extra_dirs=extra_dirs,
            slack_session_id=slack_session_id,
            is_slack=is_slack,
            is_task=is_task,
            session_context=session_context,
            trigger=trigger,
        )
    )
    _active_tasks[agent_session_id] = task

    logger.info(
        "Injected internal message into session",
        session_id=str(agent_session_id),
        turn_number=turn_number,
        agent_name=agent_config_name,
        message_length=len(message),
    )


async def deliver_subagent_result_to_parent(parent_session_id: str, result: "SubagentResult") -> None:
    """Deliver a completed subagent's result to its parent session.

    Called by subagent_queue._drain() after a subagent finishes (any outcome).
    Formats the result as a user-turn message and either:
    - Queues it in _pending_messages if the parent is currently processing a turn
    - Injects it directly via _inject_internal_message if the parent is idle

    Args:
        parent_session_id: UUID string of the parent session.
        result: SubagentResult with status, text, and metadata.
    """
    # Import here to avoid circular import at module load
    from ypl.agent_harness_service.core.subagent_queue import SubagentResult  # noqa: F401

    try:
        parent_uuid = uuid.UUID(parent_session_id)
    except ValueError:
        logger.error(
            "Invalid parent_session_id for subagent delivery",
            parent_session_id=parent_session_id,
        )
        return

    # Format the message
    status_icon = "✅" if result.status == "completed" else "❌"
    short_sid = result.db_session_id[:8] if result.db_session_id else "unknown"
    duration_str = f"{result.duration_ms / 1000:.1f}s" if result.duration_ms else "?"
    header = f"[SUBAGENT RESULT] {status_icon} `{result.agent_type}` — session `{short_sid}` — {duration_str}s"
    if result.cost_usd:
        header += f" — ${result.cost_usd:.2f}"
    if result.description:
        header += f"\n_{result.description}_"

    message = f"{header}\n\n{result.text}" if result.text else header

    # Fetch parent session metadata
    agent_session_data: dict[str, Any] | None = None
    try:
        async with get_async_session() as db:
            agent_session_obj = await db.get(AgentSession, parent_uuid)
            if agent_session_obj:
                agent_obj = await db.get(Agent, agent_session_obj.agent_id)
                agent_session_data = {
                    "creator_user_id": agent_session_obj.creator_user_id,
                    "agent_name": agent_obj.name if agent_obj else None,
                    "workspace": agent_session_obj.workspace,
                    "extra_dirs": agent_session_obj.extra_dirs or [],
                    "slack_session_id": agent_session_obj.slack_session_id,
                    "llm_session_id": agent_session_obj.llm_session_id,
                    "trigger": agent_session_obj.trigger.value if agent_session_obj.trigger else None,
                    "context": dict(agent_session_obj.context) if agent_session_obj.context else {},
                    "status": agent_session_obj.status,
                }
    except Exception:
        logger.error(
            "Failed to fetch parent session for subagent result delivery",
            parent_session_id=parent_session_id,
            exc_info=True,
        )
        return

    if not agent_session_data:
        logger.warning(
            "Parent session not found for subagent result delivery",
            parent_session_id=parent_session_id,
            agent_type=result.agent_type,
        )
        return

    creator_user_id = agent_session_data.get("creator_user_id")
    trigger = agent_session_data.get("trigger")

    # For single-turn non-interactive triggers (CRON/TASK), skip re-injection entirely.
    # These triggers tear down their BCH manager after turn 1, so a re-injected turn 2
    # would run without a BCH manager and be silently dropped on AHS restart, leaving
    # the session ACTIVE forever ("hercule-poirot problem", session 36cc6d2b, 2026-03-30).
    #
    # WEBHOOK and API sessions are NOT included: they use fire-and-forget subagents
    # (new_task) whose results arrive asynchronously via the subagent queue and must be
    # re-injected as follow-up turns. The MCP tool only returns status="spawned" — the
    # actual result is never delivered inline.
    _NON_INTERACTIVE_TRIGGERS = {
        AgentSessionTrigger.CRON.value,
        AgentSessionTrigger.TASK.value,
    }
    if trigger in _NON_INTERACTIVE_TRIGGERS:
        logger.info(
            "Skipping subagent result re-injection for non-interactive session",
            parent_session_id=parent_session_id,
            trigger=trigger,
            agent_type=result.agent_type,
        )
        return

    logger.info(
        "Delivering subagent result to parent",
        parent_session_id=parent_session_id,
        agent_type=result.agent_type,
        status=result.status,
        db_session_id=result.db_session_id,
        parent_has_inflight=parent_uuid in _active_tasks,
    )

    # Queue or inject
    if parent_uuid in _active_tasks:
        # Parent is busy — queue for delivery after current turn completes
        queue = _pending_messages.setdefault(parent_uuid, [])
        if len(queue) < _MAX_PENDING_MESSAGES:
            queue.append(
                PendingMessage(
                    message=message,
                    user_id=creator_user_id,
                    source="api",
                )
            )
            logger.info(
                "Subagent result queued (parent busy)",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                queue_depth=len(queue),
            )
        else:
            logger.error(
                "Subagent result dropped: pending queue full",
                parent_session_id=parent_session_id,
                agent_type=result.agent_type,
                queue_size=_MAX_PENDING_MESSAGES,
            )
    else:
        # Parent is idle — inject directly
        await _inject_internal_message(parent_uuid, message, agent_session_data)


async def _maybe_update_task_completion(
    trigger: str | None,
    session_context: dict[str, Any] | None,
    agent_session_id: uuid.UUID,
    success: bool,
    result: dict | None,
    error: str | None,
    error_subtype: str | None = None,
) -> None:
    """Update task completion status if session was triggered by a task.

    This is a no-op if the session was not triggered by the task executor.
    """
    if trigger != AgentSessionTrigger.TASK.value:
        return
    if not session_context or not session_context.get("task_id"):
        return

    try:
        # Inline import to avoid circular dependency (task_executor imports service.create_session)
        from ypl.agent_harness_service.task_executor import update_task_completion

        await update_task_completion(
            task_id=uuid.UUID(session_context["task_id"]),
            session_id=str(agent_session_id),
            success=success,
            result=result,
            error=error,
            error_subtype=error_subtype,
        )
    except Exception:
        logger.error(
            "Failed to update task completion",
            task_id=session_context.get("task_id"),
            session_id=str(agent_session_id),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

# All valid harness names (executor_config.model when type="harnessed").
_KNOWN_HARNESSED_MODELS: frozenset[str] = frozenset(HARNESSED_MODELS)

# All valid raw models (provider/model_id format).
_KNOWN_RAW_MODELS: frozenset[str] = frozenset(KNOWN_MODELS)


def _validate_force_model(force_model: str) -> None:
    """Validate a force_model value against known harness names and raw models.

    Raises:
        AHSValidationError: If the model is not in either known set.
    """
    if force_model in _KNOWN_HARNESSED_MODELS or force_model in _KNOWN_RAW_MODELS:
        return
    all_models = sorted(_KNOWN_HARNESSED_MODELS) + sorted(_KNOWN_RAW_MODELS)
    raise AHSValidationError(f"Unknown model {force_model!r}. Valid models: {', '.join(all_models)}")


async def create_session(request: SessionCreateRequest) -> SessionCreateResponse:
    """Create a new session or resume an existing one.

    If request.message is provided, also kicks off the first agent turn
    in the background.
    """
    # If session_id provided, try to resume.
    # Resume-without-message is read-only (returns session_id/status) so user_id
    # is not required.  Resume-with-message delegates to send_message() which
    # enforces user_id and sender==creator checks.
    if request.session_id:
        async with get_async_session() as session:
            existing = await _resolve_session(session, request.session_id)
        if existing:
            # DB session closed — send_message opens its own
            if request.message:
                _ctx = request.context or {}
                msg_request = SessionMessageRequest(
                    session_id=request.session_id,
                    message=request.message,
                    slack_user_id=_ctx.get("slack_user_id"),
                    user_id=_ctx.get("user_id") or _ctx.get("yupp_user_id") or request.user_id,
                    attachments=request.attachments,
                    source=request.source,
                )
                await send_message(msg_request)
                # If the session has an inflight turn, send_message queues the
                # message instead of rejecting — so no error handling needed.
            return SessionCreateResponse(
                session_id=str(existing.agent_session_id),
                status=existing.status.value,
            )

    # Map trigger string to enum (before DB session so we can branch on it).
    trigger_map = {
        "slack": AgentSessionTrigger.SLACK,
        "webhook": AgentSessionTrigger.WEBHOOK,  # not currently used; reserved for future integrations
        "cron": AgentSessionTrigger.CRON,
        "task": AgentSessionTrigger.TASK,  # Triggered by project task executor
        "agent": AgentSessionTrigger.AGENT,  # Triggered by a peer agent via A2A messaging
        "api": AgentSessionTrigger.API,
    }
    trigger = trigger_map.get(request.trigger.lower(), AgentSessionTrigger.API)

    # TODO: If slack_channel_id is present but slack_channel_name is missing,
    # look up the channel name via Slack conversations.info API and cache the
    # mapping (channel_id → channel_name) so subsequent sessions reuse it.
    context = request.context or {}

    # Validate and store force_model override so the task runner can apply it on
    # every turn without re-parsing the request.  Validated early (before DB work)
    # so callers get a fast 400 on bad model names.
    if request.force_model:
        _validate_force_model(request.force_model)
        context["force_model"] = request.force_model

    # Resolve user_id FIRST so it's available for personal agent resolution.
    # Priority: top-level request.user_id (preferred) → context fallbacks (backward compat).
    user_id = request.user_id or context.get("user_id") or context.get("yupp_user_id")
    if trigger == AgentSessionTrigger.SLACK:
        # Backward compat: resolve from slack_user_id if SAG didn't provide user_id.
        if not user_id:
            slack_user_id = context.get("slack_user_id")
            if slack_user_id:
                try:
                    user_id = await resolve_slack_user_to_yupp_user_id(slack_user_id)
                except Exception:
                    logger.warning(
                        "Failed to resolve slack_user_id to user_id",
                        slack_user_id=slack_user_id,
                    )

    # For AGENT-triggered sessions, user_id is derived from the sending agent's identity
    # inside the DB session below (from_agent.agent_user_id).  Skip the early check.
    if not user_id and trigger != AgentSessionTrigger.AGENT:
        logger.error(
            "Rejected session create: missing user_id",
            source=request.source,
            agent_id=request.agent_id,
            trigger=request.trigger,
            request=request.model_dump(),
        )
        raise AHSValidationError("user_id is required to create a session")

    # Resolve personal agent variant (e.g., "yuppclaw" → "yuppclaw-alice")
    # before looking up the agent in the DB.
    resolved_agent_id = request.agent_id
    display_name: str | None = None
    if request.agent_id in PERSONAL_AGENT_PREFIXES and user_id:
        try:
            personal_name, personal_display = await _resolve_personal_agent_for_user(request.agent_id, user_id)
            if personal_name:
                resolved_agent_id = personal_name
                display_name = personal_display
        except Exception:
            logger.warning(
                "Failed to resolve personal agent, using base agent",
                base_agent_name=request.agent_id,
                user_id=user_id,
                exc_info=True,
            )

    async with get_async_session() as session:
        # Resolve agent (using personal variant if resolved, else base name)
        agent = await _resolve_agent(session, resolved_agent_id)
        if not agent:
            # Auto-register agent from config if found on disk
            agent_config = load_agent_config(resolved_agent_id)
            if not agent_config:
                raise ValueError(f"Agent not found: {resolved_agent_id}")
            agent = Agent(
                name=resolved_agent_id,
                display_name=agent_config.display_name or resolved_agent_id,
                description=agent_config.description,
            )
            session.add(agent)
            await session.flush()

        # For AGENT-triggered sessions: resolve from_agent, enforce A2A authorization,
        # override creator identity, and guarantee full MCP permissions.
        if trigger == AgentSessionTrigger.AGENT:
            _from_agent_id_ctx = context.get("from_agent_id")
            if not _from_agent_id_ctx:
                raise AHSValidationError("context.from_agent_id is required when trigger=AGENT")
            # Parse the UUID first — narrow the ValueError catch to just this line so that
            # AHSValidationError (which inherits ValueError) raised below is not mistakenly
            # caught and replaced with a misleading "malformed from_agent_id" message.
            try:
                _from_agent_uuid = uuid.UUID(str(_from_agent_id_ctx))
            except ValueError as exc:
                raise AHSValidationError(f"context.from_agent_id is not a valid UUID: {_from_agent_id_ctx!r}") from exc
            _from_agent_obj = await session.get(Agent, _from_agent_uuid)
            if not _from_agent_obj:
                raise AHSValidationError(f"Sending agent not found: {_from_agent_id_ctx!r}")
            if not _from_agent_obj.agent_user_id:
                raise AHSValidationError(
                    f"Sending agent {_from_agent_obj.name!r} has no agent_user_id. "
                    "Ensure sweep_agent_user_identities() has run or re-create the agent."
                )

            # A2A authorization: enforce deny-by-default messaging policy.
            # AgentAuthorizationError propagates to routes.py where it is mapped to HTTP 403.
            _from_cfg = load_agent_config_from_db(_from_agent_obj)
            check_agent_message_authz(_from_cfg, agent.name or resolved_agent_id)

            # Override user_id: session is attributed to the sending agent's user identity.
            user_id = _from_agent_obj.agent_user_id
            logger.info(
                "AGENT trigger: resolved from_agent identity",
                from_agent_id=str(_from_agent_uuid),
                from_agent_name=_from_agent_obj.name,
                from_agent_user_id=user_id,
                from_session_id=context.get("from_session_id"),
            )

            # Identity verification: load the persisted AgentMessage record and confirm its
            # from_agent_id matches the caller's claim.  agent_message_id is required for all
            # AGENT-triggered sessions — it binds the trusted identity to a server-side DB
            # record rather than relying on the request body (which any AHS API key holder
            # could forge).  trigger=AGENT is a new code path with no legacy callers, so
            # there is no backward-compat reason to allow missing agent_message_id.
            _ctx_agent_msg_id = context.get("agent_message_id")
            if not _ctx_agent_msg_id:
                raise AHSValidationError("context.agent_message_id is required when trigger=AGENT")
            # Narrow the ValueError catch to just the UUID parse — same pattern as the
            # from_agent_id block.  AHSValidationError (ValueError subclass) raised below
            # must not be caught here and replaced with a misleading "not a valid UUID" message.
            try:
                _verify_msg_uuid = uuid.UUID(str(_ctx_agent_msg_id))
            except ValueError as exc:
                raise AHSValidationError(
                    f"context.agent_message_id is not a valid UUID: {_ctx_agent_msg_id!r}"
                ) from exc
            _verify_msg = await session.get(AgentMessage, _verify_msg_uuid)
            if _verify_msg is None or _verify_msg.from_agent_id != _from_agent_uuid:
                raise AHSValidationError(
                    f"A2A identity verification failed: agent_message_id {_ctx_agent_msg_id!r} "
                    f"does not belong to from_agent {_from_agent_id_ctx!r}"
                )

            # Grant full MCP permissions — AGENT-triggered sessions are trusted internal callers.
            # Identity is bound above by cross-checking agent_message_id against the DB record.
            if "permissions" not in context:
                context["permissions"] = SessionPermissions.full_access().model_dump(mode="json")

        # At this point user_id must be non-None:
        #   - AGENT trigger: set above from from_agent.agent_user_id
        #   - All other triggers: guarded by the early validation check above
        # Help mypy narrow the type so the has_permission_by_user_id_cached call type-checks.
        if user_id is None:
            raise AHSValidationError("Internal: user_id could not be resolved for session creation")

        if "permissions" in context:
            # Permissions already set by an earlier create_session caller. Respect them.
            pass
        else:
            try:
                has_mcp_access = await has_permission_by_user_id_cached(user_id, SoulPermission.USE_MCP)
                if not has_mcp_access:
                    logger.info(
                        "User lacks USE_MCP permission, yuppster-mcp tools will be disabled",
                        user_id=user_id,
                    )
            except Exception as e:
                has_mcp_access = False
                logger.warning(
                    "Error checking USE_MCP permission, disabling yuppster-mcp",
                    user_id=user_id,
                    error=str(e),
                )
            _perms = SessionPermissions.full_access() if has_mcp_access else SessionPermissions.restricted()
            context["permissions"] = _perms.model_dump(mode="json")

        # Ensure user_id is always in context so it appears in the system prompt
        # and is available to MCP tools (e.g., create_agent) via session context.
        if user_id:
            context.setdefault("user_id", user_id)

        # Enrich context with user_name from users table if not already set.
        # This ensures the system prompt has the user's real name regardless of
        # how the session was created (Slack, TUI, API, etc.).
        if user_id and not context.get("user_name"):
            try:
                _user_name = await _resolve_user_name_from_db(user_id)
                if _user_name:
                    context["user_name"] = _user_name
            except Exception:
                logger.warning("Failed to resolve user_name for context", user_id=user_id)

        # Store display name for gateway calls (e.g., "Alice's yClaw").
        # The gateway uses this to override the bot's Slack display name.
        if display_name:
            context["display_name"] = display_name

        # Proactively fetch the Slack thread now that the request is validated
        # (agent resolved, user_id confirmed).  We are still inside the DB session,
        # so a strict timeout (5s) prevents Slack latency from starving the DB pool.
        # The content is injected into context so the system prompt can include
        # thread messages on turn 1 without an MCP round-trip (~1.2-3.6s saved).
        if trigger == AgentSessionTrigger.SLACK:
            _channel = context.get("slack_channel_id", "")
            _thread_ts = context.get("slack_thread_ts", "")
            if _channel and _thread_ts:
                try:
                    _prefetched_thread = await asyncio.wait_for(
                        fetch_slack_thread_content(_channel, _thread_ts),
                        timeout=5.0,
                    )
                except TimeoutError:
                    logger.warning(
                        "Slack thread prefetch timed out, skipping",
                        channel=_channel,
                        thread_ts=_thread_ts,
                    )
                    _prefetched_thread = None
                if _prefetched_thread is not None:
                    context["slack_thread_prefetched"] = _prefetched_thread

        # Log the final resolved MCP permissions for debugging.
        _resolved_perms = SessionPermissions.from_context(context)
        logger.info(
            "MCP access resolved",
            agent_id=resolved_agent_id,
            trigger=request.trigger,
            user_id=user_id,
            allowed_servers=_resolved_perms.allowed_servers,
            allowed_harness_tools=_resolved_perms.allowed_harness_tools,
            has_full_tool_access=_resolved_perms.has_full_tool_access,
        )

        # Build constructor kwargs for the new session.
        _agent_session_kwargs: dict = {}

        # Create session (need agent_session_id for workspace path)
        agent_session = AgentSession(
            **_agent_session_kwargs,
            agent_id=agent.agent_id,
            slack_session_id=request.session_id,
            trigger=trigger,
            context=context,
            workspace="",  # set below after we know the session_id
            status=AgentSessionStatus.ACTIVE,
            creator_user_id=user_id,
        )
        session.add(agent_session)
        await session.flush()  # assigns agent_session_id (or confirms pre-issued one)

        # Set up workspace from scratch.
        # Consolidated session workspace: everything the agent needs lives here.
        # Layout: .claude/, .mcp.json, repo symlinks, worktrees, history/, attachments/
        workspace = os.path.join(AHS_SESSIONS_DIR, str(agent_session.agent_session_id))
        os.makedirs(workspace, exist_ok=True)

        # Symlink .claude/ so Claude CLI detects this dir as the project root.
        # Uses yupp-mind's .claude/ which has settings.json and hooks.
        claude_link = os.path.join(workspace, ".claude")
        claude_target = os.path.join(AHS_REPOS_DIR, "yupp-mind", ".claude")
        try:
            os.symlink(claude_target, claude_link)
        except FileExistsError:
            pass

        # Symlink all repos from AHS_REPOS_DIR into the workspace so agents
        # can access them without a separate --add-dir flag.
        if os.path.isdir(AHS_REPOS_DIR):
            for repo_entry in os.listdir(AHS_REPOS_DIR):
                repo_src = os.path.join(AHS_REPOS_DIR, repo_entry)
                if not os.path.isdir(repo_src):
                    continue
                repo_link = os.path.join(workspace, repo_entry)
                try:
                    os.symlink(repo_src, repo_link)
                except FileExistsError:
                    pass

        # Create history/ subdir for session history persistence
        os.makedirs(os.path.join(workspace, "history"), exist_ok=True)

        # Symlink persistent memory directory into workspace for all agents.
        # This makes agent_memories/ available across sessions.
        memory_dir = os.path.join(AHS_MEMORIES_DIR, resolved_agent_id, "agent_memories")
        os.makedirs(memory_dir, exist_ok=True)
        memory_link = os.path.join(workspace, "agent_memories")
        try:
            os.symlink(memory_dir, memory_link)
        except FileExistsError:
            pass
        logger.info(
            "Symlinked agent memory directory",
            agent_name=resolved_agent_id,
            memory_dir=memory_dir,
            workspace=workspace,
        )

        # Create the BCH manager for this session.  The bwrap proxy process starts
        # lazily on the first tool call; creating the manager here is cheap (no I/O).
        # Stored in a local so we can stop it on commit failure (try/finally below).
        _bch_manager = CommandHandlerManager(workspace=workspace)

        # Start pre-spawning the subprocess so the bwrap+CLI cold start (~4.2s) overlaps
        # with the remaining DB writes (session.commit, send_message DB ops, background
        # task setup).  Prerequisites satisfied above: workspace dir, .claude symlink,
        # repo symlinks, and memory symlink all exist.
        #
        # Constraints:
        #   • ClaudeCodeRunner only — raw executor and Codex CLI don't use Claude CLI.
        #   • No attachments — send_message() prepends attachment paths to the prompt,
        #     so the pre-built args would diverge from the final prompt.
        _pre_spawn_task: asyncio.Task | None = None
        if request.message and not request.attachments:
            _spawn_cfg = load_agent_config(request.agent_id)
            if (
                _spawn_cfg
                and _spawn_cfg.executor_config.type != EXECUTOR_TYPE_RAW
                and _spawn_cfg.executor_config.model not in (HARNESS_CODEX_CLI, HARNESS_CODEX_APP_SERVER)
                # SDK runner makes a direct HTTPS API call — no subprocess to pre-warm.
                and _spawn_cfg.executor_config.model != HARNESS_CLAUDE_SDK
            ):
                _spawn_context = RunContext(
                    session_id=str(agent_session.agent_session_id),
                    workspace=workspace,
                    llm_session_id=None,  # new session — no --resume
                    extra_dirs=[],
                    slack_session_id=request.session_id,
                    is_slack=(trigger == AgentSessionTrigger.SLACK),
                    is_task=(trigger == AgentSessionTrigger.TASK),
                    session_context=context,
                )
                _pre_spawn_task = asyncio.create_task(
                    ClaudeCodeRunner(_spawn_cfg).pre_spawn(request.message, _spawn_context)
                )
                logger.info(
                    "Subprocess pre-spawn started",
                    session_id=str(agent_session.agent_session_id),
                    agent_name=request.agent_id,
                )

        agent_session.workspace = workspace
        try:
            await session.commit()
            await session.refresh(agent_session)
        except Exception:
            # Cancel the pre-spawn task to avoid an orphaned subprocess.
            if _pre_spawn_task is not None:
                _pre_spawn_task.cancel()
            # Stop the BCH manager — it hasn't started yet (lazy start), so
            # this is a no-op today but ensures no state leaks on future retries.
            await _bch_manager.stop()
            raise

        # Register the pre-spawn task now that the commit succeeded, so
        # _run_agent_task can find it by session ID on the first turn.
        if _pre_spawn_task is not None:
            _pre_spawn_tasks[agent_session.agent_session_id] = _pre_spawn_task

        # Register the BCH manager now that the session is committed.
        # workspace_tools.py tool handlers query this dict on every tool call;
        # registering here (after commit) ensures no phantom entries survive a
        # failed creation.
        _command_handlers[agent_session.agent_session_id] = _bch_manager
        set_command_handler_manager(str(agent_session.agent_session_id), _bch_manager)
        logger.info(
            "BCH manager registered for session",
            session_id=str(agent_session.agent_session_id),
        )

        _session_perms = SessionPermissions.from_context(context)
        logger.info(
            f"Created session {agent_session.agent_session_id} for agent '{resolved_agent_id}'",
            session_id=str(agent_session.agent_session_id),
            agent_name=resolved_agent_id,
            trigger=request.trigger,
            workspace=workspace,
            allowed_servers=_session_perms.allowed_servers,
            allowed_harness_tools=_session_perms.allowed_harness_tools,
        )

    # Best-effort: notify all channels that a new session was created.
    _sid = agent_session.agent_session_id
    _link_parts: list[str] = []
    if os.environ.get("ENVIRONMENT") == "production":
        _link_parts.append(f"<https://war-room.yuppster.ai/session/{_sid}|WR>")
    if AHS_LIT_BASE_URL:
        _link_parts.append(f"<{AHS_LIT_BASE_URL}/agent_harness_console?session_id={_sid}|Lit>")
    _links = f" ({' | '.join(_link_parts)})" if _link_parts else ""
    session_notice = f"_Session {_sid}{_links}_"

    # WebSocket stream
    # TODO: this notice is effectively dropped for new sessions because WebSocket
    # clients can only subscribe after create_session() returns the UUID, and
    # InMemoryPubSub has no replay buffer. Low impact since clients already get
    # the session ID from the API response. Fix requires replay buffer or deferred
    # publish. (see PR #10756)
    try:
        pubsub = get_pubsub()
        stream_channel = f"ahs:stream:{agent_session.agent_session_id}"
        for evt in build_notice_events(session_notice):
            await pubsub.publish(stream_channel, json.dumps(evt))
    except RuntimeError:
        pass  # streaming may not be initialized (e.g., tests)

    # Gateway (Slack)
    gateway_name = TRIGGER_TO_GATEWAY.get(trigger.value)
    if gateway_name and request.session_id:
        agent_cfg = await _load_agent_config_with_db_fallback(resolved_agent_id)
        if agent_cfg:
            gw = GatewayRegistry.get_instance().get_for_session(gateway_name, agent_cfg)
            if gw:
                try:
                    await gw.send_reply(request.session_id, session_notice)
                except Exception:
                    logger.error(
                        "Failed to send session-created notice to gateway",
                        session_id=str(agent_session.agent_session_id),
                    )

    # If a message was provided, kick off the first turn.
    if request.message:
        if trigger == AgentSessionTrigger.AGENT:
            # Attachments are not yet supported for AGENT-triggered sessions —
            # the AGENT path bypasses send_message() which handles attachment downloads.
            # Fail fast rather than silently drop the files.
            if request.attachments:
                raise AHSValidationError(
                    "Attachments are not yet supported for AGENT-triggered sessions. "
                    "Send text-based content only until attachment handling is implemented."
                )

            # For AGENT-triggered sessions the initial turn is a FELLOW_AGENT message,
            # not a USER message.  We write it directly (bypassing send_message) to
            # preserve provenance (from_agent_id + agent_message_id_ref).
            #
            # Both _from_agent_uuid and _verify_msg_uuid were validated (and identity-
            # verified) in the AGENT identity resolution block above.  agent_message_id is
            # now unconditionally required, so both values are always non-None here — reuse
            # them directly rather than re-parsing the same context keys.
            _fellow_from_agent_id: uuid.UUID = _from_agent_uuid
            _fellow_agent_msg_ref: uuid.UUID = _verify_msg_uuid

            async with get_async_session() as _msg_db:
                _fellow_turn = await _next_turn_number(_msg_db, agent_session.agent_session_id)
                _fellow_msg = AgentSessionMessage(
                    agent_session_id=agent_session.agent_session_id,
                    turn_number=_fellow_turn,
                    role=AgentSessionMessageRole.FELLOW_AGENT,
                    content=request.message,
                    creator_user_id=user_id,
                    from_agent_id=_fellow_from_agent_id,
                    agent_message_id_ref=_fellow_agent_msg_ref,
                )
                _msg_db.add(_fellow_msg)
                await _msg_db.commit()

            logger.info(
                "AGENT trigger: injected FELLOW_AGENT turn",
                session_id=str(agent_session.agent_session_id),
                turn_number=_fellow_turn,
                from_agent_id=str(_fellow_from_agent_id),
                agent_message_id_ref=str(_fellow_agent_msg_ref),
            )

            # Fire agent task for this turn.
            # Guard: if task creation fails after message commit, mark the session as
            # COMPLETED (terminal) so it doesn't persist as a zombie with an unanswered
            # FELLOW_AGENT turn and no running responder.
            _active_tasks[agent_session.agent_session_id] = None  # type: ignore[assignment]
            try:
                _agent_task = create_background_task(
                    _run_agent_task(
                        agent_session_id=agent_session.agent_session_id,
                        turn_number=_fellow_turn,
                        message=request.message,
                        agent_config_name=resolved_agent_id,
                        workspace=agent_session.workspace,
                        llm_session_id=agent_session.llm_session_id,
                        extra_dirs=agent_session.extra_dirs or [],
                        slack_session_id=agent_session.slack_session_id,
                        is_slack=False,
                        is_task=False,
                        session_context=dict(agent_session.context) if agent_session.context else {},
                        trigger=agent_session.trigger.value,
                        session_created_at=agent_session.created_at,
                    )
                )
                _active_tasks[agent_session.agent_session_id] = _agent_task
            except Exception:
                logger.exception(
                    "AGENT trigger: background task creation failed after message commit "
                    "— marking session COMPLETED to prevent zombie state",
                    session_id=str(agent_session.agent_session_id),
                )
                _active_tasks.pop(agent_session.agent_session_id, None)
                async with get_async_session() as _fail_db:
                    await _mark_session_completed(_fail_db, agent_session.agent_session_id)
                    await _fail_db.commit()
        else:
            # Pass user IDs so send_message can re-check USE_MCP for the sender
            # (without these, it defaults to restricted and overrides permissions).
            msg_request = SessionMessageRequest(
                session_id=str(agent_session.agent_session_id),
                message=request.message,
                slack_user_id=context.get("slack_user_id"),
                user_id=context.get("user_id") or context.get("yupp_user_id") or request.user_id,
                attachments=request.attachments,
                source=request.source,
            )
            await send_message(msg_request)

    return SessionCreateResponse(
        session_id=str(agent_session.agent_session_id),
        status=agent_session.status.value,
    )


async def send_message(request: SessionMessageRequest) -> SessionMessageResponse:
    """Send a message to an existing session.

    Stores the user message and kicks off the agent in the background.
    Returns immediately with the turn number.
    """
    # Resolve user_id for this message before entering the DB session block.
    # Different users can send messages to the same session (e.g., Slack threads),
    # so each message tracks its own creator.
    msg_creator_user_id = request.user_id
    if not msg_creator_user_id and request.slack_user_id:
        try:
            msg_creator_user_id = await resolve_slack_user_to_yupp_user_id(request.slack_user_id)
        except Exception:
            logger.warning(
                "Failed to resolve slack_user_id for message creator",
                slack_user_id=request.slack_user_id,
            )

    if not msg_creator_user_id:
        logger.error(
            "Rejected message: missing user_id",
            source=request.source,
            session_id=request.session_id,
            request=request.model_dump(),
        )
        raise AHSValidationError("user_id is required to send a message")

    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {request.session_id}")

        # For non-Slack sessions, enforce that the message sender matches the session creator.
        # Fail closed: also reject if the session has no creator_user_id (legacy sessions).
        is_slack = agent_session.trigger == AgentSessionTrigger.SLACK
        if not is_slack:
            if not agent_session.creator_user_id:
                logger.error(
                    "Rejected message: legacy session has no creator_user_id",
                    source=request.source,
                    session_id=request.session_id,
                    sender_user_id=msg_creator_user_id,
                )
                raise AHSValidationError(
                    "Cannot send messages to a legacy session without a creator. Please create a new session."
                )
            if msg_creator_user_id != agent_session.creator_user_id:
                logger.error(
                    "Rejected message: sender does not match session creator",
                    source=request.source,
                    session_id=request.session_id,
                    sender_user_id=msg_creator_user_id,
                    session_creator_user_id=agent_session.creator_user_id,
                    request=request.model_dump(),
                )
                raise AHSValidationError("Message sender must match session creator for non-Slack sessions")

        # Lock the session row to serialize concurrent message sends
        await session.exec(
            select(AgentSession)
            .where(AgentSession.agent_session_id == agent_session.agent_session_id)
            .with_for_update()
        )

        # Queue message if a prior turn is still processing instead of rejecting.
        # The message will be drained after the current turn completes.
        # Exception: STALE sessions have an orphaned inflight turn from a now-dead
        # process.  There is no active task to drain _pending_messages for STALE
        # sessions, so we must NOT queue — fall through and start a fresh turn.
        is_stale = agent_session.status == AgentSessionStatus.STALE
        if not is_stale and await _has_inflight_turn(session, agent_session.agent_session_id):
            queue = _pending_messages.setdefault(agent_session.agent_session_id, [])
            if len(queue) >= _MAX_PENDING_MESSAGES:
                raise RuntimeError(f"Pending message queue full ({_MAX_PENDING_MESSAGES}). Try again later.")
            queue.append(
                PendingMessage(
                    message=request.message,
                    slack_ts=request.slack_ts,
                    slack_user_id=request.slack_user_id,
                    user_id=request.user_id,
                    attachments=request.attachments or [],
                    source=request.source,
                )
            )
            logger.info(
                "Message queued (agent busy)",
                session_id=str(agent_session.agent_session_id),
                queue_depth=len(queue),
            )
            return SessionMessageResponse(
                session_id=str(agent_session.agent_session_id),
                turn_number=-1,  # not yet assigned; will be set when drained
                status="queued",
            )

        agent = await session.get(Agent, agent_session.agent_id)
        if not agent:
            raise ValueError("Agent not found for session")

        agent_config = await _load_agent_config_with_db_fallback(agent.name)
        if not agent_config:
            raise ValueError(f"Agent config not found: {agent.name}")

        # Re-activate session if it is in a terminal-but-resumable state.
        # All sessions are now marked COMPLETED after each turn completes, so multi-turn
        # sessions (SLACK) receive follow-up messages with status=COMPLETED between turns.
        # Sessions marked STALE by the startup scan (interrupted mid-turn by SIGTERM) can
        # also receive follow-up messages — re-activate them too so status-based monitoring
        # shows ACTIVE during processing instead of STALE.
        if agent_session.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
            agent_session.status = AgentSessionStatus.ACTIVE

        # Store user message (turn_number is safe under FOR UPDATE lock)
        turn_number = await _next_turn_number(session, agent_session.agent_session_id)
        user_msg = AgentSessionMessage(
            agent_session_id=agent_session.agent_session_id,
            turn_number=turn_number,
            role=AgentSessionMessageRole.USER,
            content=request.message,
            slack_ts=request.slack_ts,
            creator_user_id=msg_creator_user_id,
        )
        session.add(user_msg)
        await session.commit()

        excerpt = (request.message or "")[:200]
        sid = str(agent_session.agent_session_id)[-6:]
        logger.info(
            f"session {sid} [USER]: {excerpt}",
            agent_name=agent.name,
            session_id=str(agent_session.agent_session_id),
            role="USER",
            turn_number=turn_number,
        )

    # Re-check MCP permission for the message sender (may differ from session creator).
    # For Slack sessions, different users can send follow-up messages in the same thread.
    session_context = dict(agent_session.context) if agent_session.context else {}
    is_slack = agent_session.trigger == AgentSessionTrigger.SLACK
    is_task = agent_session.trigger == AgentSessionTrigger.TASK
    if is_slack:
        # Use user_id from request (resolved by SAG) or fall back to session context.
        sender_user_id = (
            request.user_id or session_context.get("user_id") or session_context.get("yupp_user_id")  # backward compat
        )
        # Backward compat: reuse the resolution from earlier in this function
        # rather than making another Slack API + DB call.
        if not sender_user_id and request.slack_user_id:
            sender_user_id = msg_creator_user_id

        has_mcp_access = False
        if sender_user_id:
            try:
                has_mcp_access = await has_permission_by_user_id_cached(sender_user_id, SoulPermission.USE_MCP)
            except Exception:
                logger.error(
                    "Error checking USE_MCP for follow-up sender, disabling MCP",
                    user_id=sender_user_id,
                    session_id=str(agent_session.agent_session_id),
                    exc_info=True,
                )
        else:
            logger.warning(
                "Slack message missing user_id, disabling yuppster-mcp",
                slack_user_id=request.slack_user_id,
                session_id=str(agent_session.agent_session_id),
            )
        _msg_perms = SessionPermissions.full_access() if has_mcp_access else SessionPermissions.restricted()
        session_context["permissions"] = _msg_perms.model_dump(mode="json")

    # Track the current message sender in memory so MCP tools (e.g., create_pr)
    # can attribute actions to the user who asked for them.
    if msg_creator_user_id:
        set_session_current_user(str(agent_session.agent_session_id), msg_creator_user_id)
        # Also propagate to session_context so the yuppster-mcp-server can attribute
        # resources to the current turn sender (not just the session creator).
        session_context["current_turn_user_id"] = msg_creator_user_id

    # Persist updated session_context to DB so subagents (via new_task) inherit
    # the sender's MCP access rather than the stale session creator's value.
    if is_slack:
        try:
            async with get_async_session() as session:
                result = await session.exec(
                    select(AgentSession).where(AgentSession.agent_session_id == agent_session.agent_session_id)
                )
                db_session = result.one_or_none()
                if db_session:
                    db_session.context = session_context
                    await session.commit()
        except Exception:
            logger.error(
                "Failed to persist updated session_context",
                session_id=str(agent_session.agent_session_id),
                exc_info=True,
            )

    # Download attachments from GCS into the workspace and prepend paths to the
    # message so the agent knows which files are available via the `read` tool.
    agent_message = request.message
    if request.attachments and agent_session.workspace:
        attachment_paths = await _download_attachments_to_workspace(request.attachments, agent_session.workspace)
        agent_message = _prepend_attachment_paths(agent_message, attachment_paths)

        # Fire-and-forget: persist downloaded attachments to GCS immediately
        # so they survive pod restarts before the agent turn completes.
        async def _sync_attachments() -> None:
            try:
                await sync_session_to_gcs(str(agent_session.agent_session_id))
            except Exception:
                logger.warning(
                    "GCS session sync failed after attachment download",
                    session_id=str(agent_session.agent_session_id),
                    exc_info=True,
                )

        create_background_task(_sync_attachments())

    # Register a placeholder *before* creating the task so that stop_session()
    # cannot land in a gap where the inflight turn exists but no task is tracked.
    # A sentinel value (None) tells stop_session the task is being set up.
    _active_tasks[agent_session.agent_session_id] = None  # type: ignore[assignment]

    # Fire-and-forget: run agent in background.
    task = create_background_task(
        _run_agent_task(
            agent_session_id=agent_session.agent_session_id,
            turn_number=turn_number,
            message=agent_message,
            agent_config_name=agent.name,
            workspace=agent_session.workspace,
            llm_session_id=agent_session.llm_session_id,
            extra_dirs=agent_session.extra_dirs or [],
            slack_session_id=agent_session.slack_session_id,
            is_slack=is_slack,
            is_task=is_task,
            session_context=session_context,
            trigger=agent_session.trigger.value if agent_session.trigger else None,
            # Only pass session_created_at on turn 1: queue_wait_ms measures the
            # gap between session creation and CLI launch, which is only meaningful
            # for the first turn.  On subsequent turns, created_at is the session
            # age (minutes/hours), not queue delay, so we omit it to avoid skewing
            # latency metrics and alerting baselines.
            session_created_at=agent_session.created_at if turn_number == 1 else None,
        )
    )
    _active_tasks[agent_session.agent_session_id] = task

    return SessionMessageResponse(
        session_id=str(agent_session.agent_session_id),
        turn_number=turn_number,
        status="processing",
    )


async def attach_slack_to_session(request: SessionAttachSlackRequest) -> SessionAttachSlackResponse:
    """Attach Slack thread context to an existing headless AHS session.

    Called by SAG when a human replies to an agent-initiated thread.
    Updates the session's slack_session_id and merges Slack context so that
    callbacks (add_reply, etc.) route back to the correct Slack thread.
    """
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, request.session_id)
        if agent_session is None:
            raise ValueError(f"Session not found: {request.session_id}")

        agent_session.slack_session_id = request.slack_session_id

        # Merge Slack context into the session's existing context.
        # Copy the dict so SQLAlchemy detects the change on JSONB columns.
        if request.context:
            agent_session.context = {**(agent_session.context or {}), **request.context}

        session.add(agent_session)
        await session.commit()

        logger.info(
            "Attached Slack context to session",
            session_id=str(agent_session.agent_session_id),
            slack_session_id=request.slack_session_id,
        )

    return SessionAttachSlackResponse(
        session_id=str(agent_session.agent_session_id),
        status="attached",
    )


async def stop_session(session_id: str) -> SessionStopResponse:
    """Stop a running agent task for a session.

    Cancels the background task (which kills the CLI subprocess and any subagent
    processes), records a SYSTEM message noting the interruption, and marks
    the turn as complete so the session can accept new messages.

    Idempotent: stopping an already-idle session is a no-op.
    """
    # Phase 1: Check for inflight turn under lock, capture the turn number,
    # and snapshot the task reference atomically.
    agent_session_id: uuid.UUID | None = None
    inflight_turn: int | None = None
    async with get_async_session() as session:
        agent_session = await _resolve_session(session, session_id)
        if not agent_session:
            raise ValueError(f"Session not found: {session_id}")

        # Lock the session row to serialize with concurrent sends/stops
        await session.exec(
            select(AgentSession)
            .where(AgentSession.agent_session_id == agent_session.agent_session_id)
            .with_for_update()
        )

        # Always clear pending messages when the user explicitly stops — even if the
        # current turn already finished, the finally-block drain hasn't run yet and
        # would otherwise process them.
        dropped = _pending_messages.pop(agent_session.agent_session_id, [])
        if dropped:
            logger.info(
                "Discarded pending messages due to session stop",
                session_id=str(agent_session.agent_session_id),
                count=len(dropped),
            )

        # Check if there's an inflight turn to stop
        if not await _has_inflight_turn(session, agent_session.agent_session_id):
            return SessionStopResponse(
                session_id=str(agent_session.agent_session_id),
                status="no_inflight_turn",
            )

        # Capture the inflight turn number so Phase 3 writes [INTERRUPTED]
        # for this specific turn, not a newer one that might start later.
        # Check both USER and FELLOW_AGENT roles — A2A-triggered turns are inflight too.
        latest_user_turn_result = await session.exec(
            select(func.max(AgentSessionMessage.turn_number)).where(
                AgentSessionMessage.agent_session_id == agent_session.agent_session_id,
                col(AgentSessionMessage.role).in_([AgentSessionMessageRole.USER, AgentSessionMessageRole.FELLOW_AGENT]),
            )
        )
        inflight_turn = latest_user_turn_result.one() or 0

        agent_session_id = agent_session.agent_session_id

        # Snapshot the task while still under lock so a concurrent send_message
        # can't swap in a newer task between our check and our cancel.
        task = _active_tasks.get(agent_session_id)
    # Lock released — safe to wait on the task without risking deadlock.

    # Phase 2: Cancel subagent tasks and the main task outside the DB lock.
    from ypl.agent_harness_service.orchestration import cancel_subagent_tasks

    try:
        await cancel_subagent_tasks(agent_session_id)
    except Exception:
        logger.error(
            "Failed to cancel subagent tasks, proceeding with main task cancellation",
            session_id=str(agent_session_id),
            exc_info=True,
        )

    if task and not task.done():
        task.cancel()
        # Give the task a moment to clean up (proc.kill + await proc.wait)
        try:
            async with asyncio.timeout(5.0):
                await asyncio.shield(task)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass

    # Phase 3: Re-open a session to write the SYSTEM message for the
    # specific turn we observed in Phase 1. Re-check that this turn is
    # still inflight — if the task completed between Phase 1 and now,
    # it already wrote an AGENT/SYSTEM message and we should not duplicate.
    async with get_async_session() as session:
        # Check for a completed response: exclude both inbound roles (USER and
        # FELLOW_AGENT) — a FELLOW_AGENT message is the request, not the response.
        # Eager-persist drafts (IN_PROGRESS) are excluded so we don't mistake an
        # actively-running turn for one that finished naturally.
        response_count_result = await session.exec(
            select(func.count()).where(
                AgentSessionMessage.agent_session_id == agent_session_id,
                AgentSessionMessage.turn_number == inflight_turn,
                col(AgentSessionMessage.role).not_in(
                    [AgentSessionMessageRole.USER, AgentSessionMessageRole.FELLOW_AGENT]
                ),
                col(AgentSessionMessage.completion_status) != AgentSessionMessageCompletionStatus.IN_PROGRESS,
            )
        )
        if response_count_result.one() > 0:
            # Turn already completed naturally — no need to write [INTERRUPTED]
            logger.info(
                "Stop requested but turn already completed",
                session_id=str(agent_session_id),
                turn_number=inflight_turn,
            )
            return SessionStopResponse(
                session_id=str(agent_session_id),
                status="no_inflight_turn",
            )

        # Record a SYSTEM message to mark the turn as complete
        system_msg = AgentSessionMessage(
            agent_session_id=agent_session_id,
            turn_number=inflight_turn,
            role=AgentSessionMessageRole.SYSTEM,
            content="[INTERRUPTED] Session stopped by user.",
            completion_status=AgentSessionMessageCompletionStatus.ABORTED,
            error_type=AgentSessionMessageErrorType.NONE,
        )
        session.add(system_msg)
        # Transition session to COMPLETED so it doesn't linger as ACTIVE.
        # Without this, the session stays ACTIVE indefinitely until the auto-stale
        # sweep marks it STALE 6+ hours later (bab9adf8 pattern).
        await _mark_session_completed(session, agent_session_id)
        await session.commit()

    # Clean up session-specific state (auth terminal states, current user tracking,
    # polling tasks). Safe to call here since this is explicit session end.
    clear_session_state(str(agent_session_id))

    # Stop and deregister the BCH manager for this session.
    _bch_mgr = _command_handlers.pop(agent_session_id, None)
    if _bch_mgr is not None:
        set_command_handler_manager(str(agent_session_id), None)
        await _bch_mgr.stop()

    logger.info(
        "Session stopped by user",
        session_id=str(agent_session_id),
        turn_number=inflight_turn,
        had_task=task is not None,
    )

    return SessionStopResponse(
        session_id=str(agent_session_id),
        status="stopped",
    )
