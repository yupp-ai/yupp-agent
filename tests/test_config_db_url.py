"""Unit tests for Settings._build_db_url — the DB URL builder.

Covers: host:port parsing, host-only, async mode, proxy socket mode.
This would have caught the bug in PR #35 where PostgresDsn.build() couldn't
handle host:port strings correctly.
"""

import pytest
from ypl.backend.config import PostgresConnection, Settings


@pytest.fixture
def settings() -> Settings:
    """Create a Settings instance configured for local (no proxy socket)."""
    return Settings(
        ENVIRONMENT="local",
        ENABLE_CLOUDSQL_PROXY=False,
        ENABLE_CLOUD_SQL_CONNECTOR=False,
    )


class TestBuildDbUrl:
    """Test _build_db_url with various host formats."""

    def test_host_with_port(self, settings: Settings) -> None:
        conn = PostgresConnection(host="10.0.0.1:5432", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert "10.0.0.1" in url
        assert ":5432/" in url
        assert "mydb" in url
        assert url.startswith("postgresql://")

    def test_host_without_port_defaults_to_5432(self, settings: Settings) -> None:
        conn = PostgresConnection(host="myhost", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert "myhost" in url
        assert ":5432/" in url

    def test_localhost_with_port(self, settings: Settings) -> None:
        conn = PostgresConnection(host="localhost:5433", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert "localhost" in url
        assert ":5433/" in url

    def test_async_mode_uses_asyncpg_driver(self, settings: Settings) -> None:
        conn = PostgresConnection(host="localhost:5432", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=True)
        assert url.startswith("postgresql+asyncpg://")

    def test_sync_mode_uses_plain_driver(self, settings: Settings) -> None:
        conn = PostgresConnection(host="localhost:5432", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert url.startswith("postgresql://")
        assert "+asyncpg" not in url

    def test_host_with_custom_port(self, settings: Settings) -> None:
        conn = PostgresConnection(host="db.example.com:6543", user="u", password="p", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert ":6543/" in url

    def test_url_contains_credentials(self, settings: Settings) -> None:
        conn = PostgresConnection(host="localhost:5432", user="myuser", password="secret", database="mydb")
        url = settings._build_db_url(conn, async_mode=False)
        assert "myuser" in url
        assert "secret" in url


class TestBuildDbUrlProxySocket:
    """Test _build_db_url with Cloud SQL proxy socket."""

    def test_proxy_socket_sync(self) -> None:
        settings = Settings(
            ENVIRONMENT="production",
            ENABLE_CLOUDSQL_PROXY=True,
            ENABLE_CLOUD_SQL_CONNECTOR=False,
        )
        conn = PostgresConnection(
            host="10.0.0.1:5432",
            user="u",
            password="p",
            database="mydb",
            cloud_sql_proxy_socket="/cloudsql/project:region:instance",
        )
        url = settings._build_db_url(conn, async_mode=False)
        # URL-encoded form of the socket path
        assert "cloudsql" in url
        assert "mydb" in url

    def test_local_env_ignores_proxy_socket(self) -> None:
        settings = Settings(
            ENVIRONMENT="local",
            ENABLE_CLOUDSQL_PROXY=True,
        )
        conn = PostgresConnection(
            host="localhost:5432",
            user="u",
            password="p",
            database="mydb",
            cloud_sql_proxy_socket="/cloudsql/project:region:instance",
        )
        url = settings._build_db_url(conn, async_mode=False)
        # Should use direct connection, not proxy socket
        assert "localhost" in url
        assert "/cloudsql/" not in url
