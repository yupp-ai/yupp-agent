"""add system user type

Revision ID: 04308c50cf51
Revises: c4f9a1b2d3e5
Create Date: 2026-04-01 00:50:29.879876+00:00

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '04308c50cf51'
down_revision: str | None = 'c4f9a1b2d3e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE usertype ADD VALUE IF NOT EXISTS 'SYSTEM'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; no-op.
    pass
