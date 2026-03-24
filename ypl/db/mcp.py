"""Models for MCP server developer tokens and audit logs."""

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Column, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ypl.db.base import BaseModel


class MCPTokenStatus(enum.Enum):
    """Status of an MCP developer token."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class MCPAuditLogStatus(enum.Enum):
    """Status of an MCP audit log entry."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class MCPTokenType(enum.Enum):
    """Type of token used for MCP authentication."""

    DEV_TOKEN = "DEV_TOKEN"  # Token created via CLI for developer access
    OAUTH = "OAUTH"  # Token from OAuth flow (e.g., Google OAuth)


class MCPDevToken(BaseModel, table=True):
    """Developer tokens for MCP server authentication.

    Each token represents a specific engineer's access to the MCP server.
    Tokens are hashed using bcrypt for security.
    """

    __tablename__ = "mcp_dev_tokens"

    mcp_dev_token_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Lookup key for O(1) database lookup (first 4 + last 4 chars of token suffix)
    token_lookup_key: str = Field(max_length=8, nullable=False, index=True)
    token_hash: str = Field(sa_column=Column(Text, nullable=False, unique=True, index=True))
    email: str = Field(max_length=255, nullable=False, index=True)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Timestamps (timezone-aware to match BaseModel convention)
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )

    # Status
    status: MCPTokenStatus = Field(
        default=MCPTokenStatus.ACTIVE,
        sa_column=Column(
            sa.Enum(MCPTokenStatus),
            nullable=False,
            server_default=MCPTokenStatus.ACTIVE.value,
        ),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True),
    )
    revoked_by: str | None = Field(default=None, max_length=255)
    revoked_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Relationships
    mcp_audit_logs: list["MCPAuditLog"] = Relationship(back_populates="mcp_dev_token")

    __table_args__ = (Index("ix_mcp_dev_tokens_email_status", "email", "status"),)


class MCPAuditLog(BaseModel, table=True):
    """Audit log for all MCP tool invocations.

    Records every tool call made through the MCP server for compliance
    and debugging purposes. Includes who made the call, what they called,
    and the result.
    """

    __tablename__ = "mcp_audit_logs"

    mcp_audit_log_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Who - supports both DevToken and OAuth authentication
    # For DevToken: mcp_dev_token_id is set, email comes from the token
    # For OAuth: mcp_dev_token_id is None, email comes from OAuth claims
    mcp_dev_token_id: uuid.UUID | None = Field(
        default=None, foreign_key="mcp_dev_tokens.mcp_dev_token_id", nullable=True, index=True
    )
    email: str | None = Field(default=None, max_length=255, index=True)  # User email (from token or OAuth)
    callback_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))  # OAuth callback origin
    token_type: MCPTokenType = Field(
        default=MCPTokenType.DEV_TOKEN,
        sa_column=Column(
            sa.Enum(MCPTokenType),
            nullable=False,
            server_default=MCPTokenType.DEV_TOKEN.value,
        ),
    )

    # What
    tool_name: str = Field(max_length=255, nullable=False, index=True)
    tool_parameters: dict = Field(default_factory=dict, sa_column=Column("tool_parameters", JSONB, nullable=False))

    # Result
    status: MCPAuditLogStatus = Field(
        sa_column=Column(
            sa.Enum(MCPAuditLogStatus),
            nullable=False,
            index=True,
        ),
    )
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    result_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))  # Brief summary of results

    # Context
    ip_address: str | None = Field(default=None, max_length=45)  # IPv6 max length
    user_agent: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    session_id: str | None = Field(default=None, max_length=255)

    # Performance
    execution_time_ms: int | None = Field(default=None)

    # Relationships
    mcp_dev_token: MCPDevToken | None = Relationship(back_populates="mcp_audit_logs")

    __table_args__ = (
        Index("ix_mcp_audit_logs_created_at", "created_at"),
        Index("ix_mcp_audit_logs_tool_name_created_at", "tool_name", "created_at"),
    )
