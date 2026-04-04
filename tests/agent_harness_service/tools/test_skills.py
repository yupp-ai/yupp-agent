"""Tests for skills.py — load_skill MCP tool."""

from __future__ import annotations
from pathlib import Path
from unittest.mock import patch

from ypl.agent_harness_service.tools.skills import load_skill as _load_skill_tool

load_skill = _load_skill_tool.fn  # unwrap FunctionTool to get the raw callable


class TestLoadSkill:
    """Tests for the load_skill tool."""

    def test_loads_skill_content(self, tmp_path: Path) -> None:
        """Reads SKILL.md content from the skills directory."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# My Skill\n\nDo stuff.\n")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("my-skill")

        assert "My Skill" in result

    def test_strips_yaml_frontmatter(self, tmp_path: Path) -> None:
        """Strips leading --- ... --- YAML frontmatter block."""
        skill_dir = tmp_path / "typed-skill"
        skill_dir.mkdir()
        content = "---\ntitle: Typed\nversion: 1\n---\n# Actual content\n\nBody here.\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("typed-skill")

        assert result.startswith("# Actual content")
        assert "title: Typed" not in result

    def test_no_frontmatter_returned_as_is(self, tmp_path: Path) -> None:
        """Content without frontmatter returned verbatim."""
        skill_dir = tmp_path / "plain-skill"
        skill_dir.mkdir()
        content = "# Plain skill\n\nNo frontmatter here.\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("plain-skill")

        assert result == content

    def test_skill_not_found_lists_available(self, tmp_path: Path) -> None:
        """Returns error with list of available skills when skill not found."""
        # Create one real skill so we can verify it appears in the available list
        real_skill_dir = tmp_path / "real-skill"
        real_skill_dir.mkdir()
        (real_skill_dir / "SKILL.md").write_text("# Real")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("nonexistent-skill")

        assert "[ERROR]" in result
        assert "nonexistent-skill" in result
        assert "real-skill" in result

    def test_empty_skill_name_returns_error(self) -> None:
        result = load_skill("")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    def test_path_traversal_slash_blocked(self) -> None:
        result = load_skill("../../etc/passwd")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    def test_path_traversal_dotdot_blocked(self) -> None:
        result = load_skill("..evil..")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    def test_backslash_blocked(self) -> None:
        result = load_skill("skill\\evil")
        assert "[ERROR]" in result
        assert "Invalid skill name" in result

    def test_empty_skills_dir_returns_empty_available(self, tmp_path: Path) -> None:
        """When skills dir exists but is empty, available list is empty."""
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("anything")

        assert "[ERROR]" in result
        assert "Available skills:" in result

    def test_skills_dir_missing(self, tmp_path: Path) -> None:
        """When skills dir doesn't exist at all, returns error gracefully."""
        nonexistent = str(tmp_path / "does-not-exist")
        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", nonexistent):
            result = load_skill("any-skill")

        assert "[ERROR]" in result

    def test_ioerror_on_read(self, tmp_path: Path) -> None:
        """Returns error when file can't be read (e.g., permissions)."""
        skill_dir = tmp_path / "unreadable-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("content")
        skill_file.chmod(0o000)  # remove read permission

        try:
            with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
                result = load_skill("unreadable-skill")
            # If running as root, the read will succeed; otherwise we expect an error
            assert "[ERROR]" in result or "content" in result
        finally:
            skill_file.chmod(0o644)

    def test_dir_without_skill_md_not_listed(self, tmp_path: Path) -> None:
        """Directories without SKILL.md are not listed as available skills."""
        empty_dir = tmp_path / "empty-dir"
        empty_dir.mkdir()
        real_skill = tmp_path / "real-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text("# Real")

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("missing")

        assert "real-skill" in result
        assert "empty-dir" not in result

    def test_frontmatter_incomplete_not_stripped(self, tmp_path: Path) -> None:
        """Frontmatter without closing --- delimiter is NOT stripped."""
        skill_dir = tmp_path / "partial-skill"
        skill_dir.mkdir()
        # Only opening --- without closing ---
        content = "---\ntitle: Broken frontmatter\n# Content\n"
        (skill_dir / "SKILL.md").write_text(content)

        with patch("ypl.agent_harness_service.common.constants.AHS_SKILLS_DIR", str(tmp_path)):
            result = load_skill("partial-skill")

        assert result == content
