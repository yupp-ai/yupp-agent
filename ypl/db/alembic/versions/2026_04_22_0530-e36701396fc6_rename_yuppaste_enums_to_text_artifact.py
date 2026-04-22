"""rename yuppaste enum labels to TEXT / ARTIFACT_*

Renames four Postgres enum labels:
  agentartifacttype.YUPPASTE       → TEXT
  permission_enum.READ_YUPPASTE    → READ_ARTIFACT
  permission_enum.WRITE_YUPPASTE   → WRITE_ARTIFACT
  role_name_enum.YUPPASTE_USER     → ARTIFACT_USER

All four are metadata-only: Postgres ``ALTER TYPE … RENAME VALUE``
updates the catalogue label in place. Existing rows that reference
these values stay consistent because enum identity is by OID, not
label — a row currently storing ``YUPPASTE_USER`` automatically shows
``ARTIFACT_USER`` after the rename, no UPDATE required.

The artifact TYPE stays specific (``TEXT`` = textual content), but the
role + the permissions that gate artifact CRUD are generic
(``ARTIFACT_USER``, ``READ_ARTIFACT`` / ``WRITE_ARTIFACT``) since they
apply to any artifact type, not only the textual one.

Revision ID: e36701396fc6
Revises: 4cb43319077c
Create Date: 2026-04-22 05:30:00.000000+00:00

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e36701396fc6"
down_revision: str | None = "4cb43319077c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE agentartifacttype RENAME VALUE 'YUPPASTE' TO 'TEXT'")
    op.execute("ALTER TYPE permission_enum RENAME VALUE 'READ_YUPPASTE' TO 'READ_ARTIFACT'")
    op.execute("ALTER TYPE permission_enum RENAME VALUE 'WRITE_YUPPASTE' TO 'WRITE_ARTIFACT'")
    op.execute("ALTER TYPE role_name_enum RENAME VALUE 'YUPPASTE_USER' TO 'ARTIFACT_USER'")


def downgrade() -> None:
    op.execute("ALTER TYPE role_name_enum RENAME VALUE 'ARTIFACT_USER' TO 'YUPPASTE_USER'")
    op.execute("ALTER TYPE permission_enum RENAME VALUE 'WRITE_ARTIFACT' TO 'WRITE_YUPPASTE'")
    op.execute("ALTER TYPE permission_enum RENAME VALUE 'READ_ARTIFACT' TO 'READ_YUPPASTE'")
    op.execute("ALTER TYPE agentartifacttype RENAME VALUE 'TEXT' TO 'YUPPASTE'")
