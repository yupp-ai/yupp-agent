import os

import pytest
from sqlalchemy import Engine, create_engine, text


@pytest.fixture()
def alembic_engine() -> Engine:
    """Provide a real Postgres engine for pytest-alembic tests (instead of the default SQLite).

    Drops and recreates the public schema before each test to ensure a clean slate —
    this removes all tables, types, and extensions left by previous test runs.
    """
    url = os.environ.get("ALEMBIC_TEST_DB_URL", "postgresql://postgres:postgres@localhost:5432/test_yadb")
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    return engine
