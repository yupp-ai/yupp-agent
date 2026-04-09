"""Unit tests for ypl/mcp_server/tools/agent_memory.py.

Covers:
- _validate_memory_topic: empty, too long, valid nested, leading/trailing/double slash,
  invalid segment characters
- _sanitize_metadata_value: allowed chars, stripping disallowed chars
- _build_attribution_header: creates YAML front-matter with agent_name and session_id
- _build_attribution_marker: inline HTML comment with metadata
- get_agent_memory: list topics (no topic arg), topic not found (404), topic found,
  invalid topic, generic error
- store_agent_memory: invalid topic, empty content, content too large,
  new file with attribution, update with attribution, GCS conflict (412), generic error
- search_agent_memory_tool: empty query, invalid mode, invalid scope, private without agent_name,
  invalid agent_name format, invalid topic, success, generic error

All GCS and background tasks are mocked — no real network I/O required.
"""

from __future__ import annotations
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

# MCP tools are FunctionTool objects — access the raw coroutine via .fn
import ypl.mcp_server.tools.agent_memory as _mem_mod
from ypl.mcp_server.tools.agent_memory import (
    GCSPreconditionFailed,
    _build_attribution_header,
    _build_attribution_marker,
    _sanitize_metadata_value,
    _upload_agent_memory,
    _validate_memory_topic,
)

get_agent_memory = _mem_mod.get_agent_memory.fn
store_agent_memory = _mem_mod.store_agent_memory.fn
search_agent_memory_tool = _mem_mod.search_agent_memory_tool.fn


# ---------------------------------------------------------------------------
# _validate_memory_topic
# ---------------------------------------------------------------------------


class TestValidateMemoryTopic:
    def test_empty_topic_returns_error(self) -> None:
        assert _validate_memory_topic("") is not None

    def test_topic_too_long_returns_error(self) -> None:
        assert _validate_memory_topic("a" * 101) is not None

    def test_valid_simple_topic(self) -> None:
        assert _validate_memory_topic("routing-overview") is None

    def test_valid_nested_topic(self) -> None:
        assert _validate_memory_topic("bookkeeper/routing-overview") is None

    def test_valid_deep_nested_topic(self) -> None:
        assert _validate_memory_topic("a/b/c") is None

    def test_leading_slash_returns_error(self) -> None:
        assert _validate_memory_topic("/foo") is not None

    def test_trailing_slash_returns_error(self) -> None:
        assert _validate_memory_topic("foo/") is not None

    def test_double_slash_returns_error(self) -> None:
        assert _validate_memory_topic("foo//bar") is not None

    def test_segment_starting_with_hyphen_returns_error(self) -> None:
        assert _validate_memory_topic("-bad") is not None

    def test_segment_with_spaces_returns_error(self) -> None:
        assert _validate_memory_topic("bad segment") is not None

    def test_valid_topic_with_underscores(self) -> None:
        assert _validate_memory_topic("my_topic_name") is None

    def test_valid_topic_with_numbers(self) -> None:
        assert _validate_memory_topic("topic123") is None


# ---------------------------------------------------------------------------
# _sanitize_metadata_value
# ---------------------------------------------------------------------------


class TestSanitizeMetadataValue:
    def test_alphanumeric_passes_through(self) -> None:
        assert _sanitize_metadata_value("hello123") == "hello123"

    def test_allowed_special_chars_pass_through(self) -> None:
        result = _sanitize_metadata_value("eng-raccoon_session/id@host.com ")
        # Space is in the allowed set
        assert "eng-raccoon_session" in result
        assert "@" in result

    def test_disallowed_chars_stripped(self) -> None:
        result = _sanitize_metadata_value("name<script>alert()</script>")
        assert "<" not in result
        assert ">" not in result
        assert "(" not in result


# ---------------------------------------------------------------------------
# _build_attribution_header
# ---------------------------------------------------------------------------


class TestBuildAttributionHeader:
    def test_with_agent_and_session(self) -> None:
        result = _build_attribution_header("eng-raccoon", "sess-001")
        assert result.startswith("---")
        assert result.endswith("---")
        assert "agent_name" in result
        assert "eng-raccoon" in result
        assert "session_id" in result
        assert "sess-001" in result
        assert "created_at" in result

    def test_without_optional_fields(self) -> None:
        result = _build_attribution_header(None, None)
        assert "agent_name" not in result
        assert "session_id" not in result
        assert "created_at" in result


# ---------------------------------------------------------------------------
# _build_attribution_marker
# ---------------------------------------------------------------------------


class TestBuildAttributionMarker:
    def test_with_agent_and_session(self) -> None:
        result = _build_attribution_marker("eng-raccoon", "sess-001")
        assert result.startswith("<!--")
        assert result.endswith("-->")
        assert "updated:" in result
        assert "eng-raccoon" in result
        assert "sess-001" in result

    def test_without_agent(self) -> None:
        result = _build_attribution_marker(None, None)
        assert "updated:" in result
        assert "agent:" not in result
        assert "session:" not in result


# ---------------------------------------------------------------------------
# get_agent_memory
# ---------------------------------------------------------------------------


class TestGetAgentMemory:
    async def test_list_topics_when_no_topic_arg(self) -> None:
        list_result = {
            "items": [
                {"name": "memory/routing-overview.md"},
                {"name": "memory/bookkeeper/notes.md"},
                {"name": "memory/"},  # directory placeholder — no .md
            ]
        }
        mock_client = AsyncMock()
        mock_client.list_objects = AsyncMock(return_value=list_result)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client):
            result = await get_agent_memory()

        assert result["success"] is True
        assert "routing-overview" in result["topics"]
        assert "bookkeeper/notes" in result["topics"]
        # directory placeholder should be excluded
        assert "" not in result["topics"]

    async def test_invalid_topic_returns_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client):
            result = await get_agent_memory(topic="/bad-topic/")

        assert result["success"] is False
        assert "error" in result

    async def test_topic_not_found_returns_none_content(self) -> None:
        mock_error = aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=404)
        mock_client = AsyncMock()
        mock_client.download_metadata = AsyncMock(side_effect=mock_error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await get_agent_memory(topic="routing-overview")

        assert result["success"] is True
        assert result["content"] is None
        assert result["generation"] == 0
        assert "does not exist" in result["message"]

    async def test_topic_found_returns_content(self) -> None:
        mock_client = AsyncMock()
        mock_client.download_metadata = AsyncMock(return_value={"generation": "42"})
        mock_client.download = AsyncMock(return_value=b"# Routing Overview\n\nSome content here.")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await get_agent_memory(topic="routing-overview")

        assert result["success"] is True
        assert result["generation"] == 42
        assert "Routing Overview" in result["content"]

    async def test_generic_error_returns_failure(self) -> None:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(side_effect=Exception("GCS connection refused"))
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client):
            result = await get_agent_memory(topic="routing-overview")

        assert result["success"] is False
        assert "GCS connection refused" in result["error"]


# ---------------------------------------------------------------------------
# store_agent_memory
# ---------------------------------------------------------------------------


class TestStoreAgentMemory:
    async def test_invalid_topic_returns_error(self) -> None:
        result = await store_agent_memory(topic="/bad/", content="Some content")
        assert result["success"] is False

    async def test_empty_content_returns_error(self) -> None:
        result = await store_agent_memory(topic="valid-topic", content="   ")
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    async def test_content_too_large_returns_error(self) -> None:
        # 32KB + 1 byte
        huge_content = "A" * (32 * 1024 + 1)
        result = await store_agent_memory(topic="valid-topic", content=huge_content)
        assert result["success"] is False
        assert "exceeds maximum" in result["error"]

    async def test_success_new_file_with_attribution(self) -> None:
        upload_result = {"generation": "5"}

        with (
            patch(
                "ypl.mcp_server.tools.agent_memory._upload_agent_memory",
                new=AsyncMock(return_value=upload_result),
            ),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
            patch("ypl.backend.llm.agent_memory_indexing.index_topic_sections", new=AsyncMock()),
            patch("ypl.backend.utils.async_utils.create_background_task"),
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await store_agent_memory(
                topic="new-topic",
                content="# New Topic\n\nContent here.",
                expected_generation=0,
                agent_name="eng-raccoon",
                session_id="sess-001",
            )

        assert result["success"] is True
        assert result["generation"] == 5

    async def test_success_update_with_attribution(self) -> None:
        upload_result = {"generation": "10"}

        with (
            patch(
                "ypl.mcp_server.tools.agent_memory._upload_agent_memory",
                new=AsyncMock(return_value=upload_result),
            ),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
            patch("ypl.backend.llm.agent_memory_indexing.index_topic_sections", new=AsyncMock()),
            patch("ypl.backend.utils.async_utils.create_background_task"),
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await store_agent_memory(
                topic="existing-topic",
                content="# Existing Topic\n\nUpdated content.",
                expected_generation=9,
                agent_name="bookkeeper",
                session_id="sess-002",
            )

        assert result["success"] is True
        assert result["generation"] == 10

    async def test_success_without_attribution(self) -> None:
        upload_result = {"generation": "3"}

        with (
            patch(
                "ypl.mcp_server.tools.agent_memory._upload_agent_memory",
                new=AsyncMock(return_value=upload_result),
            ),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await store_agent_memory(
                topic="anon-topic",
                content="Plain content without agent attribution.",
                expected_generation=0,
            )

        assert result["success"] is True

    async def test_gcs_conflict_returns_conflict_error(self) -> None:
        with (
            patch(
                "ypl.mcp_server.tools.agent_memory._upload_agent_memory",
                new=AsyncMock(side_effect=GCSPreconditionFailed("412")),
            ),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await store_agent_memory(
                topic="contested-topic",
                content="Content that conflicts.",
                expected_generation=5,
            )

        assert result["success"] is False
        assert result["error"] == "CONFLICT"
        assert "modified by another agent" in result["message"]

    async def test_generic_error_returns_failure(self) -> None:
        with (
            patch(
                "ypl.mcp_server.tools.agent_memory._upload_agent_memory",
                new=AsyncMock(side_effect=Exception("GCS timeout")),
            ),
            patch("ypl.mcp_server.tools.agent_memory.settings") as mock_settings,
        ):
            mock_settings.AGENT_MEMORY_BUCKET = "test-bucket"
            result = await store_agent_memory(
                topic="error-topic",
                content="Valid content.",
            )

        assert result["success"] is False
        assert "GCS timeout" in result["error"]


# ---------------------------------------------------------------------------
# search_agent_memory_tool
# ---------------------------------------------------------------------------


class TestSearchAgentMemoryTool:
    async def test_empty_query_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="   ")
        assert result["success"] is False
        assert "non-empty" in result["error"]

    async def test_invalid_mode_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="routing", mode="fuzzy")
        assert result["success"] is False
        assert "Invalid mode" in result["error"]

    async def test_invalid_scope_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="routing", scope="global")
        assert result["success"] is False
        assert "Invalid scope" in result["error"]

    async def test_private_scope_without_agent_name_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="routing", scope="private", agent_name="")
        assert result["success"] is False
        assert "agent_name is required" in result["error"]

    async def test_invalid_agent_name_format_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="routing", agent_name="bad name!")
        assert result["success"] is False
        assert "Invalid agent_name" in result["error"]

    async def test_invalid_topic_filter_returns_error(self) -> None:
        result = await search_agent_memory_tool(query="routing", topic="/bad/")
        assert result["success"] is False

    async def test_success_public_scope(self) -> None:
        mock_results = [
            {"topic": "routing-overview", "section": "## Routing", "score": 0.9, "snippet": "Routing info..."}
        ]

        with patch(
            "ypl.backend.llm.agent_memory_search.search_agent_memory",
            new=AsyncMock(return_value=mock_results),
        ):
            result = await search_agent_memory_tool(query="routing", mode="semantic", top_k=3)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["scope"] == "public"
        assert len(result["results"]) == 1

    async def test_success_private_scope_with_agent_name(self) -> None:
        mock_results: list[dict[str, Any]] = []

        with patch(
            "ypl.backend.llm.agent_memory_search.search_agent_memory",
            new=AsyncMock(return_value=mock_results),
        ):
            result = await search_agent_memory_tool(
                query="routing",
                scope="private",
                agent_name="eng-raccoon",
            )

        assert result["success"] is True
        assert result["agent_name"] == "eng-raccoon"
        assert result["scope"] == "private"

    async def test_success_hybrid_mode(self) -> None:
        mock_results = [{"topic": "notes", "snippet": "Hybrid results"}]

        with patch(
            "ypl.backend.llm.agent_memory_search.search_agent_memory",
            new=AsyncMock(return_value=mock_results),
        ):
            result = await search_agent_memory_tool(query="something", mode="hybrid")

        assert result["success"] is True

    async def test_generic_error_returns_failure(self) -> None:
        with patch(
            "ypl.backend.llm.agent_memory_search.search_agent_memory",
            new=AsyncMock(side_effect=Exception("Search backend unavailable")),
        ):
            result = await search_agent_memory_tool(query="routing")

        assert result["success"] is False
        assert "Search backend unavailable" in result["error"]

    async def test_all_scope_requires_agent_name(self) -> None:
        result = await search_agent_memory_tool(query="routing", scope="all", agent_name="")
        assert result["success"] is False
        assert "agent_name is required" in result["error"]

    async def test_keyword_mode_accepted(self) -> None:
        with patch(
            "ypl.backend.llm.agent_memory_search.search_agent_memory",
            new=AsyncMock(return_value=[]),
        ):
            result = await search_agent_memory_tool(query="routing", mode="keyword")

        assert result["success"] is True


# ---------------------------------------------------------------------------
# GCSPreconditionFailed
# ---------------------------------------------------------------------------


class TestGCSPreconditionFailed:
    def test_is_exception(self) -> None:
        exc = GCSPreconditionFailed("412 Precondition Failed")
        assert isinstance(exc, Exception)
        assert "412" in str(exc)


# ---------------------------------------------------------------------------
# _upload_agent_memory — 412 → GCSPreconditionFailed
# ---------------------------------------------------------------------------


class TestUploadAgentMemory:
    async def test_412_raises_gcs_precondition_failed(self) -> None:
        mock_error = aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=412)
        mock_client = AsyncMock()
        mock_client.upload = AsyncMock(side_effect=mock_error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client),
            pytest.raises(GCSPreconditionFailed),
        ):
            await _upload_agent_memory(
                bucket="test-bucket",
                blob_path="memory/test.md",
                content_bytes=b"hello",
                expected_generation=0,
            )

    async def test_non_412_error_propagates(self) -> None:
        mock_error = aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=503)
        mock_client = AsyncMock()
        mock_client.upload = AsyncMock(side_effect=mock_error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_memory.Storage", return_value=mock_client),
            pytest.raises(aiohttp.ClientResponseError),
        ):
            await _upload_agent_memory(
                bucket="test-bucket",
                blob_path="memory/test.md",
                content_bytes=b"hello",
                expected_generation=0,
            )
