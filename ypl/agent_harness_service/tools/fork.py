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

Authorization: the actor (resolved from ``context.current_turn_user_id`` for
in-thread forks, or from the caller session's ``creator_user_id`` for
cross-session forks) must equal the source session's ``creator_user_id``.
This applies uniformly to Slack and non-Slack sources. The check is skipped
only when the source has no recorded creator (legacy / unattributed
sessions). The previous ``slack_user_id``-only check was bypassable in
shared Slack threads and short-circuited for non-Slack callers.
"""

from __future__ import annotations
import datetime
import uuid as _uuid
from typing import Any

from sqlmodel import col, select

from ypl.agent_harness_service.common.constants import mcp_session_id_var
from ypl.agent_harness_service.common.types import SessionCreateRequest, SessionMessageRequest
from ypl.agent_harness_service.tools.gateway_tools import _SendResultLike
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


# Whitelist of context fields carried into the new session. Everything else
# (permissions, task_id, project_id, routine_id, from_agent_id, current_turn_user_id,
# force_model, subagent_depth, trigger_message_id, …) is dropped so the fork starts
# from a clean accounting/authorization state. Permissions are recomputed by
# send_message() via the USE_MCP check.
_SAFE_CONTEXT_FIELDS_BASE: frozenset[str] = frozenset({"user_id", "user_name", "display_name", "yupp_user_id"})
_SAFE_CONTEXT_FIELDS_SLACK: frozenset[str] = frozenset(
    {"slack_channel_id", "slack_team_id", "slack_channel_name", "slack_user_id"}
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _derive_new_slack_session_id(src_slack_session_id: str | None, channel: str, new_thread_ts: str) -> str | None:
    """Compose a new SAG-style ``{channel}:{thread_ts}:{app_id}`` composite.

    Reuses the ``app_id`` segment from the source's composite so SAG routes
    follow-up @mentions in the new thread to the same Slack app/agent.
    Returns ``None`` when the source's composite is missing or malformed —
    callers must treat this as a hard error since silently emitting a bogus
    app_id would produce a thread where follow-up @mentions never resolve.
    """
    if not src_slack_session_id:
        return None
    parts = src_slack_session_id.split(":")
    if len(parts) < 3 or not parts[-1]:
        return None
    return f"{channel}:{new_thread_ts}:{parts[-1]}"


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

    result: _SendResultLike
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
        "Authorization: the actor (current_turn_user_id for in-thread, or "
        "caller session's creator for cross-session) must match the source "
        "session's creator_user_id. "
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
        src_creator_user_id = str(src.creator_user_id) if src.creator_user_id else None

        # Resolve who is actually triggering the fork (the "actor"):
        # - In-thread fork (caller == source): the user who sent the current turn.
        #   send_message() stamps src_ctx["current_turn_user_id"] = msg_creator_user_id
        #   on every Slack message ingestion, so this reflects the /fork sender —
        #   not the original thread originator. This is what protects shared threads.
        # - Cross-session fork: the caller session's creator_user_id.
        actor_user_id: str | None
        if effective_session_id == src_id:
            actor_user_id = src_ctx.get("current_turn_user_id") or src_creator_user_id
        else:
            caller_uuid = _uuid.UUID(effective_session_id)
            caller_sess = await db.get(AgentSession, caller_uuid)
            if not caller_sess:
                return {"status": "error", "error": f"Caller session not found: {effective_session_id}"}
            actor_user_id = str(caller_sess.creator_user_id) if caller_sess.creator_user_id else None

        # Enforce: the actor must be the source's creator. Applies uniformly to
        # Slack and non-Slack sources — the only case where we skip the check is
        # when the source has no creator (legacy / unattributed sessions).
        if src_creator_user_id and actor_user_id and src_creator_user_id != actor_user_id:
            return {
                "status": "error",
                "error": (
                    f"Only the source session's creator (user_id={src_creator_user_id}) "
                    f"can fork this session. Actor is {actor_user_id}."
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
        # Validate the source has a parseable Slack composite BEFORE posting the
        # header — otherwise we'd open a thread whose follow-up @mentions never
        # route back to the agent (silent dead-end).
        if (
            not src_slack_session_id
            or len(src_slack_session_id.split(":")) < 3
            or not src_slack_session_id.split(":")[-1]
        ):
            return {
                "status": "error",
                "error": (
                    "Source session's slack_session_id is missing or malformed — "
                    "cannot derive app_id for routing follow-up @mentions in the new thread."
                ),
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
    # 4) Build the new session's context (whitelist — drop privilege/accounting state)
    # ------------------------------------------------------------------
    allowed_fields = set(_SAFE_CONTEXT_FIELDS_BASE)
    if effective_target == "slack_new_thread":
        allowed_fields |= _SAFE_CONTEXT_FIELDS_SLACK
    new_context: dict[str, Any] = {k: v for k, v in src_ctx.items() if k in allowed_fields}

    if effective_target == "slack_new_thread" and new_thread_ts:
        new_context["slack_thread_ts"] = new_thread_ts
        new_context["slack_message_ts"] = new_thread_ts

    # Append a lineage entry. Each entry stands alone so chained forks accumulate
    # without losing intermediate hops. Attribute to the actual fork actor, not
    # the source's creator (the two may differ in the cross-session path).
    new_context["fork_lineage"] = list(src_ctx.get("fork_lineage", [])) + [
        {
            "forked_from_session_id": str(src_uuid),
            "forked_at": _now_iso(),
            "forked_by_user_id": actor_user_id,
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
        if not new_slack_session_id:
            # Already validated above; defensive guard in case _derive_new_slack_session_id
            # acquires additional failure modes.
            return {
                "status": "error",
                "error": "Failed to derive new Slack session id for the new thread.",
            }
        new_trigger = "slack"

    # TODO: this is a Layer-1 → wiring import, which violates the AHS layering
    # rule (see ARCHITECTURE.md). Convert to a registered orchestration callback
    # the same way local_mcp_server.register_orchestration_callbacks() does it.
    # The lazy import below hides the cycle from import-time detection but does
    # not change the dependency direction.
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
    # TODO: snapshot races with concurrent source-session turns — if a turn
    # flips to SUCCESS between create_session above and the SELECT below, the
    # fork picks up history the user didn't see when they typed /fork. Take a
    # SELECT … FOR UPDATE lock on the source AgentSession row the same way
    # session_lifecycle.send_message does, holding it across the snapshot.
    try:
        async with get_async_session() as db:
            result = await db.execute(
                select(AgentSessionMessage)
                .where(AgentSessionMessage.agent_session_id == src_uuid)
                .where(AgentSessionMessage.completion_status == AgentSessionMessageCompletionStatus.SUCCESS)
                .order_by(col(AgentSessionMessage.turn_number), col(AgentSessionMessage.created_at))
            )
            src_messages = list(result.scalars().all())

            for msg in src_messages:
                # Copy A2A provenance straight through: from_agent_id and
                # agent_message_id_ref reference agents/agent_messages rows that
                # are NOT session-scoped, so the FKs remain valid in the new
                # session and the FELLOW_AGENT check constraint
                # ((role='FELLOW_AGENT') = (from_agent_id IS NOT NULL)) is
                # satisfied. Omitting from_agent_id for FELLOW_AGENT rows causes
                # an IntegrityError at commit.
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
                    from_agent_id=msg.from_agent_id,
                    agent_message_id_ref=msg.agent_message_id_ref,
                )
                db.add(copy)

            max_turn = max((m.turn_number for m in src_messages), default=0)
            # Marker placement matters for dispatch: ``send_message`` (called in
            # step 7) consults ``_has_inflight_turn`` which finds the latest
            # USER/FELLOW_AGENT turn in the new session and treats it as
            # "in-flight" unless that turn has at least one non-inbound,
            # non-IN_PROGRESS message (an AGENT or SYSTEM row). Because
            # ``fork_session`` is invoked from within the source session's own
            # in-flight turn, the source's most recent USER message is
            # snapshotted but its matching AGENT response is still
            # IN_PROGRESS and therefore excluded by the SUCCESS-only filter
            # above. If we wrote the marker at ``max_turn + 1`` it would be
            # treated as a brand-new system turn rather than as a response
            # to the latest snapshotted USER — and ``_has_inflight_turn``
            # would return True, causing the additional_instructions to be
            # queued into ``_pending_messages`` with no active task to ever
            # drain it. Co-locating the marker on the same turn as the
            # latest snapshotted USER both (a) reads naturally as the
            # system's closing reply to the prior conversation and (b)
            # satisfies the inflight check so step 7 dispatches normally.
            marker_turn = max(max_turn, 1)
            marker_text = (
                f"[Forked from session {src_id} at turn {marker_turn}. "
                "The conversation above is replayed from the original session; "
                "the next user turn carries the new fork instructions.]"
            )
            marker = AgentSessionMessage(
                agent_session_id=new_session_uuid,
                turn_number=marker_turn,
                role=AgentSessionMessageRole.SYSTEM,
                content=marker_text,
                completion_status=AgentSessionMessageCompletionStatus.SUCCESS,
            )
            db.add(marker)

            new_sess = await db.get(AgentSession, new_session_uuid)
            if new_sess is not None:
                new_sess.forked_from_session_id = src_uuid

            await db.commit()
    except Exception as exc:
        logger.error(
            "fork_session: snapshot commit failed (Slack header already posted, new session row exists)",
            source_session_id=src_id,
            new_session_id=str(new_session_uuid),
            error=str(exc),
            exc_info=True,
        )
        # Surface the failure in the original thread so the user knows the
        # 🍴 Forked from… header in the new thread is orphaned.
        if effective_target == "slack_new_thread" and channel:
            orig_thread_ts = src_ctx.get("slack_thread_ts")
            try:
                await _post_slack(
                    text=f"\U0001f374 *Fork failed during snapshot:* `{exc}`. The new thread is orphaned.",
                    channel=channel,
                    session_id=effective_session_id,
                    thread_ts=orig_thread_ts,
                )
            except Exception:
                logger.warning("fork_session: failed to post fork-failure notice", exc_info=True)
        return {
            "status": "error",
            "error": f"Snapshot commit failed: {exc}",
            "new_session_id": str(new_session_uuid),
        }

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
        send_resp = await send_message(msg_req)
    except Exception as exc:
        logger.error(
            "fork_session: send_message failed (session created but no first turn dispatched)",
            new_session_id=str(new_session_uuid),
            error=str(exc),
            exc_info=True,
        )
        # Symmetric with the snapshot-commit failure path above: if the new
        # Slack thread was already opened, surface the dispatch failure in
        # the original thread so the user knows the new thread is orphaned.
        if effective_target == "slack_new_thread" and channel:
            orig_thread_ts = src_ctx.get("slack_thread_ts")
            try:
                await _post_slack(
                    text=f"\U0001f374 *Fork dispatch failed:* `{exc}`. The new thread is orphaned.",
                    channel=channel,
                    session_id=effective_session_id,
                    thread_ts=orig_thread_ts,
                )
            except Exception:
                logger.warning("fork_session: failed to post dispatch-failure notice", exc_info=True)
        # The session row + snapshot are committed; we surface the partial state
        # so the caller can decide whether to retry sending the message.
        return {
            "status": "partial",
            "new_session_id": str(new_session_uuid),
            "forked_from": src_id,
            "error": f"send_message failed: {exc}",
            "snapshot_turn_count": snapshot_turn_count,
        }

    # Defensive check: the marker-placement invariant above is what keeps
    # send_message off the "queued" path (see comment near ``marker_turn``).
    # If a future refactor reverts marker placement, send_message returns
    # ``SessionMessageResponse(status="queued")`` *without* raising, and the
    # queued message sits in the in-memory ``_pending_messages`` queue with
    # no active task on this fresh session to ever drain it — the fork dies
    # silently exactly as it did before this PR. Turn that documentary
    # invariant into an executable one: if we see ``queued`` here, the fork
    # has NOT dispatched and we must return ``partial`` rather than ``ok``.
    if send_resp.status == "queued":
        logger.error(
            "fork_session: send_message returned queued — marker-placement invariant violated, "
            "fork did not dispatch and the new session has no active task to drain _pending_messages",
            new_session_id=str(new_session_uuid),
        )
        if effective_target == "slack_new_thread" and channel:
            orig_thread_ts = src_ctx.get("slack_thread_ts")
            try:
                await _post_slack(
                    text=(
                        "\U0001f374 *Fork dispatch queued but no active task to drain it.* "
                        "The new thread is orphaned (marker-placement regression — see fork.py)."
                    ),
                    channel=channel,
                    session_id=effective_session_id,
                    thread_ts=orig_thread_ts,
                )
            except Exception:
                logger.warning("fork_session: failed to post queued-dispatch notice", exc_info=True)
        return {
            "status": "partial",
            "new_session_id": str(new_session_uuid),
            "forked_from": src_id,
            "error": "send_message queued with no active task to drain — fork did not dispatch",
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
