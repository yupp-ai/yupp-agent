"""Tests for skills.py — load_skill MCP tool.

``load_skill`` is async because it falls back to a DB lookup for SKILL
artifacts when no matching disk file exists. The DB path is mocked
through the ``load_skill_artifact`` import seam so these tests stay
pure unit tests with no DB requirement.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from ypl.agent_harness_service.tools.skills import load_skill as _load_skill_tool

load_skill = _load_skill_tool.fn  # unwrap FunctionTool to get the raw async callable


def _stub_db_not_found(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Force the DB fallback to "not found" so tests assert disk behaviour only."""

    async def _stub(name: str, **_: Any) -> dict[str, Any]:
        return {"success": False, "error": f"Skill {name!r} not found in the caller's visibility."}

    # Build a fake module so the local import inside load_skill resolves to our stub
    # without requiring an artifact_notifier / blob_store setup. Mimics the
    # ``@shared_tool`` wrapper by exposing the stub via ``__wrapped__`` so
    # load_skill uses the underlying coroutine.
    import sys
    import types as _types

    fake_mod = _types.ModuleType("ypl.mcp_server.tools.skill_artifacts")
    wrapped = AsyncMock(side_effect=_stub)
    wrapped.__wrapped__ = _stub  # type: ignore[attr-defined]
    fake_mod.load_skill_artifact = wrapped  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ypl.mcp_server.tools.skill_artifacts", fake_mod)
    return wrapped


class TestLoadSkill:
    """Tests for the load_skill tool."""

    async def test_loads_skill_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reads SKILL.md content from the skills directory."""
        _stub_db_not_found(monkeypatch)
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# My Skill\n\nDo stuff.\n")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("my-skill")

        assert "My Skill" in result

    async def test_strips_yaml_frontmatter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Strips leading --- ... --- YAML frontmatter block."""
        _stub_db_not_found(monkeypatch)
        skill_dir = tmp_path / "typed-skill"
        skill_dir.mkdir()
        content = "---\ntitle: Typed\nversion: 1\n---\n# Actual content\n\nBody here.\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("typed-skill")

        assert result.startswith("# Actual content")
        assert "title: Typed" not in result

    async def test_no_frontmatter_returned_as_is(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Content without frontmatter returned verbatim."""
        _stub_db_not_found(monkeypatch)
        skill_dir = tmp_path / "plain-skill"
        skill_dir.mkdir()
        content = "# Plain skill\n\nNo frontmatter here.\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("plain-skill")

        assert result == content

    async def test_skill_not_found_lists_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns error with list of available skills when skill not found."""
        _stub_db_not_found(monkeypatch)
        # Create one real skill so we can verify it appears in the available list
        real_skill_dir = tmp_path / "real-skill"
        real_skill_dir.mkdir()
        (real_skill_dir / "SKILL.md").write_text("# Real")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("nonexistent-skill")

        assert "[ERROR]" in result
        assert "nonexistent-skill" in result
        assert "real-skill" in result

    async def test_db_fallback_returns_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When skill is missing on disk but present in DB, returns the DB body."""
        # Stub the DB fallback to return a hit.
        import sys
        import types as _types

        async def _hit(name: str, **_: Any) -> dict[str, Any]:
            return {
                "success": True,
                "content": "---\ndescription: From DB\n---\n# DB Skill\n\nFrom database.\n",
            }

        fake_mod = _types.ModuleType("ypl.mcp_server.tools.skill_artifacts")
        wrapped = AsyncMock(side_effect=_hit)
        wrapped.__wrapped__ = _hit  # type: ignore[attr-defined]
        fake_mod.load_skill_artifact = wrapped  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ypl.mcp_server.tools.skill_artifacts", fake_mod)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("only-in-db")

        assert result.startswith("# DB Skill")
        assert "description: From DB" not in result  # frontmatter stripped

    async def test_disk_wins_on_collision(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a skill exists both on disk and in DB, disk content wins."""
        import sys
        import types as _types

        async def _db_hit(name: str, **_: Any) -> dict[str, Any]:
            return {"success": True, "content": "# From DB (should not be used)"}

        fake_mod = _types.ModuleType("ypl.mcp_server.tools.skill_artifacts")
        wrapped = AsyncMock(side_effect=_db_hit)
        wrapped.__wrapped__ = _db_hit  # type: ignore[attr-defined]
        fake_mod.load_skill_artifact = wrapped  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ypl.mcp_server.tools.skill_artifacts", fake_mod)

        skill_dir = tmp_path / "shadowed"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# From disk\n")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("shadowed")

        assert "From disk" in result
        assert "From DB" not in result
        # DB fallback should not have been invoked at all.
        wrapped.assert_not_awaited()

    async def test_empty_skill_name_returns_error(self) -> None:
        result = await load_skill("")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    async def test_path_traversal_slash_blocked(self) -> None:
        result = await load_skill("../../etc/passwd")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    async def test_path_traversal_dotdot_blocked(self) -> None:
        result = await load_skill("..evil..")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    async def test_backslash_blocked(self) -> None:
        result = await load_skill("skill\\evil")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    async def test_empty_skills_dir_returns_empty_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When skills dir exists but is empty, available list is empty."""
        _stub_db_not_found(monkeypatch)
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("anything")

        assert "[ERROR]" in result
        assert "Available disk skills:" in result

    async def test_skills_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When skills dir doesn't exist at all, returns error gracefully."""
        _stub_db_not_found(monkeypatch)
        nonexistent = str(tmp_path / "does-not-exist")
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", nonexistent):
            result = await load_skill("any-skill")

        assert "[ERROR]" in result

    async def test_ioerror_on_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns error when file can't be read (e.g., permissions)."""
        _stub_db_not_found(monkeypatch)
        skill_dir = tmp_path / "unreadable-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("content")
        skill_file.chmod(0o000)  # remove read permission

        try:
            with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
                result = await load_skill("unreadable-skill")
            # If running as root, the read will succeed; otherwise we expect an error
            assert "[ERROR]" in result or "content" in result
        finally:
            skill_file.chmod(0o644)

    async def test_dir_without_skill_md_not_listed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Directories without SKILL.md are not listed as available skills."""
        _stub_db_not_found(monkeypatch)
        empty_dir = tmp_path / "empty-dir"
        empty_dir.mkdir()
        real_skill = tmp_path / "real-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text("# Real")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("missing")

        assert "real-skill" in result
        assert "empty-dir" not in result

    async def test_frontmatter_incomplete_not_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Frontmatter without closing --- delimiter is NOT stripped."""
        _stub_db_not_found(monkeypatch)
        skill_dir = tmp_path / "partial-skill"
        skill_dir.mkdir()
        # Only opening --- without closing ---
        content = "---\ntitle: Broken frontmatter\n# Content\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = await load_skill("partial-skill")

        assert result == content
