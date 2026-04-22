"""Unit tests for ypl/agent_harness_service/artifact_routes.py.

Uses FastAPI's TestClient with mocked service-layer functions so the
tests stay hermetic (no blob store, no database).
"""

from __future__ import annotations
import base64
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ypl.agent_harness_service.artifact_routes import artifact_router
from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.db.agent_harness import AgentArtifactType

FAKE_ARTIFACT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.dependency_overrides[verify_api_key] = lambda: None
    _app.include_router(artifact_router, prefix="/ahs")
    return _app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _mock_artifact(
    *,
    artifact_id: uuid.UUID = FAKE_ARTIFACT_ID,
    title: str = "Hello",
    named_slug: str | None = "my-slug",
    version: int | None = 1,
    content_type: str = "text/markdown",
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    m = MagicMock()
    m.agent_artifact_id = artifact_id
    m.artifact_type = AgentArtifactType.TEXT
    m.title = title
    m.description = None
    m.url = f"/ahs/artifacts/{artifact_id}"
    m.content_type = content_type
    m.named_slug = named_slug
    m.version = version
    m.creator_user_id = "user-1"
    m.creator_agent_id = None
    m.agent_session_id = None
    m.agent_task_id = None
    m.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    m.artifact_metadata = metadata if metadata is not None else {}
    return m


# ---------------------------------------------------------------------------
# POST /ahs/artifacts
# ---------------------------------------------------------------------------


class TestCreateArtifactRoute:
    def test_creates_artifact_and_returns_slug_url(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with patch(
            "ypl.agent_harness_service.artifact_routes.create_artifact",
            new=AsyncMock(return_value=fake),
        ) as mock_create:
            resp = client.post(
                "/ahs/artifacts",
                json={
                    "content": "# hello",
                    "title": "Hello",
                    "named_slug": "my-slug",
                    "create_new_slug": True,
                },
            )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["artifact_id"] == str(FAKE_ARTIFACT_ID)
        assert body["slug_url"] == "/ahs/artifacts/by-slug/my-slug"
        assert body["version"] == 1
        # Service layer was invoked with encoded content.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["content"] == b"# hello"
        assert kwargs["named_slug"] == "my-slug"
        assert kwargs["create_new_slug"] is True

    def test_rejects_content_over_limit(self, client: TestClient) -> None:
        from ypl.agent_harness_service.artifact_store import MAX_CONTENT_SIZE_BYTES

        huge = "x" * (MAX_CONTENT_SIZE_BYTES + 1)
        resp = client.post(
            "/ahs/artifacts",
            json={"content": huge, "title": "Too big"},
        )
        assert resp.status_code == 413

    def test_invalid_slug_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            json={
                "content": "# ok",
                "title": "bad slug",
                "named_slug": "bad slug with space",
            },
        )
        assert resp.status_code == 422

    def test_artifact_error_returns_400(self, client: TestClient) -> None:
        from ypl.agent_harness_service.artifact_store import ArtifactError

        with patch(
            "ypl.agent_harness_service.artifact_routes.create_artifact",
            new=AsyncMock(side_effect=ArtifactError("boom")),
        ):
            resp = client.post(
                "/ahs/artifacts",
                json={"content": "# ok", "title": "x"},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "boom"

    def test_accepts_base64_attachments(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with patch(
            "ypl.agent_harness_service.artifact_routes.create_artifact",
            new=AsyncMock(return_value=fake),
        ) as mock_create:
            resp = client.post(
                "/ahs/artifacts",
                json={
                    "content": "# hi",
                    "title": "With attachment",
                    "attachments": [
                        {
                            "filename": "photo.png",
                            "content_base64": base64.b64encode(b"PNGDATA").decode(),
                            "content_type": "image/png",
                        },
                    ],
                },
            )
        assert resp.status_code == 201
        kwargs = mock_create.call_args.kwargs
        assert len(kwargs["attachments"]) == 1
        assert kwargs["attachments"][0].data == b"PNGDATA"

    def test_rejects_invalid_base64_attachment(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            json={
                "content": "# hi",
                "title": "x",
                "attachments": [
                    {
                        "filename": "photo.png",
                        "content_base64": "@@@not-base64@@@",
                        "content_type": "image/png",
                    },
                ],
            },
        )
        assert resp.status_code == 400
        assert "Invalid base64" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /ahs/artifacts
# ---------------------------------------------------------------------------


class TestListArtifactsRoute:
    def test_empty_list(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.get("/ahs/artifacts")
        assert resp.status_code == 200
        assert resp.json() == {"artifacts": []}

    def test_forwards_filters(self, client: TestClient) -> None:
        fake = _mock_artifact()
        sess_id = uuid.uuid4()
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[fake]),
        ) as mock_list:
            resp = client.get(
                "/ahs/artifacts",
                params={
                    "type": "TEXT",
                    "agent_session_id": str(sess_id),
                    "limit": 10,
                },
            )
        assert resp.status_code == 200
        assert len(resp.json()["artifacts"]) == 1
        kwargs = mock_list.call_args.kwargs
        assert kwargs["artifact_type"] == AgentArtifactType.TEXT
        assert kwargs["agent_session_id"] == sess_id
        assert kwargs["limit"] == 10


# ---------------------------------------------------------------------------
# GET /ahs/artifacts/{id}
# ---------------------------------------------------------------------------


class TestReadArtifactRoute:
    def test_returns_content_with_content_type(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(return_value=(b"# hello", "text/markdown")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 200
        assert resp.content == b"# hello"
        assert resp.headers["content-type"].startswith("text/markdown")

    def test_sets_download_filename_from_title(self, client: TestClient) -> None:
        # The browser-facing "Raw content" link should save the file using the
        # artifact title plus a content-type-appropriate extension.
        fake = _mock_artifact(title="Quarterly report 2026 Q1")
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(return_value=(b"# q1", "text/markdown")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        assert disp.startswith("attachment;")
        assert 'filename="Quarterly report 2026 Q1.md"' in disp
        assert "filename*=UTF-8''Quarterly%20report%202026%20Q1.md" in disp

    def test_download_filename_handles_unicode_and_unsafe_chars(self, client: TestClient) -> None:
        # Em dash and slashes — ASCII fallback sanitized, UTF-8 copy preserved.
        fake = _mock_artifact(title="AHS Performance — v2/final?")
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(return_value=(b"body", "text/markdown")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        # `/` and `?` stripped, em dash replaced with `_` in the ASCII fallback.
        assert 'filename="AHS Performance _ v2 final.md"' in disp
        # UTF-8 variant preserves the em dash (%E2%80%94).
        assert "%E2%80%94" in disp

    def test_404_when_missing(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 404

    def test_404_when_blob_missing(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(side_effect=FileNotFoundError("gone")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 404


class TestReadArtifactMetaRoute:
    def test_returns_metadata_json(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=fake),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}/meta")
        assert resp.status_code == 200
        body = resp.json()
        assert body["artifact_id"] == str(FAKE_ARTIFACT_ID)
        assert body["named_slug"] == "my-slug"
        assert body["version"] == 1

    def test_404_when_missing(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}/meta")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /ahs/artifacts/{id}/attachments/{filename}
# ---------------------------------------------------------------------------


class TestReadAttachmentRoute:
    def test_returns_attachment_bytes(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_attachment",
                new=AsyncMock(return_value=(b"PNGDATA", "image/png")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}/attachments/photo.png")
        assert resp.status_code == 200
        assert resp.content == b"PNGDATA"
        assert resp.headers["content-type"].startswith("image/png")

    def test_404_when_artifact_missing(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}/attachments/photo.png")
        assert resp.status_code == 404

    def test_404_when_attachment_not_found(self, client: TestClient) -> None:
        from ypl.agent_harness_service.artifact_store import ArtifactError

        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_attachment",
                new=AsyncMock(side_effect=ArtifactError("Attachment 'nope' not found")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}/attachments/nope")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /ahs/artifacts/by-slug/{slug}
# ---------------------------------------------------------------------------


class TestReadBySlugRoute:
    def test_returns_artifact(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_slug",
            new=AsyncMock(return_value=fake),
        ) as mock_get:
            resp = client.get("/ahs/artifacts/by-slug/my-slug")
        assert resp.status_code == 200
        assert resp.json()["named_slug"] == "my-slug"
        # Defaults to TEXT, no version.
        kwargs = mock_get.call_args.kwargs
        assert kwargs["version"] is None
        assert kwargs["artifact_type"] == AgentArtifactType.TEXT

    def test_forwards_version_param(self, client: TestClient) -> None:
        fake = _mock_artifact(version=3)
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_slug",
            new=AsyncMock(return_value=fake),
        ) as mock_get:
            resp = client.get("/ahs/artifacts/by-slug/my-slug", params={"version": 3})
        assert resp.status_code == 200
        assert mock_get.call_args.kwargs["version"] == 3

    def test_404_when_missing(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_slug",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get("/ahs/artifacts/by-slug/nope")
        assert resp.status_code == 404


class TestListVersionsRoute:
    def test_returns_ordered_versions(self, client: TestClient) -> None:
        v1 = _mock_artifact(version=1)
        v2 = _mock_artifact(version=2)
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
            new=AsyncMock(return_value=[v1, v2]),
        ):
            resp = client.get("/ahs/artifacts/by-slug/my-slug/versions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["named_slug"] == "my-slug"
        assert [v["version"] for v in body["versions"]] == [1, 2]


# ---------------------------------------------------------------------------
# DELETE /ahs/artifacts/{id}
# ---------------------------------------------------------------------------


class TestArchiveArtifactRoute:
    def test_archive_success_returns_204(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.archive_artifact",
            new=AsyncMock(return_value=True),
        ):
            resp = client.delete(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 204

    def test_archive_missing_returns_404(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.archive_artifact",
            new=AsyncMock(return_value=False),
        ):
            resp = client.delete(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 404


class TestArchiveBySlugRoute:
    def test_archive_all_versions(self, client: TestClient) -> None:
        v1 = _mock_artifact(version=1)
        v2 = _mock_artifact(version=2)
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
                new=AsyncMock(return_value=[v1, v2]),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.archive_artifacts_by_slug",
                new=AsyncMock(return_value=2),
            ),
        ):
            resp = client.delete("/ahs/artifacts/by-slug/my-slug")
        assert resp.status_code == 200
        assert resp.json() == {"named_slug": "my-slug", "archived_count": 2}

    def test_archive_unknown_slug_returns_404(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.delete("/ahs/artifacts/by-slug/nope")
        assert resp.status_code == 404

    def test_all_already_archived_returns_zero_count(self, client: TestClient) -> None:
        v1 = _mock_artifact(version=1)
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
                new=AsyncMock(return_value=[v1]),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.archive_artifacts_by_slug",
                new=AsyncMock(return_value=0),
            ),
        ):
            resp = client.delete("/ahs/artifacts/by-slug/already-gone")
        assert resp.status_code == 200
        assert resp.json()["archived_count"] == 0


class TestSearchArtifactsRoute:
    def test_returns_matches(self, client: TestClient) -> None:
        fake = _mock_artifact(title="Quarterly report 2026 Q1")
        with patch(
            "ypl.agent_harness_service.artifact_routes.search_artifacts",
            new=AsyncMock(return_value=[fake]),
        ) as mock_search:
            resp = client.get("/ahs/artifacts/search", params={"q": "quarterly"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["artifacts"]) == 1
        # Query forwarded positionally.
        assert mock_search.call_args.args[0] == "quarterly"

    def test_requires_query(self, client: TestClient) -> None:
        resp = client.get("/ahs/artifacts/search")
        assert resp.status_code == 422

    def test_empty_query_rejected(self, client: TestClient) -> None:
        resp = client.get("/ahs/artifacts/search", params={"q": ""})
        assert resp.status_code == 422

    def test_forwards_pagination_and_type(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.search_artifacts",
            new=AsyncMock(return_value=[]),
        ) as mock_search:
            resp = client.get(
                "/ahs/artifacts/search",
                params={"q": "foo", "limit": 10, "offset": 20, "type": "TEXT"},
            )
        assert resp.status_code == 200
        kwargs = mock_search.call_args.kwargs
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 20
        assert kwargs["artifact_type"] == AgentArtifactType.TEXT
