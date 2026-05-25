"""Client tests against an in-memory ``httpx.MockTransport``.

We deliberately use httpx's built-in mock transport instead of pulling
in ``respx``: the client is small and a hand-rolled router is easier to
audit than recording fixtures.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from ahs_memory.client import AHSAPIError, AHSMemoryClient


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> AHSMemoryClient:
    """Build an ``AHSMemoryClient`` whose underlying httpx client routes through ``handler``."""
    client = AHSMemoryClient("http://ahs.test", "test-key", user_id="user-1")
    client._http = httpx.Client(
        base_url="http://ahs.test",
        headers={"X-API-Key": "test-key", "X-User-ID": "user-1"},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    return client


def _meta(slug: str, version: int = 1, artifact_id: str = "11111111-2222-3333-4444-555555555555") -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "type": "MEMORY",
        "title": slug,
        "description": None,
        "url": None,
        "content_type": "text/markdown",
        "named_slug": slug,
        "version": version,
        "memory_scope": "user",
        "memory_scope_subject": "user-1",
        "creator_user_id": "user-1",
        "creator_agent_id": None,
        "agent_session_id": None,
        "agent_task_id": None,
        "created_at": "2026-05-25T12:00:00Z",
        "metadata": {"is_archived": False},
    }


class TestForwardingHeaders:
    def test_user_id_and_api_key_forwarded(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200, json={"artifacts": []})

        with _client_with(handler) as c:
            c.list_user_memories("user-1")

        assert captured.get("x-api-key") == "test-key"
        assert captured.get("x-user-id") == "user-1"


class TestListUserMemories:
    def test_paginates_until_exhausted(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                # First page: 2 rows + a row with no slug (defensive skip).
                return httpx.Response(
                    200,
                    json={"artifacts": [_meta("a"), _meta("b"), {**_meta("c"), "named_slug": None}] * 1},
                )
            return httpx.Response(200, json={"artifacts": []})

        with _client_with(handler) as c:
            rows = c.list_user_memories("user-1", page_size=200)

        assert [r.slug for r in rows] == ["a", "b"]
        assert len(calls) >= 1

    def test_returns_empty_on_no_results(self) -> None:
        with _client_with(lambda r: httpx.Response(200, json={"artifacts": []})) as c:
            assert c.list_user_memories("user-1") == []


class TestGetMemorySlug:
    def test_returns_none_on_404(self) -> None:
        with _client_with(lambda r: httpx.Response(404, json={"detail": "not found"})) as c:
            assert c.get_memory_slug("missing", user_id="user-1") is None

    def test_parses_meta(self) -> None:
        with _client_with(lambda r: httpx.Response(200, json=_meta("foo", version=3))) as c:
            m = c.get_memory_slug("foo", user_id="user-1")
        assert m is not None
        assert m.slug == "foo"
        assert m.version == 3


class TestFetchInlineContent:
    def test_returns_decoded_body(self) -> None:
        body = "hello\nworld"
        with _client_with(
            lambda r: httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": "text/markdown"})
        ) as c:
            assert c.fetch_inline_content("11111111-2222-3333-4444-555555555555") == body

    def test_raises_on_error(self) -> None:
        with _client_with(lambda r: httpx.Response(500, json={"detail": "boom"})) as c:
            with pytest.raises(AHSAPIError) as exc_info:
                c.fetch_inline_content("11111111-2222-3333-4444-555555555555")
            assert exc_info.value.status_code == 500


class TestCreateMemoryArtifact:
    def test_posts_expected_body(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json=_meta("foo", version=1))

        with _client_with(handler) as c:
            c.create_memory_artifact(
                slug="foo",
                inline_content="hi",
                user_id="user-1",
                title="foo",
                description="d",
                create_new_slug=True,
            )

        body = captured["body"]
        assert isinstance(body, dict)
        assert body["type"] == "MEMORY"
        assert body["memory_scope"] == "user"
        assert body["memory_scope_subject"] == "user-1"
        assert body["named_slug"] == "foo"
        assert body["inline_content"] == "hi"
        assert body["create_new_slug"] is True
        assert body["title"] == "foo"
        assert body["description"] == "d"

    def test_raises_on_error_with_detail(self) -> None:
        with _client_with(lambda r: httpx.Response(403, json={"detail": "no can do"})) as c:
            with pytest.raises(AHSAPIError) as exc_info:
                c.create_memory_artifact(
                    slug="foo",
                    inline_content="x",
                    user_id="user-1",
                    title="t",
                )
            assert exc_info.value.status_code == 403
            assert "no can do" in str(exc_info.value)
