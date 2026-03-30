"""add search_tsv columns and GIN indexes to agent search tables

Revision ID: c5d6e7f8a9b0
Revises: ab0aa44ff305
Create Date: 2026-03-30 10:00:00.000000+00:00

Adds a `search_tsv` TSVECTOR column to each of the 6 agent search tables,
populated via triggers with weighted setweight() expressions. A GIN index is
created concurrently on each column for fast full-text search.

Tables covered:
  - agent_sessions        : title (A) + context::text (C)
  - agent_session_messages: content (A)
  - agent_projects        : name (A) + description (B) + project_data::text (D)
  - agent_tasks           : title (A) + description (B)
  - agent_schedules       : name (A) + message (B) + description (C)
  - agent_artifacts       : title (A) + description (B) + url (C)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "ab0aa44ff305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Add search_tsv columns (nullable TSVECTOR) to all 6 tables
    # -------------------------------------------------------------------------
    for table in (
        "agent_sessions",
        "agent_session_messages",
        "agent_projects",
        "agent_tasks",
        "agent_schedules",
        "agent_artifacts",
    ):
        op.add_column(table, sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True))

    # -------------------------------------------------------------------------
    # 2. Create trigger functions + triggers (one per table)
    # -------------------------------------------------------------------------

    # agent_sessions: title (A) + context::text (C)
    op.execute("""
        CREATE FUNCTION agent_sessions_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.context::text, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_sessions_search_tsv
            BEFORE INSERT OR UPDATE OF title, context ON agent_sessions
            FOR EACH ROW EXECUTE FUNCTION agent_sessions_search_tsv_update();
    """)

    # agent_session_messages: content (A)
    op.execute("""
        CREATE FUNCTION agent_session_messages_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'A');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_session_messages_search_tsv
            BEFORE INSERT OR UPDATE OF content ON agent_session_messages
            FOR EACH ROW EXECUTE FUNCTION agent_session_messages_search_tsv_update();
    """)

    # agent_projects: name (A) + description (B) + project_data::text (D)
    op.execute("""
        CREATE FUNCTION agent_projects_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.project_data::text, '')), 'D');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_projects_search_tsv
            BEFORE INSERT OR UPDATE OF name, description, project_data ON agent_projects
            FOR EACH ROW EXECUTE FUNCTION agent_projects_search_tsv_update();
    """)

    # agent_tasks: title (A) + description (B)
    op.execute("""
        CREATE FUNCTION agent_tasks_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_tasks_search_tsv
            BEFORE INSERT OR UPDATE OF title, description ON agent_tasks
            FOR EACH ROW EXECUTE FUNCTION agent_tasks_search_tsv_update();
    """)

    # agent_schedules: name (A) + message (B) + description (C)
    op.execute("""
        CREATE FUNCTION agent_schedules_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.message, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_schedules_search_tsv
            BEFORE INSERT OR UPDATE OF name, message, description ON agent_schedules
            FOR EACH ROW EXECUTE FUNCTION agent_schedules_search_tsv_update();
    """)

    # agent_artifacts: title (A) + description (B) + url (C)
    op.execute("""
        CREATE FUNCTION agent_artifacts_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_tsv :=
                setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.url, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_agent_artifacts_search_tsv
            BEFORE INSERT OR UPDATE OF title, description, url ON agent_artifacts
            FOR EACH ROW EXECUTE FUNCTION agent_artifacts_search_tsv_update();
    """)

    # -------------------------------------------------------------------------
    # 3. Back-fill existing rows with the weighted tsvector expression
    # -------------------------------------------------------------------------
    op.execute("""
        UPDATE agent_sessions SET search_tsv =
            setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(context::text, '')), 'C')
    """)
    op.execute("""
        UPDATE agent_session_messages SET search_tsv =
            setweight(to_tsvector('english', COALESCE(content, '')), 'A')
    """)
    op.execute("""
        UPDATE agent_projects SET search_tsv =
            setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(project_data::text, '')), 'D')
    """)
    op.execute("""
        UPDATE agent_tasks SET search_tsv =
            setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'B')
    """)
    op.execute("""
        UPDATE agent_schedules SET search_tsv =
            setweight(to_tsvector('english', COALESCE(name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(message, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'C')
    """)
    op.execute("""
        UPDATE agent_artifacts SET search_tsv =
            setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(url, '')), 'C')
    """)

    # -------------------------------------------------------------------------
    # 4. Create GIN indexes concurrently (each in its own autocommit block)
    # -------------------------------------------------------------------------
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_sessions_search_tsv",
            "agent_sessions",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_session_messages_search_tsv",
            "agent_session_messages",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_projects_search_tsv",
            "agent_projects",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_tasks_search_tsv",
            "agent_tasks",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_schedules_search_tsv",
            "agent_schedules",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agent_artifacts_search_tsv",
            "agent_artifacts",
            ["search_tsv"],
            unique=False,
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Drop GIN indexes concurrently
    # -------------------------------------------------------------------------
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_artifacts_search_tsv",
            table_name="agent_artifacts",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_schedules_search_tsv",
            table_name="agent_schedules",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_tasks_search_tsv",
            table_name="agent_tasks",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_projects_search_tsv",
            table_name="agent_projects",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_session_messages_search_tsv",
            table_name="agent_session_messages",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agent_sessions_search_tsv",
            table_name="agent_sessions",
            postgresql_using="gin",
            postgresql_concurrently=True,
            if_exists=True,
        )

    # -------------------------------------------------------------------------
    # 2. Drop triggers and trigger functions
    # -------------------------------------------------------------------------
    op.execute("DROP TRIGGER IF EXISTS trig_agent_artifacts_search_tsv ON agent_artifacts")
    op.execute("DROP FUNCTION IF EXISTS agent_artifacts_search_tsv_update()")

    op.execute("DROP TRIGGER IF EXISTS trig_agent_schedules_search_tsv ON agent_schedules")
    op.execute("DROP FUNCTION IF EXISTS agent_schedules_search_tsv_update()")

    op.execute("DROP TRIGGER IF EXISTS trig_agent_tasks_search_tsv ON agent_tasks")
    op.execute("DROP FUNCTION IF EXISTS agent_tasks_search_tsv_update()")

    op.execute("DROP TRIGGER IF EXISTS trig_agent_projects_search_tsv ON agent_projects")
    op.execute("DROP FUNCTION IF EXISTS agent_projects_search_tsv_update()")

    op.execute("DROP TRIGGER IF EXISTS trig_agent_session_messages_search_tsv ON agent_session_messages")
    op.execute("DROP FUNCTION IF EXISTS agent_session_messages_search_tsv_update()")

    op.execute("DROP TRIGGER IF EXISTS trig_agent_sessions_search_tsv ON agent_sessions")
    op.execute("DROP FUNCTION IF EXISTS agent_sessions_search_tsv_update()")

    # -------------------------------------------------------------------------
    # 3. Drop search_tsv columns
    # -------------------------------------------------------------------------
    for table in (
        "agent_artifacts",
        "agent_schedules",
        "agent_tasks",
        "agent_projects",
        "agent_session_messages",
        "agent_sessions",
    ):
        op.drop_column(table, "search_tsv")
