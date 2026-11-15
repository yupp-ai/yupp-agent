"""slack_agents: add reply_in_thread_without_mention

Per-bot opt-in: when true, the Slack Agent Gateway answers plain thread
replies (no @mention) in threads where the bot already has a session.

Revision ID: c3d9e2a71b4f
Revises: c4a7b91d3e08
Create Date: 2026-11-15 09:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d9e2a71b4f"
down_revision: str | None = "c4a7b91d3e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "slack_agents",
        sa.Column("reply_in_thread_without_mention", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("slack_agents", "reply_in_thread_without_mention")
