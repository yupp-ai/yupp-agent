"""add named_slug, version, content_type to agent_artifacts

Revision ID: 977d90cc0796
Revises: 37ba95f14212
Create Date: 2026-04-14 12:00:00.000000+00:00

Adds three nullable columns to agent_artifacts:
  - named_slug:   human-readable identifier for an artifact (e.g. "fix-auth-bug-pr")
  - version:      monotonically increasing integer version within a named_slug
  - content_type: MIME type of artifact content; allowed values are
                  text/plain, text/markdown, text/html

Also adds:
  - composite index   ix_agent_artifacts_named_slug_version on (named_slug, version)
  - unique constraint uq_agent_artifacts_slug_version       on (named_slug, version)
  - check  constraint ck_agent_artifacts_content_type enforcing allowed MIME types
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "977d90cc0796"
down_revision: str | None = "37ba95f14212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- new columns (all nullable; no default required) ---
    op.add_column("agent_artifacts", sa.Column("named_slug", sa.Text(), nullable=True))
    op.add_column("agent_artifacts", sa.Column("version", sa.Integer(), nullable=True))
    op.add_column("agent_artifacts", sa.Column("content_type", sa.Text(), nullable=True))

    # --- content_type domain check ---
    op.create_check_constraint(
        "ck_agent_artifacts_content_type",
        "agent_artifacts",
        "content_type IN ('text/plain', 'text/markdown', 'text/html')",
    )

    # --- composite index for efficient (slug, version) look-ups ---
    op.create_index(
        "ix_agent_artifacts_named_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
        unique=False,
    )

    # --- unique constraint: (slug, version) pairs must be distinct ---
    # NULL values are never considered equal in PostgreSQL, so rows where
    # either column is NULL do not conflict with each other.
    op.create_unique_constraint(
        "uq_agent_artifacts_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_artifacts_slug_version", "agent_artifacts", type_="unique")
    op.drop_index("ix_agent_artifacts_named_slug_version", table_name="agent_artifacts")
    op.drop_constraint("ck_agent_artifacts_content_type", "agent_artifacts", type_="check")
    op.drop_column("agent_artifacts", "content_type")
    op.drop_column("agent_artifacts", "version")
    op.drop_column("agent_artifacts", "named_slug")
