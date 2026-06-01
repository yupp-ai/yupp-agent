"""Slack notifier for artifact create / update events.

Whenever an agent registers a new artifact via ``add_artifact``, saves a new
version via ``update_artifact_content``, edits metadata via
``update_artifact``, or stores a memory via ``save_memory``, this module
fires a richly-formatted notification into a single Slack channel — by
default ``#agent-artifacts`` (C0B0M10JR8E) — so the team has a live feed of
every artifact the agent fleet produces.

Design notes:

- **Fire-and-forget.** The hot artifact-creation path must never block on
  Slack — even a 200-ms post would compound across thousands of artifacts /
  day. ``notify_artifact_event`` schedules the post via
  :func:`create_background_task` and returns immediately; failures are logged
  and never surfaced to the agent.
- **Posts as OpsBot.** Every artifact-producing agent already has its writes
  attributed in the message body, so we don't need a per-agent bot. OpsBot is
  the workspace-wide app — exactly the right identity for "system" feed posts.
- **Privacy-aware MEMORY.** MEMORY artifacts have a ``(scope, subject)`` tuple
  that for ``user`` and ``agent`` scopes is a private identifier. We never
  echo the subject of a ``user`` scope (would be a PII leak). For ``agent``
  scope we echo the agent name (already public). For ``topic`` scope there is
  no subject. The body content of MEMORY artifacts is **never** included.
- **Disabled on local / test.** ``ARTIFACT_NOTIFICATIONS_CHANNEL=""`` or
  ``ENVIRONMENT in {local, test, selfhosted}`` skip the notification entirely
  — no need for developers to wire OpsBot to see artifacts on their laptop.

The module exposes one entry point:

    await notify_artifact_event(
        artifact=<AgentArtifact>,
        event=<"created"|"updated"|"new_version">,
        agent_name=<caller agent name>,
        session_id=<caller session UUID>,
        user_id=<caller user_id>,
    )

which schedules the notification and returns. Synchronous callers (none
currently) should ``await`` the returned task; the artifact tools use
fire-and-forget semantics.
"""

from __future__ import annotations
import asyncio
import os
import uuid
from typing import Any, Literal

from slack_sdk.errors import SlackApiError

from ypl.backend.config import settings
from ypl.backend.utils.async_utils import create_background_task
from ypl.db.agent_harness import AgentArtifact, AgentArtifactType
from ypl.slack_common import get_ops_bot_write_client
from ypl.structured_logger import get_logger

logger = get_logger()

ArtifactEvent = Literal["created", "updated", "new_version"]


# Environments where we never post artifact notifications — keeps developers'
# laptops + CI runs from spamming the production feed channel.
_NOTIFICATION_DISABLED_ENVIRONMENTS = frozenset({"local", "test", "selfhosted"})

# Maximum length of any single field we display, to keep messages compact and
# avoid Slack's 3000-char-per-section-block limit. Truncation is best-effort —
# the artifact viewer link in the header always points to the full content.
_MAX_TITLE_DISPLAY = 200
_MAX_DESCRIPTION_DISPLAY = 500
_MAX_SLUG_DISPLAY = 80


# Visual treatment per artifact type. The emoji shows up in the header line
# and the colour shows up as the attachment side-bar. Picked to be distinct at
# a glance: green for new code reviews, blue for prose, purple for memory,
# grey for everything else.
_TYPE_VISUALS: dict[AgentArtifactType, dict[str, str]] = {
    AgentArtifactType.TEXT: {"emoji": ":page_with_curl:", "color": "#1d9bf0"},
    AgentArtifactType.CODE_REVIEW: {"emoji": ":octocat:", "color": "#2eb886"},
    AgentArtifactType.OTHER: {"emoji": ":link:", "color": "#9aa0a6"},
    AgentArtifactType.MEMORY: {"emoji": ":brain:", "color": "#a371f7"},
    AgentArtifactType.SKILL: {"emoji": ":sparkles:", "color": "#f0a020"},
}

# Verb used in the header for each lifecycle event. Past tense reads cleanly
# in a feed (":page_with_curl: TEXT artifact *created* by ...").
_EVENT_VERBS: dict[ArtifactEvent, str] = {
    "created": "created",
    "updated": "updated",
    "new_version": "new version saved",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def notify_artifact_event(
    *,
    artifact: AgentArtifact,
    event: ArtifactEvent,
    agent_name: str | None = None,
    session_id: uuid.UUID | None = None,
    user_id: str | None = None,
) -> asyncio.Task[None] | None:
    """Schedule a Slack notification for an artifact create / update event.

    Returns the scheduled :class:`asyncio.Task` so callers that want to await
    the post (mostly tests) can do so. Production callers fire-and-forget.

    Returns ``None`` when notifications are disabled — either because the
    environment is local / test, or because ``ARTIFACT_NOTIFICATIONS_CHANNEL``
    is empty in config.
    """
    channel = _target_channel()
    if not channel:
        return None

    return create_background_task(
        _post_notification(
            channel=channel,
            artifact=artifact,
            event=event,
            agent_name=agent_name,
            session_id=session_id,
            user_id=user_id,
        )
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _target_channel() -> str | None:
    """Return the Slack channel ID to post to, or None to skip.

    Mirrors the local-environment guard in ``slack_utils.post_to_slack_channel_sync``
    — we treat artifact notifications as non-essential and never post on dev.
    """
    env = os.environ.get("ENVIRONMENT", settings.ENVIRONMENT)
    if env in _NOTIFICATION_DISABLED_ENVIRONMENTS:
        return None
    channel = (settings.ARTIFACT_NOTIFICATIONS_CHANNEL or "").strip()
    return channel or None


async def _post_notification(
    *,
    channel: str,
    artifact: AgentArtifact,
    event: ArtifactEvent,
    agent_name: str | None,
    session_id: uuid.UUID | None,
    user_id: str | None,
) -> None:
    """Build and post the Slack message. Logs and swallows any exception."""
    try:
        text, blocks, attachments = build_notification_payload(
            artifact=artifact,
            event=event,
            agent_name=agent_name,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception:
        # Never propagate formatting errors — the artifact already saved fine.
        logger.warning(
            "artifact_notifier: failed to build payload (skipping post)",
            artifact_id=str(getattr(artifact, "agent_artifact_id", None)),
            event_kind=event,
            exc_info=True,
        )
        return

    try:
        client = get_ops_bot_write_client()
    except Exception:
        # OpsBot token missing — likely a misconfigured environment. Don't
        # crash the artifact path; just log so ops can fix the env var.
        logger.warning(
            "artifact_notifier: OpsBot client unavailable (skipping post)",
            channel=channel,
            exc_info=True,
        )
        return

    try:
        response = await client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            attachments=attachments,
            unfurl_links=False,
            unfurl_media=False,
        )
        if not response.get("ok"):
            logger.warning(
                "artifact_notifier: chat.postMessage returned not-ok",
                channel=channel,
                error=response.get("error"),
                artifact_id=str(getattr(artifact, "agent_artifact_id", None)),
                event=event,
            )
    except SlackApiError as exc:
        logger.warning(
            "artifact_notifier: SlackApiError posting notification",
            channel=channel,
            error=str(exc),
            artifact_id=str(getattr(artifact, "agent_artifact_id", None)),
            event_kind=event,
        )
    except Exception:
        logger.warning(
            "artifact_notifier: unexpected error posting notification",
            channel=channel,
            artifact_id=str(getattr(artifact, "agent_artifact_id", None)),
            event_kind=event,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Payload builder (pure / deterministic — exercised directly by tests)
# ---------------------------------------------------------------------------


def build_notification_payload(
    *,
    artifact: AgentArtifact,
    event: ArtifactEvent,
    agent_name: str | None,
    session_id: uuid.UUID | None,
    user_id: str | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Build (fallback_text, blocks, attachments) for a chat.postMessage call.

    Pure function — does no I/O. Fully covered by unit tests.

    The Slack message is composed from a single attachment with a coloured
    side-bar (per-type colour) and three sections:

    1. Header — emoji + type + verb + (optional viewer link or pointer link).
    2. Field grid — the metadata fingerprint (id / slug / version / scope).
    3. Description (italic, optional) and footer with attribution.

    Memory artifacts redact the user-scope subject because it is a user_id —
    never echoed into a public channel. Their content body is also not
    included.
    """
    artifact_type = artifact.artifact_type
    visuals = _TYPE_VISUALS.get(artifact_type, _TYPE_VISUALS[AgentArtifactType.OTHER])
    emoji = visuals["emoji"]
    color = visuals["color"]
    verb = _EVENT_VERBS[event]
    type_label = artifact_type.value
    title = _truncate(artifact.title or "(untitled)", _MAX_TITLE_DISPLAY)

    # Header line — links the title to the artifact URL when one exists.
    title_link = _slack_link(artifact.url, title) if artifact.url else f"*{_mrkdwn_escape(title)}*"
    header_text = f"{emoji}  *{type_label}* artifact {verb} — {title_link}"

    # Field grid: 2-column Slack `fields` layout. Slack truncates each field
    # value to 2000 chars; we keep them under 200.
    fields: list[dict[str, str]] = []

    fields.append({"type": "mrkdwn", "text": f"*ID*\n`{artifact.agent_artifact_id}`"})

    if artifact.named_slug:
        slug = _truncate(artifact.named_slug, _MAX_SLUG_DISPLAY)
        version_suffix = f" · v{artifact.version}" if artifact.version is not None else ""
        fields.append({"type": "mrkdwn", "text": f"*Slug*\n`{slug}`{version_suffix}"})

    if artifact_type in (AgentArtifactType.MEMORY, AgentArtifactType.SKILL):
        scope_label = _format_memory_scope(artifact.memory_scope, artifact.memory_scope_subject)
        if scope_label:
            fields.append({"type": "mrkdwn", "text": f"*Scope*\n{scope_label}"})

    if artifact.url and artifact_type in (AgentArtifactType.CODE_REVIEW, AgentArtifactType.OTHER):
        # Pointer artifacts: the URL is the artifact, so surface it as its own
        # field — the user typically clicks it directly.
        fields.append({"type": "mrkdwn", "text": f"*Link*\n{_slack_link(artifact.url, 'Open')}"})

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        }
    ]

    if fields:
        blocks.append({"type": "section", "fields": fields})

    description = _truncate(artifact.description, _MAX_DESCRIPTION_DISPLAY) if artifact.description else None
    if description:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{_mrkdwn_escape(description)}_"},
            }
        )

    # Attribution footer — context block keeps it visually distinct.
    attribution_parts = _build_attribution(agent_name=agent_name, session_id=session_id, user_id=user_id)
    if attribution_parts:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(attribution_parts)}],
            }
        )

    # Wrap blocks in a coloured attachment so each artifact gets a thin
    # side-bar in the type's colour. The fallback ``text`` is used by Slack
    # when blocks aren't supported (push notifications, screen readers).
    attachments: list[dict[str, Any]] = [{"color": color, "blocks": blocks}]
    fallback_text = f"{type_label} artifact {verb}: {title}"
    return fallback_text, [], attachments


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _truncate(value: str, max_len: int) -> str:
    """Trim ``value`` to ``max_len`` chars with a single-char ellipsis."""
    if not value:
        return value
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _mrkdwn_escape(text: str) -> str:
    """Escape the three Slack mrkdwn metacharacters that affect link parsing."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_link(url: str | None, label: str) -> str:
    """Format a Slack mrkdwn link, falling back to bold-label when no URL."""
    if not url:
        return f"*{_mrkdwn_escape(label)}*"
    return f"<{url}|{_mrkdwn_escape(label)}>"


def _format_memory_scope(scope: str | None, subject: str | None) -> str | None:
    """Render a MEMORY artifact's scope, redacting user-scope subject (PII).

    Returns ``None`` when the scope tuple is empty (defensive — non-MEMORY
    artifacts shouldn't reach this code path, but null-safety is cheap).
    """
    if not scope:
        return None
    if scope == "topic":
        return "`topic` (shared)"
    if scope == "agent":
        # Agent name is public (already in the attribution line) — safe to echo.
        if subject:
            return f"`agent` (`{subject}`)"
        return "`agent`"
    if scope == "user":
        # Never echo the user_id — it's PII for a public-ish feed channel.
        return "`user` (private)"
    return f"`{scope}`"


def _build_attribution(
    *,
    agent_name: str | None,
    session_id: uuid.UUID | None,
    user_id: str | None,
) -> list[str]:
    """Build the small-print attribution line at the bottom of the card.

    ``user_id`` is intentionally accepted but not surfaced — user IDs are
    user-level identifiers and we treat the feed channel as semi-public. The
    parameter stays in the signature so callers can pass full caller context
    without forking the API if we ever need to gate user echoing on env.
    """
    del user_id  # kept for API symmetry; never echoed to the channel
    parts: list[str] = []
    if agent_name:
        parts.append(f":robot_face: agent *{agent_name}*")
    if session_id:
        parts.append(f":thread: session `{session_id}`")
    return parts
