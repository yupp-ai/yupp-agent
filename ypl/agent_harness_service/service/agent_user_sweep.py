"""Startup sweep: ensure every agent has a corresponding user row.

This module is intentionally NOT wired into server.py — that happens in a later task.
"""

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ypl.backend.db import get_async_session
from ypl.db.users import User, UserStatus, UserType
from ypl.structured_logger import get_logger

logger = get_logger()

_FIND_AGENTS_WITHOUT_USER = text("""
    SELECT agent_id, name
    FROM agents
    WHERE agents.deleted_at IS NULL
      AND (
          agent_user_id IS NULL
          OR (
              agent_user_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM users WHERE user_id = agents.agent_user_id
              )
          )
      )
""")


async def sweep_agent_user_identities() -> None:
    """Ensure every agent has a corresponding User row and agent_user_id set.

    Finds agents where agent_user_id IS NULL or no matching users row exists,
    then inserts the missing User row and updates agents.agent_user_id.

    Designed to be called as a fire-and-forget startup task.  Any exception is
    caught and logged so it never prevents the server from starting.
    """
    try:
        async with get_async_session() as session:
            result = await session.execute(_FIND_AGENTS_WITHOUT_USER)
            incomplete_agents = result.fetchall()

        if not incomplete_agents:
            return

        # Process each agent in its own short transaction so one failure
        # doesn't block the rest.
        backfilled_count = 0
        for row in incomplete_agents:
            agent_id = row.agent_id
            name = row.name
            try:
                async with get_async_session() as session:
                    # INSERT ... ON CONFLICT DO NOTHING is truly idempotent —
                    # it never overwrites existing data unlike session.merge.
                    stmt = (
                        pg_insert(User)
                        .values(
                            user_id=str(agent_id),
                            name=f"agent:{name}",
                            email=f"agent-{agent_id}@agents.yupp.ai",
                            user_type=UserType.AGENT,
                            status=UserStatus.ACTIVE,
                        )
                        .on_conflict_do_nothing(index_elements=["user_id"])
                    )
                    await session.execute(stmt)
                    await session.execute(
                        text("UPDATE agents SET agent_user_id = :uid WHERE agent_id = :aid"),
                        {"uid": str(agent_id), "aid": agent_id},
                    )
                    await session.commit()
                    backfilled_count += 1
            except Exception:
                logger.exception(
                    "sweep_agent_user_identities: failed for agent — skipping",
                    agent_id=str(agent_id),
                    agent_name=name,
                )

        logger.info(
            "sweep_agent_user_identities: completed",
            found=len(incomplete_agents),
            backfilled=backfilled_count,
            failed=len(incomplete_agents) - backfilled_count,
        )
    except Exception:
        logger.exception("sweep_agent_user_identities: unexpected failure — non-fatal")
