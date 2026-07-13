"""Unit tests for ypl/mcp_server/tools/database.py.

Tests cover SQL validation helpers, BigQuery cost checking, query execution,
and the top-level Postgres/BigQuery tool implementations.
"""

from __future__ import annotations
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.mcp_server.tools.database import (
    _check_bigquery_cost,
    _execute_bigquery_query,
    _prepare_bigquery_sql,
    _query_bigquery_impl,
    _query_postgres_impl,
    _strip_sql_comments,
    _validate_bigquery_query,
    query_agentdb,
    query_appdb,
    query_bigquery,
    query_bigquery_expensive,
)

# ---------------------------------------------------------------------------
# _strip_sql_comments
# ---------------------------------------------------------------------------


class TestStripSqlComments:
    def test_no_comments(self) -> None:
        sql = "SELECT id FROM users"
        assert _strip_sql_comments(sql) == sql

    def test_line_comment(self) -> None:
        result = _strip_sql_comments("-- find all\nSELECT 1")
        assert "--" not in result
        assert "SELECT 1" in result

    def test_block_comment(self) -> None:
        result = _strip_sql_comments("/* header */ SELECT 1")
        assert "/*" not in result
        assert "SELECT 1" in result

    def test_multiline_block_comment(self) -> None:
        sql = "/* line1\n   line2 */ SELECT id FROM tbl"
        result = _strip_sql_comments(sql)
        assert "SELECT id FROM tbl" in result

    def test_strips_surrounding_whitespace(self) -> None:
        result = _strip_sql_comments("  SELECT 1  ")
        assert result == "SELECT 1"

    def test_combined_comments(self) -> None:
        sql = "-- comment\n/* block */\nSELECT 42"
        result = _strip_sql_comments(sql)
        assert "SELECT 42" in result
        assert "--" not in result
        assert "/*" not in result


# ---------------------------------------------------------------------------
# _validate_bigquery_query
# ---------------------------------------------------------------------------


class TestValidateBigqueryQuery:
    def test_valid_select(self) -> None:
        assert _validate_bigquery_query("SELECT * FROM tbl") is None

    def test_rejects_insert(self) -> None:
        err = _validate_bigquery_query("INSERT INTO tbl VALUES (1)")
        assert err is not None
        assert err["success"] is False
        assert "Only SELECT" in err["error"]

    def test_rejects_update(self) -> None:
        err = _validate_bigquery_query("UPDATE tbl SET x=1")
        assert err is not None
        assert "Only SELECT" in err["error"]

    def test_rejects_delete(self) -> None:
        err = _validate_bigquery_query("DELETE FROM tbl")
        assert err is not None

    def test_rejects_drop(self) -> None:
        err = _validate_bigquery_query("DROP TABLE tbl")
        assert err is not None
        assert "Only SELECT" in err["error"]

    def test_rejects_create(self) -> None:
        err = _validate_bigquery_query("CREATE TABLE foo (id INT)")
        assert err is not None

    def test_rejects_truncate(self) -> None:
        err = _validate_bigquery_query("TRUNCATE TABLE foo")
        assert err is not None

    def test_rejects_alter(self) -> None:
        err = _validate_bigquery_query("ALTER TABLE foo ADD COLUMN x INT")
        assert err is not None

    def test_rejects_grant(self) -> None:
        err = _validate_bigquery_query("GRANT SELECT ON tbl TO user")
        assert err is not None

    def test_rejects_non_select(self) -> None:
        err = _validate_bigquery_query("CALL my_proc()")
        assert err is not None
        assert "Only SELECT queries are allowed" in err["error"]

    def test_word_boundary_allows_create_date_column(self) -> None:
        # "create_date" should NOT be rejected
        result = _validate_bigquery_query("SELECT create_date FROM users")
        assert result is None

    def test_strip_leading_comment_then_select(self) -> None:
        sql = "-- annotation\nSELECT 1"
        assert _validate_bigquery_query(sql) is None

    def test_case_insensitive_dangerous_keyword(self) -> None:
        err = _validate_bigquery_query("select 1; delete from foo")
        assert err is not None

    def test_cte_query_rejected(self) -> None:
        # CTE queries don't start with SELECT and are rejected (known limitation)
        err = _validate_bigquery_query("WITH t AS (SELECT 1) SELECT * FROM t")
        assert err is not None
        assert "Only SELECT" in err["error"]


# ---------------------------------------------------------------------------
# _prepare_bigquery_sql
# ---------------------------------------------------------------------------


class TestPrepareBigquerySql:
    def test_adds_limit_when_missing(self) -> None:
        result = _prepare_bigquery_sql("SELECT * FROM t", 50)
        assert "LIMIT 50" in result

    def test_no_limit_added_when_present(self) -> None:
        sql = "SELECT * FROM t LIMIT 10"
        result = _prepare_bigquery_sql(sql, 50)
        assert "LIMIT 50" not in result
        assert "LIMIT 10" in result

    def test_strips_trailing_semicolon(self) -> None:
        result = _prepare_bigquery_sql("SELECT 1;", 100)
        assert not result.rstrip().endswith(";")

    def test_case_insensitive_limit_check(self) -> None:
        sql = "SELECT * FROM t limit 5"
        result = _prepare_bigquery_sql(sql, 100)
        assert "LIMIT 100" not in result


# ---------------------------------------------------------------------------
# _check_bigquery_cost
# ---------------------------------------------------------------------------


class TestCheckBigqueryCost:
    def _make_client(self, bytes_processed: int) -> MagicMock:
        job = MagicMock()
        job.total_bytes_processed = bytes_processed
        client = MagicMock()
        client.query.return_value = job
        return client

    def test_disabled_when_limit_zero(self) -> None:
        client = self._make_client(10**12)  # 1TB — would exceed any limit
        result = _check_bigquery_cost(client, "SELECT 1", max_bytes_gb=0)
        assert result is None

    def test_passes_when_under_limit(self) -> None:
        one_gb = 1024**3
        client = self._make_client(int(0.5 * one_gb))  # 0.5 GB
        result = _check_bigquery_cost(client, "SELECT 1", max_bytes_gb=1.0)
        assert result is None

    def test_fails_when_over_limit(self) -> None:
        two_gb = 2 * 1024**3
        client = self._make_client(int(two_gb))
        result = _check_bigquery_cost(client, "SELECT 1", max_bytes_gb=1.0)
        assert result is not None
        assert result["success"] is False
        assert "exceeds the limit" in result["error"]
        assert result["limit_gb"] == 1.0

    def test_none_bytes_treated_as_zero(self) -> None:
        job = MagicMock()
        job.total_bytes_processed = None
        client = MagicMock()
        client.query.return_value = job
        result = _check_bigquery_cost(client, "SELECT 1", max_bytes_gb=1.0)
        assert result is None


# ---------------------------------------------------------------------------
# _execute_bigquery_query
# ---------------------------------------------------------------------------


class TestExecuteBigqueryQuery:
    def _make_row(self, **kwargs: Any) -> MagicMock:
        row = MagicMock()
        row.items.return_value = list(kwargs.items())
        return row

    def _make_client(self, rows: list[MagicMock], total_bytes: int = 0) -> MagicMock:
        result = MagicMock()
        result.__iter__ = MagicMock(return_value=iter(rows))
        job = MagicMock()
        job.result.return_value = result
        job.total_bytes_processed = total_bytes
        client = MagicMock()
        client.query.return_value = job
        return client

    def test_returns_formatted_rows(self) -> None:
        rows = [self._make_row(id=1, name="alice"), self._make_row(id=2, name="bob")]
        client = self._make_client(rows)
        result = _execute_bigquery_query(client, "SELECT id, name FROM t", max_rows=10)
        assert result["success"] is True
        assert result["row_count"] == 2
        assert result["results"][0]["id"] == 1
        assert result["results"][1]["name"] == "bob"

    def test_converts_datetime_to_iso(self) -> None:
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        rows = [self._make_row(created_at=dt)]
        client = self._make_client(rows)
        result = _execute_bigquery_query(client, "SELECT created_at FROM t", max_rows=10)
        assert result["results"][0]["created_at"] == dt.isoformat()

    def test_converts_decimal_to_float(self) -> None:
        rows = [self._make_row(price=Decimal("9.99"))]
        client = self._make_client(rows)
        result = _execute_bigquery_query(client, "SELECT price FROM t", max_rows=10)
        assert result["results"][0]["price"] == pytest.approx(9.99)

    def test_respects_max_rows(self) -> None:
        rows = [self._make_row(id=i) for i in range(5)]
        client = self._make_client(rows)
        result = _execute_bigquery_query(client, "SELECT id FROM t", max_rows=3)
        assert result["row_count"] == 3

    def test_empty_result(self) -> None:
        client = self._make_client([])
        result = _execute_bigquery_query(client, "SELECT 1", max_rows=10)
        assert result["success"] is True
        assert result["row_count"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# _query_postgres_impl
# ---------------------------------------------------------------------------


class TestQueryPostgresImpl:
    def _make_session_context(self, rows: list[Any], column_names: list[str]) -> MagicMock:
        """Build a mock async context manager for get_async_session_for."""
        result = MagicMock()
        result.keys.return_value = column_names
        result.fetchall.return_value = rows

        session = AsyncMock()
        session.execute.return_value = result

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def test_rejects_non_select(self) -> None:
        result = await _query_postgres_impl(
            "INSERT INTO t VALUES (1)",
            max_rows=10,
            database="agentdb",
        )
        assert result["success"] is False
        assert "Only SELECT" in result["error"]

    async def test_adds_limit(self) -> None:
        rows = [(1,)]
        ctx = self._make_session_context(rows, ["id"])
        with patch("ypl.mcp_server.tools.database.get_async_session_for", return_value=ctx) as mock_session_for:
            result = await _query_postgres_impl("SELECT id FROM t", max_rows=5, database="agentdb")
        assert result["success"] is True
        # Verify LIMIT was included in the executed SQL
        session = await mock_session_for.return_value.__aenter__()
        executed_sql = str(session.execute.call_args[0][0])
        assert "LIMIT" in executed_sql.upper()

    async def test_returns_formatted_rows(self) -> None:
        rows = [(42, "hello")]
        ctx = self._make_session_context(rows, ["id", "msg"])
        with patch("ypl.mcp_server.tools.database.get_async_session_for", return_value=ctx):
            result = await _query_postgres_impl("SELECT id, msg FROM t", max_rows=10, database="appdb")
        assert result["success"] is True
        assert result["results"][0]["id"] == 42
        assert result["results"][0]["msg"] == "hello"

    async def test_handles_exception(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("db error"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("ypl.mcp_server.tools.database.get_async_session_for", return_value=ctx):
            result = await _query_postgres_impl("SELECT 1", max_rows=10, database="appdb")
        assert result["success"] is False
        assert "db error" in result["error"]

    async def test_datetime_serialization(self) -> None:
        dt = datetime(2024, 6, 1, 10, 30, 0, tzinfo=UTC)
        rows = [(dt,)]
        ctx = self._make_session_context(rows, ["ts"])
        with patch("ypl.mcp_server.tools.database.get_async_session_for", return_value=ctx):
            result = await _query_postgres_impl("SELECT ts FROM t", max_rows=10, database="agentdb")
        assert result["results"][0]["ts"] == dt.isoformat()


# ---------------------------------------------------------------------------
# _query_bigquery_impl
# ---------------------------------------------------------------------------


class TestQueryBigqueryImpl:
    def _make_mcp_settings(self, max_gb: float = 200.0, expensive_gb: float = 10240.0) -> MagicMock:
        s = MagicMock()
        s.bigquery_max_bytes_processed_gb = max_gb
        s.bigquery_expensive_max_bytes_processed_gb = expensive_gb
        return s

    async def test_rejects_invalid_query(self) -> None:
        result = await _query_bigquery_impl("DROP TABLE t", max_rows=10, check_cost_limit=True)
        assert result["success"] is False

    async def test_cost_limit_blocks_expensive_query(self) -> None:
        settings = self._make_mcp_settings(max_gb=1.0)
        # Dry-run reports 2 GB
        two_gb = 2 * 1024**3
        dry_run_job = MagicMock()
        dry_run_job.total_bytes_processed = int(two_gb)

        bq_client = MagicMock()
        bq_client.query.return_value = dry_run_job

        with (
            patch("ypl.mcp_server.tools.database._get_bigquery_client", return_value=bq_client),
            patch("ypl.mcp_server.tools.database.get_mcp_tools_settings", AsyncMock(return_value=settings)),
        ):
            result = await _query_bigquery_impl("SELECT 1", max_rows=10, check_cost_limit=True)

        assert result["success"] is False
        assert "exceeds the limit" in result["error"]

    async def test_successful_query(self) -> None:
        settings = self._make_mcp_settings()

        bq_row = MagicMock()
        bq_row.items.return_value = [("id", 1)]
        bq_result = MagicMock()
        bq_result.__iter__ = MagicMock(return_value=iter([bq_row]))
        query_job = MagicMock()
        query_job.result.return_value = bq_result
        query_job.total_bytes_processed = 1024

        bq_client = MagicMock()
        bq_client.query.return_value = query_job

        with (
            patch("ypl.mcp_server.tools.database._get_bigquery_client", return_value=bq_client),
            patch("ypl.mcp_server.tools.database.get_mcp_tools_settings", AsyncMock(return_value=settings)),
            # Skip cost check by setting to 0 GB limit
            patch("ypl.mcp_server.tools.database._check_bigquery_cost", return_value=None),
        ):
            result = await _query_bigquery_impl("SELECT id FROM t", max_rows=10, check_cost_limit=False)

        assert result["success"] is True
        assert result["row_count"] == 1


# ---------------------------------------------------------------------------
# query_appdb / query_agentdb (MCP tools)
# ---------------------------------------------------------------------------


class TestQueryPostgresMcpTools:
    def _make_session_ctx(self, rows: list[Any], cols: list[str]) -> MagicMock:
        res = MagicMock()
        res.keys.return_value = cols
        res.fetchall.return_value = rows
        session = AsyncMock()
        session.execute.return_value = res
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def test_query_appdb_select(self) -> None:
        ctx = self._make_session_ctx([(1,)], ["id"])
        with patch("ypl.mcp_server.tools.database.get_async_session_for", return_value=ctx):
            result = await query_appdb.fn("SELECT id FROM users LIMIT 1")
        assert result["success"] is True

    async def test_query_agentdb_rejects_insert(self) -> None:
        result = await query_agentdb.fn("INSERT INTO t VALUES (1)")
        assert result["success"] is False

    async def test_query_bigquery_rejects_drop(self) -> None:
        result = await query_bigquery.fn("DROP TABLE t")
        assert result["success"] is False

    async def test_query_bigquery_expensive_rejects_create(self) -> None:
        result = await query_bigquery_expensive.fn("CREATE TABLE foo (id INT)")
        assert result["success"] is False
