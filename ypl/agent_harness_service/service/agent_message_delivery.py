"""Push-based delivery for Scenario A: new-session creation from agent messages.

Consumes the ``a2a_dispatch`` Redis list (enqueued by ``send_agent_message``) and
creates a new ``AgentSession`` for each message using an atomic DB claim protocol
to guarantee at-most-once delivery per attempt.

Design reference: http://go/p/a2a-messaging-design@5 (Sections 9, 10, 11, 13 Scenario A)

NOTE: Not wired into server.py yet — that is task [9].

## Delivery guarantees

- **Atomic claim**: ``UPDATE … RETURNING`` with ``AND status='QUEUED'`` ensures at most
  one worker processes each message per attempt.
- **Ownership guard**: both post-delivery UPDATE statements include ``AND claimed_at=:claimed_at``
  so a late-arriving worker cannot clobber an active claim.
- **Two-phase success write**: after ``create_session()`` succeeds, ``resolved_session_id``
  is written first (Phase A), then ``status='DELIVERED'`` (Phase B).  If Phase A commits
  but Phase B does not, the startup sweep (task [9]) sees ``resolved_session_id IS NOT NULL``
  and can finalize without re-creating the session.
- **Bounded concurrency**: ``_delivery_semaphore`` caps simultaneous deliveries so a
  post-outage burst cannot saturate the async DB connection pool.
- **Strong task references**: delivery tasks are held in ``_background_delivery_tasks``
  to prevent the garbage collector from cancelling them mid-execution.
"""

import asyncio
import uuid

from sqlmodel import col, select

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.service.session_lifecycle import create_session
from ypl.backend.db import get_async_session
from ypl.db.agent_harness import Agent
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Redis list key for Scenario A (new-session) messages.
A2A_DISPATCH_KEY = "a2a_dispatch"

# Cap simultaneous in-flight deliveries to avoid saturating the async DB pool
# during post-outage burst recovery.  Tune based on pool size (default = 10 conns).
_MAX_CONCURRENT_DELIVERIES = 10
_delivery_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DELIVERIES)

# Strong references to in-flight delivery tasks — prevents the event loop from
# garbage-collecting running tasks before they complete.
# See: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_delivery_tasks: set[asyncio.Task[None]] = set()

# ---------------------------------------------------------------------------
# Raw SQL for the atomic-claim protocol
# (UPDATE…RETURNING has no ORM equivalent; all other queries use sqlmodel.select)
# ---------------------------------------------------------------------------

from sqlalchemy import text as _text  # noqa: E402 — after std-lib imports

_CLAIM_MESSAGE_SQL = _text("""
    UPDATE agent_messages
    SET status        = 'DELIVERING',
        claimed_at    = now(),
        attempt_count = attempt_count + 1
    WHERE agent_message_id = :id
      AND status            = 'QUEUED'
      AND attempt_count     < max_attempts
    RETURNING
        agent_message_id,
        to_agent_id,
        from_agent_id,
        from_session_id,
        content,
        attempt_count,
        max_attempts,
        claimed_at
""")

# Phase A of the success path: record the created session before changing status.
# If Phase B fails, the startup sweep can see resolved_session_id IS NOT NULL and
# finalize delivery without re-creating the session.
_RECORD_RESOLVED_SESSION_SQL = _text("""
    UPDATE agent_messages
    SET resolved_session_id = :sid
    WHERE agent_message_id = :id
      AND status             = 'DELIVERING'
      AND claimed_at         = :claimed_at
""")

# Phase B of the success path: flip to DELIVERED once resolved_session_id is safe.
_MARK_DELIVERED_SQL = _text("""
    UPDATE agent_messages
    SET status       = 'DELIVERED',
        delivered_at = now()
    WHERE agent_message_id = :id
      AND status             = 'DELIVERING'
      AND claimed_at         = :claimed_at
""")

# Failure path: release the claim so the message can be retried (or mark FAILED).
_RELEASE_CLAIM_SQL = _text("""
    UPDATE agent_messages
    SET status     = CASE WHEN attempt_count >= max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
        error      = :err,
        claimed_at = NULL
    WHERE agent_message_id = :id
      AND status             = 'DELIVERING'
      AND claimed_at         = :claimed_at
""")


# ---------------------------------------------------------------------------
# Delivery logic
# ---------------------------------------------------------------------------


async def _deliver_new_session_message(msg_id: str) -> None:
    """Claim and deliver one Scenario-A agent message by creating a new session.

    Uses an atomic ``UPDATE … RETURNING`` to claim the message so at most one
    concurrent worker processes it.  On success the row is marked ``DELIVERED``;
    on failure the claim is released and the message is re-enqueued (or marked
    ``FAILED`` if ``attempt_count >= max_attempts``).

    All DB writes after delivery are guarded by ``AND claimed_at = :claimed_at``
    so a late-arriving worker cannot overwrite an active claim.

    Args:
        msg_id: String UUID of the ``agent_message_id`` to deliver.
    """
    try:
        msg_uuid = uuid.UUID(msg_id)
    except ValueError:
        logger.error("a2a_dispatch: invalid msg_id — skipping", msg_id=msg_id)
        return

    async with _delivery_semaphore:
        await _do_deliver(msg_uuid, msg_id)


async def _do_deliver(msg_uuid: uuid.UUID, msg_id: str) -> None:
    """Inner delivery — called inside the concurrency semaphore."""

    # -------------------------------------------------------------------
    # 1. Atomic claim — UPDATE ... RETURNING (0 rows → already claimed)
    # -------------------------------------------------------------------
    async with get_async_session() as db:
        result = await db.execute(_CLAIM_MESSAGE_SQL, {"id": str(msg_uuid)})
        row = result.mappings().one_or_none()
        await db.commit()

    if row is None:
        # Already claimed by another worker, exhausted, or not in QUEUED state.
        logger.debug(
            "a2a_dispatch: message already claimed or exhausted — skipping",
            msg_id=msg_id,
        )
        return

    claimed_at = row["claimed_at"]  # datetime; used as ownership token below

    # -------------------------------------------------------------------
    # 2. Resolve the recipient agent using sqlmodel.select (repo convention)
    # -------------------------------------------------------------------
    resolved_session_id: str | None = None
    delivery_error: str | None = None

    try:
        to_agent_uuid = uuid.UUID(str(row["to_agent_id"]))
        async with get_async_session() as db:
            agent_result = await db.exec(
                select(Agent).where(
                    Agent.agent_id == to_agent_uuid,
                    col(Agent.deleted_at).is_(None),
                )
            )
            agent = agent_result.one_or_none()

        if agent is None:
            raise ValueError(f"to_agent_id {row['to_agent_id']!r} not found in agents table")
        if not agent.name:
            raise ValueError(f"Agent {row['to_agent_id']!r} has no name")

        to_agent_name: str = agent.name
        from_session_id_raw = row["from_session_id"]

        # -------------------------------------------------------------------
        # 3. Create a new session — trigger=AGENT with full provenance context
        # -------------------------------------------------------------------
        request = SessionCreateRequest(
            agent_id=to_agent_name,
            trigger="agent",
            message=str(row["content"]),
            context={
                "from_agent_id": str(row["from_agent_id"]),
                "from_session_id": str(from_session_id_raw) if from_session_id_raw is not None else None,
                "agent_message_id": str(row["agent_message_id"]),
            },
            source="a2a",
        )
        response = await create_session(request)
        resolved_session_id = response.session_id

        logger.info(
            "a2a_dispatch: new session created",
            msg_id=msg_id,
            to_agent=to_agent_name,
            session_id=resolved_session_id,
        )

    except Exception as exc:
        delivery_error = str(exc)
        logger.exception(
            "a2a_dispatch: delivery failed",
            msg_id=msg_id,
            error=delivery_error,
        )

    # -------------------------------------------------------------------
    # 4a. Success path — two-phase write prevents duplicate-session on DB failure
    #
    #  Phase A: record resolved_session_id (owned-claim guard).
    #           If this commit fails: stay in DELIVERING, startup sweep retries.
    #           If this commit succeeds: sweep can skip re-creation if Phase B fails.
    #  Phase B: flip status to DELIVERED.
    # -------------------------------------------------------------------
    if delivery_error is None and resolved_session_id is not None:
        try:
            async with get_async_session() as db:
                await db.execute(
                    _RECORD_RESOLVED_SESSION_SQL,
                    {"sid": resolved_session_id, "id": str(msg_uuid), "claimed_at": claimed_at},
                )
                await db.commit()
        except Exception:
            logger.exception(
                "a2a_dispatch: session created but failed to record resolved_session_id "
                "— leaving in DELIVERING for startup sweep recovery (task [9])",
                msg_id=msg_id,
                session_id=resolved_session_id,
            )
            return  # leave in DELIVERING; don't touch RELEASE path

        try:
            async with get_async_session() as db:
                await db.execute(
                    _MARK_DELIVERED_SQL,
                    {"id": str(msg_uuid), "claimed_at": claimed_at},
                )
                await db.commit()
        except Exception:
            logger.exception(
                "a2a_dispatch: resolved_session_id recorded but failed to mark DELIVERED "
                "— startup sweep (task [9]) will finalize via resolved_session_id IS NOT NULL check",
                msg_id=msg_id,
                session_id=resolved_session_id,
            )
        return

    # -------------------------------------------------------------------
    # 4b. Failure path — release the claim; re-enqueue if attempts remain
    # -------------------------------------------------------------------
    attempt_count = int(row["attempt_count"])
    max_attempts = int(row["max_attempts"])

    try:
        async with get_async_session() as db:
            await db.execute(
                _RELEASE_CLAIM_SQL,
                {"err": delivery_error, "id": str(msg_uuid), "claimed_at": claimed_at},
            )
            await db.commit()
    except Exception:
        logger.exception(
            "a2a_dispatch: failed to release claim — message remains in DELIVERING until startup sweep",
            msg_id=msg_id,
        )
        return  # don't try Redis push if the DB write failed

    if attempt_count < max_attempts:
        try:
            redis_client = await get_redis_client()
            await redis_client.rpush(A2A_DISPATCH_KEY, msg_id)  # type: ignore[misc]
            logger.info(
                "a2a_dispatch: re-enqueued after failure",
                msg_id=msg_id,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
            )
        except Exception:
            logger.exception(
                "a2a_dispatch: failed to re-enqueue — will be recovered at next startup",
                msg_id=msg_id,
            )
    else:
        logger.error(
            "a2a_dispatch: message exhausted max attempts — marked FAILED",
            msg_id=msg_id,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )


# ---------------------------------------------------------------------------
# Background listener
# ---------------------------------------------------------------------------


async def listen_new_session_messages() -> None:
    """Background task: block on ``a2a_dispatch`` and dispatch deliveries.

    Uses Redis ``BLPOP`` so the task wakes up immediately when a message
    arrives (zero polling latency) and burns near-zero CPU when the queue is
    empty.  A 30-second timeout causes the blocking call to return ``None``
    periodically, keeping the loop alive and surfacing Redis connectivity
    issues promptly.

    Each delivery is dispatched as an :func:`asyncio.create_task` whose
    reference is held in ``_background_delivery_tasks`` (prevents GC of
    running tasks).  The semaphore ``_delivery_semaphore`` bounds the number
    of concurrent deliveries to avoid saturating the DB connection pool during
    post-outage burst recovery.

    The Redis client is fetched once before the loop and re-fetched only after
    a connection error to avoid per-iteration allocation overhead.

    Designed to run for the lifetime of the process.  Any exception is caught
    and logged; the loop resumes after a 1-second back-off.
    """
    logger.info("a2a_dispatch listener started")
    redis_client = await get_redis_client()
    while True:
        try:
            # BLPOP returns (key, value) on success or None on timeout.
            # Keys must be a list — passing a bare str causes redis-py to iterate
            # it character-by-character.  decode_responses=True means both
            # elements are already str (no .decode() needed).
            result: tuple[str, str] | None = await redis_client.blpop(  # type: ignore[misc]
                [A2A_DISPATCH_KEY], timeout=30
            )
            if result:
                _, msg_id = result
                task = asyncio.create_task(_deliver_new_session_message(msg_id))
                _background_delivery_tasks.add(task)
                task.add_done_callback(_background_delivery_tasks.discard)
        except Exception:
            logger.exception("a2a_dispatch listener error — retrying in 1s")
            await asyncio.sleep(1)
            redis_client = await get_redis_client()  # reconnect after error
