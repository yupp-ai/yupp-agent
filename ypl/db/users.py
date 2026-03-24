"""Simplified User model for yupp-agent.

Only contains columns needed by agent services. The full User model with
demographics, payment, onboarding, etc. lives in yupp-mind.
"""

import enum

import sqlalchemy as sa
from sqlalchemy import Column, UniqueConstraint
from sqlmodel import Field

from ypl.db.base import BaseModel


class UserStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"


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

    __table_args__ = (
        UniqueConstraint("email", name="users_email_key"),
        sa.Index("idx_users_lower_email", sa.text("lower(email)"), unique=True),
    )
