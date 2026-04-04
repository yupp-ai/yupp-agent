"""Backfill agent user rows

Revision ID: b8c3d5e7f901
Revises: 37ba95f14212
Create Date: 2026-04-04 06:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c3d5e7f901"
down_revision: str | None = "37ba95f14212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Data backfill: create a user row for every existing agent so that
    # the new agent_user_id FK can be populated.  Safe to re-run because
    # the INSERT uses ON CONFLICT DO NOTHING.
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO users (user_id, name, email, user_type, status)
            SELECT
                agent_id::text,
                'agent:' || name,
                'agent-' || name || '@yupp.ai',
                'AGENT',
                'ACTIVE'
            FROM agents
            WHERE NOT EXISTS (
                SELECT 1 FROM users WHERE user_id = agents.agent_id::text
            )
            ON CONFLICT DO NOTHING
            """
        )
    )
    # Populate agent_user_id for any agent not yet linked to a user row.
    op.execute(sa.text("UPDATE agents SET agent_user_id = agent_id::text WHERE agent_user_id IS NULL"))


def downgrade() -> None:
    # Undo the backfill: clear agent_user_id and remove agent-created user rows.
    op.execute(sa.text("UPDATE agents SET agent_user_id = NULL"))
    op.execute(sa.text("DELETE FROM users WHERE user_type = 'AGENT'"))
