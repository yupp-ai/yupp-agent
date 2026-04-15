"""Startup recovery: repair agent messages left in inconsistent state from a previous process.

Called once during :func:`ahs_startup` — *before* the BLPOP listener starts — so that
stale leases are resolved and QUEUED rows are pushed onto Redis before any new delivery
workers begin consuming the same queues.

Design reference: http://go/p/a2a-messaging-design@5 (Sections 9, 12)
"""

from sqlalchemy import text as _text

from ypl.agent_harness_service.service.agent_message_delivery import A2A_DISPATCH_KEY
from ypl.backend.db import get_async_session
from ypl.db.redis import get_redis_client
from ypl.structured_logger import get_logger

logger = get_logger()

# Lease timeout: DELIVERING rows with claimed_at older than this are assumed abandoned.
_STALE_DELIVERING_THRESHOLD_MINUTES = 5

# Redis key prefix for per-session inboxes (Scenario B).
_SESSION_INBOX_PREFIX = "session_inbox"

# ---------------------------------------------------------------------------
# Raw SQL — UPDATE … RETURNING has no ORM equivalent
# ---------------------------------------------------------------------------

# Step 1a: DELIVERING rows where Phase A (resolved_session_id write) committed
# but Phase B (status flip to DELIVERED) did not.  The session already exists —
# just finalize the status.
_FINALIZE_RESOLVED_SQL = _text("""
    UPDATE agent_messages
    SET    status       = 'DELIVERED',
           delivered_at = now(),
           claimed_at   = NULL
    WHERE  status              = 'DELIVERING'
      AND  resolved_session_id IS NOT NULL
    RETURNING agent_message_id
""")

# Step 1b: Stale DELIVERING rows where the worker died before (or during) delivery
# and no session was created yet.  Reset to QUEUED so the listener can retry, or
# FAILED if the attempt budget is exhausted.
_RESET_STALE_DELIVERING_SQL = _text("""
    UPDATE agent_messages
    SET    status     = CASE WHEN attempt_count >= max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
           claimed_at = NULL
    WHERE  status              = 'DELIVERING'
      AND  resolved_session_id IS NULL
      AND  claimed_at < now() - INTERVAL '5 minutes'
    RETURNING agent_message_id, attempt_count, max_attempts
""")

# Step 2: Fetch every QUEUED row so it can be pushed back onto Redis.
# Ordered by created_at for deterministic re-enqueue ordering.
_FETCH_QUEUED_SQL = _text("""
    SELECT agent_message_id, to_session_id
    FROM   agent_messages
    WHERE  status = 'QUEUED'
    ORDER  BY created_at
""")


async def recover_agent_messages() -> None:
    """Recover agent messages left in inconsistent state by the previous process.

    Three recovery steps performed in order:

    1. **Finalize near-complete deliveries** — ``DELIVERING`` rows where
       ``resolved_session_id IS NOT NULL`` had their session created (Phase A of
       the two-phase success write committed) but Phase B (``status = 'DELIVERED'``)
       was not persisted before the crash.  These are finalized immediately; no
       session re-creation is needed.

    2. **Reset stale leases** — ``DELIVERING`` rows with ``claimed_at`` older than
       :data:`_STALE_DELIVERING_THRESHOLD_MINUTES` minutes and no
       ``resolved_session_id`` are presumed abandoned.  They are reset to ``QUEUED``
       (or ``FAILED`` if ``attempt_count >= max_attempts``) so the BLPOP listener
       can retry them.

    3. **Re-enqueue QUEUED rows** — All rows with ``status = 'QUEUED'`` (both
       pre-existing and newly reset by step 2) are pushed onto the appropriate
       Redis queue:

       - ``to_session_id IS NULL`` → ``RPUSH a2a_dispatch`` (Scenario A)
       - ``to_session_id IS NOT NULL`` → ``RPUSH session_inbox:{to_session_id}``
         (Scenario B)

       This is a blind RPUSH — safe because the atomic-claim protocol in the
       delivery workers absorbs any duplicates.

    This coroutine is *awaited* inside :func:`ahs_startup` before
    :func:`listen_new_session_messages` is created, so there is no race between
    recovery pushes and the listener consuming the same queue.

    Non-fatal: any exception is caught and logged; startup continues regardless.
    """
    try:
        # ── Step 1a: finalize rows where session was already created ─────────
        async with get_async_session() as db:
            fin_result = await db.execute(_FINALIZE_RESOLVED_SQL)
            finalized_ids = fin_result.fetchall()
            await db.commit()

        finalized = len(finalized_ids)
        if finalized:
            logger.info(
                "recover_agent_messages: finalized DELIVERING rows with resolved_session_id",
                count=finalized,
            )

        # ── Step 1b: reset stale DELIVERING leases ───────────────────────────
        async with get_async_session() as db:
            reset_result = await db.execute(_RESET_STALE_DELIVERING_SQL)
            reset_rows = reset_result.fetchall()
            await db.commit()

        reset_to_queued = sum(1 for r in reset_rows if r.attempt_count < r.max_attempts)
        reset_to_failed = len(reset_rows) - reset_to_queued

        if reset_rows:
            logger.info(
                "recover_agent_messages: reset stale DELIVERING rows",
                reset_to_queued=reset_to_queued,
                reset_to_failed=reset_to_failed,
            )

        # ── Step 2: re-enqueue all QUEUED rows onto Redis ────────────────────
        async with get_async_session() as db:
            queued_result = await db.execute(_FETCH_QUEUED_SQL)
            queued_rows = queued_result.fetchall()

        if not queued_rows:
            logger.info(
                "recover_agent_messages: completed — no QUEUED rows to re-enqueue",
                finalized=finalized,
                reset_to_queued=reset_to_queued,
                reset_to_failed=reset_to_failed,
            )
            return

        redis_client = await get_redis_client()

        # Separate Scenario A (global dispatch) from Scenario B (per-session inboxes).
        dispatch_ids: list[str] = []
        inbox_pushes: dict[str, list[str]] = {}

        for row in queued_rows:
            msg_id = str(row.agent_message_id)
            if row.to_session_id is None:
                dispatch_ids.append(msg_id)
            else:
                key = f"{_SESSION_INBOX_PREFIX}:{row.to_session_id}"
                inbox_pushes.setdefault(key, []).append(msg_id)

        # Batch RPUSH: redis-py accepts multiple values in a single call.
        if dispatch_ids:
            await redis_client.rpush(A2A_DISPATCH_KEY, *dispatch_ids)  # type: ignore[misc]

        for inbox_key, msg_ids in inbox_pushes.items():
            await redis_client.rpush(inbox_key, *msg_ids)  # type: ignore[misc]

        re_enqueued = len(dispatch_ids) + sum(len(v) for v in inbox_pushes.values())
        logger.info(
            "recover_agent_messages: completed",
            finalized=finalized,
            reset_to_queued=reset_to_queued,
            reset_to_failed=reset_to_failed,
            re_enqueued=re_enqueued,
            dispatch_count=len(dispatch_ids),
            inbox_session_count=len(inbox_pushes),
        )

    except Exception:
        logger.exception("recover_agent_messages: unexpected failure — non-fatal")
