"""agent_sessions: add forked_from_session_id

Adds a nullable self-FK on ``agent_sessions`` so a forked session can point
back at the session it was snapshotted from. Distinct from
``parent_session_id`` (which is used by subagent spawning via ``new_task``):
forks are *peer* sessions that evolve independently, not subagents, so we
keep the two edges separate to preserve their different semantics
(parent/child for cost cascade + stop signals; forked_from for lineage only).

Revision ID: a1c0fe19f0c5
Revises: b227eabd88f2
Create Date: 2026-05-20 09:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c0fe19f0c5"
down_revision: str | None = "b227eabd88f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("forked_from_session_id", sa.Uuid(), nullable=True),
    )
    # ondelete=SET NULL: the fork is a peer session and survives the source's deletion.
    op.create_foreign_key(
        "fk_agent_sessions_forked_from_session_id",
        "agent_sessions",
        "agent_sessions",
        ["forked_from_session_id"],
        ["agent_session_id"],
        ondelete="SET NULL",
    )
    # Partial index — the column is NULL for the vast majority of rows; the only
    # access pattern is "find forks of session X", which still hits the index.
    op.create_index(
        "ix_agent_sessions_forked_from_session_id",
        "agent_sessions",
        ["forked_from_session_id"],
        unique=False,
        postgresql_where=sa.text("forked_from_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_forked_from_session_id", table_name="agent_sessions")
    op.drop_constraint("fk_agent_sessions_forked_from_session_id", "agent_sessions", type_="foreignkey")
    op.drop_column("agent_sessions", "forked_from_session_id")
