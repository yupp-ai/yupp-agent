"""Data models for Slack Agent Gateway."""

from datetime import UTC, datetime
from enum import StrEnum

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
    """A file attachment uploaded to GCS for agent processing."""

    filename: str = Field(..., description="Sanitized filename")
    content_type: str = Field(..., description="MIME type (e.g., 'image/png')")
    size: int = Field(..., description="File size in bytes")
    gcs_url: str = Field(..., description="GCS URL (e.g., gs://yupp-agents/attachments/...)")


class Message(BaseModel):
    """A message sent to the Agent Service."""

    text: str = Field(..., description="Message content")
    sender: MessageSender = Field(..., description="Who sent the message")
    ts: str = Field(..., description="Message timestamp from Slack")
    attachments: list[Attachment] = Field(default_factory=list, description="File attachments uploaded to GCS")


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


class SendMessageResponse(BaseModel):
    """Response for POST /messages/send."""

    success: bool = Field(..., description="Whether the message was sent")
    message_ts: str | None = Field(None, description="Slack message timestamp of the sent message")
    channel: str | None = Field(None, description="Channel the message was sent to")
    error: str | None = Field(None, description="Error message if failed")


# Status update (live tool-use hints)


class SendStatusUpdateRequest(BaseModel):
    """Request body for POST /sessions/status (called by AHS).

    AHS sends incremental status updates during agent execution — e.g. which
    tools were used so far.  SAG renders these as a muted context block that is
    edited in-place; no new message is created until AHS sends a real reply.
    Rate-limited to at most one Slack API call per STATUS_RATELIMIT_SECONDS.
    """

    session_id: str = Field(..., description="Session identifier")
    text: str = Field(
        ...,
        description="Short status line, e.g. '🔧 5 tools used: Bash, Grep, Read'. Truncated to 3000 chars if longer.",
    )


class SendStatusUpdateResponse(BaseModel):
    """Response for POST /sessions/status."""

    success: bool = Field(..., description="Whether the update was accepted")
    message_ts: str | None = Field(None, description="Slack ts of the status context block (may be None if deferred)")
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
