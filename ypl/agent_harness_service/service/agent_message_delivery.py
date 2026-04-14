"""Push-based delivery for Scenario A: new-session creation from agent messages.

Consumes the ``a2a_dispatch`` Redis list (enqueued by ``send_agent_message``) and
creates a new ``AgentSession`` for each message using an atomic DB claim protocol
to guarantee at-most-once delivery per attempt.

Design reference: http://go/p/a2a-messaging-design@5 (Sections 9, 10, 11, 13 Scenario A)

NOTE: Not wired into server.py yet — that is task [9].
"""

import asyncio
import uuid

from sqlalchemy import text

from ypl.agent_harness_service.common.types import SessionCreateRequest
from ypl.agent_harness_service.service.session_lifecycle import create_session
from ypl.backend.db import get_async_session
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Redis list key for Scenario A (new-session) messages.
A2A_DISPATCH_KEY = "a2a_dispatch"

# ---------------------------------------------------------------------------
# Atomic-claim SQL fragments
# ---------------------------------------------------------------------------

_CLAIM_MESSAGE_SQL = text("""
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
        max_attempts
""")

_MARK_DELIVERED_SQL = text("""
    UPDATE agent_messages
    SET status              = 'DELIVERED',
        delivered_at        = now(),
        resolved_session_id = :sid
    WHERE agent_message_id = :id
""")

_RELEASE_CLAIM_SQL = text("""
    UPDATE agent_messages
    SET status     = CASE WHEN attempt_count >= max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
        error      = :err,
        claimed_at = NULL
    WHERE agent_message_id = :id
""")

_LOOKUP_AGENT_NAME_SQL = text("""
    SELECT name FROM agents WHERE agent_id = :aid AND deleted_at IS NULL
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

    Args:
        msg_id: String UUID of the ``agent_message_id`` to deliver.
    """
    try:
        msg_uuid = uuid.UUID(msg_id)
    except ValueError:
        logger.error("a2a_dispatch: invalid msg_id — skipping", msg_id=msg_id)
        return

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

    # -------------------------------------------------------------------
    # 2. Resolve the recipient agent's name for SessionCreateRequest
    # -------------------------------------------------------------------
    resolved_session_id: str | None = None
    delivery_error: str | None = None

    try:
        async with get_async_session() as db:
            agent_result = await db.execute(
                _LOOKUP_AGENT_NAME_SQL,
                {"aid": str(row["to_agent_id"])},
            )
            agent_name_row = agent_result.mappings().one_or_none()

        if agent_name_row is None:
            raise ValueError(f"to_agent_id {row['to_agent_id']!r} not found in agents table")

        to_agent_name: str = str(agent_name_row["name"])
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
    # 4a. Success path — mark DELIVERED and record the new session ID
    # -------------------------------------------------------------------
    if delivery_error is None and resolved_session_id is not None:
        async with get_async_session() as db:
            await db.execute(
                _MARK_DELIVERED_SQL,
                {"sid": resolved_session_id, "id": str(msg_uuid)},
            )
            await db.commit()
        return

    # -------------------------------------------------------------------
    # 4b. Failure path — release the claim; re-enqueue if attempts remain
    # -------------------------------------------------------------------
    attempt_count = int(row["attempt_count"])
    max_attempts = int(row["max_attempts"])

    async with get_async_session() as db:
        await db.execute(
            _RELEASE_CLAIM_SQL,
            {"err": delivery_error, "id": str(msg_uuid)},
        )
        await db.commit()

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

    Each delivery is fired as a detached :func:`asyncio.ensure_future` so
    that one slow delivery cannot stall the listener.

    Designed to run for the lifetime of the process.  Any exception is
    caught and logged; the loop resumes after a 1-second back-off.
    """
    logger.info("a2a_dispatch listener started")
    while True:
        try:
            redis_client = await get_redis_client()
            # blpop returns [key, value] or None on timeout.
            # Keys must be passed as a list (str is iterated character-by-character otherwise).
            # decode_responses=True (configured in get_redis_client) means both
            # elements are already str — no .decode() call needed.
            result: list[str] | None = await redis_client.blpop(  # type: ignore[misc]
                [A2A_DISPATCH_KEY], timeout=30
            )
            if result:
                _, msg_id = result
                asyncio.ensure_future(_deliver_new_session_message(msg_id))
        except Exception:
            logger.exception("a2a_dispatch listener error — retrying in 1s")
            await asyncio.sleep(1)
