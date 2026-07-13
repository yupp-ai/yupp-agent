"""Database models for Slack Agent Gateway agents.

The SlackAgent table stores the list of registered Slack bots (agents) that can
receive messages through the Slack Agent Gateway. Per-agent secrets (bot_token,
signing_secret) are stored encrypted on the row itself, keyed off
``SLACK_AGENT_GW_ENCRYPTION_KEY``. For GCP-free / self-hosted deployments, the
env-var fallback (``SLACK_AGENT_GATEWAY_<BOT>_BOT_TOKEN`` etc.) still works for
agents where the encrypted columns are null — handy for importing bots not
provisioned via BotFather.
"""

import enum
import uuid

import sqlalchemy as sa
from sqlmodel import Field

from ypl.db.base import BaseModel


class SlackAgentStatus(str, enum.Enum):
    """Status of a Slack agent."""

    ACTIVE = "ACTIVE"  # Bot is active and receiving messages
    DISABLED = "DISABLED"  # Bot is disabled (ignored by gateway)
    PENDING_APPROVAL = "PENDING_APPROVAL"  # Bot creation pending approval (Bot Father)


class SlackAgent(BaseModel, table=True):
    """A registered Slack bot for the Slack Agent Gateway.

    Each row represents a Slack app connected to an AHS agent. The gateway
    uses this table to:
    1. Verify incoming webhooks (lookup by app_id)
    2. Route messages to the correct AHS agent (agent_name)
    3. Reply via the bot's OAuth token (bot_token_encrypted)

    Secrets are encrypted at rest with ``SLACK_AGENT_GW_ENCRYPTION_KEY`` via
    ``ypl.slack_agent_gateway.crypto``. For rows imported from elsewhere (not
    provisioned via BotFather), the encrypted columns can be null and the
    gateway falls back to env vars keyed off ``bot_name``.
    """

    __tablename__ = "slack_agents"

    # Primary key
    slack_agent_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)

    # Core identifiers
    app_id: str = Field(
        nullable=False,
        sa_type=sa.Text,
        unique=True,
        index=True,
        description="Slack app ID (e.g., 'A123CONFUCIUS')",
    )
    agent_name: str = Field(
        nullable=False,
        sa_type=sa.Text,
        index=True,
        description="AHS agent name (e.g., 'sre', 'code-reviewer')",
    )
    bot_name: str = Field(
        nullable=False,
        sa_type=sa.Text,
        index=True,
        description="Bot name — also the env-var fallback key when the encrypted columns are null",
    )
    display_name: str = Field(
        nullable=False,
        sa_type=sa.Text,
        description="Human-readable display name (e.g., 'Giladovski')",
    )

    # Encrypted per-agent secrets. Nullable so imported rows (no encrypted
    # payload) can fall back to env vars via ``fetch_agent_secret``.
    bot_token_encrypted: str | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Text,
        description="Fernet-encrypted bot user OAuth token (xoxb-...)",
    )
    signing_secret_encrypted: str | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Text,
        description="Fernet-encrypted Slack app signing secret",
    )

    # Status
    status: SlackAgentStatus = Field(
        default=SlackAgentStatus.ACTIVE,
        sa_column=sa.Column(
            sa.Enum(SlackAgentStatus),
            nullable=False,
            server_default=SlackAgentStatus.ACTIVE.name,
        ),
    )

    # Audit fields
    created_by_user_id: str | None = Field(
        default=None,
        sa_type=sa.Text,
        description="User ID of the creator",
    )

    # Bot Father request tracking
    bot_creation_request_id: str | None = Field(
        default=None,
        sa_type=sa.Text,
        description="Bot Father request ID that created this agent",
    )
