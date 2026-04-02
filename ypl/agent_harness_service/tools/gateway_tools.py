"""Gateway communication tools for the harness MCP server.

Provides tools for agents to interact with Slack: request feedback surveys,
post multiple-choice questions, and send proactive messages to channels.
"""

from __future__ import annotations
import uuid as _uuid_mod

from sqlalchemy import text

from ypl.agent_harness_service.common.constants import mcp_session_id_var
from ypl.agent_harness_service.tools.mcp_instance import _resolve_parent_session, _validate_session_id, mcp
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import AgentProject, AgentSession
from ypl.structured_logger import get_logger

logger = get_logger()


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
        "Pass project_id to auto-route to the project's updates thread — no manual thread_ts lookup needed. "
        "If channel is also omitted, the project's slack_channel is used automatically. "
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
        thread_ts: Optional thread timestamp to reply in an existing thread.
            Optional when project_id is given — the project's updates_thread_ts is used instead.
        project_id: AHS project UUID. When provided and thread_ts is omitted, the tool
            auto-routes to the project's updates thread (reads updates_thread_ts from
            project shared_state). Also provides the channel if channel is omitted.
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
                # Only auto-fill thread_ts when channel was also resolved from the project,
                # otherwise we'd try to reply in a thread that belongs to a different channel.
                if not effective_thread_ts and (channel_from_project or effective_channel == project.slack_channel):
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

    # Resolve harness UUID → agent name
    parent_info = await _resolve_parent_session(effective_session_id)
    agent_name = parent_info.get("agent_name")

    if not agent_name:
        return {"status": "error", "error": "Could not resolve agent name from session"}

    # Call gateway to send the message
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
