"""Gateway communication tools for the harness MCP server.

Provides tools for agents to interact with Slack: request feedback surveys,
post multiple-choice questions, and send proactive messages to channels.

Write policy for :func:`send_slack_message`:

1. If the initiating agent has a registered Slack bot (row in ``slack_agents``
   with a usable token), post through SAG as that bot — the normal case.
2. Otherwise fall back to OpsBot (the shared workspace bot), logging loudly
   so the fallback is visible in ops logs. This covers CRON / TASK sessions
   whose agent has no dedicated Slack identity.

Interactive tools (``request_feedback``, ``ask_question``) are pinned to the
app that owns the Slack thread — their callback signing won't work with any
other bot — and are never fallen-back.
"""

from __future__ import annotations
import uuid as _uuid_mod
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from ypl.agent_harness_service.common.constants import mcp_session_id_var
from ypl.agent_harness_service.tools.mcp_instance import _resolve_parent_session, _validate_session_id, mcp
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import AgentProject, AgentSession
from ypl.slack_common import agent_has_slack_presence, get_ops_bot_write_client
from ypl.structured_logger import get_logger

logger = get_logger()


class _SendResultLike(Protocol):
    """Structural type matching what ``send_slack_message`` reads from a send.

    Both :class:`GatewaySendResult` (returned by SAG) and :class:`_SendResult`
    (returned by the OpsBot fallback path) satisfy this Protocol. Declaring a
    local Protocol avoids importing from ``gateway/``, which Layer 1 ``tools/``
    is forbidden from under the AHS layering rules.
    """

    success: bool
    message_id: str | None
    destination: str | None
    error: str | None


@dataclass
class _SendResult:
    """Concrete result for the OpsBot fallback path."""

    success: bool
    message_id: str | None = None
    destination: str | None = None
    error: str | None = None


async def _post_as_opsbot(
    channel: str,
    text: str,
    thread_ts: str | None,
) -> _SendResult:
    """Post directly as OpsBot (fallback when agent has no Slack presence).

    Does not go through SAG — OpsBot isn't a per-agent bot, so SAG has nothing
    to route. Returns a :class:`_SendResult` matching the GatewaySendResult
    shape that the SAG path produces, so the caller can treat both paths the
    same way.
    """
    try:
        client = get_ops_bot_write_client()
        response = await client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
        if not response.get("ok"):
            return _SendResult(
                success=False,
                error=response.get("error", "Unknown Slack error"),
                destination=channel,
            )
        return _SendResult(
            success=True,
            message_id=response.get("ts"),
            destination=response.get("channel") or channel,
        )
    except Exception as exc:
        logger.error(
            "send_slack_message: OpsBot fallback post failed",
            channel=channel,
            error=str(exc),
            exc_info=True,
        )
        return _SendResult(success=False, error=str(exc), destination=channel)


async def _resolve_slack_session_id(harness_session_id: str) -> str | None:
    """Look up the Slack session ID for a harness session UUID.

    Args:
        harness_session_id: The harness session UUID.

    Returns:
        The slack_session_id (channel:thread_ts:app_id) or None.
    """
    async with get_async_session() as db:
        result = await db.execute(
            text("SELECT slack_session_id FROM agent_sessions WHERE agent_session_id = :sid"),
            {"sid": harness_session_id},
        )
        row = result.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="request_feedback",
    description=(
        "Request user feedback on the current session. Posts a short survey "
        "to the Slack thread asking the user to rate the experience (Good/OK/Bad) "
        "with optional comments. Use this when your work was complex, nuanced, "
        "when the conversation was long and deep, or when you think user feedback would be valuable. "
        "Only works for Slack sessions."
    ),
)
async def request_feedback(session_id: str) -> dict[str, str]:
    """Request a feedback survey be posted to the Slack thread.

    Args:
        session_id: Your harness session ID (provided in the system prompt).

    Returns:
        Dict with status and optional error message.
    """
    from ypl.agent_harness_service.gateway import GatewayRegistry

    logger.info("MCP tool: request_feedback", session_id=session_id)
    _validate_session_id(session_id)

    # Resolve harness UUID → Slack composite session_id (channel:thread_ts:app_id)
    slack_session_id = await _resolve_slack_session_id(session_id)

    if not slack_session_id:
        return {"status": "error", "error": "No Slack session found for this harness session"}

    gateway = GatewayRegistry.get_instance().get("slack")
    if not gateway:
        return {"status": "error", "error": "Slack gateway not registered"}

    try:
        success = await gateway.request_feedback(slack_session_id)
        if not success:
            return {"status": "error", "error": "Gateway rejected feedback request"}
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(
    name="ask_question",
    description=(
        "Ask the user a multiple-choice question in the Slack thread. "
        "Renders a question with clickable answer buttons so the user can respond "
        "without typing. The selected choice (or a typed reply) is returned to you "
        "as the next message in the conversation. "
        "Use this to gather structured input, clarify intent, or present options — "
        "for example: asking which environment to deploy to, or which topic to focus on. "
        "Only works for Slack sessions. Max 5 choices. "
        "Set allow_free_text=True (default) to show a hint that the user can also type a custom answer."
    ),
)
async def ask_question(
    session_id: str,
    question_id: str,
    text: str,
    choices: list[str],
    allow_free_text: bool = True,
) -> dict[str, str]:
    """Post a multiple-choice question to the Slack thread.

    Args:
        session_id: Your harness session ID (provided in the system prompt).
        question_id: A short alphanumeric identifier for this question (e.g. "deploy_env", "topic").
                     Used internally to route the response; not shown to the user.
        text: The question text shown to the user (Slack mrkdwn supported).
        choices: List of choice labels (plain text, max 5). Each becomes a button.
        allow_free_text: If True (default), show a hint that the user can type a free answer.

    Returns:
        Dict with status and optional error message.
    """
    from ypl.agent_harness_service.gateway import GatewayRegistry

    logger.info("MCP tool: ask_question", session_id=session_id, question_id=question_id, num_choices=len(choices))
    _validate_session_id(session_id)

    if not choices:
        return {"status": "error", "error": "choices must not be empty"}
    if len(choices) > 5:
        return {"status": "error", "error": "choices must have at most 5 items (Slack limit)"}

    # Resolve harness UUID → Slack composite session_id (channel:thread_ts:app_id)
    slack_session_id = await _resolve_slack_session_id(session_id)

    if not slack_session_id:
        return {"status": "error", "error": "No Slack session found for this harness session"}

    gateway = GatewayRegistry.get_instance().get("slack")
    if not gateway:
        return {"status": "error", "error": "Slack gateway not registered"}

    # Convert plain-text choice labels into the {label, value} dicts SAG expects
    choice_dicts = [{"label": c, "value": c} for c in choices]

    try:
        success = await gateway.send_questionnaire(
            session_id=slack_session_id,
            question_id=question_id,
            text=text,
            choices=choice_dicts,
            allow_free_text=allow_free_text,
        )
        if not success:
            return {"status": "error", "error": "Gateway rejected questionnaire request"}
        return {"status": "ok", "message": "Question posted. Awaiting user response."}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool(
    name="send_slack_message",
    description=(
        "Send a proactive message to a Slack channel. Use this to notify users, "
        "post updates, or initiate conversations in Slack channels where your agent "
        "has access. Unlike replying in threads, this creates a new message or "
        "replies in an existing thread by specifying thread_ts. "
        "Your agent must have a Slack presence configured to use this tool. "
        "IMPORTANT: The text parameter must be in Slack mrkdwn format, NOT standard Markdown. "
        "Key differences: bold is *text*, italic is _text_, links are <url|label>, "
        "code blocks use ``` with no language tag, no headings (#), no tables, no numbered lists. "
        "Pass project_id (without channel) to auto-route to the project's updates thread — "
        "no manual thread_ts lookup needed. "
        "If channel is also omitted, the project's slack_channel is used automatically. "
        "If you provide channel alongside project_id, the message is a new top-level post "
        "in that channel (project_id never auto-fills thread_ts when channel is explicit). "
        "NOTE: In Slack sessions, your text output is automatically relayed to Slack by the harness — "
        "only call this tool when explicitly instructed to post to a specific channel or thread. "
        "If you do use it, do not also produce text output with the same content — the harness will relay both, "
        "causing the user to see duplicates."
    ),
)
async def send_slack_message(
    text: str,
    channel: str | None = None,
    thread_ts: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    """Send a message to a Slack channel.

    Args:
        text: Message content in Slack mrkdwn format (not standard Markdown).
        channel: Slack channel name or ID (e.g., 'alert-backend', '#general', or 'C123ABC').
            Optional when project_id is given — the project's slack_channel is used instead.
            If you provide channel explicitly, the message is always a new top-level message
            in that channel (thread_ts is never auto-filled from the project in this case).
        thread_ts: Optional thread timestamp to reply in an existing thread.
        project_id: AHS project UUID. Controls auto-routing:
            - project_id only (no channel, no thread_ts): auto-resolves the project's
              slack_channel AND updates_thread_ts, posting inside the project updates thread.
            - project_id + channel: channel is used as-is; thread_ts is NOT auto-filled
              (the message is a new top-level post in the specified channel).
            - project_id + thread_ts: posts to project's channel (if channel omitted)
              in the given thread.
        session_id: The calling agent's harness session ID, used to resolve the agent name.

    Returns:
        Dict with status, message_ts, channel, and optional error message.
    """
    import datetime

    from ypl.agent_harness_service.gateway import GatewayRegistry

    # Prefer session_id from the HTTP header (injected by runner into .mcp.json).
    # Fall back to the tool parameter (passed by LLM) for backward compatibility.
    effective_session_id = mcp_session_id_var.get() or session_id
    if not effective_session_id:
        return {"status": "error", "error": "session_id is required", "channel": channel or ""}
    _validate_session_id(effective_session_id)

    effective_channel = channel
    effective_thread_ts = thread_ts

    # If project_id given: auto-resolve channel and/or updates_thread_ts.
    if project_id:
        try:
            async with get_async_session() as db:
                project = await db.get(AgentProject, _uuid_mod.UUID(project_id))
            if project:
                channel_from_project = not effective_channel and bool(project.slack_channel)
                if channel_from_project:
                    effective_channel = project.slack_channel
                # Only auto-fill thread_ts when channel was ALSO resolved from the project
                # (i.e. the caller did not provide channel explicitly).
                # If the caller provided channel, they want a top-level message — do not
                # route it into the project updates thread.  The previous condition also
                # included `or effective_channel == project.slack_channel` which caused
                # PR/checkpoint messages (which correctly pass channel but also pass
                # project_id) to land in the updates thread once updates_thread_ts was set,
                # creating the inconsistency where those messages were sometimes top-level
                # (before the thread existed) and sometimes threaded (after).
                if not effective_thread_ts and channel_from_project:
                    effective_thread_ts = (project.shared_state or {}).get("updates_thread_ts")
        except Exception:
            logger.warning(
                "send_slack_message: failed to resolve project info (continuing without)",
                project_id=project_id,
                exc_info=True,
            )

    if not effective_channel:
        return {"status": "error", "error": "channel is required (or pass project_id to auto-resolve)", "channel": ""}

    logger.info(
        "MCP tool: send_slack_message",
        session_id=effective_session_id,
        channel=effective_channel,
        text_length=len(text),
        in_thread=effective_thread_ts is not None,
        project_id=project_id,
    )

    # Resolve harness UUID → agent name. Works for every trigger type
    # (SLACK, CRON, TASK) because AgentSession.agent_id is always set.
    parent_info = await _resolve_parent_session(effective_session_id)
    agent_name = parent_info.get("agent_name")

    if not agent_name:
        return {"status": "error", "error": "Could not resolve agent name from session"}

    # Decide the poster: initiating agent's bot if it has a Slack presence,
    # else fall back to OpsBot. The DB check is cheap (indexed unique) and
    # saves an SAG round-trip for agents without a registered bot.
    async with get_async_session() as db:
        has_presence = await agent_has_slack_presence(db, agent_name)

    result: _SendResultLike
    if has_presence:
        gateway = GatewayRegistry.get_instance().get("slack")
        if not gateway:
            return {"status": "error", "error": "Slack gateway not registered", "channel": effective_channel}

        result = await gateway.send_message(
            agent_name=agent_name,
            destination=effective_channel,
            text=text,
            thread_id=effective_thread_ts,
            ahs_session_id=effective_session_id,
        )
    else:
        # Loud log so OpsBot-as-fallback usage is visible in ops dashboards.
        # Most write traffic should take the per-agent path; if this shows up
        # frequently it's a signal the triggering agent is missing a Slack bot.
        logger.warning(
            "send_slack_message: OpsBot fallback (agent has no Slack presence)",
            agent_name=agent_name,
            channel=effective_channel,
            in_thread=effective_thread_ts is not None,
            session_id=effective_session_id,
            project_id=project_id,
        )
        result = await _post_as_opsbot(
            channel=effective_channel,
            text=text,
            thread_ts=effective_thread_ts,
        )

    if not result.success:
        return {
            "status": "error",
            "error": result.error or "Unknown error",
            "channel": effective_channel,
        }

    returned_channel = result.destination or effective_channel

    # Persist the thread mapping to AgentSession.context["registered_threads"] (best-effort).
    # This makes thread→session mappings queryable from the DB, not just SAG Redis.
    # Only record top-level threads (not replies) to stay in sync with SAG semantics.
    # The root thread TS is effective_thread_ts when replying, else result.message_id
    # (the new message is itself the thread root).
    root_thread_ts = effective_thread_ts or result.message_id
    if root_thread_ts and not effective_thread_ts:
        # Only persist new top-level threads; replies don't create new thread→session mappings.
        try:
            async with get_async_session() as db:
                db_session = await db.get(AgentSession, _uuid_mod.UUID(effective_session_id))
                if db_session is not None:
                    ctx = dict(db_session.context or {})
                    registered: list[dict] = list(ctx.get("registered_threads", []))
                    registered.append(
                        {
                            "channel_id": returned_channel,
                            "thread_ts": root_thread_ts,
                            "registered_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        }
                    )
                    ctx["registered_threads"] = registered
                    db_session.context = ctx
                    await db.commit()
        except Exception:
            logger.warning(
                "send_slack_message: failed to persist registered_thread to DB (non-fatal)",
                session_id=effective_session_id,
                exc_info=True,
            )

    return {
        "status": "ok",
        "message_ts": result.message_id or "",
        "channel": returned_channel,
    }
