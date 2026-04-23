"""agent_artifacts: add MEMORY type + inline_content + memory scope columns

Schema groundwork for the "Unify Memory into Artifacts" project: a new
``MEMORY`` value on ``agentartifacttype``, inline content storage on
``agent_artifacts``, and scoping columns (``memory_scope`` /
``memory_scope_subject``) so memory can be filtered per-user / per-agent
/ per-topic at the DB layer.

The ``url`` column becomes nullable because MEMORY artifacts store their
body in ``inline_content`` instead of pointing at an external blob. A
new CHECK constraint still enforces that exactly one of the two is set
on every row, so existing non-MEMORY artifacts continue to require
``url``.

Revision ID: b227eabd88f2
Revises: e36701396fc6
Create Date: 2026-04-23 00:44:50.519412+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b227eabd88f2"
down_revision: str | None = "e36701396fc6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New MEMORY value on the existing enum. Postgres requires the ADD VALUE
    # statement to land in its own transaction before it can be referenced in
    # later DDL (including CHECK constraints), so commit around it.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentartifacttype ADD VALUE IF NOT EXISTS 'MEMORY'"))
    op.execute(sa.text("BEGIN"))

    # New columns — all nullable; existing rows stay valid.
    op.add_column("agent_artifacts", sa.Column("inline_content", sa.Text(), nullable=True))
    op.add_column("agent_artifacts", sa.Column("memory_scope", sa.Text(), nullable=True))
    op.add_column("agent_artifacts", sa.Column("memory_scope_subject", sa.Text(), nullable=True))

    # ``url`` is now optional (MEMORY artifacts use inline_content instead).
    op.alter_column(
        "agent_artifacts",
        "url",
        existing_type=sa.TEXT(),
        nullable=True,
    )

    # Exactly one of (inline_content, url) must be populated on any row.
    op.create_check_constraint(
        "ck_agent_artifacts_content_location",
        "agent_artifacts",
        "(inline_content IS NOT NULL) <> (url IS NOT NULL)",
    )

    # MEMORY <=> scope is set. Non-MEMORY rows must have both scope columns NULL.
    op.create_check_constraint(
        "ck_agent_artifacts_memory_scope_matches_type",
        "agent_artifacts",
        "(artifact_type = 'MEMORY' AND memory_scope IN ('user', 'agent', 'topic')) "
        "OR (artifact_type <> 'MEMORY' AND memory_scope IS NULL AND memory_scope_subject IS NULL)",
    )

    # topic scope has no subject; user/agent scopes require one.
    op.create_check_constraint(
        "ck_agent_artifacts_memory_subject_presence",
        "agent_artifacts",
        "memory_scope IS NULL "
        "OR (memory_scope = 'topic' AND memory_scope_subject IS NULL) "
        "OR (memory_scope IN ('user', 'agent') AND memory_scope_subject IS NOT NULL)",
    )

    # Narrow the pre-existing global slug/version unique index to exclude MEMORY
    # artifacts — their uniqueness is scope-qualified (see next index) so the
    # same slug can live in both a user scope and an agent scope.
    op.drop_index("uix_agent_artifacts_slug_version", table_name="agent_artifacts")
    op.create_index(
        "uix_agent_artifacts_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
        unique=True,
        postgresql_where=sa.text("named_slug IS NOT NULL AND version IS NOT NULL AND artifact_type <> 'MEMORY'"),
    )

    # Per-scope slug/version uniqueness for MEMORY artifacts. Parallel MEMORY
    # saves across different (scope, subject) tuples don't collide, while
    # each (scope, subject, slug) sequence stays monotonic.
    #
    # NULLS NOT DISTINCT (PG 15+): topic-scope rows have
    # ``memory_scope_subject IS NULL``; without this flag Postgres would treat
    # each NULL as distinct and let duplicate ``(topic, NULL, slug, version)``
    # rows through, breaking topic-scope uniqueness.
    op.create_index(
        "uix_memory_scope_slug_version",
        "agent_artifacts",
        ["memory_scope", "memory_scope_subject", "named_slug", "version"],
        unique=True,
        postgresql_where=sa.text("artifact_type = 'MEMORY' AND named_slug IS NOT NULL AND version IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )

    # Fast scope-filtered reads (e.g. "all memory for user X").
    op.create_index(
        "ix_memory_scope_subject",
        "agent_artifacts",
        ["memory_scope", "memory_scope_subject"],
        unique=False,
        postgresql_where=sa.text("artifact_type = 'MEMORY'"),
    )


def downgrade() -> None:
    # Refuse to downgrade if any MEMORY rows still exist. The enum rebuild at
    # the end of this function casts every ``artifact_type`` value back into a
    # new enum that doesn't include ``MEMORY``; a leftover MEMORY row would
    # fail that cast mid-migration and leave the schema in a half-downgraded
    # state. Callers must migrate or delete MEMORY artifacts first.
    bind = op.get_bind()
    n_memory = bind.execute(sa.text("SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type = 'MEMORY'")).scalar_one()
    if n_memory:
        raise RuntimeError(
            f"Refusing to downgrade b227eabd88f2: {n_memory} MEMORY artifact row(s) still "
            "exist. Delete or migrate them, then retry — the enum rebuild in this "
            "downgrade cannot cast 'MEMORY' back to a type that doesn't include it."
        )

    op.drop_index("ix_memory_scope_subject", table_name="agent_artifacts")
    op.drop_index("uix_memory_scope_slug_version", table_name="agent_artifacts")

    # Restore the original global slug/version unique index.
    op.drop_index("uix_agent_artifacts_slug_version", table_name="agent_artifacts")
    op.create_index(
        "uix_agent_artifacts_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
        unique=True,
        postgresql_where=sa.text("named_slug IS NOT NULL AND version IS NOT NULL"),
    )

    op.drop_constraint("ck_agent_artifacts_memory_subject_presence", "agent_artifacts", type_="check")
    op.drop_constraint("ck_agent_artifacts_memory_scope_matches_type", "agent_artifacts", type_="check")
    op.drop_constraint("ck_agent_artifacts_content_location", "agent_artifacts", type_="check")

    op.alter_column(
        "agent_artifacts",
        "url",
        existing_type=sa.TEXT(),
        nullable=False,
    )

    op.drop_column("agent_artifacts", "memory_scope_subject")
    op.drop_column("agent_artifacts", "memory_scope")
    op.drop_column("agent_artifacts", "inline_content")

    # Postgres can't drop an enum value directly; rebuild the type without
    # MEMORY. Safe because no rows can use MEMORY once the column drops above
    # run — and we only reach here when no MEMORY-typed artifacts exist yet.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentartifacttype RENAME TO agentartifacttype_old"))
    op.execute(sa.text("CREATE TYPE agentartifacttype AS ENUM ('TEXT', 'CODE_REVIEW', 'OTHER')"))
    op.execute(
        sa.text(
            "ALTER TABLE agent_artifacts ALTER COLUMN artifact_type "
            "TYPE agentartifacttype USING artifact_type::text::agentartifacttype"
        )
    )
    op.execute(sa.text("DROP TYPE agentartifacttype_old"))
    op.execute(sa.text("BEGIN"))
