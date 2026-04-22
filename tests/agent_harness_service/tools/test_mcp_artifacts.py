"""Unit tests for ypl/mcp_server/tools/agent_artifacts.py."""

from __future__ import annotations
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from ypl.mcp_server.tools.agent_artifacts import (
    _parse_session_id,
    add_artifact,
    list_artifacts,
    update_artifact,
)

FAKE_SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_ARTIFACT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FAKE_TASK_ID = uuid.UUID("66666666-7777-8888-9999-000000000000")


def _make_artifact(
    artifact_id: uuid.UUID = FAKE_ARTIFACT_ID,
    title: str = "My PR",
    artifact_type_value: str = "CODE_REVIEW",
    url: str = "https://github.com/pr/1",
) -> MagicMock:
    from ypl.db.agent_harness import AgentArtifactType

    a = MagicMock()
    a.agent_artifact_id = artifact_id
    a.title = title
    a.url = url
    a.artifact_type = AgentArtifactType(artifact_type_value)
    a.description = None
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
# add_artifact
# ---------------------------------------------------------------------------


class TestAddArtifact:
    async def test_success(self) -> None:
        artifact = _make_artifact()

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch("ypl.mcp_server.tools.agent_artifacts.get_requesting_user_id", return_value="user-123"),
            patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._create_artifact",
                AsyncMock(return_value=artifact),
            ),
        ):
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="My PR",
                url="https://github.com/pr/1",
            )

        assert result["success"] is True
        assert result["artifact_id"] == str(FAKE_ARTIFACT_ID)
        assert "My PR" in result["message"]

    async def test_invalid_type_returns_error(self) -> None:
        result = await add_artifact.fn(
            artifact_type="INVALID_TYPE",
            title="Test",
            url="http://example.com",
        )
        assert result["success"] is False
        assert "Invalid artifact_type" in result["error"]

    async def test_invalid_task_id_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch("ypl.mcp_server.tools.agent_artifacts.get_requesting_user_id", return_value="user-123"),
            patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)),
        ):
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
                agent_task_id="not-a-uuid",
            )

        assert result["success"] is False
        assert "Invalid agent_task_id" in result["error"]

    async def test_valid_task_id_passed(self) -> None:
        artifact = _make_artifact()

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch("ypl.mcp_server.tools.agent_artifacts.get_requesting_user_id", return_value="user-123"),
            patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._create_artifact",
                AsyncMock(return_value=artifact),
            ) as mock_create,
        ):
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
                agent_task_id=str(FAKE_TASK_ID),
            )

        assert result["success"] is True
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["agent_task_id"] == FAKE_TASK_ID

    async def test_store_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch("ypl.mcp_server.tools.agent_artifacts.get_requesting_user_id", return_value="user-123"),
            patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._create_artifact",
                AsyncMock(side_effect=RuntimeError("db error")),
            ),
        ):
            result = await add_artifact.fn(
                artifact_type="CODE_REVIEW",
                title="Test",
                url="http://example.com",
            )

        assert result["success"] is False
        assert "Internal error" in result["error"]

    async def test_all_valid_types_accepted(self) -> None:
        for artifact_type in ["TEXT", "CODE_REVIEW", "OTHER"]:
            art = _make_artifact(artifact_type_value=artifact_type)
            with (
                patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
                patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
                patch("ypl.mcp_server.tools.agent_artifacts.get_requesting_user_id", return_value="u"),
                patch("ypl.mcp_server.tools.agent_artifacts._resolve_agent_id", AsyncMock(return_value=None)),
                patch(
                    "ypl.mcp_server.tools.agent_artifacts._create_artifact",
                    AsyncMock(return_value=art),
                ),
            ):
                result = await add_artifact.fn(
                    artifact_type=artifact_type,
                    title="T",
                    url="http://x.com",
                )
            assert result["success"] is True, f"Expected success for type {artifact_type}"


# ---------------------------------------------------------------------------
# update_artifact
# ---------------------------------------------------------------------------


class TestUpdateArtifact:
    async def test_success(self) -> None:
        artifact = _make_artifact(title="Updated PR")

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._update_artifact",
                AsyncMock(return_value=artifact),
            ),
        ):
            result = await update_artifact.fn(
                artifact_id=str(FAKE_ARTIFACT_ID),
                title="Updated PR",
            )

        assert result["success"] is True
        assert result["artifact_id"] == str(FAKE_ARTIFACT_ID)

    async def test_invalid_artifact_id_returns_error(self) -> None:
        result = await update_artifact.fn(
            artifact_id="not-a-uuid",
            title="x",
        )
        assert result["success"] is False
        assert "Invalid artifact_id" in result["error"]

    async def test_no_fields_returns_error(self) -> None:
        result = await update_artifact.fn(
            artifact_id=str(FAKE_ARTIFACT_ID),
        )
        assert result["success"] is False
        assert "At least one field" in result["error"]

    async def test_artifact_not_found(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._update_artifact",
                AsyncMock(return_value=None),
            ),
        ):
            result = await update_artifact.fn(
                artifact_id=str(FAKE_ARTIFACT_ID),
                title="x",
            )

        assert result["success"] is False
        assert "not found" in result["error"]

    async def test_exception_returns_error(self) -> None:
        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_agent_name", return_value="sre"),
            patch(
                "ypl.mcp_server.tools.agent_artifacts._update_artifact",
                AsyncMock(side_effect=RuntimeError("db error")),
            ),
        ):
            result = await update_artifact.fn(
                artifact_id=str(FAKE_ARTIFACT_ID),
                title="x",
            )

        assert result["success"] is False
        assert "Internal error" in result["error"]


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


class TestListArtifacts:
    async def test_no_session_returns_empty(self) -> None:
        with patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=None):
            result = await list_artifacts.fn()

        assert result["success"] is True
        assert result["artifacts"] == []
        assert result["count"] == 0

    async def test_invalid_type_returns_error(self) -> None:
        with patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID):
            result = await list_artifacts.fn(artifact_type="INVALID")

        assert result["success"] is False
        assert "Invalid artifact_type" in result["error"]

    async def test_returns_artifacts_for_session(self) -> None:
        artifact = _make_artifact()
        scalars = MagicMock()
        scalars.all.return_value = [artifact]
        db_result = MagicMock()
        db_result.scalars.return_value = scalars

        session = AsyncMock()
        session.execute.return_value = db_result
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_artifacts.fn()

        assert result["success"] is True
        assert result["count"] == 1
        assert result["artifacts"][0]["artifact_id"] == str(FAKE_ARTIFACT_ID)

    async def test_db_exception_returns_error(self) -> None:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_async_session_read_replica", return_value=ctx),
        ):
            result = await list_artifacts.fn()

        assert result["success"] is False
        assert "Internal error" in result["error"]

    async def test_limit_capped_at_100(self) -> None:
        scalars = MagicMock()
        scalars.all.return_value = []
        db_result = MagicMock()
        db_result.scalars.return_value = scalars
        session = AsyncMock()
        session.execute.return_value = db_result
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ypl.mcp_server.tools.agent_artifacts.get_ahs_session_id", return_value=FAKE_SESSION_ID),
            patch("ypl.mcp_server.tools.agent_artifacts.get_async_session_read_replica", return_value=ctx),
        ):
            # Passing limit=999 should still succeed (capped internally to 100)
            result = await list_artifacts.fn(limit=999)

        assert result["success"] is True
        # Verify the executed SQL statement capped the LIMIT to 100
        executed_stmt = session.execute.call_args[0][0]
        compiled = executed_stmt.compile(compile_kwargs={"literal_binds": True})
        assert "100" in str(compiled)
