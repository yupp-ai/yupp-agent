"""Simplified User model for yupp-agent.

Only contains columns needed by agent services. The full User model with
demographics, payment, onboarding, etc. lives in yupp-mind.
"""

import enum

import sqlalchemy as sa
from sqlalchemy import Column, UniqueConstraint
from sqlmodel import Field

from ypl.db.base import BaseModel
from ypl.db.rbac import UserRoleAssociation as UserRole  # noqa: F401 — compat with yupp-mind imports


class UserStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"


class UserType(enum.Enum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class User(BaseModel, table=True):
    __tablename__ = "users"

    user_id: str = Field(primary_key=True, nullable=False, sa_type=sa.Text)
    name: str | None = Field(default=None, sa_type=sa.Text, index=True)
    email: str = Field(sa_column=Column("email", sa.Text, nullable=False, index=True))
    image: str | None = Field(default=None, sa_type=sa.Text)
    status: UserStatus = Field(
        default=UserStatus.ACTIVE,
        sa_column=Column(sa.Enum(UserStatus), nullable=False, server_default=UserStatus.ACTIVE.value),
    )
    user_type: UserType = Field(
        default=UserType.HUMAN,
        sa_column=Column(
            sa.Enum(UserType, name="usertype"),
            nullable=False,
            server_default=UserType.HUMAN.value,
        ),
    )

    # External identity mappings. All nullable — populated only for users
    # whose activity the platform needs to attribute across surfaces (GitHub
    # webhooks, Slack mentions, Linear sync). Uniqueness enforced by partial
    # indexes below (one row per external identity when the column is set).
    slack_user_id: str | None = Field(default=None, sa_type=sa.Text, index=True)
    github_username: str | None = Field(default=None, sa_type=sa.Text, index=True)
    linear_name: str | None = Field(default=None, sa_type=sa.Text, index=True)

    __table_args__ = (
        UniqueConstraint("email", name="users_email_key"),
        sa.Index("idx_users_lower_email", sa.text("lower(email)"), unique=True),
        sa.Index(
            "idx_users_slack_user_id_unique",
            "slack_user_id",
            unique=True,
            postgresql_where=sa.text("slack_user_id IS NOT NULL"),
        ),
        sa.Index(
            "idx_users_github_username_unique",
            "github_username",
            unique=True,
            postgresql_where=sa.text("github_username IS NOT NULL"),
        ),
    )
