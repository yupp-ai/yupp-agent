"""Database models for Slack Agent Gateway agents.

The SlackAgent table stores the list of registered Slack bots (agents) that can
receive messages through the Slack Agent Gateway. Secrets (bot_token, signing_secret)
remain in GCP Secret Manager for security.
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
    3. Identify which bot to use for replies (bot_name for GCP secrets)

    Secrets (bot_token, signing_secret) are stored in GCP Secret Manager,
    not in this table, following the naming convention:
        ym-slack-agent-gateway-{bot_name}-{secret_type}-{environment}
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
        description="AHS agent name (e.g., 'sre', 'data-scientist')",
    )
    bot_name: str = Field(
        nullable=False,
        sa_type=sa.Text,
        index=True,
        description="Bot name used for GCP secret lookup (e.g., 'giladovski')",
    )
    display_name: str = Field(
        nullable=False,
        sa_type=sa.Text,
        description="Human-readable display name (e.g., 'Giladovski')",
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
        description="Yupp user ID of the creator",
    )

    # Bot Father request tracking
    bot_creation_request_id: str | None = Field(
        default=None,
        sa_type=sa.Text,
        description="Bot Father request ID that created this agent",
    )
