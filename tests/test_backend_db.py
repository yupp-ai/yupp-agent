"""Unit tests for ypl/backend/db.py.

Covers:
- _is_disconnect_error: disconnect detection logic
- on_engine_error: ctx.is_disconnect flag
- _invalidate_connection: safe invalidation helper
- _get_session_connections: session internals extraction
- _close_session_safely: error recovery on close()
- get_engine_for / get_async_engine_for: local-env replica fallback
- make_retry_db: retry decorator factory
- _reraise_or_return_result: retry callback
- GitHubTokenData helpers (via db module import)
"""

from __future__ import annotations
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg import ConnectionDoesNotExistError, InternalClientError
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.orm.exc import StaleDataError
from ypl.backend.db import (
    _RETRY_DB_CONDITION,
    _close_session_safely,
    _get_session_connections,
    _invalidate_connection,
    _is_disconnect_error,
    _reraise_or_return_result,
    make_retry_db,
    on_engine_error,
)

# ---------------------------------------------------------------------------
# _is_disconnect_error
# ---------------------------------------------------------------------------


class TestIsDisconnectError:
    def test_internal_client_error_with_operation_2_is_disconnect(self) -> None:
        exc = InternalClientError("something operation (2) happened")
        assert _is_disconnect_error(exc) is True

    def test_internal_client_error_without_operation_2_is_not_disconnect(self) -> None:
        exc = InternalClientError("not connected")
        assert _is_disconnect_error(exc) is False

    def test_connection_does_not_exist_error_is_disconnect(self) -> None:
        exc = ConnectionDoesNotExistError()
        assert _is_disconnect_error(exc) is True

    def test_generic_exception_is_not_disconnect(self) -> None:
        exc = ValueError("some other error")
        assert _is_disconnect_error(exc) is False

    def test_operational_error_is_not_disconnect(self) -> None:
        exc = OperationalError("db error", params=None, orig=Exception())
        assert _is_disconnect_error(exc) is False


# ---------------------------------------------------------------------------
# on_engine_error
# ---------------------------------------------------------------------------


class TestOnEngineError:
    def _make_ctx(self, exc: BaseException) -> MagicMock:
        ctx = MagicMock()
        ctx.original_exception = exc
        ctx.is_disconnect = False
        return ctx

    def test_disconnect_error_sets_is_disconnect(self) -> None:
        ctx = self._make_ctx(ConnectionDoesNotExistError())
        on_engine_error(ctx)
        assert ctx.is_disconnect is True

    def test_non_disconnect_error_leaves_is_disconnect_unchanged(self) -> None:
        ctx = self._make_ctx(ValueError("not a disconnect"))
        on_engine_error(ctx)
        # is_disconnect was not set via our code (mock still has False from setup)
        assert ctx.is_disconnect is False

    def test_internal_client_error_with_operation_2_sets_is_disconnect(self) -> None:
        ctx = self._make_ctx(InternalClientError("operation (2) failed"))
        on_engine_error(ctx)
        assert ctx.is_disconnect is True

    def test_internal_client_error_without_operation_2_not_set(self) -> None:
        ctx = self._make_ctx(InternalClientError("not connected"))
        on_engine_error(ctx)
        assert ctx.is_disconnect is False


# ---------------------------------------------------------------------------
# _invalidate_connection
# ---------------------------------------------------------------------------


class TestInvalidateConnection:
    def test_none_connection_is_noop(self) -> None:
        # Should not raise
        _invalidate_connection(None)

    def test_valid_connection_is_invalidated(self) -> None:
        conn = MagicMock()
        _invalidate_connection(conn)
        conn.invalidate.assert_called_once()

    def test_invalidation_failure_is_logged_but_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        conn.invalidate.side_effect = RuntimeError("invalidation failed")
        with caplog.at_level(logging.WARNING):
            _invalidate_connection(conn)
        assert "invalidating connection" in caplog.text.lower()


# ---------------------------------------------------------------------------
# _get_session_connections
# ---------------------------------------------------------------------------


class TestGetSessionConnections:
    def _make_session(self) -> MagicMock:
        return MagicMock()

    def test_returns_empty_when_no_transaction(self) -> None:
        session = self._make_session()
        session.sync_session._transaction = None
        result = _get_session_connections(session)
        assert result == []

    def test_returns_connections_when_transaction_exists(self) -> None:
        session = self._make_session()
        mock_conn = MagicMock()
        mock_trans = MagicMock()
        mock_trans._connections = {
            "engine1": (mock_conn, "sub_tx", True, True),
        }
        session.sync_session._transaction = mock_trans
        result = _get_session_connections(session)
        assert result == [mock_conn]

    def test_returns_empty_when_connections_not_dict(self) -> None:
        session = self._make_session()
        mock_trans = MagicMock(spec=["_connections"])
        # Make _connections a non-dict (list)
        mock_trans._connections = [("conn",)]
        session.sync_session._transaction = mock_trans
        result = _get_session_connections(session)
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        # Test the "no _connections" attribute path
        session2 = self._make_session()
        mock_trans2 = MagicMock(spec=["something_else"])  # no _connections
        session2.sync_session._transaction = mock_trans2
        result = _get_session_connections(session2)
        assert result == []

    def test_returns_multiple_connections(self) -> None:
        session = self._make_session()
        conn1, conn2 = MagicMock(), MagicMock()
        mock_trans = MagicMock()
        mock_trans._connections = {
            "eng1": (conn1, None, True, False),
            "eng2": (conn2, None, True, False),
        }
        session.sync_session._transaction = mock_trans
        result = _get_session_connections(session)
        assert set(result) == {conn1, conn2}


# ---------------------------------------------------------------------------
# _close_session_safely
# ---------------------------------------------------------------------------


class TestCloseSessionSafely:
    async def test_normal_close_calls_session_close(self) -> None:
        session = MagicMock()
        session.sync_session._transaction = None
        session.close = AsyncMock()
        await _close_session_safely(session)
        session.close.assert_awaited_once()

    async def test_close_failure_invalidates_pre_captured_connections(self) -> None:
        conn = MagicMock()
        session = MagicMock()
        mock_trans = MagicMock()
        mock_trans._connections = {"eng": (conn, None, True, False)}
        session.sync_session._transaction = mock_trans

        async def failing_close() -> None:
            raise Exception("close failed")

        session.close = failing_close
        await _close_session_safely(session)
        conn.invalidate.assert_called_once()

    async def test_base_exception_reraises_after_cleanup(self) -> None:
        conn = MagicMock()
        session = MagicMock()
        mock_trans = MagicMock()
        mock_trans._connections = {"eng": (conn, None, True, False)}
        session.sync_session._transaction = mock_trans

        async def raising_keyboard_interrupt() -> None:
            raise KeyboardInterrupt("abort")

        session.close = raising_keyboard_interrupt
        with pytest.raises(KeyboardInterrupt):
            await _close_session_safely(session)
        # Connections still invalidated before re-raise
        conn.invalidate.assert_called_once()


# ---------------------------------------------------------------------------
# make_retry_db
# ---------------------------------------------------------------------------


class TestMakeRetryDb:
    def test_returns_callable_decorator(self) -> None:
        dec = make_retry_db(attempts=3)
        assert callable(dec)

    def test_decorator_wraps_function(self) -> None:
        dec = make_retry_db(attempts=3)

        @dec
        async def dummy() -> str:
            return "ok"

        assert callable(dummy)

    def test_retry_on_none_mode_returns_callable(self) -> None:
        dec = make_retry_db(attempts=2, retry_on_none=True)
        assert callable(dec)

    async def test_successful_function_returns_result(self) -> None:
        dec = make_retry_db(attempts=3, min_wait=0.0, max_wait=0.01)

        @dec
        async def succeed() -> str:
            return "success"

        result = await succeed()
        assert result == "success"

    async def test_retries_on_operational_error(self) -> None:
        """Function raising OperationalError should be retried."""
        dec = make_retry_db(attempts=3, min_wait=0.0, max_wait=0.01)
        call_count = 0

        @dec
        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OperationalError("db error", params=None, orig=Exception())
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3

    async def test_does_not_retry_programming_error(self) -> None:
        """ProgrammingError is in NEVER_TYPES, should not be retried."""
        dec = make_retry_db(attempts=4, min_wait=0.0, max_wait=0.01)
        call_count = 0

        @dec
        async def bad_sql() -> str:
            nonlocal call_count
            call_count += 1
            raise ProgrammingError("syntax error", params=None, orig=Exception())

        with pytest.raises(ProgrammingError):
            await bad_sql()
        assert call_count == 1  # No retries

    async def test_does_not_retry_integrity_error(self) -> None:
        dec = make_retry_db(attempts=4, min_wait=0.0, max_wait=0.01)
        call_count = 0

        @dec
        async def duplicate_key() -> str:
            nonlocal call_count
            call_count += 1
            raise IntegrityError("duplicate", params=None, orig=Exception())

        with pytest.raises(IntegrityError):
            await duplicate_key()
        assert call_count == 1

    async def test_does_not_retry_data_error(self) -> None:
        dec = make_retry_db(attempts=4, min_wait=0.0, max_wait=0.01)
        call_count = 0

        @dec
        async def type_mismatch() -> str:
            nonlocal call_count
            call_count += 1
            raise DataError("type mismatch", params=None, orig=Exception())

        with pytest.raises(DataError):
            await type_mismatch()
        assert call_count == 1

    async def test_does_not_retry_not_supported_error(self) -> None:
        dec = make_retry_db(attempts=4, min_wait=0.0, max_wait=0.01)
        call_count = 0

        @dec
        async def unsupported() -> str:
            nonlocal call_count
            call_count += 1
            raise NotSupportedError("not supported", params=None, orig=Exception())

        with pytest.raises(NotSupportedError):
            await unsupported()
        assert call_count == 1

    async def test_retry_on_none_retries_when_result_is_none(self) -> None:
        dec = make_retry_db(attempts=3, min_wait=0.0, max_wait=0.01, retry_on_none=True)
        call_count = 0

        @dec
        async def eventually_returns() -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return None
            return "found"

        result = await eventually_returns()
        assert result == "found"
        assert call_count == 3

    async def test_retry_on_none_returns_none_after_exhaustion(self) -> None:
        dec = make_retry_db(attempts=3, min_wait=0.0, max_wait=0.01, retry_on_none=True)

        @dec
        async def always_none() -> str | None:
            return None

        result = await always_none()
        assert result is None


# ---------------------------------------------------------------------------
# _reraise_or_return_result
# ---------------------------------------------------------------------------


class TestReraiseOrReturnResult:
    def test_reraises_on_failure(self) -> None:
        retry_state = MagicMock()
        retry_state.outcome.failed = True
        retry_state.outcome.exception.return_value = ValueError("test error")
        with pytest.raises(ValueError, match="test error"):
            _reraise_or_return_result(retry_state)

    def test_returns_result_on_success(self) -> None:
        retry_state = MagicMock()
        retry_state.outcome.failed = False
        retry_state.outcome.result.return_value = "my result"
        result = _reraise_or_return_result(retry_state)
        assert result == "my result"

    def test_returns_none_when_no_outcome(self) -> None:
        retry_state = MagicMock()
        retry_state.outcome = None
        result = _reraise_or_return_result(retry_state)
        assert result is None


# ---------------------------------------------------------------------------
# _RETRY_DB_CONDITION (module-level condition object)
# ---------------------------------------------------------------------------


class TestRetryDbCondition:
    def _exc_matches(self, exc: BaseException) -> bool:
        """Check if the retry condition matches a given exception."""
        from tenacity import RetryCallState

        state = MagicMock(spec=RetryCallState)
        state.outcome = MagicMock()
        state.outcome.exception.return_value = exc
        return bool(_RETRY_DB_CONDITION(state))

    def test_operational_error_matches(self) -> None:
        exc = OperationalError("db error", params=None, orig=Exception())
        assert self._exc_matches(exc)

    def test_stale_data_error_matches(self) -> None:
        exc = StaleDataError()
        assert self._exc_matches(exc)

    def test_programming_error_does_not_match(self) -> None:
        exc = ProgrammingError("syntax error", params=None, orig=Exception())
        assert not self._exc_matches(exc)

    def test_integrity_error_does_not_match(self) -> None:
        exc = IntegrityError("unique violation", params=None, orig=Exception())
        assert not self._exc_matches(exc)

    def test_data_error_does_not_match(self) -> None:
        exc = DataError("invalid value", params=None, orig=Exception())
        assert not self._exc_matches(exc)


# ---------------------------------------------------------------------------
# get_engine_for / get_async_engine_for: local env replica fallback
# ---------------------------------------------------------------------------


class TestEngineForLocalReplica:
    def test_get_engine_for_local_env_ignores_replica_flag(self) -> None:
        """In local env, replica=True should be treated as replica=False."""
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        mock_settings.db_url_for.return_value = "postgresql://localhost/test"
        mock_settings.db_ssl_mode = "disable"
        mock_settings.get_pg_connection = MagicMock()

        with (
            patch("ypl.backend.db.settings", mock_settings),
            patch("ypl.backend.db._engines", {}),
            patch("ypl.backend.db.create_engine") as mock_create,
            patch("ypl.backend.db._attach_engine_listeners"),
        ):
            mock_create.return_value = MagicMock()
            from ypl.backend.db import get_engine_for

            get_engine_for("yuppdb", replica=True)

            # Should have been called with replica=False (local env override)
            mock_settings.db_url_for.assert_called_once_with("yuppdb", replica=False, async_mode=False)

    def test_get_async_engine_for_local_env_ignores_replica_flag(self) -> None:
        """In local env, replica=True for async engine should be treated as False."""
        mock_settings = MagicMock()
        mock_settings.ENVIRONMENT = "local"
        mock_settings.ENABLE_CLOUD_SQL_CONNECTOR = False
        mock_conn = MagicMock()
        mock_conn.cloud_sql_proxy_socket = None
        mock_settings.get_pg_connection.return_value = mock_conn
        mock_settings.db_url_for.return_value = "postgresql+asyncpg://localhost/test"
        mock_settings.db_ssl_mode = "disable"

        with (
            patch("ypl.backend.db.settings", mock_settings),
            patch("ypl.backend.db._async_engines", {}),
            patch("ypl.backend.db.create_async_engine") as mock_create,
            patch("ypl.backend.db._attach_engine_listeners"),
        ):
            mock_create.return_value = MagicMock()
            from ypl.backend.db import get_async_engine_for

            get_async_engine_for("yuppdb", replica=True)

            # replica=True in local env → falls back to replica=False
            mock_settings.get_pg_connection.assert_called_once_with("yuppdb", replica=False)
