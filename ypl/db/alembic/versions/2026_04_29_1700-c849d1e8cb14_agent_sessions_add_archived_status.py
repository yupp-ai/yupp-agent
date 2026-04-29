"""agent_sessions: add ARCHIVED to agentsessionstatus enum

Adds a new ``ARCHIVED`` value to the ``agentsessionstatus`` enum so that
sessions can be manually archived from Slack (``/archive`` command) or
auto-archived after a long period of inactivity, without overloading
``COMPLETED`` (which is set by the agent when it finishes a turn).

Revision ID: c849d1e8cb14
Revises: b227eabd88f2
Create Date: 2026-04-29 17:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c849d1e8cb14"
down_revision: str | None = "b227eabd88f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres requires ADD VALUE to land in its own transaction before it can
    # be referenced in later DDL or queries, so commit around it.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentsessionstatus ADD VALUE IF NOT EXISTS 'ARCHIVED'"))
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    # Refuse to downgrade if any ARCHIVED rows still exist — the enum rebuild
    # below would fail to cast 'ARCHIVED' back to a type that doesn't include
    # it. Callers must update or delete those rows first.
    bind = op.get_bind()
    n_archived = bind.execute(sa.text("SELECT COUNT(*) FROM agent_sessions WHERE status = 'ARCHIVED'")).scalar_one()
    if n_archived:
        raise RuntimeError(
            f"Refusing to downgrade c849d1e8cb14: {n_archived} ARCHIVED session row(s) "
            "still exist. Update or delete them, then retry — the enum rebuild in this "
            "downgrade cannot cast 'ARCHIVED' back to a type that doesn't include it."
        )

    # Postgres can't drop an enum value directly; rebuild the type without
    # ARCHIVED. Safe because no rows can use ARCHIVED at this point (guard above).
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentsessionstatus RENAME TO agentsessionstatus_old"))
    op.execute(sa.text("CREATE TYPE agentsessionstatus AS ENUM ('ACTIVE', 'COMPLETED', 'STALE')"))
    op.execute(
        sa.text(
            "ALTER TABLE agent_sessions ALTER COLUMN status "
            "TYPE agentsessionstatus USING status::text::agentsessionstatus"
        )
    )
    op.execute(sa.text("DROP TYPE agentsessionstatus_old"))
    op.execute(sa.text("BEGIN"))
