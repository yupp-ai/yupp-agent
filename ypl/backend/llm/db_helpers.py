"""Slim DB helpers for yupp-agent.

Only includes functions used by AHS, SAG, and MCP services.
Full implementation lives in yupp-mind.
"""

from sqlalchemy import func
from sqlmodel import select

from ypl.backend.db import (
    get_async_session,
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


@async_timed_cache(seconds=3600)
@retry_db
async def get_user_id_by_github_username(github_username: str) -> str | None:
    """Resolve a GitHub username to a user ID via the ``users.github_username`` column."""
    query = select(User.user_id).where(
        User.github_username == github_username,
        User.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


@async_timed_cache(seconds=3600)
@retry_db
async def get_user_id_by_slack_user_id(slack_user_id: str) -> str | None:
    """Resolve a Slack user ID to a user ID via the ``users.slack_user_id`` column."""
    query = select(User.user_id).where(
        User.slack_user_id == slack_user_id,
        User.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


@async_timed_cache(seconds=3600)
@retry_db
async def get_email_by_slack_user_id(slack_user_id: str) -> str | None:
    """Resolve a Slack user ID to the user's email via the ``users`` row."""
    query = select(User.email).where(
        User.slack_user_id == slack_user_id,
        User.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


@async_timed_cache(seconds=3600)
@retry_db
async def get_linear_name_by_email(email: str) -> str | None:
    """Resolve a user email to their Linear display name (or ``None`` if no mapping)."""
    query = select(User.linear_name).where(
        func.lower(User.email) == func.lower(email),
        User.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    async with get_async_session() as session:
        return (await session.exec(query)).one_or_none()


async def get_active_model_count() -> int:
    """Return 0 — yupp-agent doesn't have language_models table."""
    return 0


def get_active_model_count_sync() -> int:
    """Return 0 — yupp-agent doesn't have language_models table."""
    return 0
