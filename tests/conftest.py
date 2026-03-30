import pytest
from pytest_alembic.config import Config
from pytest_alembic.runner import MigrationContext
from pytest_mock_resources import PostgresConfig, create_postgres_fixture
from sqlalchemy import Engine


@pytest.fixture
def alembic_config() -> Config:
    """Override this fixture to configure the exact alembic context setup required."""
    return Config()


@pytest.fixture(scope="session")
def pmr_postgres_config() -> PostgresConfig:
    return PostgresConfig(image="pgvector/pgvector:pg16")  # type: ignore[no-untyped-call]


alembic_engine = create_postgres_fixture()
