"""Dormant models for artifact comment threads and comments.

Currently unused — kept in-tree so Alembic keeps the underlying tables.
If/when we rebuild comments on top of textual artifacts, start from
this shape. When reactivated, a follow-up migration should rename the
tables (``yuppaste_comment_threads`` → ``artifact_comment_threads``,
etc.) and update ``__tablename__`` + foreign-key references below to
match. For now the legacy table names are intentionally retained so
the model maps cleanly to what's already in Postgres.
"""

import enum
import uuid

import sqlalchemy as sa
from sqlmodel import Field

from ypl.db.base import BaseModel


class CommentThreadStatus(str, enum.Enum):
    """Status of a comment thread."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ArtifactCommentThread(BaseModel, table=True):
    """Comment thread anchored to a specific text selection in an artifact.

    For slug-based artifacts, threads carry over to new versions when the
    anchored text still exists. For UUID-based artifacts, threads are fixed
    to that immutable artifact version.
    """

    __tablename__ = "yuppaste_comment_threads"  # legacy name; rename in future migration

    thread_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)

    # Primary identifier — always present. Legacy column name retained
    # (``yuppaste_id``) so the model matches the actual DB schema.
    yuppaste_id: uuid.UUID = Field(
        nullable=False,
        index=True,
        description="UUID of the artifact this thread belongs to",
    )

    # Optional slug for slug-based artifacts (null for UUID-only).
    slug: str | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Text,
        index=True,
        description="Named slug of the artifact (null for UUID-only)",
    )

    # Version where the thread was created (null for UUID-only artifacts).
    paste_version: int | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Integer,
        description="Artifact version where this thread was created (null for UUID-only)",
    )

    # Anchor fields for text-based positioning across versions.
    anchor_text: str = Field(
        nullable=False,
        sa_type=sa.Text,
        description="The selected text that anchors this thread (max 500 chars)",
    )
    context_before: str | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Text,
        description="~50 chars before the selection for disambiguation",
    )
    context_after: str | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Text,
        description="~50 chars after the selection for disambiguation",
    )
    anchor_start_line: int | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Integer,
        description="Original start line number (for display/fallback)",
    )
    anchor_end_line: int | None = Field(
        default=None,
        nullable=True,
        sa_type=sa.Integer,
        description="Original end line number",
    )

    status: CommentThreadStatus = Field(
        default=CommentThreadStatus.OPEN,
        sa_column=sa.Column(
            sa.Enum(CommentThreadStatus, name="commentthreadstatus"),
            nullable=False,
            server_default="OPEN",
        ),
    )


class ArtifactComment(BaseModel, table=True):
    """Individual comment with optional 2-level nested reply support.

    Supports soft deletion via ``deleted_at`` (from ``BaseModel``) so the
    thread structure is preserved even after a comment is removed.
    """

    __tablename__ = "yuppaste_comments"  # legacy name; rename in future migration

    comment_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)

    thread_id: uuid.UUID = Field(
        foreign_key="yuppaste_comment_threads.thread_id",
        nullable=False,
        index=True,
        description="Thread this comment belongs to",
    )

    parent_comment_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="yuppaste_comments.comment_id",
        nullable=True,
        description="Parent comment for nested replies (null = top-level in thread, only 2-level nesting supported)",
    )

    author_email: str = Field(
        nullable=False,
        sa_type=sa.Text,
        index=True,
        description="Email of the comment author",
    )

    content: str = Field(
        nullable=False,
        sa_type=sa.Text,
        description="Comment content (markdown supported)",
    )
