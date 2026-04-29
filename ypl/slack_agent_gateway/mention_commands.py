"""Slash commands embedded in @mention messages to agent bots.

This module owns the full surface for *mention-based* slash commands — the
ones a user types inline after @mentioning a bot, e.g. ``@raccoon /status``
or ``@raccoon /model anthropic/claude-sonnet-4-6 review my PR``.

This is distinct from ``commands.py`` which handles Slack's *native* slash
commands registered through the Slack app config (currently only Bot Father's
``/create-agent``).

The surface has three layers:

1. *Parsing* — pure text → structured command. Exposed as
   :func:`extract_bare_command`, :func:`extract_model_directive`,
   :func:`extract_agent_directive`, :func:`extract_leading_directives`.

2. *Formatting* — render results/lists as Slack mrkdwn for posting.
   Exposed as :func:`format_models_list`, :func:`format_agents_list`,
   :func:`format_session_status`, plus the static :data:`HELP_TEXT`.

3. *Dispatch* — end-to-end handling for a parsed bare command:
   :func:`dispatch_bare_command` is the single entry point used by
   ``events.py``. It hides the individual ``_handle_*`` functions so the
   caller doesn't have to know about each command.

Bare commands (full @mention message is the command) — see
:data:`BARE_COMMANDS`:

    /stop, /help, /agents, /models, /status, /verbose, /quiet,
    /attach <uuid>, /pending, /archive

Leading directives (first word of a message, prefix for the actual text):

    /model <spec>   — override the model for a new session
    /agent <name>   — override the AHS agent for a new session

Directives are parsed in :mod:`events` (not dispatched here) because they
interact with the broader session-creation flow; the formatters and parsers
they rely on live here alongside the bare-command handlers.
"""

from __future__ import annotations
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from ypl.agent_harness_service.common.constants import AHS_LIT_BASE_URL
from ypl.backend.utils.slack_utils import resolve_slack_user_to_yupp_user_id
from ypl.slack_agent_gateway.agent_client import (
    archive_session as ahs_archive_session,
)
from ypl.slack_agent_gateway.agent_client import (
    attach_slack_to_session,
    get_available_agents,
    get_available_models,
    get_pending_sessions,
    get_session_info,
    stop_agent_session,
)
from ypl.slack_agent_gateway.redis_client import (
    get_ahs_session_for_thread,
    get_session,
    save_session,
    store_thread_session_mapping,
)
from ypl.slack_agent_gateway.sessions import (
    create_session as create_sag_session,
)
from ypl.slack_agent_gateway.slack_client import build_slack_client
from ypl.slack_agent_gateway.types import AgentAppConfig, AgentSession
from ypl.structured_logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


# Pattern to strip Slack bot mentions (e.g., "<@U12345>").
MENTION_PATTERN = re.compile(r"<@\w+>\s*")

# Bare slash commands — where the @mention message *is* the command (no
# message body is forwarded to the agent). The slash is optional only for the
# legacy commands (``stop``, ``attach``) to preserve existing user behaviour;
# for the newer commands the slash is required so we don't mis-parse
# natural-language phrases like "help me with X" or "status update" as
# commands.
BARE_COMMANDS: set[str] = {
    "stop",
    "attach",
    "help",
    "agents",
    "models",
    "status",
    "verbose",
    "quiet",
    "pending",
    "archive",
}
LEGACY_NOSLASH_COMMANDS: set[str] = {"stop", "attach"}


def extract_bare_command(text: str) -> tuple[str, str] | None:
    """Extract a bare slash command from the message text, if any.

    Bare commands are ones where the entire @mention message is a single
    command (optionally followed by an arg for ``/attach``):

        /stop           interrupt the running agent turn
        /help           show usage
        /agents         list available agents
        /models         list available models
        /status         show current session metadata
        /verbose        turn tool-call display on  (default)
        /quiet          turn tool-call display off
        /attach         (rejected — needs a UUID)
        /attach <uuid>  link thread to existing AHS session
        /attach:<uuid>  legacy colon form (alias)
        /pending        list sessions waiting on the caller's input
        /archive        archive the current thread's session

    Case-insensitive. The slash is optional for ``stop`` and ``attach``
    (legacy behaviour) but **required** for the newer commands to avoid
    false-positives on natural-language phrasing.

    Returns:
        Tuple of ``(command_name, args_string)`` or None if not a command.
    """
    stripped = MENTION_PATTERN.sub("", text).strip()
    if not stripped:
        return None

    parts = stripped.split(None, 1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    # Colon-form attach alias: ``/attach:<uuid>`` → ("attach", "<uuid>").
    # (We also accept ``attach:<uuid>`` without the leading slash to match
    # the slash-optional legacy behaviour for ``attach``.)
    if ":" in first and first.lstrip("/").lower().startswith("attach:"):
        _, _, colon_arg = first.partition(":")
        return ("attach", colon_arg.strip())

    has_slash = first.startswith("/")
    cmd_lower = first.lstrip("/").lower()

    if cmd_lower not in BARE_COMMANDS:
        return None

    # Slash-less form allowed only for legacy commands.
    if not has_slash and cmd_lower not in LEGACY_NOSLASH_COMMANDS:
        return None

    if cmd_lower == "attach":
        return ("attach", rest)

    # All other bare commands take no arguments — any trailing text is ignored.
    return (cmd_lower, "")


def _extract_leading_directive(stripped: str, directive: str) -> tuple[str | None, str]:
    """Match a leading ``/<directive> <value>`` or ``/<directive>:<value>``.

    Must be the first whitespace-separated token. Both syntaxes are accepted
    for backward compatibility, but the space form is preferred for
    consistency with the bare ``/attach <uuid>`` command.

    Returns:
        ``(value, remainder)`` where ``value`` is the directive's argument
        and ``remainder`` is the message text with the directive + value
        stripped. If no directive matches, returns ``(None, stripped)``.
    """
    if not stripped:
        return None, stripped

    parts = stripped.split(None, 1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    prefix_colon = f"/{directive}:"
    if first.lower().startswith(prefix_colon):
        value = first[len(prefix_colon) :]
        if not value:
            # Bare ``/model:`` or ``/agent:`` with no value — not a directive.
            return None, stripped
        return value, rest

    if first.lower() == f"/{directive}":
        if not rest:
            # Bare ``/model`` or ``/agent`` with no value — not a directive.
            return None, stripped
        value_parts = rest.split(None, 1)
        value = value_parts[0]
        remainder = value_parts[1].strip() if len(value_parts) > 1 else ""
        return value, remainder

    return None, stripped


def extract_model_directive(text: str) -> tuple[str | None, str]:
    """Extract an optional ``/model <spec>`` (or ``/model:<spec>``) directive.

    The directive must be the *first* word after the @mention, e.g.::

        @raccoon /model anthropic/claude-sonnet-4-6 please review this PR
        @raccoon /model:anthropic/claude-sonnet-4-6 please review this PR   (alias)

    Returns:
        ``(model_spec, cleaned_text)`` where ``cleaned_text`` is the message
        with the @mention and directive stripped. If no directive is
        present, ``model_spec`` is None and ``cleaned_text`` is the
        @mention-stripped text unchanged.
    """
    stripped = MENTION_PATTERN.sub("", text).strip()
    return _extract_leading_directive(stripped, "model")


def extract_agent_directive(text: str) -> tuple[str | None, str]:
    """Extract an optional ``/agent <name>`` (or ``/agent:<name>``) directive.

    Same shape as :func:`extract_model_directive`. Used to override which
    AHS agent handles a newly-created session, regardless of which Slack
    bot was @mentioned. Only takes effect on the first message of a thread.
    """
    stripped = MENTION_PATTERN.sub("", text).strip()
    return _extract_leading_directive(stripped, "agent")


def extract_leading_directives(text: str) -> tuple[str | None, str | None, str]:
    """Extract both ``/agent`` and ``/model`` leading directives, in any order.

    Returns ``(agent_name, model_spec, cleaned_text)``. The user may stack
    both directives at the start of their message in either order::

        @raccoon /agent sre /model anthropic/claude-sonnet-4-6 alerts?
        @raccoon /model anthropic/claude-sonnet-4-6 /agent sre alerts?

    Each directive is optional. If neither matches, ``cleaned_text`` is the
    @mention-stripped message unchanged.
    """
    stripped = MENTION_PATTERN.sub("", text).strip()
    agent_value: str | None = None
    model_value: str | None = None

    # Parse up to two iterations so each directive is consumed at most once;
    # extra iterations are a no-op because the directives are gone after the
    # first hit.
    for _ in range(2):
        if agent_value is None:
            candidate, remainder = _extract_leading_directive(stripped, "agent")
            if candidate is not None:
                agent_value = candidate
                stripped = remainder
                continue
        if model_value is None:
            candidate, remainder = _extract_leading_directive(stripped, "model")
            if candidate is not None:
                model_value = candidate
                stripped = remainder
                continue
        break

    return agent_value, model_value, stripped


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


# Static help text — ordered roughly by lifecycle / frequency of use.
# Kept inline (rather than read from a file) so it ships with the service
# and stays in sync with the parser above.
HELP_TEXT = (
    "*Slash commands*\n"
    "\n"
    "_Use these when you @mention me in a thread._\n"
    "\n"
    "*Anywhere in a thread:*\n"
    "• `/help` — show this message\n"
    "• `/stop` — interrupt the currently running turn\n"
    "• `/status` — show session id, agent, model, and activity\n"
    "• `/agents` — list all AHS agents you can route to\n"
    "• `/models` — list all models you can use\n"
    "• `/verbose` — show live tool-call info in-thread (default)\n"
    "• `/quiet` — hide live tool-call info for the rest of this thread\n"
    "• `/pending` — list your sessions waiting on a turn (last 24h)\n"
    "• `/archive` — archive this thread's session (excludes it from `/pending`)\n"
    "\n"
    "*First message of a thread only:*\n"
    "• `/model SPEC` — use a specific model for this session "
    "(e.g. `/model anthropic/claude-sonnet-4-6 please review this PR`)\n"
    "• `/agent NAME` — route to a specific agent for this session "
    "(e.g. `/agent sre what alerts are firing?`)\n"
    "• `/attach UUID` — attach this thread to an existing AHS session\n"
    "\n"
    "_Colon syntax (`/model:SPEC`, `/agent:NAME`, `/attach:UUID`) is accepted as an alias._"
)


def format_models_list(models: dict) -> str:
    """Format the models dict from AHS into a human-readable Slack mrkdwn block."""
    harnessed: list[str] = models.get("harnessed", [])
    raw: list[str] = models.get("raw", [])
    lines = [
        "*Harnessed executors* (agent runs inside a managed CLI wrapper):",
        *[f"• `{m}`" for m in harnessed],
        "",
        "*Raw LLM models* (direct API calls, no CLI wrapper):",
        *[f"• `{m}`" for m in raw],
        "",
        "_Usage:_ `@agent /model provider/modelname your message here`",
    ]
    return "\n".join(lines)


def format_agents_list(agents: list[dict]) -> str:
    """Format an agents list from AHS into a human-readable Slack mrkdwn block.

    Each agent dict follows the ``AgentInfo`` schema returned by
    ``GET /ahs/agents``: ``name``, ``display_name``, ``description``,
    ``executor_type``, ``executor_model`` / ``llm_model``, etc.
    """
    if not agents:
        return "_No agents available._"

    # Sort by name for deterministic display.
    sorted_agents = sorted(agents, key=lambda a: a.get("name", ""))
    lines: list[str] = ["*Available agents* (use `/agent NAME` to override):"]
    for agent in sorted_agents:
        name = agent.get("name", "?")
        display = agent.get("display_name") or name
        description = agent.get("description") or ""
        # Prefer executor_model (harness name) over llm_model for display.
        model = agent.get("executor_model") or agent.get("llm_model") or "—"
        # First sentence of description, capped for readability.
        snippet = description.split("\n")[0].strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        header = f"• `{name}` — *{display}* (model: `{model}`)"
        if snippet:
            lines.append(f"{header}\n  _{snippet}_")
        else:
            lines.append(header)
    lines.append("")
    lines.append("_Usage:_ `@agent /agent AGENT_NAME your message here` (first message only)")
    return "\n".join(lines)


def _format_relative_time(when: datetime, *, now: datetime | None = None) -> str:
    """Format a duration like ``"2h ago"`` for /pending output."""
    now = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = now - when
    total_s = max(0, int(delta.total_seconds()))
    if total_s < 60:
        return f"{total_s}s ago"
    if total_s < 3600:
        return f"{total_s // 60}m ago"
    if total_s < 86400:
        return f"{total_s // 3600}h ago"
    return f"{total_s // 86400}d ago"


def format_pending_sessions(
    pending_human: list[dict[str, Any]],
    pending_ai: list[dict[str, Any]],
    hours_back: int,
    *,
    permalinks: dict[str, str] | None = None,
) -> str:
    """Render /pending response as Slack mrkdwn.

    ``permalinks`` maps ``session_id`` to a resolved Slack permalink. Sessions
    without a resolved permalink fall back to a non-clickable channel/thread
    reference.
    """
    permalinks = permalinks or {}

    if not pending_human and not pending_ai:
        return f"_No sessions are pending input in the last {hours_back}h. You're caught up!_ :tada:"

    def _render_one(session: dict[str, Any]) -> str:
        session_id = session.get("session_id", "")
        agent_name = session.get("agent_name") or "—"
        last_at_raw = session.get("last_message_at")
        last_at: datetime | None = None
        if isinstance(last_at_raw, str):
            try:
                last_at = datetime.fromisoformat(last_at_raw)
            except ValueError:
                last_at = None
        when_str = _format_relative_time(last_at) if last_at else "—"
        title = (session.get("title") or "").strip()
        preview = (session.get("last_message_preview") or "").strip()
        channel_name = session.get("slack_channel_name")
        channel_id = session.get("slack_channel_id")
        thread_ts = session.get("slack_thread_ts")

        permalink = permalinks.get(session_id)
        if permalink:
            label = title or (f"#{channel_name}" if channel_name else f"`{session_id[:8]}`")
            link = f"<{permalink}|{label}>"
        elif channel_id and thread_ts:
            ref = f"#{channel_name}" if channel_name else f"`{channel_id}`"
            link = f"{ref} ({thread_ts})"
        else:
            link = f"`{session_id[:8]}`"

        line = f"• {link} — `{agent_name}` · _{when_str}_"
        if preview:
            line += f"\n  › _{preview}_"
        return line

    parts: list[str] = [f"*Pending sessions* (last {hours_back}h)"]
    if pending_human:
        parts.append("")
        parts.append(f":bell: *Waiting on you* ({len(pending_human)})")
        parts.extend(_render_one(s) for s in pending_human)
    if pending_ai:
        parts.append("")
        parts.append(f":robot_face: *Waiting on the agent* ({len(pending_ai)})")
        parts.extend(_render_one(s) for s in pending_ai)
    parts.append("")
    parts.append("_Use `/archive` in a thread to remove it from this list._")
    return "\n".join(parts)


def format_session_status(
    slack_session: AgentSession | None,
    ahs_session: dict | None,
    ahs_session_id: str | None,
) -> str:
    """Format session status as a Slack mrkdwn block."""
    if ahs_session is None and slack_session is None:
        return "_No active session in this thread._"

    # Extract fields from AHS session detail (if available) first, then fall
    # back to SAG session fields.
    ahs_data = (ahs_session or {}).get("session") or {}
    agent_name = ahs_data.get("agent_name") or (slack_session.agent_name if slack_session else "—")
    model = ahs_data.get("model") or "_(agent default)_"
    message_count = ahs_data.get("message_count", 0)
    ahs_status = ahs_data.get("status") or (str(slack_session.status) if slack_session else "—")
    title = ahs_data.get("title")

    # Prefer AHS timestamp because SAG's created_at can be off by a few
    # seconds (session row written after AHS create returns).
    created_at_raw = ahs_data.get("created_at")
    created_at: datetime | None = None
    if created_at_raw:
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except (ValueError, TypeError):
            created_at = None
    if created_at is None and slack_session is not None:
        created_at = slack_session.created_at

    elapsed_str = ""
    if created_at is not None:
        # Always compute "now" as UTC and assume a naive created_at is also
        # UTC — SAG only ever writes UTC timestamps, and AHS returns ISO-8601
        # strings. A timezone-naive created_at would only show up from legacy
        # data; subtracting two UTC-aware datetimes gives a tz-aware delta
        # either way.
        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        delta = now - created_at
        total_s = int(delta.total_seconds())
        if total_s < 60:
            elapsed_str = f"{total_s}s"
        elif total_s < 3600:
            elapsed_str = f"{total_s // 60}m {total_s % 60}s"
        else:
            elapsed_str = f"{total_s // 3600}h {(total_s % 3600) // 60}m"

    # Link out to the Lit console when we have an AHS UUID.
    if ahs_session_id and AHS_LIT_BASE_URL:
        short_id = ahs_session_id[:8]
        session_ref = f"<{AHS_LIT_BASE_URL}/agent_harness_console?session_id={ahs_session_id}|{short_id}>"
    elif ahs_session_id:
        session_ref = f"`{ahs_session_id[:8]}`"
    elif slack_session is not None:
        session_ref = f"`{slack_session.session_id}`"
    else:
        session_ref = "`—`"

    lines = [
        "*Session status*",
        f"• *ID:* {session_ref}",
        f"• *Agent:* `{agent_name}`",
        f"• *Model:* {model}",
        f"• *Status:* `{ahs_status}`",
        f"• *Messages:* {message_count}",
    ]
    if elapsed_str:
        lines.append(f"• *Elapsed:* {elapsed_str}")
    if slack_session is not None:
        tool_mode = "verbose" if slack_session.show_tool_calls else "quiet"
        lines.append(f"• *Tool-call display:* `{tool_mode}`")
    if title:
        lines.append(f"• *Title:* _{title}_")
    lines.append("")
    lines.append("_Token and cost aggregates coming in a follow-up PR._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers — one per bare command. All are module-private; call sites should
# use ``dispatch_bare_command`` below which routes by name.
# ---------------------------------------------------------------------------


async def _handle_stop_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    user_id: str,
) -> dict[str, Any]:
    """Handle the /stop command: stop the running agent and notify the user.

    1. Look up the existing session (if none, post a note and return)
    2. Call AHS POST /ahs/session/stop
    3. Post a status-appropriate message to Slack
    """
    session_id = AgentSession.build_session_id(channel_id, thread_ts, app_config.app_id)
    session = await get_session(session_id)

    if not session:
        logger.info(
            "Stop command but no active session",
            channel_id=channel_id,
            thread_ts=thread_ts,
            agent_name=app_config.agent_name,
        )
        try:
            client = build_slack_client(app_config.app_id, app_config.bot_token)
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="_No active task to stop._",
            )
        except Exception:
            logger.error("Failed to post no-session reply for /stop", channel_id=channel_id)
        return {"status": "no_session", "agent_name": app_config.agent_name}

    # Clean up any placeholder message from the current turn
    if session.placeholder_ts:
        try:
            client = build_slack_client(app_config.app_id, app_config.bot_token)
            await client.chat_delete(channel=channel_id, ts=session.placeholder_ts)
        except Exception:
            pass
        session.placeholder_ts = None
        await save_session(session)

    # Call AHS to stop the session
    result = await stop_agent_session(
        agent_name=app_config.agent_name,
        session_id=session.session_id,
    )

    ahs_status = result.get("status") if result else None
    logger.info(
        "Stop command processed",
        session_id=session.session_id,
        agent_name=app_config.agent_name,
        user_id=user_id,
        ahs_status=ahs_status,
    )

    if ahs_status == "stopped":
        slack_text = "_Interrupted. What should the agent do instead?_"
    elif ahs_status == "no_inflight_turn":
        slack_text = "_No active task to stop._"
    else:
        slack_text = "_Failed to stop the agent. Please try again._"

    try:
        client = build_slack_client(app_config.app_id, app_config.bot_token)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=slack_text,
        )
    except Exception:
        logger.error("Failed to post stop status message", session_id=session.session_id)

    return {
        "status": ahs_status or "error",
        "session_id": session.session_id,
        "agent_name": app_config.agent_name,
    }


async def _handle_attach_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    ts: str,
    user_id: str,
    session_id_arg: str,
    channel_name: str | None = None,
) -> dict[str, Any]:
    """Handle the /attach {session_id} command.

    Only works as the first message in a thread (top-level message). Stores
    the thread→session mapping and calls AHS to attach Slack context.
    """
    client = build_slack_client(app_config.app_id, app_config.bot_token)

    # Validate: must be a top-level message (first message in a new thread)
    is_top_level = thread_ts == ts
    if not is_top_level:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: `/attach` can only be used as the first message in a thread.",
            )
        except Exception:
            pass
        return {"status": "attach_rejected", "reason": "not_top_level"}

    # Validate session_id arg
    if not session_id_arg:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: Usage: `/attach {session_id}` — provide an AHS session UUID.",
            )
        except Exception:
            pass
        return {"status": "attach_rejected", "reason": "missing_session_id"}

    # Validate it looks like a UUID
    try:
        uuid.UUID(session_id_arg)
    except ValueError:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":warning: Invalid session ID: `{session_id_arg}`. Expected a UUID.",
            )
        except Exception:
            pass
        return {"status": "attach_rejected", "reason": "invalid_uuid"}

    # Fetch session info from AHS to validate it exists and get metadata
    session_detail = await get_session_info(session_id_arg)
    if not session_detail or "session" not in session_detail:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":warning: Session not found: `{session_id_arg}`",
            )
        except Exception:
            pass
        return {"status": "attach_rejected", "reason": "session_not_found"}

    session_info = session_detail["session"]
    agent_name = session_info.get("agent_name", "unknown")
    message_count = session_info.get("message_count", 0)
    created_at = session_info.get("created_at")
    session_status = session_info.get("status", "unknown")

    sag_session_id = AgentSession.build_session_id(channel_id, thread_ts, app_config.app_id)

    # Attach Slack context to AHS session FIRST — only persist local state on
    # success to avoid orphaned Redis/SAG mappings that silently drop agent
    # replies.
    attach_result = await attach_slack_to_session(
        ahs_session_id=session_id_arg,
        slack_session_id=sag_session_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        channel_name=channel_name,
    )
    if attach_result is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":warning: Failed to attach Slack context to session `{session_id_arg}`. "
                "The AHS service may be unavailable — please try again.",
            )
        except Exception:
            pass
        return {"status": "attach_failed", "reason": "ahs_attach_error"}

    # AHS attach succeeded — now persist local routing state
    await store_thread_session_mapping(channel_id, thread_ts, session_id_arg)

    await create_sag_session(
        channel_id=channel_id,
        thread_ts=thread_ts,
        app_id=app_config.app_id,
        agent_name=app_config.agent_name,
        creator_slack_user_id=user_id,
        channel_name=channel_name,
    )

    short_id = session_id_arg[:8]
    session_link = (
        f"<{AHS_LIT_BASE_URL}/agent_harness_console?session_id={session_id_arg}|{short_id}>"
        if AHS_LIT_BASE_URL
        else f"`{short_id}`"
    )
    time_str = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at)
            time_str = f" since {dt.strftime('%b %d, %H:%M UTC')}"
        except (ValueError, TypeError):
            time_str = f" since {created_at}"

    confirm_text = (
        f":link: Attached to session {session_link} "
        f"(agent: *{agent_name}*, status: {session_status})\n"
        f"_{message_count} message{'s' if message_count != 1 else ''}{time_str}_"
    )

    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=confirm_text,
        )
    except Exception:
        logger.error("Failed to post attach confirmation", channel_id=channel_id)

    logger.info(
        "Attached thread to AHS session via /attach command",
        ahs_session_id=session_id_arg,
        sag_session_id=sag_session_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id=user_id,
    )

    return {
        "status": "attached",
        "ahs_session_id": session_id_arg,
        "sag_session_id": sag_session_id,
        "agent_name": agent_name,
    }


async def _handle_help_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    """Handle the /help command: post the command reference in-thread."""
    try:
        client = build_slack_client(app_config.app_id, app_config.bot_token)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=HELP_TEXT,
        )
    except Exception as e:
        logger.warning("Failed to post /help response", error=str(e))
    return {"status": "help_shown"}


async def _handle_agents_list_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    """Handle the /agents command: list available agents from AHS."""
    client = build_slack_client(app_config.app_id, app_config.bot_token)
    agents = await get_available_agents()
    if agents is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: Could not fetch the agent list — AHS may be unavailable.",
            )
        except Exception as e:
            logger.warning("Failed to post /agents error", error=str(e))
        return {"status": "agents_unavailable"}

    text = format_agents_list(agents)
    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    except Exception as e:
        logger.warning("Failed to post /agents response", error=str(e))
    return {"status": "agents_listed", "count": len(agents)}


async def _handle_models_list_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    """Handle the /models command: list available models from AHS."""
    client = build_slack_client(app_config.app_id, app_config.bot_token)
    available = await get_available_models()
    if available is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: Could not fetch the model list — AHS may be unavailable.",
            )
        except Exception as e:
            logger.warning("Failed to post /models error", error=str(e))
        return {"status": "models_unavailable"}

    text = format_models_list(available)
    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    except Exception as e:
        logger.warning("Failed to post /models response", error=str(e))
    return {"status": "models_listed"}


async def _handle_status_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    """Handle the /status command: show session metadata."""
    client = build_slack_client(app_config.app_id, app_config.bot_token)

    sag_session_id = AgentSession.build_session_id(channel_id, thread_ts, app_config.app_id)
    slack_session = await get_session(sag_session_id)

    # Resolve the best AHS identifier we have:
    # 1. Thread initiated by an agent → stored as UUID in the thread-mapping.
    # 2. Otherwise the SAG composite ID itself (AHS accepts either form).
    ahs_session_id = await get_ahs_session_for_thread(channel_id, thread_ts)
    if ahs_session_id is None and slack_session is not None:
        ahs_session_id = slack_session.session_id

    ahs_session_detail: dict | None = None
    if ahs_session_id is not None:
        ahs_session_detail = await get_session_info(ahs_session_id)

    text = format_session_status(
        slack_session=slack_session,
        ahs_session=ahs_session_detail,
        ahs_session_id=ahs_session_id,
    )
    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    except Exception as e:
        logger.warning("Failed to post /status response", error=str(e))
    return {"status": "status_shown", "has_session": slack_session is not None}


async def _handle_verbose_or_quiet_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    verbose: bool,
) -> dict[str, Any]:
    """Toggle tool-call display for the current session.

    Requires an existing SAG session — the flag is stored on ``AgentSession``
    and flipped back and forth as the user re-issues the commands. If there's
    no session yet, we tell the user to start one first so the default
    (verbose) stays intuitive.
    """
    client = build_slack_client(app_config.app_id, app_config.bot_token)
    sag_session_id = AgentSession.build_session_id(channel_id, thread_ts, app_config.app_id)
    slack_session = await get_session(sag_session_id)

    if slack_session is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    "_No active session in this thread — start one by mentioning me first._\n"
                    "_(Tool-call display is on by default for new sessions.)_"
                ),
            )
        except Exception as e:
            logger.warning("Failed to post no-session reply for /verbose or /quiet", error=str(e))
        return {"status": "no_session"}

    was_verbose = slack_session.show_tool_calls
    slack_session.show_tool_calls = verbose
    await save_session(slack_session)

    if verbose and not was_verbose:
        msg = ":loud_sound: Verbose mode: tool-call info will be shown in this thread."
    elif verbose and was_verbose:
        msg = ":loud_sound: Already in verbose mode."
    elif not verbose and was_verbose:
        msg = ":mute: Quiet mode: tool-call info will be hidden for the rest of this thread."
    else:
        msg = ":mute: Already in quiet mode."

    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=msg,
        )
    except Exception as e:
        logger.warning(
            "Failed to post /verbose or /quiet confirmation",
            verbose=verbose,
            error=str(e),
        )

    logger.info(
        "Toggled tool-call display",
        session_id=slack_session.session_id,
        verbose=verbose,
        was_verbose=was_verbose,
    )
    return {"status": "verbose" if verbose else "quiet"}


async def _resolve_permalinks(
    client: Any,
    sessions: list[dict[str, Any]],
) -> dict[str, str]:
    """Best-effort resolve Slack permalinks for a batch of pending sessions.

    Failures are swallowed — sessions without a permalink still render with
    a fallback channel/thread reference.
    """
    import asyncio

    async def _one(s: dict[str, Any]) -> tuple[str, str | None]:
        channel_id = s.get("slack_channel_id")
        thread_ts = s.get("slack_thread_ts")
        session_id = s.get("session_id", "")
        if not channel_id or not thread_ts:
            return session_id, None
        try:
            resp = await client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)
            link = resp.get("permalink") if isinstance(resp, dict) else getattr(resp, "data", {}).get("permalink")
            return session_id, link
        except Exception:
            return session_id, None

    if not sessions:
        return {}
    results = await asyncio.gather(*[_one(s) for s in sessions])
    return {sid: link for sid, link in results if link}


async def _handle_pending_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    user_id: str,
) -> dict[str, Any]:
    """Handle the /pending command: list the caller's sessions awaiting a turn.

    Resolves the calling Slack user to a Yupp user_id, then asks AHS for the
    pending split. Posts results inline to the thread that invoked it.
    """
    client = build_slack_client(app_config.app_id, app_config.bot_token)

    try:
        yupp_user_id = await resolve_slack_user_to_yupp_user_id(user_id, bot_token=app_config.bot_token)
    except Exception:
        logger.exception(
            "Failed to resolve Slack user to Yupp user_id for /pending",
            slack_user_id=user_id,
        )
        yupp_user_id = None

    if not yupp_user_id:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: I couldn't match your Slack account to a Yupp user. "
                "Make sure your account is linked, then try `/pending` again.",
            )
        except Exception as e:
            logger.warning("Failed to post /pending unresolved-user reply", error=str(e))
        return {"status": "user_unresolved"}

    payload = await get_pending_sessions(yupp_user_id, hours_back=24)
    if payload is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: Could not fetch pending sessions — AHS may be unavailable.",
            )
        except Exception as e:
            logger.warning("Failed to post /pending error", error=str(e))
        return {"status": "ahs_unavailable"}

    pending_human = payload.get("pending_human") or []
    pending_ai = payload.get("pending_ai") or []
    hours_back = int(payload.get("hours_back", 24))

    permalinks = await _resolve_permalinks(client, [*pending_human, *pending_ai])
    text = format_pending_sessions(
        pending_human=pending_human,
        pending_ai=pending_ai,
        hours_back=hours_back,
        permalinks=permalinks,
    )

    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
    except Exception as e:
        logger.warning("Failed to post /pending response", error=str(e))

    return {
        "status": "pending_listed",
        "pending_human": len(pending_human),
        "pending_ai": len(pending_ai),
    }


async def _handle_archive_command(
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    user_id: str,
) -> dict[str, Any]:
    """Handle the /archive command: archive the current thread's session.

    Looks up the session ID from the thread mapping (or falls back to the
    SAG composite ID), then asks AHS to set ``status = ARCHIVED``.
    """
    client = build_slack_client(app_config.app_id, app_config.bot_token)

    sag_session_id = AgentSession.build_session_id(channel_id, thread_ts, app_config.app_id)
    slack_session = await get_session(sag_session_id)

    # Prefer the AHS UUID stored in the thread mapping when this thread was
    # initiated by an agent; otherwise the SAG composite ID is what AHS knows
    # the session by.
    ahs_session_id = await get_ahs_session_for_thread(channel_id, thread_ts)
    target_id = ahs_session_id or (slack_session.session_id if slack_session else sag_session_id)

    if not slack_session and not ahs_session_id:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="_No session in this thread to archive._",
            )
        except Exception as e:
            logger.warning("Failed to post /archive no-session reply", error=str(e))
        return {"status": "no_session"}

    result = await ahs_archive_session(target_id)
    if result is None:
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":warning: Failed to archive this session — AHS may be unavailable.",
            )
        except Exception as e:
            logger.warning("Failed to post /archive error", error=str(e))
        return {"status": "ahs_unavailable"}

    ahs_status = result.get("status")
    if ahs_status == "archived":
        msg = ":file_cabinet: Archived. This thread won't appear in `/pending` anymore."
    elif ahs_status == "already_archived":
        msg = ":file_cabinet: Already archived."
    else:
        msg = f":warning: Unexpected archive response: `{ahs_status}`"

    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=msg,
        )
    except Exception as e:
        logger.warning("Failed to post /archive confirmation", error=str(e))

    logger.info(
        "Archived session via /archive command",
        sag_session_id=sag_session_id,
        ahs_session_id=ahs_session_id,
        target_id=target_id,
        ahs_status=ahs_status,
        slack_user_id=user_id,
    )
    return {
        "status": ahs_status or "error",
        "target_id": target_id,
    }


# ---------------------------------------------------------------------------
# Dispatch — single entry point called by ``events.handle_app_mention`` once
# a bare command has been parsed out of the message text.
# ---------------------------------------------------------------------------


async def dispatch_bare_command(
    cmd_name: str,
    cmd_args: str,
    *,
    app_config: AgentAppConfig,
    channel_id: str,
    thread_ts: str,
    ts: str,
    user_id: str,
    channel_name: str | None = None,
) -> dict[str, Any] | None:
    """Dispatch a parsed bare slash command to its handler.

    Args:
        cmd_name: Command name from :func:`extract_bare_command` (e.g. ``"stop"``).
        cmd_args: Any trailing argument (currently only used by ``/attach``).
        app_config: The agent-bot config for the @mentioned Slack app.
        channel_id, thread_ts, ts, user_id: Slack event context.
        channel_name: Human-readable channel name, if already resolved.

    Returns:
        The handler's result dict, or None if ``cmd_name`` doesn't match any
        known command (defensive — parsing guarantees it will match).
    """
    if cmd_name == "stop":
        return await _handle_stop_command(
            app_config=app_config,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )
    if cmd_name == "attach":
        return await _handle_attach_command(
            app_config=app_config,
            channel_id=channel_id,
            thread_ts=thread_ts,
            ts=ts,
            user_id=user_id,
            session_id_arg=cmd_args,
            channel_name=channel_name,
        )
    if cmd_name == "help":
        return await _handle_help_command(app_config, channel_id, thread_ts)
    if cmd_name == "agents":
        return await _handle_agents_list_command(app_config, channel_id, thread_ts)
    if cmd_name == "models":
        return await _handle_models_list_command(app_config, channel_id, thread_ts)
    if cmd_name == "status":
        return await _handle_status_command(app_config, channel_id, thread_ts)
    if cmd_name == "verbose":
        return await _handle_verbose_or_quiet_command(app_config, channel_id, thread_ts, verbose=True)
    if cmd_name == "quiet":
        return await _handle_verbose_or_quiet_command(app_config, channel_id, thread_ts, verbose=False)
    if cmd_name == "pending":
        return await _handle_pending_command(app_config, channel_id, thread_ts, user_id)
    if cmd_name == "archive":
        return await _handle_archive_command(app_config, channel_id, thread_ts, user_id)

    # Parsing contract says this is unreachable; return None so callers can
    # decide (e.g. fall through to normal message forwarding).
    logger.warning("Unknown bare command — falling through", cmd_name=cmd_name)
    return None
