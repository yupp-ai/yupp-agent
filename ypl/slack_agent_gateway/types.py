"""Data models for Slack Agent Gateway."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    """Get current UTC time with timezone info."""
    return datetime.now(UTC)


class SessionStatus(StrEnum):
    """Status of an agent session."""

    ACTIVE = "active"
    EXPIRED = "expired"


class MessageSender(BaseModel):
    """Sender information for a message."""

    slack_user_id: str = Field(..., description="Slack user ID (e.g., 'U123ABC')")
    username: str | None = Field(None, description="Slack username")
    display_name: str | None = Field(None, description="User's display name")


class Attachment(BaseModel):
    """A file attachment uploaded to the configured blob store."""

    filename: str = Field(..., description="Sanitized filename")
    content_type: str = Field(..., description="MIME type (e.g., 'image/png')")
    size: int = Field(..., description="File size in bytes")
    blob_path: str = Field(
        ...,
        description="Logical blob-store path (e.g., 'attachments/{session_id}/file.png'). "
        "Resolved against the configured BlobStore backend at read time.",
    )


class Message(BaseModel):
    """A message sent to the Agent Service."""

    text: str = Field(..., description="Message content")
    sender: MessageSender = Field(..., description="Who sent the message")
    ts: str = Field(..., description="Message timestamp from Slack")
    attachments: list[Attachment] = Field(
        default_factory=list,
        description="File attachments uploaded to the configured blob store",
    )


class AgentAppConfig(BaseModel):
    """Configuration for a Slack agent app."""

    app_id: str = Field(..., description="Slack app ID (e.g., 'A123CONFUCIUS')")
    agent_name: str = Field(..., description="AHS agent name (e.g., 'sre', 'data-scientist')")
    slack_name: str = Field(..., description="Slack bot name (e.g., 'giladovski', 'tianfucius')")
    bot_token: str = Field(..., description="Slack bot token (xoxb-...)")
    signing_secret: str = Field(..., description="Slack signing secret")
    display_name: str = Field(..., description="Human-readable name (e.g., 'Giladovski')")


class AgentSession(BaseModel):
    """Tracks a conversation session between Slack and an agent.

    Session ID format: {channel_id}:{thread_ts}:{app_id}
    - Uses app_id (stable) instead of agent_name (can be renamed)
    - thread_ts is normalized: event.thread_ts ?? event.ts (top-level mentions have no thread_ts)
    - Delimiter ':' is safe since channel_id, thread_ts, and app_id don't contain colons
    """

    session_id: str = Field(..., description="Format: {channel_id}:{thread_ts}:{app_id}")

    # Slack context
    channel_id: str = Field(..., description="Slack channel ID")
    channel_name: str | None = Field(None, description="Human-readable channel name")
    thread_ts: str = Field(..., description="Thread timestamp (normalized from event.thread_ts or event.ts)")
    creator_slack_user_id: str = Field(..., description="Slack user ID who created the session")
    creator_slack_username: str | None = Field(None, description="Username of session creator")
    app_id: str = Field(..., description="Which Slack app received this session")

    # Agent context
    agent_name: str = Field(..., description="Internal agent name for routing")

    # Reply tracking
    last_reply_ts: str | None = Field(None, description="Timestamp of the last reply message")
    last_reply_content: str | None = Field(None, description="Content of the last reply")
    last_reply_type: str | None = Field(None, description="Content type of the last reply (e.g. 'thinking')")
    placeholder_ts: str | None = Field(None, description="Timestamp of the 'Thinking...' placeholder message")
    status_message_ts: str | None = Field(
        None,
        description="Timestamp of the current status context block (tool-use hints). "
        "Edited in-place for each status update; cleared when a real reply arrives.",
    )

    # Presentation preferences (toggled by /verbose and /quiet slash commands)
    show_tool_calls: bool = Field(
        default=True,
        description=(
            "Whether tool-call start/result events should render as the live status "
            "cluster in this thread. Default True (verbose). Flipped to False by "
            "/quiet, back to True by /verbose. Purely a presentation flag — the "
            "underlying agent behaviour is unchanged."
        ),
    )

    # State - soft expiration model
    status: SessionStatus = Field(default=SessionStatus.ACTIVE, description="Derived from expires_at")
    created_at: datetime = Field(default_factory=_utc_now, description="When session was created")
    last_activity_at: datetime = Field(default_factory=_utc_now, description="Last activity timestamp")
    expires_at: datetime = Field(..., description="When session expires (soft expiration)")

    @classmethod
    def build_session_id(cls, channel_id: str, thread_ts: str, app_id: str) -> str:
        """Build a session ID from its components."""
        return f"{channel_id}:{thread_ts}:{app_id}"

    @classmethod
    def parse_session_id(cls, session_id: str) -> tuple[str, str, str]:
        """Parse a session ID into (channel_id, thread_ts, app_id).

        Raises:
            ValueError: If session_id format is invalid.
        """
        parts = session_id.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid session_id format: {session_id}")
        return parts[0], parts[1], parts[2]

    def is_expired(self) -> bool:
        """Check if the session is expired based on expires_at."""
        return datetime.now(UTC) > self.expires_at


class SlackSessionInfoResponse(BaseModel):
    """Response for POST /sessions/info."""

    session_id: str = Field(..., description="Session identifier")
    channel_id: str = Field(..., description="Slack channel ID")
    channel_name: str | None = Field(None, description="Human-readable channel name")
    thread_ts: str = Field(..., description="Thread timestamp")
    creator_slack_user_id: str = Field(..., description="Who created the session")
    creator_slack_username: str | None = Field(None, description="Creator's username")
    agent_name: str = Field(..., description="Internal agent name")
    status: SessionStatus = Field(..., description="Session status")
    created_at: datetime = Field(..., description="When session was created")
    last_activity_at: datetime = Field(..., description="Last activity timestamp")


# Request/Response models for Callback APIs


class GetSessionInfoRequest(BaseModel):
    """Request body for POST /sessions/info."""

    session_id: str = Field(..., description="Session identifier")


class AddReplyRequest(BaseModel):
    """Request body for POST /sessions/reply."""

    session_id: str = Field(..., description="Session identifier")
    text: str = Field(..., description="Reply message content")
    reply_type: str | None = Field(
        None,
        description="Content type hint (e.g. 'thinking', 'tool_use'). "
        "None means regular text. Gateway decides how to render each type.",
    )
    username: str | None = Field(
        None,
        description="Override bot display name for this message (requires chat:write.customize scope).",
    )


class AddReplyResponse(BaseModel):
    """Response for POST /sessions/reply."""

    success: bool = Field(..., description="Whether the reply was posted")
    message_ts: str | None = Field(None, description="Slack message timestamp of the new reply")
    error: str | None = Field(None, description="Error message if failed")


class AppendToReplyRequest(BaseModel):
    """Request body for POST /sessions/reply/append."""

    session_id: str = Field(..., description="Session identifier")
    text: str = Field(..., description="Text to append to the last reply")
    reply_type: str | None = Field(
        None,
        description="Content type hint (e.g. 'thinking', 'tool_use'). "
        "Must match the type of the message being appended to. "
        "If it differs, the buffer is flushed and a new message is started.",
    )
    username: str | None = Field(
        None,
        description="Override bot display name for this message (requires chat:write.customize scope).",
    )


class AppendToReplyResponse(BaseModel):
    """Response for POST /sessions/reply/append."""

    success: bool = Field(..., description="Whether the append was buffered")
    buffered: bool = Field(default=True, description="Whether text was buffered (vs immediately flushed)")
    error: str | None = Field(None, description="Error message if failed")


class UpdateReplyRequest(BaseModel):
    """Request body for POST /sessions/reply/update."""

    session_id: str = Field(..., description="Session identifier")
    text: str = Field(..., description="New content to replace the last reply")


class UpdateReplyResponse(BaseModel):
    """Response for POST /sessions/reply/update."""

    success: bool = Field(..., description="Whether the reply was updated")
    message_ts: str | None = Field(None, description="Slack message timestamp of the updated reply")
    error: str | None = Field(None, description="Error message if failed")


class TurnEndRequest(BaseModel):
    """Request body for POST /sessions/turn-end (called by AHS at turn boundary)."""

    session_id: str = Field(..., description="Session identifier")


class TurnEndResponse(BaseModel):
    """Response for POST /sessions/turn-end."""

    success: bool = Field(..., description="Whether the cleanup ran (False only if session is unknown)")
    error: str | None = Field(None, description="Error message if failed")


class RequestFeedbackRequest(BaseModel):
    """Request body for POST /sessions/request-feedback (called by AHS)."""

    session_id: str = Field(..., description="Session identifier")
    prompt: str | None = Field(None, description="Optional custom survey prompt text")


class RequestFeedbackResponse(BaseModel):
    """Response for POST /sessions/request-feedback."""

    success: bool
    message_ts: str | None = None
    error: str | None = None


# Proactive message sending (no session required)


class SendMessageRequest(BaseModel):
    """Request body for POST /messages/send.

    Allows an agent to proactively send a message to a Slack channel.
    Does not require an existing session.
    """

    agent_name: str = Field(..., description="Internal agent name (e.g., 'sre', 'data-scientist')")
    channel: str = Field(..., description="Slack channel name or ID (e.g., 'alert-backend', '#general', or 'C123ABC')")
    text: str = Field(..., description="Message content")
    thread_ts: str | None = Field(None, description="Optional thread timestamp to reply in a thread")
    ahs_session_id: str | None = Field(
        None,
        description="Optional AHS session UUID. When provided for a top-level message, "
        "SAG registers the new thread so that human replies route to this existing session.",
    )
    username: str | None = Field(
        None,
        description="Override bot display name for this message (requires chat:write.customize scope).",
    )
    unfurl_links: bool = Field(
        True,
        description="Whether Slack should auto-unfurl plain-text / HTML links in the message. "
        "Default True (Slack's own default for user messages). Set False to suppress link previews.",
    )
    unfurl_media: bool = Field(
        True,
        description="Whether Slack should auto-unfurl media links (images, videos). "
        "Default True. Set False to suppress media previews.",
    )


class SendMessageResponse(BaseModel):
    """Response for POST /messages/send."""

    success: bool = Field(..., description="Whether the message was sent")
    message_ts: str | None = Field(None, description="Slack message timestamp of the sent message")
    channel: str | None = Field(None, description="Channel the message was sent to")
    error: str | None = Field(None, description="Error message if failed")


# Tool use events (structured, replaces plain-text status updates)


class ToolResultStatus(StrEnum):
    """Result status of a single tool call."""

    RUNNING = "running"  # call in-flight, no result yet
    DONE = "done"  # succeeded with non-empty output
    EMPTY = "empty"  # succeeded but returned no output
    FAILED = "failed"  # errored


class ToolUseEntry(BaseModel):
    """One tool invocation tracked for the live tool-cluster display."""

    tool_use_id: str = Field(..., description="Unique tool-call identifier (from Anthropic event id field)")
    name: str = Field(..., description="Tool name, e.g. 'Bash', 'Grep', 'Read'")
    command: str = Field(..., description="Formatted command string shown in the cluster block (max 200 chars)")
    result_status: ToolResultStatus = Field(default=ToolResultStatus.RUNNING, description="Current result status")
    error_msg: str | None = Field(None, description="Short error message (FAILED only, max 50 chars)")
    result_content: str | None = Field(None, description="First line of tool output (DONE only, max 150 chars)")


class ToolEventKind(StrEnum):
    """Discriminates tool-start vs tool-result events."""

    START = "start"
    RESULT = "result"


class SendToolEventRequest(BaseModel):
    """Request body for POST /sessions/tool (called by AHS).

    AHS sends a START event when a tool call begins (name + command known) and
    a RESULT event when the result arrives (result_status + optional error_msg).
    SAG accumulates entries per session, renders the last 3 in a live context-block
    message edited in-place, and replaces it with a summary when text output starts.
    """

    session_id: str = Field(..., description="Session identifier")
    kind: ToolEventKind = Field(..., description="'start' or 'result'")
    tool_use_id: str = Field(..., description="Unique identifier correlating start ↔ result")

    # START fields
    name: str | None = Field(None, description="Tool name (required for 'start' events)")
    command: str | None = Field(None, description="Formatted command string (required for 'start' events)")

    # RESULT fields
    result_status: Literal["done", "empty", "failed"] | None = Field(
        None, description="Result status (required for 'result' events)"
    )
    error_msg: str | None = Field(None, description="Short error text (FAILED only, max 50 chars)")
    result_content: str | None = Field(None, description="First line of tool output (DONE only, max 150 chars)")


class SendToolEventResponse(BaseModel):
    """Response for POST /sessions/tool."""

    success: bool = Field(..., description="Whether the event was accepted")
    message_ts: str | None = Field(None, description="Slack ts of the tool-cluster message (may be None if deferred)")
    error: str | None = Field(None, description="Error message if failed")


# Questionnaire (interactive multiple-choice prompt)


class QuestionChoice(BaseModel):
    """A single selectable choice in a questionnaire question."""

    label: str = Field(..., description="Button label shown to the user in Slack")
    value: str = Field(..., description="Opaque value returned to the caller (may differ from label)")


class SendQuestionnaireRequest(BaseModel):
    """Request body for POST /sessions/questionnaire (called by AHS).

    Posts a multiple-choice question to the Slack thread with action buttons.
    The user's selection (or a typed reply) is forwarded back to AHS as a
    regular session message so the agent can continue its turn.
    """

    session_id: str = Field(..., description="Session identifier")
    question_id: str = Field(
        ...,
        description=(
            "Caller-assigned identifier for this question (alphanumeric + underscores). "
            "Embedded in Slack action_id so the interaction handler can route the response."
        ),
    )
    text: str = Field(..., description="Question text shown to the user")
    choices: list[QuestionChoice] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="Choices rendered as Slack action buttons (Slack allows up to 5 per actions block)",
    )
    allow_free_text: bool = Field(
        default=True,
        description="If True, a hint is shown telling the user they can type a free-text reply instead",
    )


class SendQuestionnaireResponse(BaseModel):
    """Response for POST /sessions/questionnaire."""

    success: bool
    message_ts: str | None = None
    error: str | None = None
