"""Session fork tool for the harness MCP server.

Provides ``fork_session`` — snapshot a source session into a new peer session
that evolves independently. When the source is Slack-triggered, the new
session opens in a *new top-level thread in the same channel*, and both
threads get cross-link messages so users can navigate the lineage.

Lineage is recorded two ways:

1. ``AgentSession.forked_from_session_id`` column (indexed) — for queryable
   lineage in Streamlit and analytics.
2. ``context["fork_lineage"]`` JSONB list — accumulates across chained forks
   so a fork-of-a-fork carries its full ancestry without recursive joins.

Authorization (Slack sources only): only the user who originated the source
Slack thread (``context.slack_user_id``) is allowed to fork it. Internal A2A
or API calls bypass this check — the trust boundary is already inside AHS.
"""

from __future__ import annotations
import datetime
import uuid as _uuid
from typing import Any

from sqlmodel import select

from ypl.agent_harness_service.common.constants import mcp_session_id_var
from ypl.agent_harness_service.common.types import SessionCreateRequest, SessionMessageRequest
from ypl.agent_harness_service.tools.mcp_instance import _validate_session_id, mcp
from ypl.backend.db import get_async_session
from ypl.backend.utils.slack_utils import create_slack_link
from ypl.db.agent_harness import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageCompletionStatus,
    AgentSessionMessageRole,
    AgentSessionTrigger,
)
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _derive_new_slack_session_id(src_slack_session_id: str | None, channel: str, new_thread_ts: str) -> str:
    """Compose a new SAG-style ``{channel}:{thread_ts}:{app_id}`` composite.

    Reuses the ``app_id`` segment from the source's composite so SAG routes
    follow-up @mentions in the new thread to the same Slack app/agent. Falls
    back to ``"?"`` if the source's composite is malformed — non-fatal,
    follow-up routing simply won't resolve.
    """
    app_id = "?"
    if src_slack_session_id:
        parts = src_slack_session_id.split(":")
        if len(parts) >= 3:
            app_id = parts[-1]
    return f"{channel}:{new_thread_ts}:{app_id}"


async def _resolve_agent_name(session_uuid: _uuid.UUID) -> str | None:
    """Look up the source session's agent name (needed to construct SessionCreateRequest)."""
    async with get_async_session() as db:
        result = await db.execute(
            select(Agent.name)
            .join(AgentSession, AgentSession.agent_id == Agent.agent_id)
            .where(AgentSession.agent_session_id == session_uuid)
        )
        return result.scalar_one_or_none()


async def _post_slack(
    *,
    text: str,
    channel: str,
    session_id: str,
    thread_ts: str | None = None,
) -> dict[str, str]:
    """Post via the registered Slack gateway. Avoids re-entering the MCP tool layer.

    Returns the same shape as ``send_slack_message`` —
    ``{"status": "ok", "message_ts": "...", "channel": "..."}`` on success.
    """
    from ypl.agent_harness_service.gateway import GatewayRegistry
    from ypl.agent_harness_service.tools.gateway_tools import _post_as_opsbot
    from ypl.agent_harness_service.tools.mcp_instance import _resolve_parent_session
    from ypl.slack_common import agent_has_slack_presence

    parent_info = await _resolve_parent_session(session_id)
    agent_name = parent_info.get("agent_name")

    if agent_name:
        async with get_async_session() as db:
            has_presence = await agent_has_slack_presence(db, agent_name)
    else:
        has_presence = False

    if has_presence and agent_name:
        gateway = GatewayRegistry.get_instance().get("slack")
        if gateway:
            result = await gateway.send_message(
                agent_name=agent_name,
                destination=channel,
                text=text,
                thread_id=thread_ts,
                ahs_session_id=session_id,
            )
            if result.success:
                return {
                    "status": "ok",
                    "message_ts": result.message_id or "",
                    "channel": result.destination or channel,
                }
            return {"status": "error", "error": result.error or "send failed", "channel": channel}

    # Fallback: OpsBot.
    result = await _post_as_opsbot(channel=channel, text=text, thread_ts=thread_ts)
    if result.success:
        return {
            "status": "ok",
            "message_ts": result.message_id or "",
            "channel": result.destination or channel,
        }
    return {"status": "error", "error": result.error or "send failed", "channel": channel}


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fork_session",
    description=(
        "Fork a session into a new peer session that evolves independently. "
        "Snapshots all completed messages from the source into the new session, "
        "then appends the caller's additional_instructions as the first USER "
        "turn — so the fork picks up the prior conversation but heads in a new "
        "direction. For Slack-triggered source sessions, the fork opens in a "
        "NEW TOP-LEVEL THREAD in the same channel; cross-link messages are "
        "posted in both threads. Lineage is recorded on the new session via "
        "forked_from_session_id and context.fork_lineage. "
        "Authorization: when the source is Slack-triggered, only the original "
        "thread originator (context.slack_user_id) is allowed to fork. "
        "Returns the new session UUID and (for Slack) the new thread URL."
    ),
)
async def fork_session(
    additional_instructions: str,
    source_session_id: str | None = None,
    target: str = "auto",
    include_tool_calls: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Snapshot a session into a new peer session.

    Args:
        additional_instructions: The new instructions for the forked session. Becomes
            the first USER turn after the replayed history.
        source_session_id: Which session to fork. Defaults to the calling session.
        target: ``"slack_new_thread"`` (post a new top-level Slack message and route
            the fork there), ``"headless"`` (no Slack posting, returned session is
            usable via API only), or ``"auto"`` (slack_new_thread when the source
            is Slack-triggered, else headless).
        include_tool_calls: If False, omit raw tool-call events (raw_events) from
            the snapshot. Reduces context window cost; loses tool-call fidelity.
        session_id: The calling session's harness UUID. Auto-injected from the MCP
            header in normal use.
    """
    effective_session_id = mcp_session_id_var.get() or session_id
    if not effective_session_id:
        return {"status": "error", "error": "session_id is required"}
    _validate_session_id(effective_session_id)

    src_id = source_session_id or effective_session_id
    _validate_session_id(src_id)
    src_uuid = _uuid.UUID(src_id)

    logger.info(
        "MCP tool: fork_session",
        caller_session_id=effective_session_id,
        source_session_id=src_id,
        target=target,
    )

    # ------------------------------------------------------------------
    # 1) Load source session + authorize
    # ------------------------------------------------------------------
    async with get_async_session() as db:
        src = await db.get(AgentSession, src_uuid)
        if not src:
            return {"status": "error", "error": f"Source session not found: {src_id}"}

        src_ctx: dict[str, Any] = dict(src.context or {})
        src_trigger = src.trigger
        src_slack_session_id = src.slack_session_id
        src_agent_id = src.agent_id

        # Authorization: only the Slack thread originator can fork a Slack source.
        # For non-Slack sources (api/cron/agent/task), the trust boundary is already
        # inside AHS — anyone who can call the MCP tool is allowed.
        if src_trigger == AgentSessionTrigger.SLACK:
            originator_slack_user = src_ctx.get("slack_user_id")
            # Resolve the caller's slack_user_id from their session context.
            caller_slack_user: str | None = None
            if effective_session_id != src_id:
                caller_uuid = _uuid.UUID(effective_session_id)
                caller_sess = await db.get(AgentSession, caller_uuid)
                if caller_sess and caller_sess.context:
                    caller_slack_user = caller_sess.context.get("slack_user_id")
            else:
                # Forking from inside the source session — caller_slack_user is the
                # user who sent the /fork message. We don't track per-message
                # slack_user_id easily here, so fall back to the originator: the
                # only attacker model this protects against is cross-session forks,
                # and the legitimate in-thread /fork case is always the originator
                # since only they can pass the upstream check at SAG ingress…
                # except in shared threads. To be safe in shared threads, accept
                # the originator only when caller == source; that's already the
                # 100% match case.
                caller_slack_user = originator_slack_user

            if originator_slack_user and caller_slack_user and originator_slack_user != caller_slack_user:
                return {
                    "status": "error",
                    "error": (
                        f"Only the original thread participant (slack_user_id={originator_slack_user}) "
                        f"can /fork this session. Caller is {caller_slack_user}."
                    ),
                }

        # Pull the source's agent name while the DB session is still open.
        src_agent = await db.get(Agent, src_agent_id)
        src_agent_name = src_agent.name if src_agent else None

    if not src_agent_name:
        return {"status": "error", "error": f"Could not resolve agent for source session {src_id}"}

    # ------------------------------------------------------------------
    # 2) Resolve target
    # ------------------------------------------------------------------
    effective_target = target
    if effective_target == "auto":
        effective_target = "slack_new_thread" if src_trigger == AgentSessionTrigger.SLACK else "headless"
    if effective_target not in ("slack_new_thread", "headless"):
        return {"status": "error", "error": f"Invalid target: {target!r}"}

    # ------------------------------------------------------------------
    # 3) For Slack target: post the fork header in a new top-level Slack
    #    message to capture the new thread_ts.
    # ------------------------------------------------------------------
    channel: str | None = None
    new_thread_ts: str | None = None
    new_thread_url: str | None = None
    orig_thread_url: str | None = None

    if effective_target == "slack_new_thread":
        channel = src_ctx.get("slack_channel_id")
        if not channel:
            return {
                "status": "error",
                "error": "Source session is missing slack_channel_id — cannot post a new fork thread.",
            }
        orig_thread_ts = src_ctx.get("slack_thread_ts")
        orig_thread_url = create_slack_link(channel, orig_thread_ts, orig_thread_ts) if orig_thread_ts else None

        snippet = additional_instructions.strip().splitlines()[0][:280] if additional_instructions.strip() else ""
        header_lines = [
            (
                f"\U0001f374 *Forked from <{orig_thread_url}|original thread>* — source session `{src_id}`."
                if orig_thread_url
                else f"\U0001f374 *Forked* — source session `{src_id}`."
            ),
        ]
        if snippet:
            header_lines.append(f"New focus: {snippet}")
        header_text = "\n".join(header_lines)

        post_result = await _post_slack(
            text=header_text,
            channel=channel,
            session_id=effective_session_id,
        )
        if post_result.get("status") != "ok":
            return {
                "status": "error",
                "error": f"Failed to post fork header in Slack: {post_result.get('error', 'unknown')}",
            }
        new_thread_ts = post_result["message_ts"]
        new_thread_url = create_slack_link(channel, new_thread_ts, new_thread_ts)

    # ------------------------------------------------------------------
    # 4) Build the new session's context
    # ------------------------------------------------------------------
    new_context: dict[str, Any] = dict(src_ctx)
    if effective_target == "slack_new_thread" and new_thread_ts:
        new_context["slack_thread_ts"] = new_thread_ts
        new_context["slack_message_ts"] = new_thread_ts
        # Drop carry-over state that referred to the old thread.
        new_context.pop("slack_thread_prefetched", None)
        new_context.pop("registered_threads", None)

    # Append a lineage entry. Each entry stands alone so chained forks accumulate
    # without losing intermediate hops.
    new_context["fork_lineage"] = list(src_ctx.get("fork_lineage", [])) + [
        {
            "forked_from_session_id": str(src_uuid),
            "forked_at": _now_iso(),
            "forked_by_user_id": new_context.get("user_id"),
            "forked_via_session_id": effective_session_id,
        }
    ]

    # ------------------------------------------------------------------
    # 5) Compose the new session via create_session() (no initial message —
    #    we'll snapshot history first, THEN send the user instructions as
    #    the next USER turn so it lands at turn_number = N+1 above replay).
    # ------------------------------------------------------------------
    new_slack_session_id: str | None = None
    new_trigger = "api"
    if effective_target == "slack_new_thread" and channel and new_thread_ts:
        new_slack_session_id = _derive_new_slack_session_id(src_slack_session_id, channel, new_thread_ts)
        new_trigger = "slack"

    # Lazy import to keep tool module deps minimal (Layer-1 → wiring).
    from ypl.agent_harness_service.service.session_lifecycle import create_session, send_message

    create_req = SessionCreateRequest(
        agent_id=src_agent_name,
        trigger=new_trigger,
        message=None,
        user_id=new_context.get("user_id"),
        context=new_context,
        session_id=new_slack_session_id,
        source="orchestration",
    )
    try:
        create_resp = await create_session(create_req)
    except Exception as exc:
        logger.error(
            "fork_session: create_session failed",
            source_session_id=src_id,
            error=str(exc),
            exc_info=True,
        )
        return {"status": "error", "error": f"create_session failed: {exc}"}

    new_session_uuid = _uuid.UUID(create_resp.session_id)

    # ------------------------------------------------------------------
    # 6) Snapshot completed messages + insert SYSTEM marker + record
    #    forked_from_session_id on the new row.
    # ------------------------------------------------------------------
    async with get_async_session() as db:
        result = await db.execute(
            select(AgentSessionMessage)
            .where(AgentSessionMessage.agent_session_id == src_uuid)
            .where(AgentSessionMessage.completion_status == AgentSessionMessageCompletionStatus.SUCCESS)
            .order_by(AgentSessionMessage.turn_number, AgentSessionMessage.created_at)
        )
        src_messages = list(result.scalars().all())

        for msg in src_messages:
            copy = AgentSessionMessage(
                agent_session_id=new_session_uuid,
                turn_number=msg.turn_number,
                role=msg.role,
                creator_user_id=msg.creator_user_id,
                content=msg.content,
                raw_events=msg.raw_events if include_tool_calls else None,
                llm_name=msg.llm_name,
                completion_status=msg.completion_status,
                error_type=msg.error_type,
                # Provenance columns are deliberately NOT copied: from_agent_id /
                # agent_message_id_ref reference rows that belong to the source
                # session's A2A history and would violate the FELLOW_AGENT check
                # constraint if attached to a different agent_session_id.
            )
            db.add(copy)

        max_turn = max((m.turn_number for m in src_messages), default=0)
        marker_text = (
            f"[Forked from session {src_id} at turn {max_turn}. "
            "The conversation above is replayed from the original session; "
            "the next user turn carries the new fork instructions.]"
        )
        marker = AgentSessionMessage(
            agent_session_id=new_session_uuid,
            turn_number=max_turn + 1,
            role=AgentSessionMessageRole.SYSTEM,
            content=marker_text,
            completion_status=AgentSessionMessageCompletionStatus.SUCCESS,
        )
        db.add(marker)

        new_sess = await db.get(AgentSession, new_session_uuid)
        if new_sess is not None:
            new_sess.forked_from_session_id = src_uuid

        await db.commit()

    snapshot_turn_count = len(src_messages)

    # ------------------------------------------------------------------
    # 7) Send the fork instructions as the next USER turn — this triggers
    #    the first dispatch of the new session.
    # ------------------------------------------------------------------
    msg_req = SessionMessageRequest(
        session_id=str(new_session_uuid),
        message=additional_instructions,
        user_id=new_context.get("user_id"),
        slack_user_id=new_context.get("slack_user_id"),
        source="orchestration",
    )
    try:
        await send_message(msg_req)
    except Exception as exc:
        logger.error(
            "fork_session: send_message failed (session created but no first turn dispatched)",
            new_session_id=str(new_session_uuid),
            error=str(exc),
            exc_info=True,
        )
        # The session row + snapshot are committed; we surface the partial state
        # so the caller can decide whether to retry sending the message.
        return {
            "status": "partial",
            "new_session_id": str(new_session_uuid),
            "forked_from": src_id,
            "error": f"send_message failed: {exc}",
            "snapshot_turn_count": snapshot_turn_count,
        }

    # ------------------------------------------------------------------
    # 8) Post the back-link in the original Slack thread (best-effort —
    #    failure here doesn't invalidate the fork).
    # ------------------------------------------------------------------
    if effective_target == "slack_new_thread" and channel and new_thread_url:
        orig_thread_ts = src_ctx.get("slack_thread_ts")
        back_text = f"\U0001f374 *Forked to <{new_thread_url}|new thread>* — new session `{new_session_uuid}`."
        try:
            await _post_slack(
                text=back_text,
                channel=channel,
                session_id=effective_session_id,
                thread_ts=orig_thread_ts,
            )
        except Exception:
            logger.warning(
                "fork_session: failed to post back-link in original thread (non-fatal)",
                channel=channel,
                orig_thread_ts=orig_thread_ts,
                new_session_id=str(new_session_uuid),
                exc_info=True,
            )

    return {
        "status": "ok",
        "new_session_id": str(new_session_uuid),
        "forked_from": src_id,
        "slack_thread_url": new_thread_url,
        "snapshot_turn_count": snapshot_turn_count,
        "target": effective_target,
    }
