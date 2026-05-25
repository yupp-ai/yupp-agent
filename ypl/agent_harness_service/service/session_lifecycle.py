"""Session lifecycle — create, message, stop, attach, and internal dispatch."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
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
    AHS_REPOS_DIR,
    AHS_SESSIONS_DIR,
    EXECUTOR_TYPE_RAW,
    HARNESS_CLAUDE_SDK,
    HARNESS_CODEX_APP_SERVER,
    HARNESS_CODEX_CLI,
    HARNESSED_MODELS,
)
from ypl.agent_harness_service.common.providers import KNOWN_MODELS
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
from ypl.agent_harness_service.executors.runner import (
    ClaudeCodeRunner,
    RunContext,
)
from ypl.agent_harness_service.gateway import TRIGGER_TO_GATEWAY, GatewayRegistry
from ypl.agent_harness_service.gateway.slack_prefetch import fetch_slack_thread_content
from ypl.agent_harness_service.memory_materialization import materialize_memory_for_session
from ypl.agent_harness_service.memory_store import MemoryCallerContext
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
    _SLACK_RESTART_COURTESY_MSG_IDLE,
    _SLACK_RESTART_COURTESY_MSG_INTERRUPTED,
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
from ypl.db.rbac import Permission
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# FELLOW_AGENT message wrapping
# ---------------------------------------------------------------------------


def _wrap_fellow_agent_content(sender_agent_name: str, raw_content: str) -> str:
    """Prepend a non-spoofable sender-identity marker to FELLOW_AGENT message bodies.

    AHS does not surface FELLOW_AGENT provenance (``from_agent_id``) in the
    receiver's prompt automatically — without an explicit wrap the receiver
    only sees ``raw_content`` and has to rely on the sender embedding its own
    identity inside the body, which is a fragile body-text convention that
    breaks for any sender that doesn't follow it.

    Wrapping at delivery time guarantees:
      • Every FELLOW_AGENT message starts with a consistent ``[External
        message from agent ...]`` marker, so receivers (and the prompts that
        instruct them — see ``deploy/shared/SOUL.md``) can detect inbound
        agent messages without parsing the body.
      • The marker is harness-injected, so a malicious or buggy sender
        cannot suppress it or impersonate a different agent in the marker.
      • The original ``raw_content`` is preserved verbatim for the receiver
        to act on (or relay to Slack).

    Used by both the inbox-drain path (``_drain_session_inbox``) and the
    initial AGENT-trigger turn injection in ``create_session``.
    """
    return f"[External message from agent `{sender_agent_name}`]\n\n{raw_content}"


# ---------------------------------------------------------------------------
# Slack courtesy helpers
# ---------------------------------------------------------------------------


# Window for the shutdown-side broadcast.  Sessions that haven't been touched
# in this many hours are excluded from the "server is restarting" notice — we
# don't want to wake up Slack threads that the user has long since abandoned.
_COURTESY_BROADCAST_WINDOW_HOURS = 24


def _record_courtesy_metric(event: str, outcome: str, count: int = 1) -> None:
    """Emit ``ahs/courtesy_broadcast`` Prometheus / structured-log counter.

    ``event`` is ``"shutdown"``, ``"restart"``, or ``"auto_resume"``;
    ``outcome`` is one of ``"sent"`` / ``"failed"`` / ``"skipped"`` for
    courtesy broadcasts, or ``"dispatched"`` / ``"skipped"`` / ``"failed"``
    for the auto-resume hook fired from ``dispatch_resume_turn``.

    Wrapped in a try/except because the metrics layer reaches out to GCP
    and we never want a metric write to break a courtesy broadcast — the
    structured log already carries the same information.
    """
    try:
        from ypl.backend.utils.monitoring import metric_inc_by_with_labels

        metric_inc_by_with_labels(
            "ahs/courtesy_broadcast",
            count,
            {"event": event, "outcome": outcome},
        )
    except Exception:  # pragma: no cover — monitoring failure must not propagate
        logger.debug("metric_inc failed for ahs/courtesy_broadcast", exc_info=True)


def _is_monolith_mode() -> bool:
    """Return True iff the current process is running as the AHS+SAG monolith.

    Imported lazily so the AHS package does not require ``ypl.mono_server`` to
    be importable in standalone-AHS deployments (and to avoid an import cycle:
    mono_server imports AHS).
    """
    try:
        from ypl.mono_server.runtime import is_monolith_mode

        return is_monolith_mode()
    except Exception:
        return False


async def _deliver_courtesy_via_callback(slack_session_id: str, text: str) -> bool:
    """Post a courtesy reply to Slack using the in-process SAG callback.

    Used in monolith mode so the message reaches Slack even when the uvicorn
    HTTP listener is closed (during shutdown ``__aexit__``) or not yet open
    (during startup ``__aenter__``) — the two windows when the courtesy
    helpers run and an HTTP loopback would silently fail.
    """
    try:
        from ypl.slack_agent_gateway.callbacks import add_reply
        from ypl.slack_agent_gateway.types import AddReplyRequest

        response = await add_reply(AddReplyRequest(session_id=slack_session_id, text=text))
        if not response.success:
            logger.warning(
                "In-process SAG add_reply rejected courtesy",
                slack_session_id=slack_session_id,
                error=response.error,
            )
            return False
        return True
    except Exception:
        logger.warning(
            "In-process SAG add_reply raised — courtesy not delivered",
            slack_session_id=slack_session_id,
            exc_info=True,
        )
        return False


async def _send_slack_courtesy(session_ids: list[uuid.UUID], text: str, event: str) -> None:
    """Send a courtesy message to the Slack threads for the given session IDs.

    Resolves each ``agent_session_id`` to its ``slack_session_id`` and posts
    the message.  In monolith mode the SAG callback is invoked in-process so
    delivery works during shutdown (after uvicorn closes the listener) and
    during startup (before uvicorn opens the listener).  In standalone mode
    we fall back to the registered HTTP gateway.

    Errors from individual sends are logged but do not propagate — courtesy
    messages are best-effort.
    """
    if not session_ids:
        return

    monolith = _is_monolith_mode()
    gateway = None
    if not monolith:
        registry = GatewayRegistry.get_instance()
        gateway = registry.get("slack")
        if gateway is None:
            logger.warning("Slack gateway unavailable — skipping courtesy messages", courtesy_event=event)
            _record_courtesy_metric(event, "skipped", count=len(session_ids))
            return

    async with get_async_session() as db_session:
        result = await db_session.exec(
            select(AgentSession)
            .where(col(AgentSession.agent_session_id).in_(session_ids))
            .where(col(AgentSession.slack_session_id).is_not(None))
        )
        slack_sessions = result.all()

    if not slack_sessions:
        logger.info("No in-scope Slack sessions for courtesy message", courtesy_event=event)
        return

    logger.info(
        "Sending courtesy messages to Slack sessions",
        courtesy_event=event,
        count=len(slack_sessions),
        delivery="in_process" if monolith else "http_gateway",
    )

    async def _deliver(slack_session_id: str) -> bool:
        if monolith:
            return await _deliver_courtesy_via_callback(slack_session_id, text)
        # gateway is non-None on this branch — guaranteed by the early return above.
        assert gateway is not None
        return await gateway.send_reply(slack_session_id, text)

    results = await asyncio.gather(
        *[_deliver(s.slack_session_id) for s in slack_sessions if s.slack_session_id],
        return_exceptions=True,
    )

    sent = sum(1 for r in results if r is True)
    failed = len(results) - sent
    logger.info(
        "Slack courtesy messages complete",
        courtesy_event=event,
        count=len(results),
        sent=sent,
        failed=failed,
        delivery="in_process" if monolith else "http_gateway",
    )
    if sent:
        _record_courtesy_metric(event, "sent", count=sent)
    if failed:
        _record_courtesy_metric(event, "failed", count=failed)


async def _query_active_slack_session_ids(window_hours: int) -> list[uuid.UUID]:
    """Return every top-level ACTIVE Slack session modified within ``window_hours``.

    Used as the audience for the shutdown-side courtesy broadcast: a normal
    Slack thread spends >90% of its wall-clock idle between turns, so the old
    ``_active_tasks.keys()`` audience covered almost no real users.  We cap to
    the recent-activity window so abandoned threads aren't woken on every
    deploy.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    async with get_async_session() as db_session:
        result = await db_session.exec(
            select(AgentSession.agent_session_id)
            .where(AgentSession.status == AgentSessionStatus.ACTIVE)
            .where(col(AgentSession.slack_session_id).is_not(None))
            .where(col(AgentSession.parent_session_id).is_(None))
            .where(col(AgentSession.modified_at) > cutoff)
        )
        return list(result.all())


async def send_slack_shutdown_courtesy() -> None:
    """Send a courtesy message to every live Slack thread before shutdown.

    Called during graceful shutdown (SIGTERM) so users know the server is
    restarting and their session will be available again shortly.

    The audience is now driven by a DB query (every top-level ``ACTIVE`` Slack
    session modified in the last
    :data:`_COURTESY_BROADCAST_WINDOW_HOURS` hours) instead of
    ``_active_tasks.keys()``.  ``_active_tasks`` only contains sessions that
    are *currently mid-turn*, which excludes the >90% of Slack threads that
    are idle between turns when SIGTERM lands — exactly the users we most
    need to notify.
    """
    session_ids = await _query_active_slack_session_ids(_COURTESY_BROADCAST_WINDOW_HOURS)
    if not session_ids:
        logger.info("No live Slack sessions to notify on shutdown", courtesy_event="shutdown")
        return
    await _send_slack_courtesy(session_ids, _SLACK_SHUTDOWN_COURTESY_MSG, "shutdown")


async def send_slack_restart_courtesy(
    slack_session_ids: list[uuid.UUID],
    *,
    interrupted: bool = False,
) -> None:
    """Send a courtesy message to Slack sessions after the server restarts.

    Called from ``_recover_stale_sessions`` at startup, once per bucket:

    * ``interrupted=True`` — the previous turn was killed mid-flight and a
      synthetic resume turn is about to fire via ``dispatch_resume_turn``.
      The wording reflects "picking up where we left off".
    * ``interrupted=False`` (default) — the previous turn finished cleanly
      before the restart.  The wording is a bare informational notice and
      no follow-up turn is dispatched.

    Both wordings deliberately drop the "send me a message" ask: PR 3 made
    the auto-resume path real, so the user does not need to take any action.
    """
    text = _SLACK_RESTART_COURTESY_MSG_INTERRUPTED if interrupted else _SLACK_RESTART_COURTESY_MSG_IDLE
    await _send_slack_courtesy(
        slack_session_ids,
        text,
        "restart",
    )


# ---------------------------------------------------------------------------
# Auto-resume preamble — synthetic context the LLM sees when a user message
# arrives on a session whose previous turn was killed by a server restart.
#
# Backed by two short-lived Redis keys:
#
#   ahs:executor_running:{session_id}    SET when ``_run_agent_task`` enters,
#                                        DEL in its ``finally``.  TTL=30min
#                                        self-heals if a leak ever happens.
#                                        On AHS startup any key still present
#                                        is — by definition — a session whose
#                                        executor was killed mid-flight.
#
#   ahs:resume_pending:{session_id}      SET on startup for every leftover
#                                        executor_running key (effectively
#                                        ``RENAME``).  GETDEL'd by the next
#                                        ``send_message``; if non-empty, the
#                                        preamble built by
#                                        ``build_resume_context`` is prepended
#                                        to the runner input.  TTL=30d so
#                                        sessions the user never returns to
#                                        clear themselves up.
#
# The choice of Redis (vs. a persistent ``agent_sessions`` column) is
# deliberate: the flag's useful lifetime is "between AHS restart and the
# user's next ping", which is exactly what an ephemeral key fits.  A Redis
# crash between AHS startup and the next user message would lose the flag —
# tolerable on a rare-rare double event, and avoided ones-of-a-kind schema
# bloat on the busy ``agent_sessions`` table.
# ---------------------------------------------------------------------------


# How many characters of the prior user request and prior AGENT draft to keep
# in the preamble.  The LLM already has full conversation history via
# ``--resume``; the preamble is a hint, not a replacement for context.  We
# cap aggressively so a long stream-of-consciousness draft doesn't push the
# real user message out of the model's attention window.
_RESUME_PREAMBLE_USER_EXCERPT_CHARS = 280
_RESUME_PREAMBLE_DRAFT_EXCERPT_CHARS = 500


# Redis key prefixes — must stay in sync with ``promote_executor_running_to_resume_pending``.
_EXECUTOR_RUNNING_KEY_PREFIX = "ahs:executor_running:"
_RESUME_PENDING_KEY_PREFIX = "ahs:resume_pending:"

# Self-heal TTL for the in-flight executor key.  A turn that legitimately
# runs longer than this is almost certainly stuck (the per-turn timeout in
# every agent config we ship is well below it), so letting the key auto-expire
# avoids a stale preamble being delivered after a missed DEL on shutdown.
_EXECUTOR_RUNNING_TTL_SECONDS = 30 * 60  # 30 minutes

# How long a "your previous turn was interrupted" flag waits for the user to
# return.  30 days lets a Slack thread the user revisits the next morning (or
# the next workweek) still get the preamble; any longer and the context is
# stale enough that the message-history excerpt is no longer useful anyway.
_RESUME_PENDING_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _executor_running_key(session_id: uuid.UUID) -> str:
    return f"{_EXECUTOR_RUNNING_KEY_PREFIX}{session_id}"


def _resume_pending_key(session_id: uuid.UUID) -> str:
    return f"{_RESUME_PENDING_KEY_PREFIX}{session_id}"


async def mark_executor_running(session_id: uuid.UUID, turn_number: int) -> None:
    """Mark this session's executor as in-flight in Redis.

    Called from ``_run_agent_task`` immediately before the runner loop starts.
    The key carries the ``turn_number`` as its value so an operator running
    ``redis-cli SCAN`` during an incident can see *which* turn was killed
    without cross-referencing the DB.

    Failures are swallowed — Redis being temporarily unreachable must never
    take down an agent turn that would otherwise succeed.  The cost of a
    swallowed SET is "no resume preamble next time" for this one session,
    which is the same outcome as the user's old behaviour.
    """
    try:
        client = await get_redis_client()
        await client.set(
            _executor_running_key(session_id),
            str(turn_number),
            ex=_EXECUTOR_RUNNING_TTL_SECONDS,
        )
    except Exception:
        logger.warning(
            "mark_executor_running: Redis SET failed — resume preamble disabled for this turn",
            session_id=str(session_id),
            turn_number=turn_number,
            exc_info=True,
        )


async def mark_executor_finished(session_id: uuid.UUID) -> None:
    """Clear the in-flight executor marker for this session.

    Called from ``_run_agent_task``'s ``finally`` block on every exit path
    (success, failure, cancellation).  Failures are swallowed; if the DEL is
    lost, the key auto-expires after ``_EXECUTOR_RUNNING_TTL_SECONDS`` and
    the next AHS startup either misses it (acceptable) or treats it as a
    crash (acceptable false positive — the next user message just gets a
    spurious "previous turn was interrupted" preamble that the LLM can
    ignore).
    """
    try:
        client = await get_redis_client()
        await client.delete(_executor_running_key(session_id))
    except Exception:
        logger.warning(
            "mark_executor_finished: Redis DEL failed — relying on TTL self-heal",
            session_id=str(session_id),
            exc_info=True,
        )


async def list_resume_pending_session_ids() -> set[uuid.UUID]:
    """Return every session ID that currently has an ``ahs:resume_pending:*`` key.

    Used at startup by ``_recover_stale_sessions`` to partition Slack sessions
    into the "interrupted" bucket (got auto-continued via ``dispatch_resume_turn``
    and a "picking up where we left off" courtesy) versus the "idle" bucket
    (purely informational "back online" notice, no follow-up turn).

    Best-effort: returns an empty set on any Redis error — the caller falls
    through to treating every session as idle, which is the same behaviour
    the user already sees today.
    """
    out: set[uuid.UUID] = set()
    try:
        client = await get_redis_client()
        cursor: int = 0
        while True:
            cursor, keys = await client.scan(
                cursor=cursor,
                match=f"{_RESUME_PENDING_KEY_PREFIX}*",
                count=200,
            )
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                sid_str = key[len(_RESUME_PENDING_KEY_PREFIX) :]
                try:
                    out.add(uuid.UUID(sid_str))
                except ValueError:
                    logger.warning(
                        "list_resume_pending_session_ids: skipping malformed key",
                        key=key,
                    )
                    continue
            if cursor == 0:
                break
    except Exception:
        logger.warning(
            "list_resume_pending_session_ids: Redis SCAN failed — partition will fall through to idle bucket",
            exc_info=True,
        )
    return out


async def consume_resume_pending(session_id: uuid.UUID) -> bool:
    """Atomically check-and-clear the "previous turn interrupted" flag.

    Uses ``GETDEL`` so two concurrent ``send_message`` calls for the same
    session can never both observe the flag set — the one that wins the GETDEL
    sees it, the other does not, and the preamble fires exactly once.  This
    replaces the ``FOR UPDATE`` race protection the DB-column version of this
    code needed.

    Returns ``True`` iff a flag was present.  Returns ``False`` on any Redis
    error (we'd rather skip the preamble than fail the user message).
    """
    try:
        client = await get_redis_client()
        value = await client.getdel(_resume_pending_key(session_id))
        return value is not None
    except Exception:
        logger.warning(
            "consume_resume_pending: Redis GETDEL failed — skipping preamble",
            session_id=str(session_id),
            exc_info=True,
        )
        return False


async def promote_executor_running_to_resume_pending() -> int:
    """At AHS startup, convert leftover executor_running keys → resume_pending keys.

    Every ``ahs:executor_running:*`` key that survives an AHS restart is, by
    definition, a session whose runner subprocess was killed before the
    matching ``finally`` block ran.  We "rename" each one to the
    ``ahs:resume_pending:*`` namespace so the next inbound user message picks
    up a preamble.

    Done via SCAN + per-key SET/DEL rather than ``RENAME`` so we can attach a
    fresh TTL (``RENAME`` would inherit the 30-min self-heal TTL, which is
    way too short for a flag that needs to wait for the user to return).

    Returns the number of sessions promoted, for the startup log line.  Any
    Redis-side error is swallowed and logged: a failure here means a user
    misses one preamble, which we'd rather do than block startup.
    """
    promoted = 0
    try:
        client = await get_redis_client()
        cursor: int = 0
        while True:
            cursor, keys = await client.scan(
                cursor=cursor,
                match=f"{_EXECUTOR_RUNNING_KEY_PREFIX}*",
                count=200,
            )
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                session_id_str = key[len(_EXECUTOR_RUNNING_KEY_PREFIX) :]
                # Defensive: skip malformed session IDs rather than crash the
                # whole sweep — a stray key from a typo shouldn't take out the
                # rest of the recovery.
                try:
                    sid = uuid.UUID(session_id_str)
                except ValueError:
                    logger.warning(
                        "promote_executor_running_to_resume_pending: skipping malformed key",
                        key=key,
                    )
                    continue
                # SET first then DEL so a failure between the two leaves the
                # session in the "needs preamble" state (safe) rather than
                # losing the signal entirely.
                await client.set(
                    _resume_pending_key(sid),
                    "1",
                    ex=_RESUME_PENDING_TTL_SECONDS,
                )
                await client.delete(key)
                promoted += 1
            if cursor == 0:
                break
    except Exception:
        logger.warning(
            "promote_executor_running_to_resume_pending: Redis sweep failed — some sessions may miss the preamble",
            promoted_before_error=promoted,
            exc_info=True,
        )

    if promoted:
        logger.info(
            "Promoted leftover executor_running keys to resume_pending",
            count=promoted,
        )
    return promoted


def _truncate_for_preamble(text: str | None, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars, appending an ellipsis if truncated.

    Returns ``""`` for ``None`` / empty inputs.  Single-line representation —
    newlines are collapsed to spaces because the preamble lives inside a
    bracketed system note where embedded line breaks would look ragged.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


async def build_resume_context(session_id: uuid.UUID) -> str:
    """Build the preamble shown to the LLM when a user resumes an interrupted session.

    Reads the most recent USER turn and the most recent AGENT message
    (whether it's an IN_PROGRESS draft from the killed turn or a successful
    response that completed before the interruption) and returns a single
    string suitable for prepending to the next user message.

    Returns ``""`` if there is no useful context to relay (e.g. the session
    has no messages at all, or DB lookups fail) — callers should fall through
    to the normal request path in that case rather than emitting an empty
    "[SYSTEM] previous turn was interrupted by..." note with no detail.
    """
    try:
        async with get_async_session() as db_session:
            # Latest USER turn — the request the agent was working on when
            # the previous instance died.
            user_msg_result = await db_session.exec(
                select(AgentSessionMessage)
                .where(AgentSessionMessage.agent_session_id == session_id)
                .where(col(AgentSessionMessage.role) == AgentSessionMessageRole.USER)
                .order_by(
                    col(AgentSessionMessage.turn_number).desc(),
                    col(AgentSessionMessage.created_at).desc(),
                )
                .limit(1)
            )
            last_user_msg = user_msg_result.one_or_none()

            # Latest AGENT message — typically the IN_PROGRESS draft written
            # by the eager-persist path before the interruption, but we also
            # accept a SUCCESS draft (a turn that completed before the
            # restart but whose status flag never landed) so the preamble has
            # *some* anchor text in either path.
            agent_msg_result = await db_session.exec(
                select(AgentSessionMessage)
                .where(AgentSessionMessage.agent_session_id == session_id)
                .where(col(AgentSessionMessage.role) == AgentSessionMessageRole.AGENT)
                .order_by(
                    col(AgentSessionMessage.turn_number).desc(),
                    col(AgentSessionMessage.created_at).desc(),
                )
                .limit(1)
            )
            last_agent_msg = agent_msg_result.one_or_none()
    except Exception:
        logger.warning(
            "build_resume_context: DB lookup failed — emitting empty preamble",
            session_id=str(session_id),
            exc_info=True,
        )
        return ""

    user_excerpt = _truncate_for_preamble(
        last_user_msg.content if last_user_msg else None,
        _RESUME_PREAMBLE_USER_EXCERPT_CHARS,
    )
    draft_excerpt = ""
    if last_agent_msg is not None:
        # Distinguish the genuinely-interrupted IN_PROGRESS draft (the common
        # case) from a SUCCESS reply that just hadn't been committed before
        # the restart — the wording we produce for each is different.
        is_draft = last_agent_msg.completion_status == AgentSessionMessageCompletionStatus.IN_PROGRESS
        draft_excerpt = _truncate_for_preamble(
            last_agent_msg.content,
            _RESUME_PREAMBLE_DRAFT_EXCERPT_CHARS,
        )
    else:
        is_draft = False

    if not user_excerpt and not draft_excerpt:
        # Nothing concrete to include — caller falls through to plain message.
        return ""

    parts: list[str] = [
        "[SYSTEM] The previous turn on this session was interrupted by an "
        "AHS server restart before you could finish responding.",
    ]
    if user_excerpt:
        parts.append(f'You were responding to the user request: "{user_excerpt}".')
    if draft_excerpt:
        if is_draft:
            parts.append(f'Your partial response so far was: "{draft_excerpt}".')
        else:
            parts.append(f'Your last completed response was: "{draft_excerpt}".')
    parts.append(
        "The user has now sent a follow-up message (below). "
        "Either continue from where you left off or ask the user for "
        "clarification if you need it. The user's new message follows."
    )
    return " ".join(parts) + "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Auto-resume dispatch — fire a synthetic continuation turn at startup so the
# user does not have to send a message just to nudge the agent back to life.
# ---------------------------------------------------------------------------


# Marker stored on the synthetic USER row written by ``dispatch_resume_turn``.
# Carried in ``raw_events`` so the Lit console / Couch frontends can render it
# differently from a user-typed message and ops can grep / filter on it.
_AUTO_RESUME_USER_CONTENT = "[AUTO-RESUME] AHS server restart — auto-continuing."


async def dispatch_resume_turn(session_id: uuid.UUID) -> None:
    """Auto-fire a synthetic continuation turn for an interrupted session.

    Called at startup from ``_recover_stale_sessions`` for every Slack session
    whose ``ahs:resume_pending:*`` Redis flag was set by
    ``promote_executor_running_to_resume_pending``.

    Behaviour:

    1. Resolve the session + agent from the DB (and bail with ``skipped`` if
       either is missing — the courtesy already fired so degrading to a no-op
       is fine).
    2. Build the auto-resume preamble (the same ``build_resume_context``
       call that ``send_message`` uses).  If the preamble is empty we have
       nothing useful to say — bail with ``skipped``; the Redis flag is left
       intact so the user's eventual reply still picks up the (admittedly
       empty) preamble path.
    3. Atomically claim the Redis flag with ``consume_resume_pending``.  If
       the GETDEL races with a real user message that already drained the
       flag, bail with ``skipped`` — that path will run the resume preamble
       on the user's behalf.
    4. Inside a single DB transaction (``FOR UPDATE`` on the session row to
       serialise with concurrent ``send_message``):

       * Re-activate the session if it is in a terminal-but-resumable state.
       * Bump ``turn_number``.
       * Insert a synthetic ``USER`` row whose content is a clear
         ``[AUTO-RESUME]`` marker and whose ``raw_events`` payload carries
         ``is_system_continuation=True``.  The row exists *only* so the
         queue/dedupe path (``_has_inflight_turn``) sees a real inbound
         turn and routes any racing user message into ``_pending_messages``
         instead of starting a parallel turn — its ``content`` is **never**
         the runner input.

    5. Register a sentinel in ``_active_tasks`` and fire ``_run_agent_task``
       as a background task.  The runner input is the preamble (no
       ``[AUTO-RESUME]`` marker text) so the LLM sees the same shape it
       would see on a normal ``send_message`` resume path.

    Failure handling: any exception after we have committed to the run is
    logged and emits ``ahs/courtesy_broadcast{event=auto_resume,outcome=failed}``.
    The Redis flag is consumed only when we have everything else lined up,
    so transient DB failures earlier in the prep phase still leave the
    safety-net "preamble fires on the next user message" behaviour intact.
    """
    # Local import: ``_run_agent_task`` is already imported at module load,
    # but ``create_session`` etc. expect ``session_lifecycle`` to be fully
    # initialised before they pull in run_task — keep the symbol available
    # via the existing module-level import (no change here).

    # 1. Resolve session metadata.
    try:
        async with get_async_session() as db_session:
            agent_session = await db_session.get(AgentSession, session_id)
            if agent_session is None:
                logger.warning(
                    "dispatch_resume_turn: session not found — skipping",
                    session_id=str(session_id),
                )
                _record_courtesy_metric("auto_resume", "skipped", count=1)
                return
            agent_obj = await db_session.get(Agent, agent_session.agent_id)
            if agent_obj is None:
                logger.warning(
                    "dispatch_resume_turn: agent missing for session — skipping",
                    session_id=str(session_id),
                    agent_id=str(agent_session.agent_id),
                )
                _record_courtesy_metric("auto_resume", "skipped", count=1)
                return
            # Snapshot the fields we'll need outside the transaction.
            agent_name = agent_obj.name
            workspace = agent_session.workspace
            llm_session_id = agent_session.llm_session_id
            extra_dirs = list(agent_session.extra_dirs or [])
            slack_session_id = agent_session.slack_session_id
            session_context = dict(agent_session.context) if agent_session.context else {}
            trigger_val = agent_session.trigger.value if agent_session.trigger else None
            creator_user_id = agent_session.creator_user_id
            is_slack = agent_session.trigger == AgentSessionTrigger.SLACK
            is_task = agent_session.trigger == AgentSessionTrigger.TASK
    except Exception:
        logger.error(
            "dispatch_resume_turn: failed to load session metadata",
            session_id=str(session_id),
            exc_info=True,
        )
        _record_courtesy_metric("auto_resume", "failed", count=1)
        return

    # 2. Build the preamble before claiming the Redis flag — if the session
    # has nothing useful to anchor on, the auto-resume turn would just be an
    # awkward "[SYSTEM] previous turn was interrupted." with no detail; let
    # the next user message handle it instead.
    try:
        preamble = await build_resume_context(session_id)
    except Exception:
        logger.warning(
            "dispatch_resume_turn: build_resume_context raised — skipping",
            session_id=str(session_id),
            exc_info=True,
        )
        _record_courtesy_metric("auto_resume", "skipped", count=1)
        return

    if not preamble:
        logger.info(
            "dispatch_resume_turn: empty preamble — skipping",
            session_id=str(session_id),
        )
        _record_courtesy_metric("auto_resume", "skipped", count=1)
        return

    # 3. Atomic claim — if a racing send_message already cleared the flag,
    # bail without firing a parallel turn; the user-message path will run the
    # preamble itself.
    if not await consume_resume_pending(session_id):
        logger.info(
            "dispatch_resume_turn: resume_pending flag already cleared — skipping (race with user message)",
            session_id=str(session_id),
        )
        _record_courtesy_metric("auto_resume", "skipped", count=1)
        return

    # 4. Persist the synthetic USER row + status flip in a single committed
    # transaction so a racing send_message either (a) sees the row and queues,
    # or (b) finds the session genuinely idle.  We hold ``FOR UPDATE`` to
    # serialise with concurrent ``send_message`` calls on the same session.
    try:
        async with get_async_session() as db_session:
            await db_session.exec(
                select(AgentSession).where(AgentSession.agent_session_id == session_id).with_for_update()
            )

            # Re-activate the session if it's in a terminal-but-resumable
            # state so status-based monitoring shows ACTIVE during the turn
            # rather than STALE/COMPLETED.
            agent_session_for_update = await db_session.get(AgentSession, session_id)
            if agent_session_for_update is None:
                logger.warning(
                    "dispatch_resume_turn: session disappeared between metadata load and FOR UPDATE",
                    session_id=str(session_id),
                )
                _record_courtesy_metric("auto_resume", "failed", count=1)
                return
            if agent_session_for_update.status in (AgentSessionStatus.COMPLETED, AgentSessionStatus.STALE):
                agent_session_for_update.status = AgentSessionStatus.ACTIVE

            turn_number = await _next_turn_number(db_session, session_id)
            synthetic_user_msg = AgentSessionMessage(
                agent_session_id=session_id,
                turn_number=turn_number,
                role=AgentSessionMessageRole.USER,
                content=_AUTO_RESUME_USER_CONTENT,
                creator_user_id=creator_user_id,
                # Marked so the Couch frontend can render this as
                # a system event rather than a real user utterance.
                raw_events=[
                    {
                        "type": "auto_resume",
                        "is_system_continuation": True,
                    }
                ],
            )
            db_session.add(synthetic_user_msg)
            await db_session.commit()
    except Exception:
        logger.error(
            "dispatch_resume_turn: failed to persist synthetic USER row",
            session_id=str(session_id),
            exc_info=True,
        )
        _record_courtesy_metric("auto_resume", "failed", count=1)
        return

    # 5. Fire the runner.  Identity is inherited from the session creator;
    # we deliberately do not look up `slack_user_id` from any in-flight
    # request payload because the synthetic turn is system-initiated, not
    # user-initiated, and must not escalate beyond the session's existing
    # permission baseline.
    try:
        # Sentinel so a concurrent ``stop_session`` cannot land in a gap
        # where the inflight turn exists but no task is tracked.
        _active_tasks[session_id] = None  # type: ignore[assignment]
        task = create_background_task(
            _run_agent_task(
                agent_session_id=session_id,
                turn_number=turn_number,
                message=preamble,
                agent_config_name=agent_name,
                workspace=workspace,
                llm_session_id=llm_session_id,
                extra_dirs=extra_dirs,
                slack_session_id=slack_session_id,
                is_slack=is_slack,
                is_task=is_task,
                session_context=session_context,
                trigger=trigger_val,
            )
        )
        _active_tasks[session_id] = task
    except Exception:
        # Pop the sentinel so a later send_message isn't blocked thinking a
        # ghost task is in-flight.
        _active_tasks.pop(session_id, None)
        logger.error(
            "dispatch_resume_turn: failed to fire _run_agent_task",
            session_id=str(session_id),
            exc_info=True,
        )
        _record_courtesy_metric("auto_resume", "failed", count=1)
        return

    logger.info(
        "Auto-resume turn dispatched after AHS restart",
        session_id=str(session_id),
        turn_number=turn_number,
        agent_name=agent_name,
        preamble_chars=len(preamble),
    )
    _record_courtesy_metric("auto_resume", "dispatched", count=1)


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
                        SET status        = 'DELIVERING',
                            claimed_at    = now(),
                            attempt_count = attempt_count + 1
                        WHERE agent_message_id = :id
                          AND status = 'QUEUED'
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
                            SET status = 'QUEUED', claimed_at = NULL
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

                # Resolve the sending agent's name so we can prepend a non-spoofable
                # sender-identity marker to the FELLOW_AGENT body.  Failure to look up
                # the sender is non-fatal — we fall back to a generic "another agent"
                # marker rather than dropping the message, since the AgentMessage row
                # already encodes provenance and the receiver still needs to act.
                from_agent_obj = await msg_db.get(Agent, uuid.UUID(str(row["from_agent_id"])))
                from_agent_name = from_agent_obj.name if from_agent_obj else "unknown"
                wrapped_content = _wrap_fellow_agent_content(from_agent_name, row["content"])

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
                    # but the AgentMessage row retains provenance.  We still inject
                    # the sender-identity marker so the receiver can detect that this
                    # USER-role message originated from another agent.
                    _pending_messages.setdefault(session_id, []).append(
                        PendingMessage(
                            message=wrapped_content,
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
                    # ``wrapped_content`` carries the sender-identity marker so the
                    # receiver's prompt makes the inbound A2A origin unambiguous
                    # without relying on the sender embedding it in the body.
                    content=wrapped_content,
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
                        message=wrapped_content,
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
                                               WHEN attempt_count >= max_attempts THEN 'FAILED'
                                               ELSE 'QUEUED'
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
            # Cache the agent name into a local while the DB session is still open —
            # used downstream (after the ``async with`` block exits) to wrap the
            # initial FELLOW_AGENT message body with a non-spoofable sender marker.
            _from_agent_name: str = _from_agent_obj.name

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
                has_mcp_access = await has_permission_by_user_id_cached(user_id, Permission.USE_MCP)
                if not has_mcp_access:
                    logger.info(
                        "User lacks USE_MCP permission, agcouch-mcp tools will be disabled",
                        user_id=user_id,
                    )
            except Exception as e:
                has_mcp_access = False
                logger.warning(
                    "Error checking USE_MCP permission, disabling agcouch-mcp",
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
        # Uses yupp-agent's .claude/ which has settings.json and hooks.
        claude_link = os.path.join(workspace, ".claude")
        claude_target = os.path.join(AHS_REPOS_DIR, "yupp-agent", ".claude")
        try:
            os.symlink(claude_target, claude_link)
        except FileExistsError:
            pass

        # Symlink AHS_REPOS_DIR as a single ``repos/`` directory in the
        # workspace. Repos are shared across all sessions, so every workspace
        # sees the same set — including any repos a session checks out or
        # clones into ``repos/`` itself. (The bwrap sandbox still mounts the
        # shared dir read-only; agents that need write access to a repo call
        # ``request_write_access`` to create a per-session worktree.)
        if os.path.isdir(AHS_REPOS_DIR):
            repos_link = os.path.join(workspace, "repos")
            try:
                os.symlink(AHS_REPOS_DIR, repos_link)
            except FileExistsError:
                pass

        # Create history/ subdir for session history persistence
        os.makedirs(os.path.join(workspace, "history"), exist_ok=True)

        # Provide ``agent_memories/`` in the workspace by materializing every
        # MEMORY artifact visible to (user, agent) from the DB into a fresh
        # subdirectory of the sandbox. The artifact registry is the source of
        # truth — the disk copy is a per-session working cache that the
        # runtime is free to discard when the session ends. No symlink, no
        # GCS sync, no manifest.
        memory_local_dir = os.path.join(workspace, "agent_memories")
        os.makedirs(memory_local_dir, exist_ok=True)
        try:
            materialized = await materialize_memory_for_session(
                workspace=workspace,
                caller=MemoryCallerContext(
                    user_id=user_id,
                    agent_name=resolved_agent_id,
                ),
            )
        except Exception:
            # Materialization is best-effort — a transient DB blip should
            # not block session creation. The agent can still call MCP
            # load_memory / search_memory at runtime to read from the DB
            # directly.
            materialized = 0
            logger.warning(
                "Failed to materialize MEMORY artifacts for session",
                session_id=str(agent_session.agent_session_id),
                agent_name=resolved_agent_id,
                exc_info=True,
            )
        logger.info(
            "Materialized agent memory directory",
            agent_name=resolved_agent_id,
            files=materialized,
            workspace=workspace,
        )

        # Create the BCH manager for this session.  The bwrap proxy process starts
        # lazily on the first tool call; creating the manager here is cheap (no I/O).
        # Stored in a local so we can stop it on commit failure (try/finally below).
        _bch_manager = CommandHandlerManager(workspace=workspace)

        # Start pre-spawning the subprocess so the bwrap+CLI cold start (~4.2s) overlaps
        # with the remaining DB writes (session.commit, send_message DB ops, background
        # task setup).  Prerequisites satisfied above: workspace dir, .claude symlink,
        # repo symlinks, and the materialized agent_memories/ directory all exist.
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

            # Wrap the body with a non-spoofable sender-identity marker so the
            # receiver (and the SOUL.md "Inbound Messages from Other Agents"
            # contract) can detect this is an A2A message without parsing the
            # body for a sender-supplied "From <agent>: ..." prefix.
            # ``_from_agent_name`` was cached above while the DB session was
            # still open — see the AGENT-trigger identity resolution block.
            _wrapped_initial_message = _wrap_fellow_agent_content(_from_agent_name, request.message)

            async with get_async_session() as _msg_db:
                _fellow_turn = await _next_turn_number(_msg_db, agent_session.agent_session_id)
                _fellow_msg = AgentSessionMessage(
                    agent_session_id=agent_session.agent_session_id,
                    turn_number=_fellow_turn,
                    role=AgentSessionMessageRole.FELLOW_AGENT,
                    content=_wrapped_initial_message,
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
                        message=_wrapped_initial_message,
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
                has_mcp_access = await has_permission_by_user_id_cached(sender_user_id, Permission.USE_MCP)
            except Exception:
                logger.error(
                    "Error checking USE_MCP for follow-up sender, disabling MCP",
                    user_id=sender_user_id,
                    session_id=str(agent_session.agent_session_id),
                    exc_info=True,
                )
        else:
            logger.warning(
                "Slack message missing user_id, disabling agcouch-mcp",
                slack_user_id=request.slack_user_id,
                session_id=str(agent_session.agent_session_id),
            )
        _msg_perms = SessionPermissions.full_access() if has_mcp_access else SessionPermissions.restricted()
        session_context["permissions"] = _msg_perms.model_dump(mode="json")

    # Track the current message sender in memory so MCP tools (e.g., create_pr)
    # can attribute actions to the user who asked for them.
    if msg_creator_user_id:
        set_session_current_user(str(agent_session.agent_session_id), msg_creator_user_id)
        # Also propagate to session_context so the agcouch-mcp-server can attribute
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

    # If the previous turn was killed by an AHS restart, prepend a synthetic
    # system note so the LLM knows it was mid-flight and can either continue
    # from its draft or ask the user for clarification.  We DO NOT mutate the
    # stored ``user_msg.content`` — only the runner input — so the persisted
    # conversation history still shows what the user actually typed.
    #
    # ``consume_resume_pending`` does an atomic GETDEL on the Redis flag, so
    # two concurrent send_message calls cannot both observe it set: the first
    # GETDEL clears the key, the second sees nothing.  No DB lock needed.
    if await consume_resume_pending(agent_session.agent_session_id):
        try:
            preamble = await build_resume_context(agent_session.agent_session_id)
        except Exception:
            logger.warning(
                "build_resume_context raised — sending message without resume preamble",
                session_id=str(agent_session.agent_session_id),
                exc_info=True,
            )
            preamble = ""
        if preamble:
            agent_message = preamble + agent_message
            logger.info(
                "Prepended auto-resume preamble after restart-interrupted turn",
                session_id=str(agent_session.agent_session_id),
                turn_number=turn_number,
                preamble_chars=len(preamble),
            )

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
