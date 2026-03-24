"""Database model for Slack OAuth token storage.

Stores encrypted OAuth tokens for the Slack Agent Gateway, specifically for
Bot Father's App Configuration tokens. Tokens are encrypted at the application
layer before storage.

The encryption key is stored in GCP Secret Manager and loaded at startup.
Tokens are cached in Redis for fast access, with the DB serving as the
durable source of truth.
"""

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlmodel import Field

from ypl.db.base import BaseModel


class SlackOAuthTokenType(str, enum.Enum):
    """Type of OAuth token stored."""

    # Bot Father's App Configuration token for Slack Manifests API
    BOT_FATHER_APP_CONFIG = "BOT_FATHER_APP_CONFIG"


class SlackOAuthToken(BaseModel, table=True):
    """Encrypted OAuth token storage for Slack integrations.

    Stores refresh tokens and optionally access tokens for OAuth integrations.
    All token values are encrypted at the application layer before storage.

    Access tokens are typically cached in Redis and only stored in DB as a
    backup. Refresh tokens must be persisted durably since they rotate on use.
    """

    __tablename__ = "slack_oauth_tokens"

    # Primary key
    token_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)

    # Token type - allows for multiple OAuth integrations in the future
    token_type: SlackOAuthTokenType = Field(
        sa_column=sa.Column(
            sa.Enum(SlackOAuthTokenType),
            nullable=False,
            unique=True,  # Only one row per token type
            index=True,
        ),
    )

    # Encrypted token values (encrypted at application layer using Fernet)
    encrypted_refresh_token: str = Field(
        nullable=False,
        sa_type=sa.Text,
        description="Fernet-encrypted refresh token",
    )

    # Access token is optional - primarily cached in Redis
    encrypted_access_token: str | None = Field(
        default=None,
        sa_type=sa.Text,
        description="Fernet-encrypted access token (optional, primarily cached in Redis)",
    )

    # Access token expiry for cache invalidation
    access_token_expires_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        sa_type=sa.DateTime(timezone=True),
        description="When the access token expires (UTC)",
    )
