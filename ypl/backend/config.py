"""Minimal settings for yupp-agent database connectivity.

Mirrors the relevant subset of yupp-mind's ypl/backend/config.py.
"""

import os
from typing import Literal

import sqlalchemy
from pydantic import PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvironmentType = Literal["production", "staging", "test", "local"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    ENVIRONMENT: EnvironmentType = "local"

    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost:5432")
    POSTGRES_DATABASE: str = os.getenv("POSTGRES_DATABASE", "yupp_agent")

    POSTGRES_USER_READ_REPLICA: str = os.getenv("POSTGRES_USER_READ_REPLICA", "postgres")
    POSTGRES_PASSWORD_READ_REPLICA: str = os.getenv("POSTGRES_PASSWORD_READ_REPLICA", "postgres")
    POSTGRES_HOST_READ_REPLICA: str = os.getenv("POSTGRES_HOST_READ_REPLICA", "localhost:5432")
    POSTGRES_DATABASE_READ_REPLICA: str = os.getenv("POSTGRES_DATABASE_READ_REPLICA", "yupp_agent")

    # Cloud SQL proxy settings
    ENABLE_CLOUDSQL_PROXY: bool = False
    ENABLE_CLOUD_SQL_CONNECTOR: bool = False
    CLOUD_PRIMARY_SQL_PROXY_INSTANCE_UNIX_SOCKET: str = ""
    CLOUD_READ_REPLICA_SQL_PROXY_INSTANCE_UNIX_SOCKET: str = ""

    def _use_proxy_socket(self, async_mode: bool) -> bool:
        if not self.CLOUD_PRIMARY_SQL_PROXY_INSTANCE_UNIX_SOCKET:
            return False
        if not self.ENABLE_CLOUDSQL_PROXY:
            return False
        if async_mode and self.ENABLE_CLOUD_SQL_CONNECTOR:
            return False
        return True

    def _db_url(self, async_mode: bool) -> str:
        scheme = "postgresql" + ("+asyncpg" if async_mode else "")
        if self._use_proxy_socket(async_mode):
            return sqlalchemy.engine.url.URL.create(
                drivername=scheme,
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                database=self.POSTGRES_DATABASE,
                query={
                    "host": f"{self.CLOUD_PRIMARY_SQL_PROXY_INSTANCE_UNIX_SOCKET}"
                    + ("/.s.PGSQL.5432" if async_mode else "")
                },
            ).render_as_string(hide_password=False)
        return PostgresDsn.build(
            scheme=scheme,
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            path=f"{self.POSTGRES_DATABASE}",
        ).unicode_string()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url(self) -> str:
        return self._db_url(async_mode=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url_async(self) -> str:
        return self._db_url(async_mode=True)
