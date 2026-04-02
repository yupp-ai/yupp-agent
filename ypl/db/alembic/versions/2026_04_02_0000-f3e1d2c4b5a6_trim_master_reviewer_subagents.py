"""trim master-reviewer allowed_subagents to claude+codex only

Remove reviewer-glm (P50=50s queue, P99=304s), reviewer-kimi (401 errors),
and reviewer-minimax (401 errors) from master-reviewer's allowed_subagents.
Only reviewer-claude and reviewer-codex remain.

Revision ID: f3e1d2c4b5a6
Revises: 04308c50cf51
Create Date: 2026-04-02 00:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3e1d2c4b5a6"
down_revision: str | None = "04308c50cf51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agents
        SET config = jsonb_set(
            config,
            '{allowed_subagents}',
            '["reviewer-claude", "reviewer-codex"]'::jsonb
        )
        WHERE name = 'master-reviewer'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents
        SET config = jsonb_set(
            config,
            '{allowed_subagents}',
            '["reviewer-claude", "reviewer-codex", "reviewer-glm", "reviewer-kimi", "reviewer-minimax"]'::jsonb
        )
        WHERE name = 'master-reviewer'
        """
    )
