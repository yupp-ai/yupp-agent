"""Slim DB helpers for yupp-agent.

Only includes functions used by AHS, SAG, and MCP services.
Full implementation lives in yupp-mind.
"""

from sqlalchemy import func
from sqlmodel import col, select

from ypl.backend.db import (
    get_async_session,
    get_async_session_read_replica,
    get_engine_read_replica,
    retry_db,
)
from ypl.db.users import User
from ypl.utils import async_timed_cache


@async_timed_cache(seconds=3600 * 24)
@retry_db
async def get_user_id_by_email(email: str) -> str | None:
    """Resolve a user email (case-insensitive) to a user ID, or None if not found."""
    query = select(User.user_id).where(func.lower(User.email) == func.lower(email), User.deleted_at.is_(None))  # type: ignore[union-attr]
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


@retry_db
async def get_user_email(user_id: str) -> str | None:
    query = select(User.email).where(User.user_id == user_id)
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


async def get_active_model_count() -> int:
    """Return 0 — yupp-agent doesn't have language_models table."""
    return 0


def get_active_model_count_sync() -> int:
    """Return 0 — yupp-agent doesn't have language_models table."""
    return 0
