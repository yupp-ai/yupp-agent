"""agent_sessions: add was_interrupted_by_restart flag

PR 2 of the AHS-restart-courtesy-+-auto-resume project.  PR 1 fixed the
shutdown / restart courtesy *delivery*; this PR makes the resume real.
The startup recovery sweep already distinguishes "interrupted mid-turn"
(SIGTERM during a turn) from "completed but unwritten" / "auto-staled
after 6h idle" by inspecting the message history.  We persist the
mid-turn-interrupted classification on the session row so that when the
next user message arrives, ``send_message`` can prepend a synthetic
"the previous turn was interrupted" preamble before handing control to
the runner.

The flag is intentionally separate from the ``STALE`` status enum value:
``STALE`` is also produced by the periodic 6-hour idle sweep, and those
rows are NOT crash leftovers — preambling the user with "your previous
turn was interrupted" on a session they had simply abandoned would be
misleading.  A dedicated boolean keeps the two concerns orthogonal.

Reversible: ``downgrade()`` drops the column.

Revision ID: c3a9d12e4b7f
Revises: b227eabd88f2
Create Date: 2026-04-30 21:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a9d12e4b7f"
down_revision: str | None = "b227eabd88f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column(
            "was_interrupted_by_restart",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "was_interrupted_by_restart")
