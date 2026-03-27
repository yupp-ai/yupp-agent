import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

import asyncpg
from asyncpg import ConnectionDoesNotExistError, InternalClientError, ProtocolViolationError, SerializationError
from fastapi import Depends
from google.cloud.sqlcommenter.sqlalchemy.executor import BeforeExecuteFactory
from sqlalchemy import ClauseElement, Compiled, Engine, ExceptionContext, ScalarResult, event
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from ypl.backend.config import settings
from ypl.backend.utils.context_utils import async_instrumenting_context_manager
from ypl.backend.utils.monitoring import metric_record
from ypl.structured_logger import get_logger
from ypl.utils import log_retry_attempt

logger = get_logger()


DB_METRICS_EXPORT_INTERVAL_SECS = 5
RETRY_ATTEMPTS = 4
RETRY_ATTEMPTS_EXTENDED = 8  # For batch jobs that run infrequently and need more resilience
RETRY_WAIT_MULTIPLIER = 1.618  # golden ratio
RETRY_WAIT_MULTIPLIER_EXTENDED = 2.0
RETRY_WAIT_MIN = 1
RETRY_WAIT_MAX = 30
RETRY_WAIT_MAX_EXTENDED = 130


# Shared pool configuration for all engines
_POOL_CONFIG: dict[str, Any] = {
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "pool_size": 45,
    "max_overflow": 100,
}

# pgbouncer/MCP transaction-mode compat (used by all async engines)
_PGBOUNCER_CONNECT_ARGS: dict[str, Any] = {
    "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    "statement_cache_size": 0,
    "prepared_statement_cache_size": 0,
}

engine: Engine | None = None
engine_read_replica: Engine | None = None
async_engine: AsyncEngine | None = None
async_engine_read_replica: AsyncEngine | None = None

# Cloud SQL Python Connector singleton (lazy-initialized)
_cloud_sql_connector: Any = None
_cloud_sql_connector_lock = asyncio.Lock()


async def _get_cloud_sql_connector() -> Any:
    global _cloud_sql_connector
    async with _cloud_sql_connector_lock:
        if _cloud_sql_connector is None:
            from google.cloud.sql.connector import create_async_connector

            _cloud_sql_connector = await create_async_connector()
    return _cloud_sql_connector


async def close_cloud_sql_connector() -> None:
    global _cloud_sql_connector
    if _cloud_sql_connector is not None:
        await _cloud_sql_connector.close_async()
        _cloud_sql_connector = None


def _attach_engine_listeners(target_engine: AsyncEngine | Engine) -> None:
    """Attach sqlcommenter and error-handling listeners to an engine."""
    listener = BeforeExecuteFactory(
        with_db_driver=True,
        with_db_framework=True,
        with_opentelemetry=True,
    )
    sync_engine = target_engine.sync_engine if isinstance(target_engine, AsyncEngine) else target_engine
    event.listen(sync_engine, "before_cursor_execute", listener, retval=True)
    event.listen(sync_engine, "handle_error", on_engine_error)


def _make_cloud_sql_creator(instance_name: str, user: str, password: str, db: str) -> Any:
    """Build a ``creator`` for cloud-sql-python-connector.

    Uses ``creator`` instead of ``async_creator`` because the latter bypasses
    ``connect_args`` entirely (see asyncpg#1058). All three pgbouncer/MCP compat
    settings must reach the dialect: ``statement_cache_size``,
    ``prepared_statement_cache_size``, and ``prepared_statement_name_func``.
    """

    async def _create_raw_conn(**kwargs: Any) -> Any:
        connector = await _get_cloud_sql_connector()
        return await connector.connect_async(
            instance_name,
            "asyncpg",
            user=user,
            password=password,
            db=db,
            **kwargs,
        )

    def _creator() -> Any:
        dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)  # type: ignore[no-untyped-call]
        return dbapi.connect(  # type: ignore[no-untyped-call]
            async_creator_fn=_create_raw_conn,
            **_PGBOUNCER_CONNECT_ARGS,
        )

    return _creator


def _is_disconnect_error(exception: BaseException) -> bool:
    """
    Check if an exception indicates a database connection disconnect.

    Args:
        exception: The exception to check

    Returns:
        True if the exception indicates a disconnect, False otherwise.
    """
    if isinstance(exception, InternalClientError) and "operation (2)" in str(exception):
        # operation (2) maps to PROTOCOL_ERROR_CONSUME in asyncpg, a connection gets to this state when it receives
        # an error. Practically, this happens most when a connection is disconnected from the DB.
        # We don't treat all the InternalClientError as disconnect-ish conditions, as we also see
        # `InternalClientError: not connected` errors, specifically when trying to terminate a connection.
        return True
    if isinstance(exception, ConnectionDoesNotExistError):
        # ConnectionDoesNotExistError indicates the connection appears closed, mark as disconnect
        return True
    return False


def on_engine_error(ctx: ExceptionContext) -> None:
    # ctx.original_exception is the DBAPI/driver error (asyncpg here)
    if _is_disconnect_error(ctx.original_exception):
        # tell SQLAlchemy this is a disconnect-ish condition and to discard the connection
        ctx.is_disconnect = True  # type: ignore[misc]
        error_type_name = type(ctx.original_exception).__name__
        logger.warning(
            f"Got {error_type_name}, marking connection as disconnect-ish.",
            exc_info=ctx.original_exception,
        )


def get_engine() -> Engine:
    global engine
    if engine is None:
        engine = create_engine(
            str(settings.db_url),
            **_POOL_CONFIG,
            connect_args={"sslmode": settings.db_ssl_mode},
        )
        _attach_engine_listeners(engine)
    return engine


def get_engine_read_replica() -> Engine:
    # if it's not production, return regular primary instance engine
    if settings.ENVIRONMENT == "local":
        return get_engine()
    global engine_read_replica
    if engine_read_replica is None:
        engine_read_replica = create_engine(
            str(settings.db_url_read_replica),
            **_POOL_CONFIG,
            connect_args={"sslmode": settings.db_ssl_mode},
        )
        _attach_engine_listeners(engine_read_replica)
    return engine_read_replica


def get_db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def get_raw_sql(query: ClauseElement) -> Compiled:
    return query.compile(engine, compile_kwargs={"literal_binds": True})


SessionDep = Annotated[Session, Depends(get_db)]


def get_async_engine() -> AsyncEngine:
    global async_engine
    if async_engine is None:
        if settings.ENABLE_CLOUD_SQL_CONNECTOR:
            async_engine = create_async_engine(
                "postgresql+asyncpg://",
                creator=_make_cloud_sql_creator(
                    settings.cloud_sql_instance_primary,
                    settings.POSTGRES_USER,
                    settings.POSTGRES_PASSWORD,
                    settings.POSTGRES_DATABASE,
                ),
                **_POOL_CONFIG,
            )
        else:
            async_engine = create_async_engine(
                str(settings.db_url_async),
                **_POOL_CONFIG,
                connect_args={"ssl": settings.db_ssl_mode, **_PGBOUNCER_CONNECT_ARGS},
            )
        _attach_engine_listeners(async_engine)
    return async_engine


def get_async_engine_read_replica() -> AsyncEngine:
    # if it's not production, return regular primary instance engine
    if settings.ENVIRONMENT == "local":
        return get_async_engine()
    global async_engine_read_replica
    if async_engine_read_replica is None:
        if settings.ENABLE_CLOUD_SQL_CONNECTOR:
            async_engine_read_replica = create_async_engine(
                "postgresql+asyncpg://",
                creator=_make_cloud_sql_creator(
                    settings.cloud_sql_instance_read_replica,
                    # We're connecting to Managed Connection Pooler,
                    # which needs the same credentials as the primary instance.
                    settings.POSTGRES_USER,
                    settings.POSTGRES_PASSWORD,
                    settings.POSTGRES_DATABASE,
                ),
                **_POOL_CONFIG,
                isolation_level="READ COMMITTED",
            )
        else:
            async_engine_read_replica = create_async_engine(
                str(settings.db_url_async_read_replica),
                **_POOL_CONFIG,
                isolation_level="READ COMMITTED",
                connect_args={"ssl": settings.db_ssl_mode, **_PGBOUNCER_CONNECT_ARGS},
            )
        _attach_engine_listeners(async_engine_read_replica)
    return async_engine_read_replica


def get_ypl_caller() -> str:
    frame = sys._getframe(
        5
    )  # 0=this fn, 1=event‐dispatcher, 2=first SQLAlchemy frame, 3=context_utils.py, 4=contextlib
    if frame:
        mod = frame.f_globals.get("__name__", "")
        code = frame.f_code
        return f"{mod}.{code.co_name}"
    return "(unknown)"  # type: ignore[unreachable]


async_session_maker = async_sessionmaker(
    get_async_engine(),
    # Uses the SQLModel AsyncSession class to ensure that the session is compatible with SQLModel
    class_=AsyncSession,
    expire_on_commit=False,
    close_resets_only=False,
)

# Add session maker for read replica
async_session_maker_replica = async_sessionmaker(
    get_async_engine_read_replica(),
    # Uses the SQLModel AsyncSession class to ensure that the session is compatible with SQLModel
    class_=AsyncSession,
    expire_on_commit=False,
    close_resets_only=False,
)


def _get_session_connections(session: AsyncSession) -> list[Any]:
    """Extract all existing SA Connections from a session without creating new ones.

    SessionTransaction.close() nullifies session._transaction *before* closing the
    underlying connections.  If close() then raises, the orphaned connections become
    unreachable through the session's public API.  By capturing them beforehand we can
    still invalidate them in the error handler.

    Returns a list of SA Connection objects if any are currently checked out, else [].
    """
    try:
        trans = session.sync_session._transaction
        # Guard against internal structure changes in SQLAlchemy
        if trans is not None and hasattr(trans, "_connections") and isinstance(trans._connections, dict):
            # _connections maps engine -> (connection, sub_transaction, should_commit, autoclose)
            return [conn for conn, *_ in trans._connections.values()]
        if trans is not None:
            logging.warning("SessionTransaction internals changed - _connections not found or not a dict")
    except Exception:
        logging.warning(
            "Could not extract raw connections from session internals. "
            "If session.close() fails, connections may be orphaned.",
            exc_info=True,
        )
    return []


async def _close_session_safely(session: AsyncSession) -> None:
    """
    Safely close an async session, ensuring connections are invalidated if close fails.

    When session.close() fails (e.g., due to a dead connection), connections may be left
    in a "non-checked-in" state, causing the garbage collector to later warn about them.
    This function captures references to the underlying connections *before* calling close(),
    so it can invalidate them directly if close() raises.
    """
    # Capture all connections before close() clears session._transaction.
    # See SessionTransaction.close() in SQLAlchemy: it sets
    #   self.session._transaction = self._parent  (often None)
    # *before* attempting connection.close(), so after a failed close() the
    # orphaned connections are unreachable through the session.
    pre_close_conns = _get_session_connections(session)

    try:
        await session.close()
    except BaseException as close_error:
        logging.warning("Error closing session, attempting to invalidate connections", exc_info=True)
        # When close() fails, the connections are likely dead. Invalidate them so the
        # pool discards them rather than leaving them as "non-checked-in".
        #
        # IMPORTANT: We must NOT call `await session.connection()` here because
        # session._transaction is None at this point (cleared before the failure),
        # so session.connection() would create a NEW connection via _autobegin_t()
        # instead of returning the orphaned ones.
        for conn in pre_close_conns:
            _invalidate_connection(conn)
        # CancelledError/KeyboardInterrupt/SystemExit must propagate after cleanup.
        if not isinstance(close_error, Exception):
            raise


def _invalidate_connection(conn: Any) -> None:
    """Invalidate a connection, logging any errors. This is synchronous (no I/O)."""
    if conn is None:
        return
    try:
        conn.invalidate()
        logging.info("Successfully invalidated orphaned connection")
    except Exception:
        # If invalidation also fails, the connection will eventually be cleaned up
        # by the pool's other mechanisms (pool_pre_ping, pool_recycle).
        logging.warning("Error invalidating connection after failed session close", exc_info=True)


@async_instrumenting_context_manager(metric_prefix="db/session/primary")
@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    session = None
    try:
        session = async_session_maker()
        yield session
    finally:
        if session is not None:
            await _close_session_safely(session)


@async_instrumenting_context_manager(metric_prefix="db/session/read_replica")
@asynccontextmanager
async def get_async_session_read_replica() -> AsyncGenerator[AsyncSession, None]:
    session = None
    try:
        session = async_session_maker_replica() if settings.ENVIRONMENT != "local" else async_session_maker()
        yield session
    finally:
        if session is not None:
            await _close_session_safely(session)


async def periodically_export_db_metrics() -> None:
    engines: dict[str, Engine | AsyncEngine] = {
        "sync": get_engine(),
        "sync_read_replica": get_engine_read_replica(),
        "async": get_async_engine(),
        "async_read_replica": get_async_engine_read_replica(),
    }
    while True:
        await asyncio.sleep(DB_METRICS_EXPORT_INTERVAL_SECS)
        for name, engine in engines.items():
            metric_record(f"db_engine/{name}/active_db_connections", engine.pool.checkedout())  # type: ignore
            metric_record(f"db_engine/{name}/idle_db_connections", engine.pool.checkedin())  # type: ignore
            metric_record(f"db_engine/{name}/overflow_db_connections", engine.pool.overflow())  # type: ignore
            metric_record(f"db_engine/{name}/pool_size", engine.pool.size())  # type: ignore


# Two-layer retry strategy for DB exceptions:
#
# Layer 1 – asyncpg raw types: asyncpg PostgresError subclasses (e.g.
# SerializationError, ConnectionDoesNotExistError) propagate *unwrapped* from
# connection-creation / pool-checkout paths. We list them explicitly so tenacity
# can match them directly.
#
# Layer 2 – SQLAlchemy-wrapped types: During query execution, asyncpg errors are
# translated by the SA asyncpg dialect. Most transient asyncpg errors (40xxx
# transaction rollback, 08xxx connection, 53xxx resources, 57xxx operator
# intervention) become generic sa.DBAPIError because the dialect's catch-all maps
# asyncpg.PostgresError → dbapi.Error → sa.DBAPIError. We therefore include
# DBAPIError here and exclude the non-transient subclasses (ProgrammingError,
# DataError, NotSupportedError, IntegrityError) via _RETRY_DB_NEVER_TYPES below.
#
# SQLAlchemy exceptions: https://docs.sqlalchemy.org/en/20/core/exceptions.html
_RETRY_DB_EXCEPTION_TYPES = (
    # --- asyncpg (unwrapped from connection-creation / pool-checkout) ---
    # asyncpg: connection does not exist (08003); retriable.
    ConnectionDoesNotExistError,
    # built-in: connection refused (e.g. DB not listening); retriable.
    ConnectionRefusedError,
    # built-in: connection reset by peer; retriable.
    ConnectionResetError,
    # asyncpg: unexpected client-side errors; broad but kept for transient cases.
    InternalClientError,
    # asyncpg: protocol violation / connection corruption (08P01); retriable.
    ProtocolViolationError,
    # asyncpg: PostgreSQL 40001 serialization_failure; standard retry case.
    SerializationError,
    # built-in: socket/connection timeout; retriable.
    TimeoutError,
    # --- SQLAlchemy (wrapped from query execution) ---
    # SA catch-all for asyncpg transient errors (serialization 40001, deadlock
    # 40P01, connection 08xxx, etc.) that the dialect maps to generic DBAPIError.
    # Non-transient subclasses are excluded via _RETRY_DB_NEVER_TYPES.
    DBAPIError,
    # SA/DB-API: e.g. "current transaction is aborted"; retriable with fresh session.
    InternalError,
    # SA/DB-API: database interface/connection issues; retriable.
    InterfaceError,
    # SA/DB-API: disconnects, pool timeout, operational failures; retriable.
    OperationalError,
    # SQLAlchemy ORM: optimistic lock conflict; retry can succeed.
    StaleDataError,
)

# Non-transient DB-API subclasses that should never be retried, even though their
# parent (DBAPIError) is in the retry list.
_RETRY_DB_NEVER_TYPES = (
    DataError,
    IntegrityError,
    NotSupportedError,
    ProgrammingError,
)

_RETRY_DB_CONDITION = retry_if_exception_type(_RETRY_DB_EXCEPTION_TYPES).__and__(
    retry_if_not_exception_type(_RETRY_DB_NEVER_TYPES)
)

_RETRY_DB_AFTER = log_retry_attempt(logging.getLogger(), logging.WARNING, finally_log_error=True)


def _reraise_or_return_result(retry_state: RetryCallState) -> Any:
    """When retries are exhausted: reraise if the last attempt raised, otherwise return the result."""
    if retry_state.outcome and retry_state.outcome.failed:
        raise retry_state.outcome.exception()  # type: ignore[misc]
    return retry_state.outcome.result() if retry_state.outcome else None


def make_retry_db(
    attempts: int = RETRY_ATTEMPTS,
    multiplier: float = RETRY_WAIT_MULTIPLIER,
    min_wait: float = RETRY_WAIT_MIN,
    max_wait: float = RETRY_WAIT_MAX,
    retry_on_none: bool = False,
) -> Any:
    """Create a retry decorator for DB operations with configurable timing.

    Use this for non-default retry configs (e.g., ``retry_on_none``).
    For standard DB operations, use the pre-built ``retry_db``, ``retry_db_fast``,
    or ``retry_db_extended`` decorators.

    Args:
        retry_on_none: Also retry when the function returns None (e.g., waiting for a
            row that's being written by a concurrent background task). When retries are
            exhausted, None is returned (not raised as RetryError).
    """
    stop_rule = stop_after_attempt(attempts)
    wait_rule = wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait)

    if retry_on_none:
        # Retry on DB exceptions *or* None results. Use a callback so that exhausted
        # None-retries return None instead of raising RetryError.
        return retry(
            stop=stop_rule,
            wait=wait_rule,
            retry=_RETRY_DB_CONDITION | retry_if_result(lambda r: r is None),
            after=_RETRY_DB_AFTER,
            retry_error_callback=_reraise_or_return_result,
        )

    return retry(
        stop=stop_rule,
        wait=wait_rule,
        retry=_RETRY_DB_CONDITION,
        after=_RETRY_DB_AFTER,
        reraise=True,
    )


# Default retry decorator for DB operations
retry_db = retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=RETRY_WAIT_MULTIPLIER, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    retry=_RETRY_DB_CONDITION,
    after=_RETRY_DB_AFTER,
    reraise=True,
)

# Fast retry decorator for latency-sensitive paths (e.g., waiting for a background-task DB write)
retry_db_fast = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.1, min=0.1, max=0.3),
    retry=_RETRY_DB_CONDITION,
    after=_RETRY_DB_AFTER,
    reraise=True,
)

# Extended retry decorator for batch/daily jobs that need more resilience
retry_db_extended = retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS_EXTENDED),
    wait=wait_exponential(multiplier=RETRY_WAIT_MULTIPLIER_EXTENDED, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX_EXTENDED),
    retry=_RETRY_DB_CONDITION,
    after=_RETRY_DB_AFTER,
    reraise=True,
)


@retry_db
async def query_db(session: AsyncSession, query: Any) -> Sequence[Any]:
    try:
        result: ScalarResult = await session.exec(query)
        return result.all()
    except Exception:
        try:
            await session.rollback()
        except Exception as e:
            logger.warning("Erorr in rollback", error=e)
        logger.warning("Error querying database", query=query)
        raise


@retry_db
async def query_read_replica(query: Any, debug: bool = False) -> Sequence[Any]:
    async with get_async_session_read_replica() as session:
        try:
            if debug:
                logger.info("Compiled SQL query", query=str(query.compile(compile_kwargs={"literal_binds": True})))
            result: ScalarResult = await session.exec(query)
            return result.all()
        except Exception:
            await session.rollback()
            logger.warning("Error querying database", query=query)
            raise
