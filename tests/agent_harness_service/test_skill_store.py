"""Unit tests for ypl.agent_harness_service.skill_store.

Pure helper module — no DB / blob store involvement, so these are fast
parser tests. Covers:

- ``parse_skill_frontmatter`` recognises the YAML block and ignores
  bodies without one.
- ``strip_frontmatter`` removes the leading block and preserves
  everything after.
- ``build_skill_metadata`` always uses the caller-supplied ``name``
  rather than the frontmatter ``name`` (the slug is the source of truth).
- ``trigger_keywords`` parsing handles the two shapes operators tend
  to write (comma-separated, JSON-ish array) and rejects shapes we
  don't yet understand.
"""

from __future__ import annotations

from ypl.agent_harness_service.skill_store import (
    build_skill_metadata,
    parse_skill_frontmatter,
    strip_frontmatter,
)


class TestParseSkillFrontmatter:
    def test_no_frontmatter(self) -> None:
        fm = parse_skill_frontmatter("# Just a heading\n\nBody.")
        assert fm.name is None
        assert fm.description is None
        assert fm.trigger_keywords == []
        assert fm.raw == {}

    def test_unterminated_frontmatter_ignored(self) -> None:
        fm = parse_skill_frontmatter("---\nname: foo\n# missing closing block\n")
        assert fm.name is None
        assert fm.raw == {}

    def test_name_and_description_extracted(self) -> None:
        body = '---\nname: my-skill\ndescription: "Does cool stuff"\n---\n# Hello\n'
        fm = parse_skill_frontmatter(body)
        assert fm.name == "my-skill"
        assert fm.description == "Does cool stuff"

    def test_trigger_keywords_comma_separated(self) -> None:
        body = "---\nname: x\ntrigger_keywords: alpha, beta, gamma\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha", "beta", "gamma"]

    def test_trigger_keywords_json_array(self) -> None:
        body = "---\nname: x\ntrigger_keywords: [alpha, beta, gamma]\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha", "beta", "gamma"]

    def test_trigger_keywords_multiline_block_list(self) -> None:
        # The most idiomatic YAML shape — previously silently produced [].
        body = "---\nname: x\ntrigger_keywords:\n  - alpha\n  - beta\n  - gamma\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha", "beta", "gamma"]
        # The raw block list is preserved for round-tripping.
        assert fm.raw["trigger_keywords"] == ["alpha", "beta", "gamma"]

    def test_trigger_keywords_multiline_block_list_quoted(self) -> None:
        body = "---\ntrigger_keywords:\n  - \"alpha\"\n  - 'beta'\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha", "beta"]

    def test_multiline_list_does_not_swallow_next_key(self) -> None:
        body = "---\ntrigger_keywords:\n  - alpha\nname: after-list\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha"]
        assert fm.name == "after-list"

    def test_empty_value_without_list_stays_empty(self) -> None:
        body = "---\nname: x\ndescription:\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.description is None
        assert fm.raw["description"] == ""

    def test_matched_pair_quotes_only(self) -> None:
        # Old .strip("\"'") stripped both quotes → "x"; matched-pair keeps inner.
        body = "---\ndescription: '\"x\"'\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.description == '"x"'

    def test_apostrophe_inside_double_quotes_preserved(self) -> None:
        body = '---\ndescription: "It\'s a thing"\n---\n'
        fm = parse_skill_frontmatter(body)
        assert fm.description == "It's a thing"

    def test_closing_delimiter_must_be_own_line(self) -> None:
        # A "---" embedded in a value line is NOT a closing delimiter.
        body = "---\ndescription: a---b\nname: x\n---\n# Body\n"
        fm = parse_skill_frontmatter(body)
        assert fm.name == "x"
        assert fm.description == "a---b"

    def test_trigger_keywords_dedupes_blanks(self) -> None:
        body = "---\ntrigger_keywords: alpha, , beta\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.trigger_keywords == ["alpha", "beta"]

    def test_comment_lines_ignored(self) -> None:
        body = "---\n# this is a comment\nname: x\n---\n"
        fm = parse_skill_frontmatter(body)
        assert fm.name == "x"


class TestStripFrontmatter:
    def test_strips_block_with_following_body(self) -> None:
        body = "---\nname: x\n---\n\n# Real content\n"
        assert strip_frontmatter(body) == "# Real content\n"

    def test_no_frontmatter_returned_verbatim(self) -> None:
        body = "# No frontmatter\n\nBody.\n"
        assert strip_frontmatter(body) == body

    def test_unterminated_frontmatter_returned_verbatim(self) -> None:
        body = "---\nname: oops\n# Missing closing delim\n"
        assert strip_frontmatter(body) == body

    def test_embedded_dashes_not_treated_as_closing(self) -> None:
        # "---" inside a value line must not end the block prematurely.
        body = "---\ndescription: a---b\n---\n# Real content\n"
        assert strip_frontmatter(body) == "# Real content\n"


class TestBuildSkillMetadata:
    def test_override_name_wins_over_frontmatter_name(self) -> None:
        fm = parse_skill_frontmatter("---\nname: frontmatter-name\ndescription: D\n---\n")
        meta = build_skill_metadata(fm, override_name="canonical-name")
        assert meta["skill"]["name"] == "canonical-name"
        # Frontmatter is still preserved in ``frontmatter`` for round-tripping.
        assert meta["skill"]["frontmatter"]["name"] == "frontmatter-name"

    def test_description_and_keywords_in_payload(self) -> None:
        fm = parse_skill_frontmatter("---\nname: x\ndescription: Hello\ntrigger_keywords: [foo, bar]\n---\n")
        meta = build_skill_metadata(fm, override_name="x")
        assert meta["skill"]["description"] == "Hello"
        assert meta["skill"]["trigger_keywords"] == ["foo", "bar"]
