"""Unit tests for ypl/mcp_server/tools/agent_artifacts.py (unified surface)
and ypl/mcp_server/tools/memory_artifacts.py (memory tools split from the
same surface).
"""

from __future__ import annotations
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.mcp_common.auth_context import RequestContext
from ypl.mcp_server.tools.agent_artifacts import (
    _parse_session_id,
    add_artifact,
    archive_artifact_slug,
    artifact_url,
    list_artifact_versions,
    list_artifacts,
    search_artifacts,
    update_artifact,
    update_artifact_content,
)
from ypl.mcp_server.tools.memory_artifacts import (
    list_memory,
    load_memory,
    save_memory,
    search_memory,
)

FAKE_SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_ARTIFACT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FAKE_TASK_ID = uuid.UUID("66666666-7777-8888-9999-000000000000")


def _make_ctx(
    *,
    session_id: str | None = FAKE_SESSION_ID,
    agent_name: str | None = "sre",
    user_id: str | None = "user-123",
) -> RequestContext:
    """Construct a typed :class:`RequestContext` for tests."""
    return RequestContext(
        auth_kind="agent_secret",
        requesting_user_id=user_id,
        ahs_session_id=session_id,
        ahs_agent_name=agent_name,
    )


def _make_artifact(
    artifact_id: uuid.UUID = FAKE_ARTIFACT_ID,
    title: str = "My PR",
    artifact_type_value: str = "CODE_REVIEW",
    url: str = "https://github.com/pr/1",
    named_slug: str | None = None,
    version: int | None = None,
    content_type: str | None = None,
) -> MagicMock:
    from ypl.db.agent_harness import AgentArtifactType

    a = MagicMock()
    a.agent_artifact_id = artifact_id
    a.title = title
    a.url = url
    a.artifact_type = AgentArtifactType(artifact_type_value)
    a.description = None
    a.named_slug = named_slug
    a.version = version
    a.content_type = content_type
    a.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    a.artifact_metadata = None
    a.deleted_at = None
    return a


# ---------------------------------------------------------------------------
# _parse_session_id
# ---------------------------------------------------------------------------


class TestParseSessionId:
    def test_valid_uuid(self) -> None:
        parsed = _parse_session_id(FAKE_SESSION_ID)
        assert parsed == uuid.UUID(FAKE_SESSION_ID)

    def test_none_returns_none(self) -> None:
        assert _parse_session_id(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_session_id("") is None

    def test_invalid_uuid_returns_none(self) -> None:
        assert _parse_session_id("not-a-uuid") is None


# ---------------------------------------------------------------------------
# add_artifact — pointer flows (CODE_REVIEW / OTHER)
# ---------------------------------------------------------------------------
#
# The artifact tools write to AL arti via ``_arti_backend``; these tests mock
# that module and pin how each tool validates input, threads caller context /
# provenance, and maps arti errors onto the tool's ``{success, error}`` shape.

_BACKEND = "ypl.mcp_server.tools._arti_backend"


def _enter_caller_ctx(stack: ExitStack) -> None:
    """Apply the standard MCP-header patches via ``ExitStack``."""
    ctx = _make_ctx()
    stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts.current_request_context", return_value=ctx))
    stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)))


def _patch_backend(stack: ExitStack, name: str, **kwargs: Any) -> AsyncMock:
    mock = AsyncMock(**kwargs)
    stack.enter_context(patch(f"{_BACKEND}.{name}", mock))
    return mock


class TestAddArtifactPointer:
    async def test_code_review_success(self) -> None:
        arti_result = {
            "success": True,
            "artifact_id": str(FAKE_ARTIFACT_ID),
            "url": "https://github.com/pr/1",
            "title": "My PR",
            "message": "Registered CODE_REVIEW 'My PR'.",
        }
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            mock_add = _patch_backend(stack, "add_pointer", return_value=arti_result)
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="My PR",
                url="https://github.com/pr/1",
            )
        assert result == arti_result
        kwargs = mock_add.call_args.kwargs
        assert kwargs["artifact_type"] == "CODE_REVIEW"
        assert kwargs["url"] == "https://github.com/pr/1"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["session_id"] == uuid.UUID(FAKE_SESSION_ID)
        assert f"session:{FAKE_SESSION_ID}" in kwargs["prov"]
        assert "agent:sre" in kwargs["prov"]

    async def test_other_requires_url(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            result = await add_artifact.fn(
                artifact_type="OTHER",
                title="Dashboard link",
            )
        assert result["success"] is False
        assert "require a 'url'" in result["error"]

    async def test_invalid_type_returns_error(self) -> None:
        result = await add_artifact.fn(
            artifact_type="INVALID_TYPE",
            title="Test",
            url="http://example.com",
        )
        assert result["success"] is False
        assert "Invalid artifact_type" in result["error"]

    async def test_invalid_task_id_returns_error(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
                agent_task_id="not-a-uuid",
            )
        assert result["success"] is False
        assert "Invalid agent_task_id" in result["error"]

    async def test_valid_task_id_becomes_provenance_label(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            mock_add = _patch_backend(stack, "add_pointer", return_value={"success": True})
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
                agent_task_id=str(FAKE_TASK_ID),
            )
        assert result["success"] is True
        assert f"task:{FAKE_TASK_ID}" in mock_add.call_args.kwargs["prov"]

    async def test_arti_tool_error_returns_error(self) -> None:
        from ypl.external_mcp.arti_client import ArtiToolError

        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            _patch_backend(stack, "add_pointer", side_effect=ArtiToolError("forbidden"))
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
            )
        assert result["success"] is False
        assert "AL arti rejected the write" in result["error"]

    async def test_arti_unavailable_returns_error(self) -> None:
        from ypl.external_mcp.arti_client import ArtiUnavailable

        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            _patch_backend(stack, "add_pointer", side_effect=ArtiUnavailable("not registered"))
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
            )
        assert result["success"] is False
        assert "AL arti unavailable" in result["error"]


# ---------------------------------------------------------------------------
# add_artifact — TEXT flow (content-carrying)
# ---------------------------------------------------------------------------


class TestAddArtifactText:
    async def test_text_requires_content(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            result = await add_artifact.fn(
                artifact_type="TEXT",
                title="Report",
            )
        assert result["success"] is False
        assert "require a 'content'" in result["error"]

    async def test_text_success(self) -> None:
        arti_result = {
            "success": True,
            "artifact_id": str(FAKE_ARTIFACT_ID),
            "slug": "my-report",
            "version": 1,
            "content_type": "text/markdown",
        }
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            mock_add = _patch_backend(stack, "add_text", return_value=arti_result)
            result = await add_artifact.fn(
                artifact_type="TEXT",
                title="Report",
                content="# Hello",
                named_slug="my-report",
                create_new_slug=True,
                labels=["Ops"],
            )
        assert result == arti_result
        kwargs = mock_add.call_args.kwargs
        assert kwargs["content"] == "# Hello"
        assert kwargs["named_slug"] == "my-report"
        assert kwargs["create_new_slug"] is True
        assert kwargs["labels"] == ["ops"]  # normalized before reaching arti

    async def test_text_attachments_rejected(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            mock_add = _patch_backend(stack, "add_text")
            result = await add_artifact.fn(
                artifact_type="TEXT",
                title="Report",
                content="x",
                attachments='[{"filename": "a.png", "content_base64": "AA==", "content_type": "image/png"}]',
            )
        assert result["success"] is False
        assert "attachments are not yet supported" in result["error"]
        mock_add.assert_not_called()


# ---------------------------------------------------------------------------
# update_artifact
# ---------------------------------------------------------------------------


class TestUpdateArtifact:
    async def test_success(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts._caller_user_id", return_value="u1"))
            mock_update = _patch_backend(stack, "update_meta", return_value={"success": True})
            result = await update_artifact.fn(
                artifact_id=str(FAKE_ARTIFACT_ID),
                title="Updated PR",
                labels=["Release"],
            )
        assert result["success"] is True
        kwargs = mock_update.call_args.kwargs
        assert kwargs["ident"] == str(FAKE_ARTIFACT_ID)
        assert kwargs["title"] == "Updated PR"
        assert kwargs["labels"] == ["release"]
        assert kwargs["user_id"] == "u1"

    async def test_invalid_artifact_id_returns_error(self) -> None:
        result = await update_artifact.fn(artifact_id="not-a-uuid", title="x")
        assert result["success"] is False
        assert "Invalid artifact_id" in result["error"]

    async def test_no_fields_returns_error(self) -> None:
        result = await update_artifact.fn(artifact_id=str(FAKE_ARTIFACT_ID))
        assert result["success"] is False
        assert "At least one field" in result["error"]

    async def test_artifact_not_found(self) -> None:
        from ypl.external_mcp.arti_client import ArtiToolError

        with ExitStack() as stack:
            _patch_backend(stack, "update_meta", side_effect=ArtiToolError("artifact not found"))
            result = await update_artifact.fn(artifact_id=str(FAKE_ARTIFACT_ID), title="x")
        assert result["success"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# update_artifact_content
# ---------------------------------------------------------------------------


class TestUpdateArtifactContent:
    async def test_success(self) -> None:
        arti_result = {"success": True, "slug": "my-report", "version": 2}
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            mock_add = _patch_backend(stack, "add_version", return_value=arti_result)
            result = await update_artifact_content.fn(
                slug="my-report",
                content="# v2",
            )
        assert result == arti_result
        kwargs = mock_add.call_args.kwargs
        assert kwargs["slug"] == "my-report"
        assert kwargs["content"] == "# v2"
        assert kwargs["title"] == "my-report"  # defaults to the slug

    async def test_arti_error_returned(self) -> None:
        from ypl.external_mcp.arti_client import ArtiToolError

        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            _patch_backend(stack, "add_version", side_effect=ArtiToolError("slug 'my-report' does not exist yet"))
            result = await update_artifact_content.fn(slug="my-report", content="x")
        assert result["success"] is False
        assert "does not exist yet" in result["error"]


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


class TestListArtifacts:
    async def test_no_session_returns_empty(self) -> None:
        with patch("ypl.mcp_server.tools.agent_artifacts._caller_session_id", return_value=None):
            result = await list_artifacts.fn()
        assert result["success"] is True
        assert result["artifacts"] == []
        assert result["count"] == 0

    async def test_invalid_type_returns_error(self) -> None:
        with patch(
            "ypl.mcp_server.tools.agent_artifacts._caller_session_id",
            return_value=uuid.UUID(FAKE_SESSION_ID),
        ):
            result = await list_artifacts.fn(artifact_type="INVALID")
        assert result["success"] is False
        assert "Invalid artifact_type" in result["error"]

    async def test_returns_artifacts_for_session(self) -> None:
        arti_result = {"success": True, "artifacts": [{"artifact_id": str(FAKE_ARTIFACT_ID)}], "count": 1}
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts._caller_session_id",
                    return_value=uuid.UUID(FAKE_SESSION_ID),
                )
            )
            mock_list = _patch_backend(stack, "list_for_session", return_value=arti_result)
            result = await list_artifacts.fn(limit=500)
        assert result == arti_result
        assert mock_list.call_args.kwargs["session_id"] == uuid.UUID(FAKE_SESSION_ID)
        assert mock_list.call_args.kwargs["limit"] == 100  # clamped


# ---------------------------------------------------------------------------
# list_artifact_versions
# ---------------------------------------------------------------------------


class TestListArtifactVersions:
    async def test_success(self) -> None:
        arti_result = {"success": True, "slug": "my-report", "versions": [{"version": 1}, {"version": 2}], "count": 2}
        with ExitStack() as stack:
            _patch_backend(stack, "list_versions", return_value=arti_result)
            result = await list_artifact_versions.fn(slug="my-report")
        assert result == arti_result

    async def test_not_found_is_empty(self) -> None:
        from ypl.external_mcp.arti_client import ArtiToolError

        with ExitStack() as stack:
            _patch_backend(stack, "list_versions", side_effect=ArtiToolError("no such slug"))
            result = await list_artifact_versions.fn(slug="x")
        assert result == {"success": True, "slug": "x", "versions": [], "count": 0}

    async def test_unavailable(self) -> None:
        from ypl.external_mcp.arti_client import ArtiUnavailable

        with ExitStack() as stack:
            _patch_backend(stack, "list_versions", side_effect=ArtiUnavailable("down"))
            result = await list_artifact_versions.fn(slug="x")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# search_artifacts
# ---------------------------------------------------------------------------


class TestSearchArtifacts:
    async def test_empty_query_rejected(self) -> None:
        result = await search_artifacts.fn(query="   ")
        assert result["success"] is False
        assert "non-empty" in result["error"]

    async def test_invalid_type_rejected(self) -> None:
        result = await search_artifacts.fn(query="x", artifact_type="INVALID")
        assert result["success"] is False
        assert "Invalid artifact_type" in result["error"]

    async def test_success(self) -> None:
        arti_result = {"success": True, "results": [{"title": "Hello world report"}], "count": 1}
        with ExitStack() as stack:
            mock_search = _patch_backend(stack, "search", return_value=arti_result)
            result = await search_artifacts.fn(query="hello", limit=5, offset=0)
        assert result == arti_result
        assert mock_search.call_args.kwargs["query"] == "hello"
        assert mock_search.call_args.kwargs["limit"] == 5


# ---------------------------------------------------------------------------
# artifact_url — AL arti first, legacy Yupp store fallback
# ---------------------------------------------------------------------------


class TestArtifactUrl:
    async def test_arti_hit(self) -> None:
        arti_result = {"success": True, "url": "https://arti/a/x", "artifact_type": "TEXT"}
        with ExitStack() as stack:
            _patch_backend(stack, "url_of", return_value=arti_result)
            legacy = stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.get_artifact_by_slug", AsyncMock())
            )
            result = await artifact_url.fn(id_or_slug="my-slug")
        assert result == arti_result
        legacy.assert_not_called()

    async def test_falls_back_to_legacy_by_id(self) -> None:
        art = _make_artifact(artifact_type_value="TEXT", url="https://viewer/artifacts/x")
        with ExitStack() as stack:
            _patch_backend(stack, "url_of", return_value=None)
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.get_artifact_by_id", AsyncMock(return_value=art))
            )
            result = await artifact_url.fn(id_or_slug=str(FAKE_ARTIFACT_ID))
        assert result["success"] is True
        assert result["url"] == "https://viewer/artifacts/x"
        assert result["artifact_type"] == "TEXT"

    async def test_falls_back_to_legacy_when_arti_unavailable(self) -> None:
        from ypl.external_mcp.arti_client import ArtiUnavailable

        art = _make_artifact(artifact_type_value="TEXT", named_slug="my-slug", url="https://viewer/artifacts/y")
        with ExitStack() as stack:
            _patch_backend(stack, "url_of", side_effect=ArtiUnavailable("down"))
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.get_artifact_by_slug", AsyncMock(return_value=art))
            )
            result = await artifact_url.fn(id_or_slug="my-slug")
        assert result["success"] is True
        assert result["url"] == "https://viewer/artifacts/y"

    async def test_not_found_anywhere(self) -> None:
        from ypl.external_mcp.arti_client import ArtiToolError

        with ExitStack() as stack:
            _patch_backend(stack, "url_of", side_effect=ArtiToolError("not found"))
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.get_artifact_by_slug", AsyncMock(return_value=None))
            )
            result = await artifact_url.fn(id_or_slug="missing")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# archive_artifact_slug — AL arti first, legacy Yupp store fallback
# ---------------------------------------------------------------------------


class TestArchiveArtifactSlug:
    async def test_arti_success(self) -> None:
        with ExitStack() as stack:
            _patch_backend(stack, "archive", return_value=True)
            legacy = stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.archive_artifacts_by_slug", AsyncMock())
            )
            result = await archive_artifact_slug.fn(slug="my-report")
        assert result["success"] is True
        assert result["slug"] == "my-report"
        legacy.assert_not_called()

    async def test_legacy_fallback(self) -> None:
        with ExitStack() as stack:
            _patch_backend(stack, "archive", return_value=None)
            stack.enter_context(
                patch("ypl.mcp_server.tools.agent_artifacts.archive_artifacts_by_slug", AsyncMock(return_value=3))
            )
            result = await archive_artifact_slug.fn(slug="my-report")
        assert result["success"] is True
        assert result["archived_count"] == 3

    async def test_legacy_exception(self) -> None:
        with ExitStack() as stack:
            _patch_backend(stack, "archive", return_value=None)
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.agent_artifacts.archive_artifacts_by_slug",
                    AsyncMock(side_effect=RuntimeError("db down")),
                )
            )
            result = await archive_artifact_slug.fn(slug="x")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Memory MCP tools — scope-authz helpers
# ---------------------------------------------------------------------------


def _memory_artifact(
    *,
    scope: str = "agent",
    subject: str | None = "eng-raccoon",
    slug: str = "feedback_style",
    version: int = 1,
    inline_content: str = "# Feedback",
) -> MagicMock:
    from ypl.db.agent_harness import AgentArtifactType

    a = MagicMock()
    a.agent_artifact_id = uuid.uuid4()
    a.artifact_type = AgentArtifactType.MEMORY
    a.title = slug
    a.description = None
    a.url = None
    a.content_type = "text/markdown"
    a.inline_content = inline_content
    a.named_slug = slug
    a.version = version
    a.memory_scope = scope
    a.memory_scope_subject = subject
    a.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    a.artifact_metadata = {}
    a.deleted_at = None
    return a


def _caller_ctx(stack: ExitStack, *, user_id: str | None = "USR_X", agent_name: str | None = "eng-raccoon") -> None:
    """Install MCP caller-context patches inside ``stack``.

    Both ``agent_artifacts`` and ``memory_artifacts`` consume the typed
    :class:`RequestContext` via ``current_request_context()``; we patch
    that in each tool module's namespace so the tests stay independent of
    the global ContextVar.
    """
    session_id_value = FAKE_SESSION_ID if user_id or agent_name else None
    ctx = _make_ctx(session_id=session_id_value, agent_name=agent_name, user_id=user_id)
    # agent_artifacts namespace (used inside _resolve_caller_context).
    stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts.current_request_context", return_value=ctx))
    stack.enter_context(patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)))
    # memory_artifacts namespace (used by _memory_caller_from_context and the
    # write-through sandbox copy in save_memory).
    stack.enter_context(patch("ypl.mcp_server.tools.memory_artifacts.current_request_context", return_value=ctx))


class TestSaveMemory:
    async def test_default_scope_is_agent_and_fills_subject(self) -> None:
        art = _memory_artifact(scope="agent", subject="eng-raccoon")
        with ExitStack() as stack:
            _caller_ctx(stack)
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=None),
                )
            )
            mock_create = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.create_artifact",
                    AsyncMock(return_value=art),
                )
            )
            result = await save_memory.fn(topic="feedback_style", content="# Feedback")
        assert result["success"] is True
        assert result["scope"] == "agent"
        assert result["subject"] == "eng-raccoon"
        assert result["address"] == "a:eng-raccoon:feedback_style"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["memory_scope"] == "agent"
        assert kwargs["memory_scope_subject"] == "eng-raccoon"
        assert kwargs["inline_content"] == "# Feedback"
        assert kwargs["named_slug"] == "feedback_style"
        assert kwargs["create_new_slug"] is True  # first save → new slug

    async def test_version_bump_on_existing_slug(self) -> None:
        existing = _memory_artifact(version=1)
        new_version = _memory_artifact(version=2)
        with ExitStack() as stack:
            _caller_ctx(stack)
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=existing),
                )
            )
            mock_create = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.create_artifact",
                    AsyncMock(return_value=new_version),
                )
            )
            result = await save_memory.fn(topic="feedback_style", content="# v2")
        assert result["success"] is True
        assert result["version"] == 2
        # Second save → append version (not new slug).
        assert mock_create.call_args.kwargs["create_new_slug"] is False

    async def test_user_scope_defaults_to_caller_user_id(self) -> None:
        art = _memory_artifact(scope="user", subject="USR_X", slug="prefs")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="eng-raccoon")
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=None),
                )
            )
            mock_create = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.create_artifact",
                    AsyncMock(return_value=art),
                )
            )
            result = await save_memory.fn(topic="prefs", content="...", scope="user")
        assert result["success"] is True
        assert mock_create.call_args.kwargs["memory_scope_subject"] == "USR_X"

    async def test_cross_user_write_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="eng-raccoon")
            result = await save_memory.fn(
                topic="prefs",
                content="...",
                scope="user",
                subject="USR_OTHER",
            )
        assert result["success"] is False
        assert "Cross-scope write rejected" in result["error"]

    async def test_cross_agent_write_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            result = await save_memory.fn(
                topic="notes",
                content="...",
                scope="agent",
                subject="bob",
            )
        assert result["success"] is False
        assert "Cross-scope write rejected" in result["error"]

    async def test_user_scope_without_user_id_rejected(self) -> None:
        """Caller with no user_id can't write to user scope at all."""
        with ExitStack() as stack:
            _caller_ctx(stack, user_id=None, agent_name="alice")
            result = await save_memory.fn(topic="prefs", content="...", scope="user")
        assert result["success"] is False
        # Either 'requires subject' or 'cross-scope rejected' — both are correct.
        assert "subject" in result["error"].lower() or "cross-scope" in result["error"].lower()

    async def test_topic_scope_allowed_for_any_caller(self) -> None:
        art = _memory_artifact(scope="topic", subject=None, slug="routing_tips")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.create_artifact",
                    AsyncMock(return_value=art),
                )
            )
            result = await save_memory.fn(topic="routing_tips", content="...", scope="topic")
        assert result["success"] is True
        assert result["address"] == "t:routing_tips"

    async def test_invalid_scope_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack)
            result = await save_memory.fn(topic="x", content="y", scope="project")
        assert result["success"] is False
        assert "Invalid scope" in result["error"]


class TestLoadMemory:
    async def test_load_own_agent_scope(self) -> None:
        art = _memory_artifact(scope="agent", subject="alice")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=art),
                )
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.read_artifact_content",
                    AsyncMock(return_value=(b"# Feedback", "text/markdown")),
                )
            )
            result = await load_memory.fn(topic="feedback_style")
        assert result["success"] is True
        assert result["content"] == "# Feedback"
        assert result["address"] == "a:alice:feedback_style"

    async def test_cross_user_read_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            result = await load_memory.fn(topic="prefs", scope="user", subject="USR_OTHER")
        assert result["success"] is False
        assert "Cross-user" in result["error"]

    async def test_cross_agent_read_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            result = await load_memory.fn(topic="notes", scope="agent", subject="bob")
        assert result["success"] is False
        assert "Cross-agent" in result["error"]

    async def test_topic_read_allowed(self) -> None:
        art = _memory_artifact(scope="topic", subject=None, slug="tips")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=art),
                )
            )
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.read_artifact_content",
                    AsyncMock(return_value=(b"# Tips", "text/markdown")),
                )
            )
            result = await load_memory.fn(topic="tips", scope="topic")
        assert result["success"] is True
        assert result["address"] == "t:tips"

    async def test_not_found(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack)
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts.get_artifact_by_slug",
                    AsyncMock(return_value=None),
                )
            )
            result = await load_memory.fn(topic="missing")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestSearchMemory:
    async def test_threads_caller_context(self) -> None:
        art = _memory_artifact(scope="topic", subject=None, slug="tips")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            mock_search = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts._search_artifacts",
                    AsyncMock(return_value=[art]),
                )
            )
            result = await search_memory.fn(query="tips")
        assert result["success"] is True
        caller = mock_search.call_args.kwargs["memory_caller"]
        assert caller is not None
        assert caller.user_id == "USR_X"
        assert caller.agent_name == "alice"

    async def test_cross_user_subject_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            result = await search_memory.fn(query="x", scope="user", subject="USR_OTHER")
        assert result["success"] is False
        assert "Cross-user" in result["error"]

    async def test_topic_visibility_for_caller_without_user(self) -> None:
        """A caller with only agent_name still sees topic memories in results."""
        art_topic = _memory_artifact(scope="topic", subject=None, slug="tips")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id=None, agent_name="alice")
            stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts._search_artifacts",
                    AsyncMock(return_value=[art_topic]),
                )
            )
            result = await search_memory.fn(query="tips")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["results"][0]["scope"] == "topic"

    async def test_empty_query(self) -> None:
        result = await search_memory.fn(query="   ")
        assert result["success"] is False


class TestListMemory:
    async def test_threads_caller_context(self) -> None:
        mine = _memory_artifact(scope="agent", subject="alice")
        topic = _memory_artifact(scope="topic", subject=None, slug="tips")
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            mock_list = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts._list_artifacts",
                    AsyncMock(return_value=[mine, topic]),
                )
            )
            result = await list_memory.fn()
        assert result["success"] is True
        assert result["count"] == 2
        caller = mock_list.call_args.kwargs["memory_caller"]
        assert caller is not None
        assert caller.agent_name == "alice"

    async def test_scope_user_defaults_to_caller_subject(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, user_id="USR_X", agent_name="alice")
            mock_list = stack.enter_context(
                patch(
                    "ypl.mcp_server.tools.memory_artifacts._list_artifacts",
                    AsyncMock(return_value=[]),
                )
            )
            await list_memory.fn(scope="user")
        assert mock_list.call_args.kwargs["memory_scope_subject"] == "USR_X"

    async def test_cross_agent_list_rejected(self) -> None:
        with ExitStack() as stack:
            _caller_ctx(stack, agent_name="alice")
            result = await list_memory.fn(scope="agent", subject="bob")
        assert result["success"] is False
        assert "Cross-agent" in result["error"]


class TestAddArtifactRejectsMemory:
    """add_artifact is the pointer tool — MEMORY must go through save_memory."""

    async def test_memory_routed_to_save_memory(self) -> None:
        with ExitStack() as stack:
            _enter_caller_ctx(stack)
            result = await add_artifact.fn(
                artifact_type="MEMORY",
                title="prefs",
                content="...",
            )
        assert result["success"] is False
        assert "save_memory" in result["error"]
