"""End-to-end CLI tests — argparse wiring + exit codes + output shape."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from ahs_memory.cli import main
from ahs_memory.client import AHSMemoryClient


class TestWalkSubcommand:
    def test_prints_table_and_exits_zero(self, sample_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["walk", str(sample_workspace)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "notes/weekly-plan" in captured.err
        assert "notes/daily" in captured.err
        assert "walked 4 files" in captured.out

    def test_prefix_flag(self, sample_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["walk", str(sample_workspace), "--prefix", "openclaw/"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "openclaw/notes/daily" in captured.err

    def test_include_filter(self, sample_workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["walk", str(sample_workspace), "--include", "notes/*"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "notes/daily" in captured.err
        assert "projects" not in captured.err

    def test_missing_root_exits_non_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["walk", str(tmp_path / "nope")])
        captured = capsys.readouterr()
        assert rc == 2
        assert "workspace root not found" in captured.err

    def test_empty_workspace_warns(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["walk", str(tmp_path)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "no .md files" in captured.err


class TestPushSubcommand:
    def test_push_uses_resolved_config_and_uploads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "a.md").write_text("hi", encoding="utf-8")
        monkeypatch.setenv("AHS_API_KEY", "test-key")
        monkeypatch.setenv("AHS_USER_ID", "user-1")
        monkeypatch.setenv("AHS_API_URL", "http://ahs.test")

        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                201,
                json={
                    "artifact_id": "11111111-2222-3333-4444-555555555555",
                    "type": "MEMORY",
                    "title": "a",
                    "description": None,
                    "url": None,
                    "content_type": "text/markdown",
                    "named_slug": "a",
                    "version": 1,
                    "memory_scope": "user",
                    "memory_scope_subject": "user-1",
                    "creator_user_id": "user-1",
                    "creator_agent_id": None,
                    "agent_session_id": None,
                    "agent_task_id": None,
                    "created_at": "2026-05-25T12:00:00Z",
                    "metadata": {"is_archived": False},
                },
            )

        # Monkey-patch the client constructor so push uses our mock transport.
        orig_init = AHSMemoryClient.__init__

        def new_init(
            self: AHSMemoryClient,
            api_url: str,
            api_key: str,
            user_id: str | None,
            *,
            timeout: float = 30.0,
        ) -> None:
            orig_init(self, api_url, api_key, user_id, timeout=timeout)
            self._http = httpx.Client(
                base_url="http://ahs.test",
                headers={"X-API-Key": "test-key", "X-User-ID": "user-1"},
                timeout=5.0,
                transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr(AHSMemoryClient, "__init__", new_init)

        rc = main(["push", str(tmp_path)])
        out = capsys.readouterr()

        assert rc == 0
        assert "pushed 1 file" in out.out
        assert "1 created" in out.out
        assert any(r.url.path == "/ahs/artifacts" and r.method == "POST" for r in captured_requests)

    def test_push_errors_on_missing_api_key(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "a.md").write_text("hi", encoding="utf-8")
        rc = main(["push", str(tmp_path), "--user-id", "user-1"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "AHS_API_KEY" in captured.err

    def test_push_errors_on_missing_user_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "a.md").write_text("hi", encoding="utf-8")
        monkeypatch.setenv("AHS_API_KEY", "test-key")
        rc = main(["push", str(tmp_path)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "user_id" in captured.err
