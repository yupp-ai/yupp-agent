"""agent_artifacts: add SKILL artifact type (scoped inline content)

Adds ``SKILL`` to the ``agentartifacttype`` enum and broadens the existing
MEMORY scoping infrastructure to cover SKILL artifacts as well:

* The ``ck_agent_artifacts_memory_scope_matches_type`` check constraint is
  rewritten to allow MEMORY *or* SKILL rows to use the
  ``memory_scope`` / ``memory_scope_subject`` columns (other types must
  still leave both NULL).
* ``uix_agent_artifacts_slug_version`` is narrowed to exclude SKILL (its
  uniqueness is scope-qualified, like MEMORY).
* A new ``uix_skill_scope_slug_version`` index mirrors the existing
  MEMORY scope-qualified unique index.
* ``ix_skill_scope_subject`` adds a fast scope-filtered read index for
  SKILL rows (used by the system-prompt skill catalog builder).

SKILL artifacts store their markdown body in ``inline_content`` (the
same column MEMORY uses), with parsed YAML frontmatter persisted into
``artifact_metadata->'skill'`` for indexed catalog scans without
deserialising the body.

Revision ID: a3f1c4d5e6b7
Revises: b227eabd88f2
Create Date: 2026-05-20 12:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c4d5e6b7"
down_revision: str | None = "b227eabd88f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New SKILL value on the existing enum. Postgres requires ADD VALUE to
    # land in its own transaction before it can be referenced in subsequent
    # DDL (including the rewritten CHECK constraint below), so commit around it.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentartifacttype ADD VALUE IF NOT EXISTS 'SKILL'"))
    op.execute(sa.text("BEGIN"))

    # Broaden the scope/type check to cover MEMORY + SKILL. Other types must
    # still leave the scope columns NULL.
    op.drop_constraint("ck_agent_artifacts_memory_scope_matches_type", "agent_artifacts", type_="check")
    op.create_check_constraint(
        "ck_agent_artifacts_memory_scope_matches_type",
        "agent_artifacts",
        "(artifact_type IN ('MEMORY', 'SKILL') AND memory_scope IN ('user', 'agent', 'topic')) "
        "OR (artifact_type NOT IN ('MEMORY', 'SKILL') "
        "    AND memory_scope IS NULL AND memory_scope_subject IS NULL)",
    )

    # Narrow the pre-existing global slug/version unique index to also exclude
    # SKILL — like MEMORY, SKILL artifacts get scope-qualified uniqueness.
    op.drop_index("uix_agent_artifacts_slug_version", table_name="agent_artifacts")
    op.create_index(
        "uix_agent_artifacts_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
        unique=True,
        postgresql_where=sa.text(
            "named_slug IS NOT NULL AND version IS NOT NULL AND artifact_type NOT IN ('MEMORY', 'SKILL')"
        ),
    )

    # Per-scope slug/version uniqueness for SKILL artifacts. NULLS NOT DISTINCT
    # ensures topic-scope rows (subject IS NULL) still collide on duplicate
    # ``(topic, NULL, slug, version)``, matching the MEMORY index.
    op.create_index(
        "uix_skill_scope_slug_version",
        "agent_artifacts",
        ["memory_scope", "memory_scope_subject", "named_slug", "version"],
        unique=True,
        postgresql_where=sa.text("artifact_type = 'SKILL' AND named_slug IS NOT NULL AND version IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )

    # Fast scope-filtered reads for SKILL (the catalog merger in the
    # system-prompt builder hits this on every session start).
    op.create_index(
        "ix_skill_scope_subject",
        "agent_artifacts",
        ["memory_scope", "memory_scope_subject"],
        unique=False,
        postgresql_where=sa.text("artifact_type = 'SKILL'"),
    )


def downgrade() -> None:
    # Refuse to downgrade if any SKILL rows still exist — the enum rebuild at
    # the end of this function casts every ``artifact_type`` back into a new
    # enum that doesn't include SKILL; leftover SKILL rows would fail the cast
    # and leave the schema half-migrated.
    bind = op.get_bind()
    n_skill = bind.execute(sa.text("SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type = 'SKILL'")).scalar_one()
    if n_skill:
        raise RuntimeError(
            f"Refusing to downgrade a3f1c4d5e6b7: {n_skill} SKILL artifact row(s) still exist. "
            "Delete or migrate them before downgrading."
        )

    op.drop_index("ix_skill_scope_subject", table_name="agent_artifacts")
    op.drop_index("uix_skill_scope_slug_version", table_name="agent_artifacts")

    # Restore the original (MEMORY-only) narrowing on the slug/version index.
    op.drop_index("uix_agent_artifacts_slug_version", table_name="agent_artifacts")
    op.create_index(
        "uix_agent_artifacts_slug_version",
        "agent_artifacts",
        ["named_slug", "version"],
        unique=True,
        postgresql_where=sa.text("named_slug IS NOT NULL AND version IS NOT NULL AND artifact_type <> 'MEMORY'"),
    )

    # Restore the original MEMORY-only check constraint.
    op.drop_constraint("ck_agent_artifacts_memory_scope_matches_type", "agent_artifacts", type_="check")
    op.create_check_constraint(
        "ck_agent_artifacts_memory_scope_matches_type",
        "agent_artifacts",
        "(artifact_type = 'MEMORY' AND memory_scope IN ('user', 'agent', 'topic')) "
        "OR (artifact_type <> 'MEMORY' AND memory_scope IS NULL AND memory_scope_subject IS NULL)",
    )

    # Postgres can't drop an enum value directly; rebuild the type without SKILL.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE agentartifacttype RENAME TO agentartifacttype_old"))
    op.execute(sa.text("CREATE TYPE agentartifacttype AS ENUM ('TEXT', 'CODE_REVIEW', 'OTHER', 'MEMORY')"))
    op.execute(
        sa.text(
            "ALTER TABLE agent_artifacts ALTER COLUMN artifact_type "
            "TYPE agentartifacttype USING artifact_type::text::agentartifacttype"
        )
    )
    op.execute(sa.text("DROP TYPE agentartifacttype_old"))
    op.execute(sa.text("BEGIN"))
