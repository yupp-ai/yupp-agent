"""Backfill session titles for AgentSessions with title IS NULL.

Re-runs the title-generation flow against existing sessions whose title was
never set (or was lost when the title model regressed in PR #237). Uses the
fixed _generate_title_text in core/session_title.py.

Usage:
    poetry run python scripts/backfill_session_titles.py --dry-run
    poetry run python scripts/backfill_session_titles.py --limit 50
    poetry run python scripts/backfill_session_titles.py --since-days 7
    poetry run python scripts/backfill_session_titles.py        # all sessions
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select
from ypl.agent_harness_service.core.session_title import (
    _TRIGGER_PREFIX,
    _generate_title_text,
)
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import (
    AgentSession,
    AgentSessionMessage,
    AgentSessionMessageRole,
)


async def _user_messages_for(agent_session_id) -> list[str]:
    async with get_async_session() as session:
        result = await session.exec(
            select(AgentSessionMessage.content)
            .where(
                AgentSessionMessage.agent_session_id == agent_session_id,
                col(AgentSessionMessage.role) == AgentSessionMessageRole.USER,
                AgentSessionMessage.content.isnot(None),  # type: ignore[union-attr]
            )
            .order_by(col(AgentSessionMessage.turn_number))
            .limit(5)
        )
        return [row for row in result.all() if row]


async def _candidate_sessions(limit: int | None, since: datetime | None) -> list[AgentSession]:
    async with get_async_session() as session:
        stmt = select(AgentSession).where(col(AgentSession.title).is_(None))
        if since is not None:
            stmt = stmt.where(AgentSession.created_at >= since)
        stmt = stmt.order_by(col(AgentSession.created_at).desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await session.exec(stmt)
        return list(result.all())


async def _backfill_one(s: AgentSession, dry_run: bool) -> tuple[str, str | None]:
    """Return (status, title). status is one of: filled, skip-no-msgs, skip-no-title."""
    user_messages = await _user_messages_for(s.agent_session_id)
    if not user_messages:
        return "skip-no-msgs", None

    title = await _generate_title_text(user_messages)
    if not title:
        return "skip-no-title", None

    if s.trigger:
        prefix = _TRIGGER_PREFIX.get(s.trigger.value, "")
        if prefix:
            title = prefix + title

    if not dry_run:
        async with get_async_session() as session:
            row = await session.get(AgentSession, s.agent_session_id)
            if row and row.title is None:
                row.title = title
                await session.commit()

    return "filled", title


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Max sessions to process")
    parser.add_argument("--since-days", type=int, default=None, help="Only sessions created in the last N days")
    parser.add_argument("--dry-run", action="store_true", help="Generate titles but don't write to DB")
    args = parser.parse_args()

    since = datetime.now(tz=UTC) - timedelta(days=args.since_days) if args.since_days else None
    candidates = await _candidate_sessions(args.limit, since)
    print(f"Found {len(candidates)} sessions with NULL title (limit={args.limit}, since_days={args.since_days})")
    if args.dry_run:
        print("DRY RUN — no writes will be performed")

    counts: dict[str, int] = {"filled": 0, "skip-no-msgs": 0, "skip-no-title": 0, "error": 0}
    for i, s in enumerate(candidates, 1):
        try:
            status, title = await _backfill_one(s, args.dry_run)
        except Exception as e:
            counts["error"] += 1
            print(f"[{i}/{len(candidates)}] {s.agent_session_id} ERROR: {type(e).__name__}: {e}")
            continue
        counts[status] += 1
        if status == "filled":
            print(f"[{i}/{len(candidates)}] {s.agent_session_id} -> {title!r}")
        else:
            print(f"[{i}/{len(candidates)}] {s.agent_session_id} {status}")

    print("\nSummary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
