"""Unit tests for ypl/mcp_server/tools/gcp_alert_search.py and gcp_logs.py."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ypl.mcp_server.tools.gcp_alert_search import (
    _ALERT_ID_PATTERN,
    extract_alert_id,
    get_gcp_alert_metadata,
)
from ypl.mcp_server.tools.gcp_logs import (
    _extract_timestamp_comparison_value,
    _is_gcp_logging_rate_limited_error,
    _parse_query_timestamp_comparison,
    search_gcp_logs,
    search_vercel_logs,
)

# ===========================================================================
# gcp_alert_search.py
# ===========================================================================


class TestExtractAlertId:
    def test_full_url(self) -> None:
        url = "https://console.cloud.google.com/monitoring/alerting/alerts/0.o3gwwfx7rf2c"
        assert extract_alert_id(url) == "0.o3gwwfx7rf2c"

    def test_url_with_query_params(self) -> None:
        url = "https://console.cloud.google.com/monitoring/alerting/alerts/abc-123?project=my-project"
        assert extract_alert_id(url) == "abc-123"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Could not extract alert ID"):
            extract_alert_id("https://example.com/not-monitoring")

    def test_bare_alert_id_raises(self) -> None:
        # bare ID is NOT a URL, so extract_alert_id raises
        with pytest.raises(ValueError):
            extract_alert_id("0.o3gwwfx7rf2c")

    def test_alert_id_with_underscores_and_dots(self) -> None:
        url = "https://console.cloud.google.com/monitoring/alerting/alerts/a.b_c-d"
        assert extract_alert_id(url) == "a.b_c-d"


class TestAlertIdPattern:
    def test_valid_id(self) -> None:
        assert _ALERT_ID_PATTERN.match("abc123") is not None
        assert _ALERT_ID_PATTERN.match("0.o3gwwfx7rf2c") is not None
        assert _ALERT_ID_PATTERN.match("alert-123_foo") is not None

    def test_invalid_id_with_space(self) -> None:
        assert _ALERT_ID_PATTERN.match("alert id") is None

    def test_empty_string(self) -> None:
        assert _ALERT_ID_PATTERN.match("") is None


class TestGetGcpAlertMetadata:
    async def test_bare_id_success(self) -> None:
        mock_alert = {"name": "projects/test/alerts/abc123", "state": "OPEN"}

        with (
            patch("ypl.mcp_server.tools.gcp_alert_search.settings") as mock_settings,
            patch("ypl.mcp_server.tools.gcp_alert_search.google.auth.default") as mock_auth,
            patch("ypl.mcp_server.tools.gcp_alert_search.build") as mock_build,
        ):
            mock_settings.GCP_PROJECT_ID = "test-project"
            mock_auth.return_value = (MagicMock(), "test-project")

            # Set up service chain: service.projects().alerts().get(name=...).execute()
            mock_execute = MagicMock(return_value=mock_alert)
            mock_get = MagicMock()
            mock_get.return_value = MagicMock(execute=mock_execute)
            mock_alerts = MagicMock()
            mock_alerts.return_value = MagicMock(get=mock_get)
            mock_projects = MagicMock()
            mock_projects.return_value = MagicMock(alerts=mock_alerts)
            mock_service = MagicMock()
            mock_service.projects = mock_projects
            mock_build.return_value = mock_service

            result = await get_gcp_alert_metadata("abc123")

        assert result["success"] is True
        assert result["alert_id"] == "abc123"
        assert result["project_id"] == "test-project"
        assert result["metadata"] == mock_alert

    async def test_url_input_extracted(self) -> None:
        url = "https://console.cloud.google.com/monitoring/alerting/alerts/xyz-789"

        with (
            patch("ypl.mcp_server.tools.gcp_alert_search.settings") as mock_settings,
            patch("ypl.mcp_server.tools.gcp_alert_search.google.auth.default") as mock_auth,
            patch("ypl.mcp_server.tools.gcp_alert_search.build") as mock_build,
        ):
            mock_settings.GCP_PROJECT_ID = "test-project"
            mock_auth.return_value = (MagicMock(), "test-project")

            mock_execute = MagicMock(return_value={"state": "CLOSED"})
            mock_get = MagicMock(return_value=MagicMock(execute=mock_execute))
            mock_alerts = MagicMock(return_value=MagicMock(get=mock_get))
            mock_projects = MagicMock(return_value=MagicMock(alerts=mock_alerts))
            mock_service = MagicMock(projects=mock_projects)
            mock_build.return_value = mock_service

            result = await get_gcp_alert_metadata(url)

        assert result["success"] is True
        assert result["alert_id"] == "xyz-789"

    async def test_invalid_bare_id_returns_error(self) -> None:
        result = await get_gcp_alert_metadata("invalid alert id with spaces")
        assert result["success"] is False
        assert "Invalid alert ID format" in result["error"]

    async def test_missing_project_id(self) -> None:
        with patch("ypl.mcp_server.tools.gcp_alert_search.settings") as mock_settings:
            mock_settings.GCP_PROJECT_ID = None
            result = await get_gcp_alert_metadata("abc123")
        assert result["success"] is False
        assert "GCP_PROJECT_ID not configured" in result["error"]

    async def test_http_404_returns_not_found(self) -> None:
        from googleapiclient.errors import HttpError

        fake_resp = MagicMock()
        fake_resp.status = 404
        http_error = HttpError(resp=fake_resp, content=b"not found")

        with (
            patch("ypl.mcp_server.tools.gcp_alert_search.settings") as mock_settings,
            patch("ypl.mcp_server.tools.gcp_alert_search.google.auth.default") as mock_auth,
            patch("ypl.mcp_server.tools.gcp_alert_search.build") as mock_build,
        ):
            mock_settings.GCP_PROJECT_ID = "test-project"
            mock_auth.return_value = (MagicMock(), "test-project")

            mock_execute = MagicMock(side_effect=http_error)
            mock_get = MagicMock(return_value=MagicMock(execute=mock_execute))
            mock_alerts = MagicMock(return_value=MagicMock(get=mock_get))
            mock_projects = MagicMock(return_value=MagicMock(alerts=mock_alerts))
            mock_service = MagicMock(projects=mock_projects)
            mock_build.return_value = mock_service

            result = await get_gcp_alert_metadata("abc123")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    async def test_generic_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.gcp_alert_search.settings") as mock_settings,
            patch("ypl.mcp_server.tools.gcp_alert_search.google.auth.default") as mock_auth,
            patch("ypl.mcp_server.tools.gcp_alert_search.build", side_effect=RuntimeError("boom")),
        ):
            mock_settings.GCP_PROJECT_ID = "test-project"
            mock_auth.return_value = (MagicMock(), "test-project")
            result = await get_gcp_alert_metadata("abc123")

        assert result["success"] is False
        assert "boom" in result["error"]


# ===========================================================================
# gcp_logs.py
# ===========================================================================


class TestIsGcpLoggingRateLimitedError:
    def test_resource_exhausted(self) -> None:
        from google.api_core.exceptions import ResourceExhausted

        assert _is_gcp_logging_rate_limited_error(ResourceExhausted("quota")) is True

    def test_too_many_requests(self) -> None:
        from google.api_core.exceptions import TooManyRequests

        assert _is_gcp_logging_rate_limited_error(TooManyRequests("quota")) is True

    def test_generic_exception(self) -> None:
        assert _is_gcp_logging_rate_limited_error(RuntimeError("boom")) is False

    def test_value_error(self) -> None:
        assert _is_gcp_logging_rate_limited_error(ValueError("x")) is False


class TestExtractTimestampComparisonValue:
    def test_double_quoted_gte(self) -> None:
        query = 'timestamp >= "2024-01-01T00:00:00Z"'
        result = _extract_timestamp_comparison_value(query, ">=")
        assert result == "2024-01-01T00:00:00Z"

    def test_single_quoted_lte(self) -> None:
        query = "timestamp <= '2024-06-15T12:00:00Z'"
        result = _extract_timestamp_comparison_value(query, "<=")
        assert result == "2024-06-15T12:00:00Z"

    def test_missing_comparison(self) -> None:
        query = "severity=ERROR"
        result = _extract_timestamp_comparison_value(query, ">=")
        assert result is None

    def test_empty_query(self) -> None:
        result = _extract_timestamp_comparison_value("", ">=")
        assert result is None

    def test_last_match_returned(self) -> None:
        query = 'timestamp >= "2024-01-01T00:00:00Z" AND timestamp >= "2024-06-01T00:00:00Z"'
        result = _extract_timestamp_comparison_value(query, ">=")
        assert result == "2024-06-01T00:00:00Z"

    def test_case_insensitive(self) -> None:
        query = 'TIMESTAMP >= "2024-01-01T00:00:00Z"'
        result = _extract_timestamp_comparison_value(query, ">=")
        assert result == "2024-01-01T00:00:00Z"


class TestParseQueryTimestampComparison:
    def test_valid_rfc3339(self) -> None:
        query = 'timestamp >= "2024-01-15T10:30:00Z"'
        with patch(
            "ypl.mcp_server.tools.gcp_logs.parse_rfc3339_timestamp",
            return_value=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        ):
            result = _parse_query_timestamp_comparison(query, ">=")
        assert result is not None
        assert result.year == 2024

    def test_missing_timestamp_returns_none(self) -> None:
        result = _parse_query_timestamp_comparison("severity=ERROR", ">=")
        assert result is None


class TestSearchGcpLogs:
    def _make_rate_limit_watcher(self, rate_limited: bool = False) -> tuple[AsyncMock, AsyncMock]:
        is_limited = AsyncMock(return_value=rate_limited)
        mark_limited = AsyncMock()
        return is_limited, mark_limited

    async def test_rate_limited_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited",
            AsyncMock(return_value=True),
        ):
            result = await search_gcp_logs.fn(query="severity=ERROR")
        assert result["success"] is False
        assert result["error"] == "RATE_LIMITED"
        assert "retry_after_seconds" in result

    async def test_successful_search_returns_results(self) -> None:
        fake_entry: dict[str, Any] = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "severity": "ERROR",
            "message": {"msg": "test error"},
            "trace": None,
            "labels": {},
            "resource": None,
        }

        with (
            patch("ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited", AsyncMock(return_value=False)),
            patch("ypl.mcp_server.tools.gcp_logs.google_logging.Client"),
            patch(
                "ypl.mcp_server.tools.gcp_logs.asyncio.wait_for",
                AsyncMock(return_value=[fake_entry]),
            ),
        ):
            result = await search_gcp_logs.fn(query="severity=ERROR", hours_back=2, max_results=10)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["severity"] == "ERROR"

    async def test_explicit_timestamp_range_not_injected(self) -> None:
        """When both >= and <= are present, hours_back should not be injected."""
        fake_entries: list[dict[str, Any]] = []

        with (
            patch("ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited", AsyncMock(return_value=False)),
            patch("ypl.mcp_server.tools.gcp_logs.google_logging.Client"),
            patch("ypl.mcp_server.tools.gcp_logs.asyncio.wait_for", AsyncMock(return_value=fake_entries)),
            patch(
                "ypl.mcp_server.tools.gcp_logs.parse_rfc3339_timestamp",
                side_effect=lambda v: datetime(2024, 1, 1, tzinfo=UTC),
            ),
        ):
            query = 'timestamp >= "2024-01-01T00:00:00Z" AND timestamp <= "2024-01-02T00:00:00Z"'
            result = await search_gcp_logs.fn(query=query, hours_back=24)

        assert result["success"] is True
        # With both bounds present, source should be "query"
        assert result["time_range"]["source"] == "query"

    async def test_timeout_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited", AsyncMock(return_value=False)),
            patch("ypl.mcp_server.tools.gcp_logs.google_logging.Client"),
            patch("ypl.mcp_server.tools.gcp_logs.asyncio.wait_for", AsyncMock(side_effect=TimeoutError())),
        ):
            result = await search_gcp_logs.fn(query="severity=ERROR")

        assert result["success"] is False
        assert "timed out" in result["error"]

    async def test_rate_limit_error_from_gcp_triggers_cooldown(self) -> None:
        from google.api_core.exceptions import ResourceExhausted

        with (
            patch("ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited", AsyncMock(return_value=False)),
            patch(
                "ypl.mcp_server.tools.gcp_logs.google_logging.Client",
                side_effect=ResourceExhausted("quota"),
            ),
            patch(
                "ypl.mcp_server.tools.gcp_logs._mark_gcp_logs_search_rate_limited",
                AsyncMock(),
            ) as mock_mark,
        ):
            result = await search_gcp_logs.fn(query="severity=ERROR")

        assert result["success"] is False
        assert result["error"] == "RATELIMITED"
        mock_mark.assert_called_once()

    async def test_generic_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.gcp_logs._is_gcp_logs_search_rate_limited", AsyncMock(return_value=False)),
            patch("ypl.mcp_server.tools.gcp_logs.google_logging.Client", side_effect=RuntimeError("boom")),
        ):
            result = await search_gcp_logs.fn(query="severity=ERROR")

        assert result["success"] is False
        assert "boom" in result["error"]


class TestSearchVercelLogs:
    async def test_prepends_vercel_filter(self) -> None:
        fake_result = {"success": True, "count": 0, "results": [], "time_range": {}}

        with patch(
            "ypl.mcp_server.tools.gcp_logs.search_gcp_logs.fn",
            AsyncMock(return_value=fake_result),
        ) as mock_search:
            result = await search_vercel_logs.fn(query='severity="ERROR"', hours_back=1, max_results=10)

        assert result["success"] is True
        assert result.get("source") == "vercel"
        call_kwargs = mock_search.call_args.kwargs
        assert "vercel-log-drain" in call_kwargs["query"]
        assert 'severity="ERROR"' in call_kwargs["query"]

    async def test_vercel_filter_only_when_no_user_query(self) -> None:
        fake_result = {"success": True, "count": 0, "results": [], "time_range": {}}

        with patch(
            "ypl.mcp_server.tools.gcp_logs.search_gcp_logs.fn",
            AsyncMock(return_value=fake_result),
        ) as mock_search:
            await search_vercel_logs.fn(query="", hours_back=1, max_results=10)

        call_kwargs = mock_search.call_args.kwargs
        # Without user query, filter should just be the vercel filter (no " AND ()")
        assert call_kwargs["query"] == 'resource.labels.service_name="vercel-log-drain"'

    async def test_exception_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.gcp_logs.search_gcp_logs.fn",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await search_vercel_logs.fn(query="test")

        assert result["success"] is False
        assert "boom" in result["error"]
