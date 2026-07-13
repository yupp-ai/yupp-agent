import sys
from logging.config import fileConfig

import sqlmodel
from alembic import context
from sqlalchemy import engine_from_config, pool

from ypl.backend.config import Settings
from ypl.db.all_models import all_models  # noqa: F401 for populating metadata.

config = context.config
settings = Settings()

# This repo manages agentdb migrations (not appdb). Prefer the admin
# connection (schema_manager role) if configured — plain agentdb is typically
# the runtime app user (be_app_user) which doesn't have DDL privileges.
# The admin property falls back to agentdb when POSTGRES_CONNECTION_AGENTDB_ADMIN
# is empty, so single-role deployments keep working.
ALEMBIC_DB_URL = settings.agentdb_admin_url(async_mode=False)
ALEMBIC_DB_HOST = settings.agentdb_admin.host

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


target_metadata = sqlmodel.SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=ALEMBIC_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        user_module_prefix="sqlmodel.sql.sqltypes.",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = str(ALEMBIC_DB_URL)

    connectable = context.config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
            connect_args={
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 15,
                "keepalives_count": 5,
            },
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            user_module_prefix="sqlmodel.sql.sqltypes.",
        )

        with context.begin_transaction():
            context.run_migrations()


if ("upgrade" in sys.argv or "downgrade" in sys.argv) and (
    "localhost" not in ALEMBIC_DB_HOST and "127.0.0.1" not in ALEMBIC_DB_HOST
):
    print(f"WARNING: You are about to upgrade {ALEMBIC_DB_HOST} @ {settings.ENVIRONMENT.upper()}!")
    approval = input("Type 'yupp' to continue: ").strip().lower()
    if approval != "yupp":
        print("Migration aborted!")
        exit(1)

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

print("Alembic migration complete!")

config.set_main_option("sqlalchemy.url", ALEMBIC_DB_URL.replace("%", "%%"))
