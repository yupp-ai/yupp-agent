"""Data models for Bot Father — automated Slack bot creation."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


def _utc_now() -> datetime:
    """Get current UTC time with timezone info."""
    return datetime.now(UTC)


class BotApprovalStatus(StrEnum):
    """Status of a bot creation request."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    AWAITING_INSTALLATION = "AWAITING_INSTALLATION"  # Slack app created, waiting for OAuth install
    DENIED = "DENIED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class BotCreationRequest(BaseModel):
    """Incoming request to create a Slack bot for an agent."""

    agent_name: str = Field(..., description="AHS agent name (e.g., 'sre', 'data-scientist')")
    slack_name: str = Field(
        ...,
        description="Desired Slack bot username (e.g., 'giladovski'). Must be lowercase, alphanumeric + hyphens.",
    )
    display_name: str = Field(..., description="Human-readable display name (e.g., 'Giladovski')")
    requested_by: str | None = Field(
        None,
        description=(
            "Slack user ID (e.g., 'U123ABC') or Yupp user ID (UUID). Optional if requested_by_email is provided."
        ),
    )
    requested_by_email: str | None = Field(None, description="Email of the requester")
    description: str | None = Field(None, description="Description of what the agent does")
    system_prompt: str | None = Field(None, description="Additional system prompt or persona for the agent")

    @model_validator(mode="after")
    def validate_requester_identity(self) -> "BotCreationRequest":
        """Ensure at least one of requested_by or requested_by_email is provided."""
        if not self.requested_by and not self.requested_by_email:
            raise ValueError("At least one of 'requested_by' or 'requested_by_email' must be provided")
        return self


class BotCreationRecord(BaseModel):
    """Full state of a bot creation request, stored in Redis."""

    request_id: str = Field(..., description="Unique request ID (UUID)")
    agent_name: str = Field(..., description="AHS agent name")
    slack_name: str = Field(..., description="Desired Slack bot username")
    display_name: str = Field(..., description="Human-readable display name")
    requested_by: str | None = Field(
        None,
        description="Slack user ID (e.g., 'U123ABC') or Yupp user ID (UUID)",
    )
    requested_by_email: str | None = Field(None, description="Email of the requester")
    # Resolved Yupp user ID (set during request processing)
    requested_by_user_id: str | None = Field(None, description="Resolved Yupp user ID of the requester")
    description: str | None = Field(None, description="Description of what the agent does")
    system_prompt: str | None = Field(None, description="Additional system prompt or persona for the agent")

    status: BotApprovalStatus = Field(default=BotApprovalStatus.PENDING, description="Current status")
    created_at: datetime = Field(default_factory=_utc_now, description="When the request was created")
    updated_at: datetime = Field(default_factory=_utc_now, description="When the record was last updated")

    # Set after approval
    approver_slack_user_id: str | None = Field(None, description="Slack user ID of the approver/denier")
    approved_at: datetime | None = Field(None, description="When the request was approved")

    # Set after Slack app creation (Phase 1)
    slack_app_id: str | None = Field(None, description="Created Slack app ID (e.g., 'A123BOTFATHER')")
    oauth_client_id: str | None = Field(None, description="OAuth client ID for the app")
    oauth_client_secret: str | None = Field(None, description="OAuth client secret (encrypted or redacted in logs)")
    oauth_install_url: str | None = Field(None, description="URL for admin to click to complete OAuth install")
    signing_secret_encrypted: str | None = Field(
        None,
        description=(
            "Fernet-encrypted Slack app signing secret. Captured when the app is created, "
            "then written onto the slack_agents row alongside the bot token after OAuth completes."
        ),
    )

    # Set after OAuth completion (Phase 2)
    completed_at: datetime | None = Field(None, description="When the bot was fully created")

    # Tracking for Slack message updates
    approval_message_ts: str | None = Field(None, description="Slack message timestamp of the approval request")
    approval_message_channel: str | None = Field(None, description="Slack channel the approval message was posted to")

    # Error info for FAILED status
    error: str | None = Field(None, description="Error message if status is FAILED")


class BotCreationResponse(BaseModel):
    """API response for POST /bot-father/request."""

    request_id: str = Field(..., description="Created request ID")
    status: BotApprovalStatus = Field(..., description="Initial status (always PENDING)")
    message: str = Field(..., description="Human-readable status message")


class BotStatusResponse(BaseModel):
    """API response for GET /bot-father/status/{agent_name}."""

    agent_name: str = Field(..., description="AHS agent name")
    has_slack_bot: bool = Field(..., description="Whether the agent already has a live Slack bot")
    pending_request: BotCreationRecord | None = Field(None, description="Pending/recent creation request, if any")
    slack_app_id: str | None = Field(None, description="Slack app ID if bot exists")
    slack_name: str | None = Field(None, description="Slack bot username if bot exists")
