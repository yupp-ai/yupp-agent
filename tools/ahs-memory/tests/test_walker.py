"""Walker correctness — what gets emitted, in what order, with what status."""

from __future__ import annotations

from pathlib import Path

import pytest
from ahs_memory.walker import DEFAULT_MAX_BYTES, Candidate, walk_workspace


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestWalkFixtureWorkspace:
    def test_picks_up_three_markdown_files(self, sample_workspace: Path) -> None:
        rows = walk_workspace(sample_workspace)
        rel_paths = [r.rel_path for r in rows]
        assert rel_paths == [
            "README.md",
            "notes/Weekly Plan.md",
            "notes/daily.md",
            "projects/yupp-agent/api-notes.md",
        ]

    def test_slug_for_spaced_filename(self, sample_workspace: Path) -> None:
        by_path = {r.rel_path: r for r in walk_workspace(sample_workspace)}
        assert by_path["notes/Weekly Plan.md"].slug == "notes/weekly-plan"

    def test_prefix_is_applied(self, sample_workspace: Path) -> None:
        rows = walk_workspace(sample_workspace, prefix="vault/")
        slugs = sorted(r.slug for r in rows)
        assert slugs == [
            "vault/notes/daily",
            "vault/notes/weekly-plan",
            "vault/projects/yupp-agent/api-notes",
            "vault/readme",
        ]

    def test_skips_dotfiles_and_non_md(self, sample_workspace: Path) -> None:
        rows = walk_workspace(sample_workspace)
        for r in rows:
            assert not any(part.startswith(".") for part in r.rel_path.split("/"))
            assert r.path.suffix.lower() == ".md"


class TestWalkOversizeAndSkips:
    def test_oversize_is_flagged_but_emitted(self, tmp_path: Path) -> None:
        _write(tmp_path / "big.md", "x" * 200)
        rows = walk_workspace(tmp_path, max_bytes=100)
        assert len(rows) == 1
        assert rows[0].skip_reason.startswith("oversize")
        assert "oversize" in rows[0].tags

    def test_safe_slug_no_skip_reason(self, tmp_path: Path) -> None:
        _write(tmp_path / "ok.md", "hi")
        rows = walk_workspace(tmp_path)
        assert rows[0].skip_reason == ""
        assert rows[0].slug == "ok"

    def test_unsafe_slug_is_marked(self, tmp_path: Path) -> None:
        # A path that normalizes to empty (all chars rejected).
        _write(tmp_path / "!!!.md", "hi")
        rows = walk_workspace(tmp_path)
        assert len(rows) == 1
        assert rows[0].slug == ""
        assert rows[0].skip_reason.startswith("slug-empty")


class TestWalkFilters:
    def test_include_glob(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.md", "a")
        _write(tmp_path / "sub/b.md", "b")
        rows = walk_workspace(tmp_path, include=["sub/*"])
        assert [r.rel_path for r in rows] == ["sub/b.md"]

    def test_exclude_glob(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.md", "a")
        _write(tmp_path / "sub/b.md", "b")
        rows = walk_workspace(tmp_path, exclude=["sub/*"])
        assert [r.rel_path for r in rows] == ["a.md"]

    def test_include_then_exclude(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.md", "a")
        _write(tmp_path / "b.md", "b")
        _write(tmp_path / "c.md", "c")
        rows = walk_workspace(tmp_path, include=["*.md"], exclude=["b.md"])
        assert [r.rel_path for r in rows] == ["a.md", "c.md"]


class TestWalkErrorPaths:
    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            walk_workspace(tmp_path / "missing")

    def test_root_is_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "single.md"
        f.write_text("hi", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            walk_workspace(f)


def test_default_max_bytes_matches_server() -> None:
    """Sanity: keep the CLI cap in lockstep with the server's MAX_CONTENT_SIZE_BYTES."""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024


def test_candidate_is_hashable() -> None:
    """Frozen dataclass — needed for sets/dicts in the test suite itself."""
    c = Candidate(path=Path("/x"), rel_path="x.md", slug="x", size_bytes=0)
    assert hash(c) == hash(c)
