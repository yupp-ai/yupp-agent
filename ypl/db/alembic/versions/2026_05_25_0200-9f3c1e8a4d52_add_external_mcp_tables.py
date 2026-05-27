"""Add external MCP registry + per-user grants

Revision ID: 9f3c1e8a4d52
Revises: a1c0fe19f0c5
Create Date: 2026-05-25 02:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9f3c1e8a4d52"
down_revision: str | None = "a1c0fe19f0c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(now())"), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.text("(now())"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "transport",
            sa.Enum("STREAMABLE_HTTP", "SSE", name="mcp_transport"),
            nullable=False,
        ),
        sa.Column(
            "auth_type",
            sa.Enum("OAUTH_OBO", "M2M_SHARED", "M2M_PER_USER", "NONE", name="mcp_auth_type"),
            nullable=False,
        ),
        sa.Column("oauth_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("allow_token_to_agent", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("mcp_server_id", name=op.f("pk_mcp_servers")),
        sa.UniqueConstraint("slug", name="uq_mcp_servers_slug"),
    )
    op.create_index(op.f("ix_mcp_servers_slug"), "mcp_servers", ["slug"], unique=True)

    op.create_table(
        "mcp_server_secrets",
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("oauth_client_secret_enc", sa.Text(), nullable=True),
        sa.Column("m2m_shared_token_enc", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.mcp_server_id"],
            name=op.f("fk_mcp_server_secrets_mcp_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mcp_server_id", name=op.f("pk_mcp_server_secrets")),
    )

    op.create_table(
        "mcp_server_roles",
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.mcp_server_id"],
            name=op.f("fk_mcp_server_roles_mcp_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.role_id"],
            name=op.f("fk_mcp_server_roles_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mcp_server_id", "role_id", name="pk_mcp_server_roles"),
    )

    op.create_table(
        "mcp_server_agents",
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.mcp_server_id"],
            name=op.f("fk_mcp_server_agents_mcp_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name=op.f("fk_mcp_server_agents_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mcp_server_id", "agent_id", name="pk_mcp_server_agents"),
    )

    op.create_table(
        "mcp_user_grants",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(now())"), nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.text("(now())"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_mcp_user_grants_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.mcp_server_id"],
            name=op.f("fk_mcp_user_grants_mcp_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_mcp_user_grants")),
    )
    op.create_index(op.f("ix_mcp_user_grants_user_id"), "mcp_user_grants", ["user_id"], unique=False)
    op.create_index(op.f("ix_mcp_user_grants_mcp_server_id"), "mcp_user_grants", ["mcp_server_id"], unique=False)
    op.create_index(
        "uq_mcp_user_grants_active",
        "mcp_user_grants",
        ["user_id", "mcp_server_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "mcp_grant_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum("GRANTED", "REFRESHED", "REFRESH_FAILED", "USED", "REVOKED", name="mcp_grant_event_type"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name=op.f("fk_mcp_grant_events_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.mcp_server_id"],
            name=op.f("fk_mcp_grant_events_mcp_server_id_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.agent_session_id"],
            name=op.f("fk_mcp_grant_events_agent_session_id_agent_sessions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_mcp_grant_events")),
    )
    op.create_index(op.f("ix_mcp_grant_events_user_id"), "mcp_grant_events", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_mcp_grant_events_mcp_server_id"), "mcp_grant_events", ["mcp_server_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mcp_grant_events_mcp_server_id"), table_name="mcp_grant_events")
    op.drop_index(op.f("ix_mcp_grant_events_user_id"), table_name="mcp_grant_events")
    op.drop_table("mcp_grant_events")
    sa.Enum(name="mcp_grant_event_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("uq_mcp_user_grants_active", table_name="mcp_user_grants")
    op.drop_index(op.f("ix_mcp_user_grants_mcp_server_id"), table_name="mcp_user_grants")
    op.drop_index(op.f("ix_mcp_user_grants_user_id"), table_name="mcp_user_grants")
    op.drop_table("mcp_user_grants")

    op.drop_table("mcp_server_agents")
    op.drop_table("mcp_server_roles")
    op.drop_table("mcp_server_secrets")
    op.drop_index(op.f("ix_mcp_servers_slug"), table_name="mcp_servers")
    op.drop_table("mcp_servers")
    sa.Enum(name="mcp_auth_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="mcp_transport").drop(op.get_bind(), checkfirst=True)
