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


@pytest.fixture(autouse=True)
def _patch_resolve_attribution() -> Any:
    """Stub out the DB-backed attribution resolver for all route tests."""
    with patch(
        "ypl.agent_harness_service.artifact_routes.resolve_attribution",
        new=AsyncMock(return_value=({}, {})),
    ) as mock_resolve:
        yield mock_resolve


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
    # TEXT artifacts never set inline_content. Make this explicit so the
    # ``read_artifact_content`` inline short-circuit doesn't trip on
    # MagicMock's auto-attribute behavior.
    m.inline_content = None
    m.named_slug = named_slug
    m.version = version
    m.memory_scope = None
    m.memory_scope_subject = None
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
        # ``total`` is null by default — the count query is opt-in via ``include_total``.
        assert resp.json() == {"artifacts": [], "total": None}

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

    def test_forwards_creator_agent_and_time_range(self, client: TestClient) -> None:
        fake = _mock_artifact()
        agent_id = uuid.uuid4()
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[fake]),
        ) as mock_list:
            resp = client.get(
                "/ahs/artifacts",
                params={
                    "creator_user_id": "user-42",
                    "creator_agent_id": str(agent_id),
                    "created_after": "2026-04-01T00:00:00+00:00",
                    "created_before": "2026-05-01T00:00:00+00:00",
                    "limit": 5,
                    "offset": 5,
                },
            )
        assert resp.status_code == 200
        kwargs = mock_list.call_args.kwargs
        assert kwargs["creator_user_id"] == "user-42"
        assert kwargs["creator_agent_id"] == agent_id
        assert kwargs["created_after"] == datetime(2026, 4, 1, tzinfo=UTC)
        assert kwargs["created_before"] == datetime(2026, 5, 1, tzinfo=UTC)
        assert kwargs["offset"] == 5

    def test_returns_total_when_requested(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.list_artifacts",
                new=AsyncMock(return_value=[fake]),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.count_artifacts",
                new=AsyncMock(return_value=137),
            ) as mock_count,
        ):
            resp = client.get("/ahs/artifacts", params={"include_total": "true", "limit": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 137
        assert mock_count.await_count == 1

    def test_total_omitted_by_default(self, client: TestClient) -> None:
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.list_artifacts",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.count_artifacts",
                new=AsyncMock(return_value=0),
            ) as mock_count,
        ):
            resp = client.get("/ahs/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        # Field present (Pydantic emits it) but null — and the count query
        # was skipped so we don't pay for it on every list call.
        assert body.get("total") is None
        assert mock_count.await_count == 0


# ---------------------------------------------------------------------------
# GET /ahs/artifacts/creators
# ---------------------------------------------------------------------------


class TestListCreatorsRoute:
    def test_returns_users_and_agents(self, client: TestClient) -> None:
        agent_uuid = uuid.uuid4()
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_distinct_creators",
            new=AsyncMock(
                return_value=(
                    [("user-1", "Alice"), ("user-2", None)],
                    [(agent_uuid, "eng-raccoon")],
                )
            ),
        ):
            resp = client.get("/ahs/artifacts/creators")
        assert resp.status_code == 200
        body = resp.json()
        assert body["users"] == [
            {"id": "user-1", "name": "Alice"},
            {"id": "user-2", "name": None},
        ]
        assert body["agents"] == [{"id": str(agent_uuid), "name": "eng-raccoon"}]

    def test_creators_path_does_not_match_artifact_id(self, client: TestClient) -> None:
        # Regression guard: ``/creators`` is a literal path that must be matched
        # before the ``/{artifact_id}`` route — otherwise FastAPI tries to parse
        # "creators" as a UUID and 422s.
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_distinct_creators",
            new=AsyncMock(return_value=([], [])),
        ):
            resp = client.get("/ahs/artifacts/creators")
        assert resp.status_code == 200


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

    def test_multi_segment_slug_round_trips(self, client: TestClient) -> None:
        # Hierarchical memory slugs contain ``/`` — the ``{slug:path}``
        # converter must forward the full path to the handler instead of
        # 404ing at the first separator.
        fake = _mock_artifact(named_slug="openclaw/notes/daily")
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_slug",
            new=AsyncMock(return_value=fake),
        ) as mock_get:
            resp = client.get("/ahs/artifacts/by-slug/openclaw/notes/daily")
        assert resp.status_code == 200
        assert mock_get.call_args.args[0] == "openclaw/notes/daily"


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

    def test_multi_segment_slug_not_shadowed_by_catch_all(self, client: TestClient) -> None:
        # The bare ``/by-slug/{slug:path}`` route is registered after this one;
        # confirm a multi-segment slug + ``/versions`` suffix still reaches the
        # versions handler (slug excludes the suffix) rather than being swallowed.
        v1 = _mock_artifact(version=1)
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
            new=AsyncMock(return_value=[v1]),
        ) as mock_list:
            resp = client.get("/ahs/artifacts/by-slug/openclaw/notes/daily/versions")
        assert resp.status_code == 200
        assert resp.json()["named_slug"] == "openclaw/notes/daily"
        assert mock_list.call_args.args[0] == "openclaw/notes/daily"


# ---------------------------------------------------------------------------
# DELETE /ahs/artifacts/{id}
# ---------------------------------------------------------------------------


class TestArchiveArtifactRoute:
    def test_archive_success_returns_204(self, client: TestClient) -> None:
        fake = _mock_artifact()
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.archive_artifact",
                new=AsyncMock(return_value=True),
            ),
        ):
            resp = client.delete(f"/ahs/artifacts/{FAKE_ARTIFACT_ID}")
        assert resp.status_code == 204

    def test_archive_missing_returns_404(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=None),
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


# ---------------------------------------------------------------------------
# MEMORY — create / list / read / delete scope authz
# ---------------------------------------------------------------------------


def _memory_artifact(
    *,
    artifact_id: uuid.UUID | None = None,
    scope: str = "agent",
    subject: str | None = "eng-raccoon",
    slug: str = "feedback_style",
    version: int = 1,
    inline_content: str = "# Feedback\nPrefers short answers.",
) -> MagicMock:
    a = MagicMock()
    a.agent_artifact_id = artifact_id or uuid.uuid4()
    a.artifact_type = AgentArtifactType.MEMORY
    a.title = slug
    a.description = None
    a.url = None  # MEMORY has no url
    a.content_type = "text/markdown"
    a.inline_content = inline_content
    a.named_slug = slug
    a.version = version
    a.memory_scope = scope
    a.memory_scope_subject = subject
    a.creator_user_id = None
    a.creator_agent_id = None
    a.agent_session_id = None
    a.agent_task_id = None
    a.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    a.artifact_metadata = {}
    return a


class TestCreateMemoryRoute:
    def test_create_agent_scope_success(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="agent", subject="eng-raccoon")
        with patch(
            "ypl.agent_harness_service.artifact_routes.create_artifact",
            new=AsyncMock(return_value=fake),
        ) as mock_create:
            resp = client.post(
                "/ahs/artifacts",
                headers={"X-AHS-Agent-Name": "eng-raccoon", "X-User-ID": "USR_X"},
                json={
                    "type": "MEMORY",
                    "title": "feedback_style",
                    "inline_content": "# Feedback\nPrefers short answers.",
                    "memory_scope": "agent",
                    "memory_scope_subject": "eng-raccoon",
                    "named_slug": "feedback_style",
                    "create_new_slug": True,
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["memory_scope"] == "agent"
        assert body["memory_scope_subject"] == "eng-raccoon"
        assert body["url"] is None
        assert body["type"] == "MEMORY"
        # Store was called with inline_content, scope fields, and no content.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["content"] is None
        assert kwargs["inline_content"] == "# Feedback\nPrefers short answers."
        assert kwargs["memory_scope"] == "agent"
        assert kwargs["memory_scope_subject"] == "eng-raccoon"

    def test_create_cross_agent_rejected_403(self, client: TestClient) -> None:
        """Caller is agent 'alice' trying to write to agent 'bob' — 403."""
        resp = client.post(
            "/ahs/artifacts",
            headers={"X-AHS-Agent-Name": "alice", "X-User-ID": "USR_X"},
            json={
                "type": "MEMORY",
                "title": "notes",
                "inline_content": "plotting",
                "memory_scope": "agent",
                "memory_scope_subject": "bob",
                "named_slug": "notes",
                "create_new_slug": True,
            },
        )
        assert resp.status_code == 403
        assert "memory_scope=" in resp.json()["detail"]

    def test_create_cross_user_rejected_403(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            headers={"X-AHS-Agent-Name": "alice", "X-User-ID": "USR_X"},
            json={
                "type": "MEMORY",
                "title": "prefs",
                "inline_content": "...",
                "memory_scope": "user",
                "memory_scope_subject": "USR_OTHER",
                "named_slug": "prefs",
                "create_new_slug": True,
            },
        )
        assert resp.status_code == 403

    def test_create_topic_scope_anyone(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="topic", subject=None, slug="routing_tips")
        with patch(
            "ypl.agent_harness_service.artifact_routes.create_artifact",
            new=AsyncMock(return_value=fake),
        ):
            resp = client.post(
                "/ahs/artifacts",
                headers={"X-AHS-Agent-Name": "alice", "X-User-ID": "USR_X"},
                json={
                    "type": "MEMORY",
                    "title": "routing_tips",
                    "inline_content": "# Routing",
                    "memory_scope": "topic",
                    "named_slug": "routing_tips",
                    "create_new_slug": True,
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["memory_scope"] == "topic"

    def test_create_memory_requires_inline_content(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            headers={"X-AHS-Agent-Name": "alice"},
            json={
                "type": "MEMORY",
                "title": "foo",
                "memory_scope": "agent",
                "memory_scope_subject": "alice",
            },
        )
        assert resp.status_code == 400
        assert "inline_content" in resp.json()["detail"]

    def test_create_memory_requires_scope(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            headers={"X-AHS-Agent-Name": "alice"},
            json={"type": "MEMORY", "title": "foo", "inline_content": "x"},
        )
        assert resp.status_code == 400
        assert "memory_scope" in resp.json()["detail"]

    def test_text_rejects_scope_params(self, client: TestClient) -> None:
        resp = client.post(
            "/ahs/artifacts",
            json={
                "type": "TEXT",
                "title": "foo",
                "content": "hi",
                "memory_scope": "user",
                "memory_scope_subject": "USR_X",
            },
        )
        assert resp.status_code == 400
        assert "only apply to MEMORY" in resp.json()["detail"]


class TestListMemoryRoute:
    def test_list_threads_caller_context(self, client: TestClient) -> None:
        """Caller headers become a MemoryCallerContext passed to the store."""
        mine = _memory_artifact(scope="agent", subject="alice")
        topic = _memory_artifact(scope="topic", subject=None, slug="tips")
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[mine, topic]),
        ) as mock_list:
            resp = client.get(
                "/ahs/artifacts",
                headers={"X-AHS-Agent-Name": "alice", "X-User-ID": "USR_X"},
                params={"type": "MEMORY"},
            )
        assert resp.status_code == 200
        caller = mock_list.call_args.kwargs["memory_caller"]
        assert caller is not None
        assert caller.user_id == "USR_X"
        assert caller.agent_name == "alice"

    def test_list_without_identity_uses_admin_mode(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[]),
        ) as mock_list:
            resp = client.get("/ahs/artifacts", params={"type": "MEMORY"})
        assert resp.status_code == 200
        assert mock_list.call_args.kwargs["memory_caller"] is None

    def test_list_scope_user_defaults_subject_to_caller(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[]),
        ) as mock_list:
            resp = client.get(
                "/ahs/artifacts",
                headers={"X-User-ID": "USR_X"},
                params={"type": "MEMORY", "scope": "user"},
            )
        assert resp.status_code == 200
        kwargs = mock_list.call_args.kwargs
        assert kwargs["memory_scope"] == "user"
        assert kwargs["memory_scope_subject"] == "USR_X"

    def test_list_cross_user_subject_rejected(self, client: TestClient) -> None:
        resp = client.get(
            "/ahs/artifacts",
            headers={"X-User-ID": "USR_X", "X-AHS-Agent-Name": "alice"},
            params={"type": "MEMORY", "scope": "user", "subject": "USR_OTHER"},
        )
        assert resp.status_code == 403

    def test_list_cross_agent_subject_rejected(self, client: TestClient) -> None:
        resp = client.get(
            "/ahs/artifacts",
            headers={"X-User-ID": "USR_X", "X-AHS-Agent-Name": "alice"},
            params={"type": "MEMORY", "scope": "agent", "subject": "bob"},
        )
        assert resp.status_code == 403

    def test_list_scope_topic_allowed(self, client: TestClient) -> None:
        with patch(
            "ypl.agent_harness_service.artifact_routes.list_artifacts",
            new=AsyncMock(return_value=[]),
        ) as mock_list:
            resp = client.get(
                "/ahs/artifacts",
                headers={"X-User-ID": "USR_X", "X-AHS-Agent-Name": "alice"},
                params={"type": "MEMORY", "scope": "topic"},
            )
        assert resp.status_code == 200
        assert mock_list.call_args.kwargs["memory_scope"] == "topic"


class TestReadMemoryBySlugRoute:
    def test_requires_scope_for_memory(self, client: TestClient) -> None:
        resp = client.get(
            "/ahs/artifacts/by-slug/feedback_style",
            params={"type": "MEMORY"},
        )
        assert resp.status_code == 400
        assert "scope" in resp.json()["detail"]

    def test_defaults_user_subject_to_caller(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="user", subject="USR_X", slug="prefs")
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_slug",
            new=AsyncMock(return_value=fake),
        ) as mock_get:
            resp = client.get(
                "/ahs/artifacts/by-slug/prefs",
                headers={"X-User-ID": "USR_X"},
                params={"type": "MEMORY", "scope": "user"},
            )
        assert resp.status_code == 200, resp.text
        kwargs = mock_get.call_args.kwargs
        assert kwargs["memory_scope"] == "user"
        assert kwargs["memory_scope_subject"] == "USR_X"

    def test_cross_user_read_403(self, client: TestClient) -> None:
        resp = client.get(
            "/ahs/artifacts/by-slug/prefs",
            headers={"X-User-ID": "USR_X"},
            params={"type": "MEMORY", "scope": "user", "subject": "USR_OTHER"},
        )
        assert resp.status_code == 403


class TestReadMemoryByIdRoute:
    def test_admin_can_read_any_memory(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="user", subject="USR_X")
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(return_value=(b"hi", "text/markdown")),
            ),
        ):
            resp = client.get(f"/ahs/artifacts/{fake.agent_artifact_id}")
        assert resp.status_code == 200

    def test_cross_user_read_returns_404(self, client: TestClient) -> None:
        """Identity-bearing caller can't see another user's memory — 404 (not 403) to avoid existence leak."""
        fake = _memory_artifact(scope="user", subject="USR_OTHER")
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=fake),
        ):
            resp = client.get(
                f"/ahs/artifacts/{fake.agent_artifact_id}",
                headers={"X-User-ID": "USR_X"},
            )
        assert resp.status_code == 404

    def test_cross_agent_read_returns_404(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="agent", subject="bob")
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=fake),
        ):
            resp = client.get(
                f"/ahs/artifacts/{fake.agent_artifact_id}",
                headers={"X-AHS-Agent-Name": "alice"},
            )
        assert resp.status_code == 404

    def test_topic_memory_visible_to_any_authenticated(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="topic", subject=None, slug="tips")
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
                new=AsyncMock(return_value=fake),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.read_artifact_content",
                new=AsyncMock(return_value=(b"tips", "text/markdown")),
            ),
        ):
            resp = client.get(
                f"/ahs/artifacts/{fake.agent_artifact_id}",
                headers={"X-AHS-Agent-Name": "alice"},
            )
        assert resp.status_code == 200


class TestArchiveMemoryRoute:
    def test_by_id_requires_write_permission(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="agent", subject="bob")
        with patch(
            "ypl.agent_harness_service.artifact_routes.get_artifact_by_id",
            new=AsyncMock(return_value=fake),
        ):
            resp = client.delete(
                f"/ahs/artifacts/{fake.agent_artifact_id}",
                headers={"X-AHS-Agent-Name": "alice"},
            )
        assert resp.status_code == 403

    def test_by_slug_requires_write_authz(self, client: TestClient) -> None:
        resp = client.delete(
            "/ahs/artifacts/by-slug/feedback_style",
            headers={"X-AHS-Agent-Name": "alice"},
            params={"type": "MEMORY", "scope": "agent", "subject": "bob"},
        )
        assert resp.status_code == 403

    def test_by_slug_own_agent_succeeds(self, client: TestClient) -> None:
        fake = _memory_artifact(scope="agent", subject="alice")
        with (
            patch(
                "ypl.agent_harness_service.artifact_routes.list_artifact_versions",
                new=AsyncMock(return_value=[fake]),
            ),
            patch(
                "ypl.agent_harness_service.artifact_routes.archive_artifacts_by_slug",
                new=AsyncMock(return_value=1),
            ),
        ):
            resp = client.delete(
                "/ahs/artifacts/by-slug/feedback_style",
                headers={"X-AHS-Agent-Name": "alice"},
                params={"type": "MEMORY", "scope": "agent"},
            )
        assert resp.status_code == 200
        assert resp.json()["archived_count"] == 1


class TestResolveMemorySlugScopeSkill:
    """SKILL is a scope+subject-addressable inline type, so by-slug lookups
    must resolve scope the same way MEMORY does — regression for the viewer's
    SKILL slug links (previously short-circuited to (None, None) → HTTP 400)."""

    def test_skill_topic_scope_resolves(self) -> None:
        from ypl.agent_harness_service.memory_routes import resolve_memory_slug_scope
        from ypl.agent_harness_service.memory_store import MemoryCallerContext

        result = resolve_memory_slug_scope(
            artifact_type=AgentArtifactType.SKILL,
            scope="topic",
            subject=None,
            caller=MemoryCallerContext(),
        )
        assert result == ("topic", None)

    def test_skill_without_scope_400s(self) -> None:
        from fastapi import HTTPException
        from ypl.agent_harness_service.memory_routes import resolve_memory_slug_scope
        from ypl.agent_harness_service.memory_store import MemoryCallerContext

        with pytest.raises(HTTPException) as exc:
            resolve_memory_slug_scope(
                artifact_type=AgentArtifactType.SKILL,
                scope=None,
                subject=None,
                caller=MemoryCallerContext(),
            )
        assert exc.value.status_code == 400

    def test_unscoped_type_skips_scope_resolution(self) -> None:
        from ypl.agent_harness_service.memory_routes import resolve_memory_slug_scope
        from ypl.agent_harness_service.memory_store import MemoryCallerContext

        # TEXT is not a scoped-inline type → scope params are ignored.
        result = resolve_memory_slug_scope(
            artifact_type=AgentArtifactType.TEXT,
            scope="topic",
            subject=None,
            caller=MemoryCallerContext(),
        )
        assert result == (None, None)
