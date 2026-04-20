"""slack_agents: add encrypted bot_token + signing_secret columns

Revision ID: 6a3f8c9b2e4d
Revises: 79205821473e
Create Date: 2026-04-20 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a3f8c9b2e4d"
down_revision: str | None = "79205821473e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("slack_agents", sa.Column("bot_token_encrypted", sa.Text(), nullable=True))
    op.add_column("slack_agents", sa.Column("signing_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("slack_agents", "signing_secret_encrypted")
    op.drop_column("slack_agents", "bot_token_encrypted")
