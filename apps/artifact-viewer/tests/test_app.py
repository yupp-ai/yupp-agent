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

    def test_download_unauthenticated_redirects(self, client: TestClient) -> None:
        resp = client.get(f"/artifacts/{ART_ID}/download")
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
        with (
            patch(
                "artifact_viewer.ahs_client.list_recent",
                new=AsyncMock(
                    return_value={
                        "artifacts": [_meta(title="row-a"), _meta(title="row-b")],
                        "total": 2,
                    }
                ),
            ),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get("/")
        assert resp.status_code == 200
        assert "row-a" in resp.text
        assert "row-b" in resp.text
        # Filter row + apply button must be on the page.
        assert 'class="filter-bar"' in resp.text
        assert "Apply" in resp.text

    def test_default_page_size_is_20(self, client: TestClient) -> None:
        _sign_in(client)
        list_recent = AsyncMock(return_value={"artifacts": [], "total": 0})
        with (
            patch("artifact_viewer.ahs_client.list_recent", new=list_recent),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get("/")
        assert resp.status_code == 200
        assert list_recent.await_count == 1
        assert list_recent.await_args.kwargs["limit"] == 20
        assert list_recent.await_args.kwargs["offset"] == 0
        assert list_recent.await_args.kwargs["include_total"] is True

    def test_forwards_filters_and_pagination(self, client: TestClient) -> None:
        _sign_in(client)
        list_recent = AsyncMock(return_value={"artifacts": [], "total": 0})
        with (
            patch("artifact_viewer.ahs_client.list_recent", new=list_recent),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get(
                "/",
                params={
                    "type": "CODE_REVIEW",
                    "creator_user_id": "user-1",
                    "creator_agent_id": "11111111-2222-3333-4444-555555555555",
                    "created_after": "2026-04-01",
                    "created_before": "2026-04-15",
                    "offset": 40,
                    "limit": 20,
                },
            )
        assert resp.status_code == 200
        kwargs = list_recent.await_args.kwargs
        assert kwargs["artifact_type"] == "CODE_REVIEW"
        assert kwargs["creator_user_id"] == "user-1"
        assert kwargs["creator_agent_id"] == "11111111-2222-3333-4444-555555555555"
        # Date inputs are normalized to ISO timestamps; the upper bound walks
        # to the next midnight so the picker is date-inclusive.
        assert kwargs["created_after"] == "2026-04-01T00:00:00+00:00"
        assert kwargs["created_before"] == "2026-04-16T00:00:00+00:00"
        assert kwargs["offset"] == 40
        assert kwargs["limit"] == 20

    def test_invalid_type_filter_is_dropped(self, client: TestClient) -> None:
        # Defense in depth: a stray query string with a bogus type value
        # falls back to "no filter" rather than 400ing the upstream call.
        _sign_in(client)
        list_recent = AsyncMock(return_value={"artifacts": [], "total": 0})
        with (
            patch("artifact_viewer.ahs_client.list_recent", new=list_recent),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get("/", params={"type": "NOT-A-REAL-TYPE"})
        assert resp.status_code == 200
        assert list_recent.await_args.kwargs["artifact_type"] is None

    def test_renders_creator_dropdown_options(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.list_recent",
                new=AsyncMock(return_value={"artifacts": [], "total": 0}),
            ),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(
                    return_value={
                        "users": [{"id": "user-1", "name": "Alice"}],
                        "agents": [{"id": "agent-uuid", "name": "eng-raccoon"}],
                    }
                ),
            ),
        ):
            resp = client.get("/")
        assert resp.status_code == 200
        assert "Alice" in resp.text
        assert "eng-raccoon" in resp.text

    def test_pagination_links(self, client: TestClient) -> None:
        # Page 2 of 3 (offset=20, total=50) should show both Prev and Next.
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.list_recent",
                new=AsyncMock(
                    return_value={
                        "artifacts": [_meta(title=f"row-{i}") for i in range(20)],
                        "total": 50,
                    }
                ),
            ),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get("/", params={"offset": 20})
        assert resp.status_code == 200
        # Pager links round-trip the offset.
        assert "offset=0" in resp.text  # Prev (max(0, 20-20))
        assert "offset=40" in resp.text  # Next (20+20)
        # Window indicator: "Showing 21-40 of 50" (en dash in the rendered text).
        assert "21" in resp.text
        assert "40" in resp.text
        assert "50" in resp.text

    def test_no_pagination_links_when_only_one_page(self, client: TestClient) -> None:
        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.list_recent",
                new=AsyncMock(
                    return_value={
                        "artifacts": [_meta(title="only-row")],
                        "total": 1,
                    }
                ),
            ),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(return_value={"users": [], "agents": []}),
            ),
        ):
            resp = client.get("/")
        assert resp.status_code == 200
        # Neither prev nor next anchor should appear on a single-page result.
        assert 'rel="prev"' not in resp.text
        assert 'rel="next"' not in resp.text

    def test_creators_endpoint_failure_does_not_break_page(self, client: TestClient) -> None:
        from artifact_viewer.ahs_client import AHSError

        _sign_in(client)
        with (
            patch(
                "artifact_viewer.ahs_client.list_recent",
                new=AsyncMock(return_value={"artifacts": [], "total": 0}),
            ),
            patch(
                "artifact_viewer.ahs_client.list_creators",
                new=AsyncMock(side_effect=AHSError(503, "creators down")),
            ),
        ):
            resp = client.get("/")
        # The page still renders even though the creator dropdown couldn't be
        # populated — we don't want a 502 every time the optional sub-call fails.
        assert resp.status_code == 200
        assert "filter-bar" in resp.text


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


class TestDownload:
    def test_serves_as_attachment_with_extension_from_content_type(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_artifact_content",
            new=AsyncMock(return_value=(b"# Heading\n\nbody", "text/markdown")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}/download")
        assert resp.status_code == 200
        assert resp.content == b"# Heading\n\nbody"
        # Must be served as opaque bytes, never as the upstream content-type —
        # so an HTML artifact can't execute script in the viewer's origin.
        assert resp.headers["content-type"].startswith("application/octet-stream")
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert f'filename="{ART_ID}.md"' in disposition
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_html_artifact_downloads_with_html_extension(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_artifact_content",
            new=AsyncMock(return_value=(b"<b>hi</b>", "text/html; charset=utf-8")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/octet-stream")
        assert f'filename="{ART_ID}.html"' in resp.headers["content-disposition"]

    def test_unknown_content_type_falls_back_to_txt(self, client: TestClient) -> None:
        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_artifact_content",
            new=AsyncMock(return_value=(b"opaque", "application/octet-stream")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}/download")
        assert resp.status_code == 200
        assert f'filename="{ART_ID}.txt"' in resp.headers["content-disposition"]

    def test_upstream_error_surfaces_as_error_page(self, client: TestClient) -> None:
        from artifact_viewer.ahs_client import AHSError

        _sign_in(client)
        with patch(
            "artifact_viewer.ahs_client.get_artifact_content",
            new=AsyncMock(side_effect=AHSError(404, "not found")),
        ):
            resp = client.get(f"/artifacts/{ART_ID}/download")
        assert resp.status_code == 404
        assert "Upstream error" in resp.text


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

    async def test_list_recent_omits_unset_filters(self) -> None:
        """Empty / None filters must not be forwarded as ``key=`` query params."""
        from artifact_viewer import ahs_client

        captured_url: str = ""

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(req.url)
            return httpx.Response(200, json={"artifacts": [], "total": 0})

        def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="http://ahs.test",
                headers={"X-API-Key": "test-key"},
                transport=httpx.MockTransport(handler),
            )

        with patch.object(ahs_client, "_client", fake_client):
            await ahs_client.list_recent(
                limit=20,
                offset=0,
                artifact_type=None,
                creator_user_id=None,
                creator_agent_id=None,
                created_after=None,
                created_before=None,
                include_total=True,
            )

        assert "limit=20" in captured_url
        assert "offset=0" in captured_url
        assert "include_total=true" in captured_url
        for absent in ("type=", "creator_user_id=", "creator_agent_id=", "created_after=", "created_before="):
            assert absent not in captured_url

    async def test_list_recent_forwards_all_filters(self) -> None:
        from artifact_viewer import ahs_client

        captured_url: str = ""

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(req.url)
            return httpx.Response(200, json={"artifacts": [], "total": 0})

        def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="http://ahs.test",
                headers={"X-API-Key": "test-key"},
                transport=httpx.MockTransport(handler),
            )

        with patch.object(ahs_client, "_client", fake_client):
            await ahs_client.list_recent(
                limit=20,
                offset=20,
                artifact_type="TEXT",
                creator_user_id="user-1",
                creator_agent_id="11111111-2222-3333-4444-555555555555",
                created_after="2026-04-01T00:00:00+00:00",
                created_before="2026-05-01T00:00:00+00:00",
            )

        assert "type=TEXT" in captured_url
        assert "creator_user_id=user-1" in captured_url
        assert "creator_agent_id=11111111-2222-3333-4444-555555555555" in captured_url
        # httpx URL-encodes `:` and `+`; just sanity-check that the keys are present.
        assert "created_after=" in captured_url
        assert "created_before=" in captured_url

    async def test_list_creators_hits_creators_path(self) -> None:
        from artifact_viewer import ahs_client

        captured_url: str = ""

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(req.url)
            return httpx.Response(200, json={"users": [], "agents": []})

        def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="http://ahs.test",
                headers={"X-API-Key": "test-key"},
                transport=httpx.MockTransport(handler),
            )

        with patch.object(ahs_client, "_client", fake_client):
            data = await ahs_client.list_creators()

        assert "/ahs/artifacts/creators" in captured_url
        assert data == {"users": [], "agents": []}
