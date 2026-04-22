"""Route-level tests with the Starlette TestClient and a mocked AHS."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from artifact_viewer.app import build_app
from starlette.testclient import TestClient

ART_ID = "11111111-2222-3333-4444-555555555555"


def _meta(**overrides: Any) -> dict[str, Any]:
    body = {
        "artifact_id": ART_ID,
        "type": "TEXT",
        "title": "Hello",
        "description": None,
        "url": f"/ahs/artifacts/{ART_ID}",
        "content_type": "text/markdown",
        "named_slug": "sample",
        "version": 1,
        "creator_user_id": "user-1",
        "creator_agent_id": None,
        "agent_session_id": None,
        "agent_task_id": None,
        "created_at": "2026-04-21T12:00:00Z",
        "metadata": {"attachments": [], "is_archived": False},
    }
    body.update(overrides)
    return body


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), follow_redirects=False)


def _sign_in(client: TestClient, email: str = "alice@agcouch.com") -> None:
    """Poke the signed session cookie directly so tests don't need to
    round-trip through real Google OAuth."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    # Mirror SessionMiddleware's cookie format (b64(json) + signature).
    signer = TimestampSigner("test-secret-key")
    data = {"email": email, "name": email.split("@")[0], "picture": ""}
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    signed = signer.sign(encoded).decode()
    # SessionMiddleware default cookie name is ``session``.
    client.cookies.set("session", signed)


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestAuthGating:
    def test_home_unauthenticated_redirects_to_login(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 307  # Starlette RedirectResponse default
        assert resp.headers["location"].startswith("/auth/login")
        # Preserves the ``next`` param.
        assert "next=%2F" in resp.headers["location"]

    def test_healthz_is_public(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200

    def test_static_is_public(self, client: TestClient) -> None:
        # Style sheet exists at /static/style.css.
        resp = client.get("/static/style.css")
        assert resp.status_code == 200

    def test_artifact_unauthenticated_redirects(self, client: TestClient) -> None:
        resp = client.get(f"/artifacts/{ART_ID}")
        assert resp.status_code == 307
        assert resp.headers["location"].startswith("/auth/login")


# ---------------------------------------------------------------------------
# Authenticated flows
# ---------------------------------------------------------------------------


class TestArtifactPage:
    def test_renders_markdown_artifact(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.get_artifact_meta",
                new=AsyncMock(return_value=_meta()),
            ),
            patch(
                "artifact_viewer.ahs_client.get_artifact_content",
                new=AsyncMock(return_value=(b"# Heading\n\nbody", "text/markdown")),
            ),
        ):
            resp = client.get(f"/artifacts/{ART_ID}")
        assert resp.status_code == 200
        body = resp.text
        assert "<h1>Heading</h1>" in body
        assert ART_ID in body
        # HTML iframe should NOT be used for markdown content.
        assert "<iframe" not in body

    def test_renders_html_artifact_in_iframe(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.get_artifact_meta",
                new=AsyncMock(return_value=_meta(content_type="text/html")),
            ),
            patch(
                "artifact_viewer.ahs_client.get_artifact_content",
                new=AsyncMock(return_value=(b"<b>hi</b>", "text/html")),
            ),
        ):
            resp = client.get(f"/artifacts/{ART_ID}")
        assert resp.status_code == 200
        body = resp.text
        assert "<iframe" in body
        assert "sandbox=" in body

    def test_upstream_404_surfaces_as_error_page(self, client: TestClient) -> None:
        from artifact_viewer.ahs_client import AHSError

        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_artifact_meta",
            new=AsyncMock(side_effect=AHSError(404, "not found")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}")
        assert resp.status_code == 404
        assert "Upstream error" in resp.text


class TestHome:
    def test_lists_recent(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.list_recent",
            new=AsyncMock(return_value={"artifacts": [_meta(title="row-a"), _meta(title="row-b")]}),
        ):
            resp = client.get("/")
        assert resp.status_code == 200
        assert "row-a" in resp.text
        assert "row-b" in resp.text


class TestSearch:
    def test_empty_query_shows_prompt(self, client: TestClient) -> None:
        _sign_in(client)
        resp = client.get("/search")
        assert resp.status_code == 200
        assert "Type a query" in resp.text

    def test_with_query_hits_search_endpoint(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.search",
            new=AsyncMock(return_value={"artifacts": [_meta(title="matched")]}),
        ) as mock_search:
            resp = client.get("/search?q=quarterly")
        assert resp.status_code == 200
        assert "matched" in resp.text
        assert mock_search.call_args.args[0] == "quarterly"


class TestBySlug:
    def test_latest_version(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.get_artifact_by_slug",
                new=AsyncMock(return_value=_meta(version=3)),
            ),
            patch(
                "artifact_viewer.ahs_client.get_artifact_content",
                new=AsyncMock(return_value=(b"body", "text/markdown")),
            ),
        ):
            resp = client.get("/artifacts/by-slug/sample")
        assert resp.status_code == 200
        assert "v3" in resp.text

    def test_pinned_version(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.get_artifact_by_slug",
                new=AsyncMock(return_value=_meta(version=2)),
            ) as mock_by_slug,
            patch(
                "artifact_viewer.ahs_client.get_artifact_content",
                new=AsyncMock(return_value=(b"body", "text/markdown")),
            ),
        ):
            resp = client.get("/artifacts/by-slug/sample/v/2")
        assert resp.status_code == 200
        assert mock_by_slug.call_args.kwargs.get("version") == 2

    def test_versions_list(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.list_versions",
            new=AsyncMock(
                return_value={
                    "named_slug": "sample",
                    "versions": [_meta(version=1), _meta(version=2)],
                }
            ),
        ):
            resp = client.get("/artifacts/by-slug/sample/versions")
        assert resp.status_code == 200
        assert "v1" in resp.text and "v2" in resp.text


class TestAttachment:
    def test_proxies_bytes_and_content_type(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_attachment",
            new=AsyncMock(return_value=(b"PNGDATA", "image/png")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}/attachments/pic.png")
        assert resp.status_code == 200
        assert resp.content == b"PNGDATA"
        assert resp.headers["content-type"].startswith("image/png")


# ---------------------------------------------------------------------------
# AHS proxy client
# ---------------------------------------------------------------------------


class TestAHSClient:
    async def test_forwards_api_key_header(self) -> None:
        """AHS requests carry the configured API key in X-API-Key."""
        from artifact_viewer import ahs_client

        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(req.headers)
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"artifacts": []})

        def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="http://ahs.test",
                headers={"X-API-Key": "test-key"},
                transport=httpx.MockTransport(handler),
            )

        with patch.object(ahs_client, "_client", fake_client):
            await ahs_client.list_recent(limit=5)

        assert captured["headers"].get("x-api-key") == "test-key"
        assert "/ahs/artifacts?limit=5" in captured["url"]
