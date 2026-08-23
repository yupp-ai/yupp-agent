"""mcp_servers: add auth_header for non-Bearer credentials

Some MCP providers authenticate on a bespoke header rather than
``Authorization: Bearer`` — arti wants ``X-Arti-Service-Secret``, plenty of
internal services want a plain ``X-API-Key``.  NULL keeps the existing
RFC 6750 behavior, so every registered server is unaffected.

Revision ID: c4a7b91d3e08
Revises: 9f3c1e8a4d52
Create Date: 2026-08-22 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a7b91d3e08"
down_revision: str | None = "9f3c1e8a4d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("auth_header", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "auth_header")
