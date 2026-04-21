"""Unit tests for ypl/tools/ahs_artifact_cli.py.

Uses ``httpx.MockTransport`` to stub the AHS REST API so the tests
are fully hermetic. Each test patches ``ahs_artifact_cli._client`` to
swap in a client pointed at the mock transport.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from ypl.tools import ahs_artifact_cli

FAKE_ID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Mock transport fixture
# ---------------------------------------------------------------------------


class _Recorder:
    """Captures (request, response) pairs for assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def _mock_client(handler: callable) -> callable:  # type: ignore[valid-type]
    def factory() -> httpx.Client:
        transport = httpx.MockTransport(handler)
        return httpx.Client(base_url="https://test.example", transport=transport)

    return factory


def _patch_client(handler: callable):  # type: ignore[no-untyped-def, valid-type]
    return patch.object(ahs_artifact_cli, "_client", _mock_client(handler))


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_HARNESS_SERVICE_API_KEY", "test-key")
    monkeypatch.setenv("AHS_BASE_URL", "https://test.example")


def _artifact_json(**overrides: Any) -> dict[str, Any]:
    body = {
        "artifact_id": FAKE_ID,
        "type": "YUPPASTE",
        "title": "sample",
        "description": None,
        "url": f"/ahs/artifacts/{FAKE_ID}",
        "content_type": "text/markdown",
        "named_slug": "sample-slug",
        "version": 1,
        "creator_user_id": None,
        "creator_agent_id": None,
        "agent_session_id": None,
        "agent_task_id": None,
        "created_at": "2026-04-21T12:00:00Z",
        "metadata": {"attachments": [], "is_archived": False},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Small unit helpers (pure functions)
# ---------------------------------------------------------------------------


class TestResolveContentType:
    def test_markdown_extension(self, tmp_path: Path) -> None:
        p = tmp_path / "note.md"
        assert ahs_artifact_cli._resolve_content_type(p, None) == "text/markdown"

    def test_html_extension(self, tmp_path: Path) -> None:
        p = tmp_path / "page.html"
        assert ahs_artifact_cli._resolve_content_type(p, None) == "text/html"

    def test_unknown_extension_falls_back_to_plain(self, tmp_path: Path) -> None:
        p = tmp_path / "weird.json"
        assert ahs_artifact_cli._resolve_content_type(p, None) == "text/plain"

    def test_stdin_defaults_to_markdown(self) -> None:
        assert ahs_artifact_cli._resolve_content_type(None, None) == "text/markdown"

    def test_type_override_alias(self, tmp_path: Path) -> None:
        p = tmp_path / "x.md"
        assert ahs_artifact_cli._resolve_content_type(p, "plain") == "text/plain"
        assert ahs_artifact_cli._resolve_content_type(p, "html") == "text/html"

    def test_type_override_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "x.md"
        with pytest.raises(ahs_artifact_cli.CLIError):
            ahs_artifact_cli._resolve_content_type(p, "application/pdf")


class TestIsUuid:
    def test_valid(self) -> None:
        assert ahs_artifact_cli._is_uuid(FAKE_ID)

    def test_slug_not_uuid(self) -> None:
        assert not ahs_artifact_cli._is_uuid("my-slug")

    def test_malformed(self) -> None:
        assert not ahs_artifact_cli._is_uuid("not-a-uuid-at-all")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_uploads_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / "hello.md"
        f.write_text("# hello")

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(201, json=_artifact_json(named_slug=None, version=None))

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["add", str(f), "--title", "Hello"])

        assert rc == 0
        assert len(calls) == 1
        req = calls[0]
        assert req.method == "POST"
        assert req.url.path == "/ahs/artifacts"
        body = json.loads(req.content)
        assert body["content"] == "# hello"
        assert body["content_type"] == "text/markdown"
        assert body["title"] == "Hello"
        assert "named_slug" not in body
        # URL printed to stdout.
        out = capsys.readouterr().out.strip()
        assert out.endswith(f"/ahs/artifacts/{FAKE_ID}")

    def test_stdin_default_markdown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"hi from stdin"), encoding="utf-8"))

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["content_type"] == "text/markdown"
            assert body["content"] == "hi from stdin"
            assert body["title"] == "Stdin paste"
            return httpx.Response(201, json=_artifact_json())

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["add", "--title", "Stdin paste"])
        assert rc == 0

    def test_type_override(self, tmp_path: Path) -> None:
        f = tmp_path / "weird.txt"
        f.write_text("<b>html</b>")

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["content_type"] == "text/html"
            return httpx.Response(201, json=_artifact_json())

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["add", str(f), "--title", "X", "--type", "html"])
        assert rc == 0

    def test_slug_append_falls_back_to_create_when_slug_unknown(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text("body")
        bodies: list[dict[str, Any]] = []
        responses = iter(
            [
                httpx.Response(
                    400,
                    json={"detail": "named_slug 'new' does not exist yet; set create_new_slug=True to create it."},
                ),
                httpx.Response(201, json=_artifact_json(named_slug="new", version=1)),
            ]
        )

        def handler(req: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(req.content))
            return next(responses)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["add", str(f), "--slug", "new", "--title", "T"])
        assert rc == 0
        assert bodies[0]["create_new_slug"] is False
        assert bodies[1]["create_new_slug"] is True

    def test_new_slug_flag_does_not_retry(self, tmp_path: Path) -> None:
        f = tmp_path / "x.md"
        f.write_text("body")
        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            return httpx.Response(
                400,
                json={"detail": "named_slug 'existing' already has active versions"},
            )

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["add", str(f), "--slug", "existing", "--new-slug", "--title", "T"])
        # Should not retry — only one POST.
        assert len(calls) == 1
        assert rc == 1

    def test_missing_file(self) -> None:
        with _patch_client(lambda req: httpx.Response(500)):
            rc = ahs_artifact_cli.main(["add", "/no/such/file", "--title", "x"])
        assert rc == 1

    def test_empty_stdin_rejected(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8"))
        with _patch_client(lambda req: httpx.Response(500)):
            rc = ahs_artifact_cli.main(["add", "--title", "x"])
        assert rc == 1


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


class TestGet:
    def test_by_uuid_prints_content_and_meta(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == f"/ahs/artifacts/{FAKE_ID}/meta":
                return httpx.Response(200, json=_artifact_json())
            if req.url.path == f"/ahs/artifacts/{FAKE_ID}":
                return httpx.Response(200, content=b"# hello", headers={"content-type": "text/markdown"})
            return httpx.Response(404)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["get", FAKE_ID])
        assert rc == 0
        captured = capsys.readouterr()
        assert "# hello" in captured.out
        # Metadata on stderr.
        assert "artifact_id:" in captured.err
        assert FAKE_ID in captured.err

    def test_by_slug_fetches_via_slug_route(self) -> None:
        seen_paths: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_paths.append(req.url.path)
            if req.url.path == "/ahs/artifacts/by-slug/my-slug":
                return httpx.Response(200, json=_artifact_json())
            if req.url.path == f"/ahs/artifacts/{FAKE_ID}":
                return httpx.Response(200, content=b"body", headers={"content-type": "text/markdown"})
            return httpx.Response(404)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["get", "my-slug"])
        assert rc == 0
        assert "/ahs/artifacts/by-slug/my-slug" in seen_paths

    def test_meta_only_skips_content_fetch(self) -> None:
        fetched_content = False

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal fetched_content
            if req.url.path.endswith("/meta"):
                return httpx.Response(200, json=_artifact_json())
            fetched_content = True
            return httpx.Response(200, content=b"x")

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["get", FAKE_ID, "-m"])
        assert rc == 0
        assert fetched_content is False

    def test_content_only_suppresses_meta_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/meta"):
                return httpx.Response(200, json=_artifact_json())
            return httpx.Response(200, content=b"# body", headers={"content-type": "text/markdown"})

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["get", FAKE_ID, "-q"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "# body" in captured.out
        assert "artifact_id:" not in captured.err


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


class TestRm:
    def test_rm_uuid_confirms_and_archives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            if req.url.path.endswith("/meta"):
                return httpx.Response(200, json=_artifact_json(title="sample"))
            if req.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(404)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["rm", FAKE_ID])
        assert rc == 0
        assert any(c.method == "DELETE" for c in calls)

    def test_rm_uuid_abort_on_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "n")

        calls: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req)
            if req.url.path.endswith("/meta"):
                return httpx.Response(200, json=_artifact_json())
            return httpx.Response(500)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["rm", FAKE_ID])
        assert rc == 1
        assert not any(c.method == "DELETE" for c in calls)

    def test_rm_slug_requires_typed_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "delete")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/versions"):
                return httpx.Response(
                    200,
                    json={"named_slug": "my-slug", "versions": [_artifact_json()]},
                )
            if req.method == "DELETE":
                return httpx.Response(200, json={"named_slug": "my-slug", "archived_count": 2})
            return httpx.Response(404)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["rm", "my-slug"])
        assert rc == 0

    def test_rm_slug_wrong_confirm_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "nope")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/versions"):
                return httpx.Response(
                    200,
                    json={"named_slug": "my-slug", "versions": [_artifact_json()]},
                )
            return httpx.Response(500)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["rm", "my-slug"])
        assert rc == 1

    def test_rm_no_tty_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/meta"):
                return httpx.Response(200, json=_artifact_json())
            return httpx.Response(500)

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["rm", FAKE_ID])
        assert rc == 1


# ---------------------------------------------------------------------------
# ls / versions / url / search
# ---------------------------------------------------------------------------


class TestLs:
    def test_forwards_limit_and_offset(self) -> None:
        seen: list[httpx.URL] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url)
            return httpx.Response(200, json={"artifacts": [_artifact_json()]})

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["ls", "--limit", "5", "--offset", "10"])
        assert rc == 0
        assert seen[0].params["limit"] == "5"
        assert seen[0].params["offset"] == "10"

    def test_prints_empty_marker_when_no_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"artifacts": []})

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["ls"])
        assert rc == 0
        assert "(no results)" in capsys.readouterr().err


class TestVersions:
    def test_prints_versions(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "named_slug": "s",
                    "versions": [
                        _artifact_json(version=1, title="v1"),
                        _artifact_json(version=2, title="v2"),
                    ],
                },
            )

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["versions", "s"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "v1" in out and "v2" in out


class TestUrl:
    def test_url_for_uuid(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # Should NOT be called for UUID — CLI builds URL locally.
            raise AssertionError("should not hit the server for UUID url")

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["url", FAKE_ID])
        assert rc == 0
        assert capsys.readouterr().out.strip() == f"https://test.example/ahs/artifacts/{FAKE_ID}"

    def test_url_for_slug(self, capsys: pytest.CaptureFixture[str]) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/ahs/artifacts/by-slug/my-slug"
            return httpx.Response(200, json=_artifact_json())

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["url", "my-slug"])
        assert rc == 0
        assert capsys.readouterr().out.strip().endswith(f"/ahs/artifacts/{FAKE_ID}")


class TestSearch:
    def test_forwards_query(self) -> None:
        seen: list[httpx.URL] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url)
            return httpx.Response(200, json={"artifacts": [_artifact_json()]})

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["search", "quarterly"])
        assert rc == 0
        assert seen[0].params["q"] == "quarterly"


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_missing_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_HARNESS_SERVICE_API_KEY", raising=False)
        rc = ahs_artifact_cli.main(["ls"])
        assert rc == 1

    def test_server_error_surfaced(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        with _patch_client(handler):
            rc = ahs_artifact_cli.main(["ls"])
        assert rc == 1
