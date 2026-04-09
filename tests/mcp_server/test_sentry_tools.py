"""Unit tests for ypl/mcp_server/tools/sentry.py.

Covers:
- _fmt_issue: markdown formatting of a Sentry issue response
- _fmt_frame: single stacktrace frame formatting
- _fmt_event: event markdown formatting (exceptions, request, tags, contexts)
- _fmt_tag_values: tag value distribution formatting
- _fmt_trace: trace details formatting (transactions, orphan errors, empty)
- _fmt_breadcrumbs: breadcrumb formatting
- _resolve_issue_id: short ID resolution, numeric passthrough
- get_sentry_issue_details: success (issue + event), error path
- get_sentry_issue_tag_values: invalid tag key, success, error path
- get_sentry_trace_details: invalid trace ID, success, error path
- get_sentry_breadcrumbs: specific event, latest event, error path
- close_sentry_session: closes and resets the module-level session

All tests run without real network I/O.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "shortId": "YUPP-HEAD-QKE",
        "title": "TypeError: cannot read property",
        "permalink": "https://sentry.io/issues/123/",
        "status": "unresolved",
        "substatus": "new",
        "level": "error",
        "priority": "high",
        "platform": "javascript",
        "project": {"slug": "yupp-head"},
        "assignedTo": {"name": "Alice", "email": "alice@example.com"},
        "culprit": "app.js in handleClick",
        "count": "42",
        "userCount": 7,
        "firstSeen": "2024-01-01T00:00:00Z",
        "lastSeen": "2024-01-02T00:00:00Z",
        "metadata": {"filename": "app.js", "function": "handleClick"},
        "tags": [{"key": "browser"}, {"key": "os"}],
    }
    data.update(overrides)
    return data


def _make_event_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "eventID": "abc123",
        "title": "TypeError: oops",
        "dateCreated": "2024-01-01T12:00:00Z",
        "groupID": "999",
        "entries": [
            {
                "type": "exception",
                "data": {
                    "values": [
                        {
                            "type": "TypeError",
                            "value": "oops",
                            "mechanism": {"type": "onerror", "handled": False},
                            "stacktrace": {
                                "frames": [
                                    {
                                        "function": "handleClick",
                                        "filename": "app.js",
                                        "lineNo": 10,
                                        "colNo": 5,
                                        "inApp": True,
                                        "context": [(9, "prev line"), (10, "bad line"), (11, "next line")],
                                    },
                                    {
                                        "function": "lib_func",
                                        "filename": "lib.js",
                                        "lineNo": 100,
                                        "colNo": 1,
                                        "inApp": False,
                                    },
                                ]
                            },
                        }
                    ]
                },
            },
            {
                "type": "request",
                "data": {
                    "method": "GET",
                    "url": "https://example.com/api",
                    "headers": [
                        ["Content-Type", "application/json"],
                        ["Authorization", "Bearer secret"],
                        ["X-Custom", "value"],
                    ],
                },
            },
        ],
        "tags": [{"key": "browser", "value": "Chrome"}, {"key": "os", "value": "macOS"}],
        "contexts": {
            "runtime": {"name": "node", "version": "18.0.0", "type": "runtime"},
            "empty_ctx": {"type": "custom"},
        },
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# _fmt_issue
# ---------------------------------------------------------------------------


class TestFmtIssue:
    def test_basic_formatting(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_issue

        result = _fmt_issue(_make_issue_data())
        assert "YUPP-HEAD-QKE" in result
        assert "TypeError: cannot read property" in result
        assert "alice@example.com" in result
        assert "browser" in result
        assert "os" in result

    def test_unassigned_issue(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_issue

        data = _make_issue_data(assignedTo=None)
        result = _fmt_issue(data)
        assert "Unassigned" in result

    def test_no_tags(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_issue

        data = _make_issue_data(tags=[])
        result = _fmt_issue(data)
        assert "## Tags" not in result

    def test_no_metadata_filename(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_issue

        data = _make_issue_data(metadata={})
        result = _fmt_issue(data)
        assert "**File**" not in result

    def test_metadata_with_filename(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_issue

        data = _make_issue_data(metadata={"filename": "routes.py", "function": "handle"})
        result = _fmt_issue(data)
        assert "routes.py" in result
        assert "handle" in result


# ---------------------------------------------------------------------------
# _fmt_frame
# ---------------------------------------------------------------------------


class TestFmtFrame:
    def test_full_frame(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_frame

        frame = {"function": "doThing", "filename": "app.py", "lineNo": 42, "colNo": 7}
        result = _fmt_frame(frame)
        assert "app.py" in result
        assert "42" in result
        assert "doThing" in result

    def test_missing_function(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_frame

        frame = {"filename": "app.py", "lineNo": 1, "colNo": 1}
        result = _fmt_frame(frame)
        assert "?" in result


# ---------------------------------------------------------------------------
# _fmt_event
# ---------------------------------------------------------------------------


class TestFmtEvent:
    def test_formats_exception_entry(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        result = _fmt_event(_make_event_data())
        assert "TypeError: oops" in result
        assert "Mechanism: onerror" in result
        assert "### Stacktrace" in result
        assert "handleClick" in result

    def test_inapp_frame_has_context(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        result = _fmt_event(_make_event_data())
        # The inApp frame's context lines should appear
        assert "bad line" in result
        assert ">>>" in result

    def test_formats_request_entry_strips_auth_header(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        result = _fmt_event(_make_event_data())
        assert "## Request: GET" in result
        assert "Content-Type" in result
        # Sensitive header must be stripped
        assert "Bearer secret" not in result

    def test_formats_tags(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        result = _fmt_event(_make_event_data())
        assert "**browser**" in result
        assert "Chrome" in result

    def test_formats_contexts_skips_empty(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        result = _fmt_event(_make_event_data())
        assert "## Contexts" in result
        assert "runtime" in result
        # empty_ctx has only "type" and no other non-None fields → skipped
        assert "empty_ctx" not in result

    def test_no_entries(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_event

        data = _make_event_data(entries=[], tags=[], contexts={})
        result = _fmt_event(data)
        assert "Event abc123" in result


# ---------------------------------------------------------------------------
# _fmt_tag_values
# ---------------------------------------------------------------------------


class TestFmtTagValues:
    def test_formats_values(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_tag_values

        values = [
            {"value": "Chrome", "count": 10, "firstSeen": "2024-01-01", "lastSeen": "2024-01-02"},
            {"value": "Firefox", "count": 5, "firstSeen": "2024-01-01", "lastSeen": "2024-01-02"},
        ]
        result = _fmt_tag_values("123", "browser", values)
        assert "browser" in result
        assert "Chrome" in result
        assert "Firefox" in result
        assert "10" in result

    def test_empty_values(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_tag_values

        result = _fmt_tag_values("999", "os", [])
        assert "os" in result
        assert "999" in result


# ---------------------------------------------------------------------------
# _fmt_trace
# ---------------------------------------------------------------------------


class TestFmtTrace:
    def test_with_transactions(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_trace

        data = {
            "transactions": [
                {"transaction": "/api/v1", "project_slug": "backend", "span_id": "abc123"},
            ],
            "orphan_errors": [],
        }
        result = _fmt_trace("deadbeef" * 4, data)
        assert "/api/v1" in result
        assert "backend" in result

    def test_with_orphan_errors(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_trace

        data = {
            "transactions": [],
            "orphan_errors": [
                {"title": "SomeError", "project_slug": "frontend", "event_id": "evt1"},
            ],
        }
        result = _fmt_trace("deadbeef" * 4, data)
        assert "SomeError" in result
        assert "frontend" in result

    def test_empty_trace(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_trace

        data: dict[str, Any] = {"transactions": [], "orphan_errors": []}
        result = _fmt_trace("deadbeef" * 4, data)
        assert "No transactions" in result


# ---------------------------------------------------------------------------
# _fmt_breadcrumbs
# ---------------------------------------------------------------------------


class TestFmtBreadcrumbs:
    def test_formats_breadcrumbs(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_breadcrumbs

        crumbs = [
            {"timestamp": "2024-01-01T00:00:00Z", "category": "navigation", "level": "info", "message": "Navigated"},
            {"timestamp": "2024-01-01T00:01:00Z", "category": "fetch", "level": "error", "message": "Failed"},
        ]
        result = _fmt_breadcrumbs("123", "evt1", crumbs)
        assert "navigation" in result
        assert "Navigated" in result
        assert "Failed" in result
        assert "Count: 2" in result

    def test_empty_breadcrumbs(self) -> None:
        from ypl.mcp_server.tools.sentry import _fmt_breadcrumbs

        result = _fmt_breadcrumbs("123", None, [])
        assert "Count: 0" in result


# ---------------------------------------------------------------------------
# _resolve_issue_id
# ---------------------------------------------------------------------------


class TestResolveIssueId:
    async def test_numeric_id_passes_through(self) -> None:
        from ypl.mcp_server.tools.sentry import _resolve_issue_id

        result = await _resolve_issue_id("12345")
        assert result == "12345"

    async def test_short_id_resolved(self) -> None:
        from ypl.mcp_server.tools.sentry import _resolve_issue_id

        mock_data = {"groupId": 99}
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=mock_data)):
            result = await _resolve_issue_id("YUPP-HEAD-QKE")
        assert result == "99"


# ---------------------------------------------------------------------------
# get_sentry_issue_details
# ---------------------------------------------------------------------------


class TestGetSentryIssueDetails:
    async def test_issue_summary_success(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_details

        issue_data = _make_issue_data()
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=issue_data)):
            result = await get_sentry_issue_details("12345")

        assert result["success"] is True
        assert "data" in result

    async def test_specific_event_success(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_details

        event_data = _make_event_data()
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=event_data)):
            result = await get_sentry_issue_details("12345", event_id="abc123")

        assert result["success"] is True
        assert "abc123" in result["data"]

    async def test_error_returns_failure(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_details

        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(side_effect=Exception("network error"))):
            result = await get_sentry_issue_details("12345")

        assert result["success"] is False
        assert "network error" in result["error"]

    async def test_short_id_resolved_before_fetch(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_details

        issue_data = _make_issue_data()
        # First call resolves short ID, second fetches issue
        sentry_get = AsyncMock(side_effect=[{"groupId": 42}, issue_data])
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=sentry_get):
            result = await get_sentry_issue_details("YUPP-HEAD-QKE")

        assert result["success"] is True
        assert result["issue_id"] == "42"


# ---------------------------------------------------------------------------
# get_sentry_issue_tag_values
# ---------------------------------------------------------------------------


class TestGetSentryIssueTagValues:
    async def test_invalid_tag_key(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_tag_values

        result = await get_sentry_issue_tag_values("123", "tag with spaces!")
        assert result["success"] is False
        assert "Invalid tag key" in result["error"]

    async def test_success(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_tag_values

        tag_values = [
            {"value": "Chrome", "count": 10, "firstSeen": "2024-01-01", "lastSeen": "2024-01-02"},
        ]
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=tag_values)):
            result = await get_sentry_issue_tag_values("12345", "browser")

        assert result["success"] is True
        assert result["tag_key"] == "browser"
        assert "Chrome" in result["data"]

    async def test_error_path(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_tag_values

        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await get_sentry_issue_tag_values("12345", "browser")

        assert result["success"] is False
        assert "timeout" in result["error"]

    async def test_valid_tag_key_formats(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_issue_tag_values

        tag_values: list[dict[str, Any]] = []
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=tag_values)):
            result = await get_sentry_issue_tag_values("123", "server_name")
        assert result["success"] is True

        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=tag_values)):
            result = await get_sentry_issue_tag_values("123", "user.email")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# get_sentry_trace_details
# ---------------------------------------------------------------------------


class TestGetSentryTraceDetails:
    async def test_invalid_trace_id(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_trace_details

        result = await get_sentry_trace_details("not-a-hex-id")
        assert result["success"] is False
        assert "Invalid trace ID" in result["error"]

    async def test_valid_trace_id_success(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_trace_details

        trace_data = {
            "transactions": [{"transaction": "/api/v1", "project_slug": "backend", "span_id": "abc"}],
            "orphan_errors": [],
        }
        valid_trace_id = "a" * 32
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=trace_data)):
            result = await get_sentry_trace_details(valid_trace_id)

        assert result["success"] is True
        assert result["trace_id"] == valid_trace_id
        assert "/api/v1" in result["data"]

    async def test_error_path(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_trace_details

        valid_trace_id = "b" * 32
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(side_effect=Exception("API error"))):
            result = await get_sentry_trace_details(valid_trace_id)

        assert result["success"] is False
        assert "API error" in result["error"]


# ---------------------------------------------------------------------------
# get_sentry_breadcrumbs
# ---------------------------------------------------------------------------


class TestGetSentryBreadcrumbs:
    def _make_event_with_breadcrumbs(self) -> dict[str, Any]:
        return {
            "eventID": "evt-001",
            "entries": [
                {
                    "type": "breadcrumbs",
                    "data": {
                        "values": [
                            {"timestamp": "2024-01-01T00:00:00Z", "category": "navigation", "level": "info"},
                        ]
                    },
                }
            ],
        }

    async def test_specific_event_breadcrumbs(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_breadcrumbs

        event = self._make_event_with_breadcrumbs()
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=event)):
            result = await get_sentry_breadcrumbs("12345", event_id="evt-001")

        assert result["success"] is True
        assert result["event_id"] == "evt-001"
        assert "navigation" in result["data"]

    async def test_latest_event_breadcrumbs(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_breadcrumbs

        event = self._make_event_with_breadcrumbs()
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=event)):
            result = await get_sentry_breadcrumbs("12345")

        assert result["success"] is True
        assert "navigation" in result["data"]

    async def test_error_path(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_breadcrumbs

        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await get_sentry_breadcrumbs("12345")

        assert result["success"] is False
        assert "timeout" in result["error"]

    async def test_no_breadcrumb_entries(self) -> None:
        from ypl.mcp_server.tools.sentry import get_sentry_breadcrumbs

        event = {"eventID": "evt-002", "entries": [{"type": "exception", "data": {}}]}
        with patch("ypl.mcp_server.tools.sentry._sentry_get", new=AsyncMock(return_value=event)):
            result = await get_sentry_breadcrumbs("12345")

        assert result["success"] is True
        assert "Count: 0" in result["data"]


# ---------------------------------------------------------------------------
# close_sentry_session
# ---------------------------------------------------------------------------


class TestCloseSentrySession:
    async def test_close_when_no_session(self) -> None:
        """Closing when no session has been created should be a no-op."""
        import ypl.mcp_server.tools.sentry as sentry_module

        sentry_module._session = None
        await sentry_module.close_sentry_session()
        assert sentry_module._session is None

    async def test_close_open_session(self) -> None:
        """Closing an open session should close and reset it."""
        import ypl.mcp_server.tools.sentry as sentry_module

        mock_session = AsyncMock()
        mock_session.closed = False
        sentry_module._session = mock_session

        await sentry_module.close_sentry_session()

        mock_session.close.assert_called_once()
        assert sentry_module._session is None

    async def test_close_already_closed_session(self) -> None:
        """A session that's already closed should not have close() called again."""
        import ypl.mcp_server.tools.sentry as sentry_module

        mock_session = MagicMock()
        mock_session.closed = True
        sentry_module._session = mock_session

        await sentry_module.close_sentry_session()

        mock_session.close.assert_not_called()
        assert sentry_module._session is None
