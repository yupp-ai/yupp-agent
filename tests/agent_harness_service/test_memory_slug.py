"""Unit tests for ypl/agent_harness_service/memory_slug.py.

The module is two pure functions plus a regex, so we exhaustively cover:

  - every branch of ``normalize_path_to_slug`` (with and without prefix,
    with each side falsy, with case-folding, ``.md`` stripping,
    whitespace / unicode / traversal substitution, segment cleanup, and
    OS-separator normalization)
  - every branch of ``is_safe_slug`` (regex hit/miss for each kind of
    invalid input, segment check, length boundary at 255/256)

Goal is 100% branch coverage on ``memory_slug.py`` so the CLI (T2) and
the bulk-import endpoint (T6) can rely on the contract without
re-verifying it themselves.
"""

from __future__ import annotations

import pytest
from ypl.agent_harness_service.memory_slug import (
    MAX_SLUG_LEN,
    is_safe_slug,
    normalize_path_to_slug,
)

# ---------------------------------------------------------------------------
# normalize_path_to_slug
# ---------------------------------------------------------------------------


class TestNormalizePathToSlug:
    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            # Simple, already-canonical input.
            ("notes.md", "notes"),
            ("foo/bar.md", "foo/bar"),
            # ``.md`` stripping is case-insensitive but only consumes one
            # extension — multi-dot stems keep the inner ``.``.
            ("Notes.MD", "notes"),
            ("notes.v2.md", "notes.v2"),
            # A file that doesn't end in ``.md`` (the CLI walker may pass
            # in non-markdown paths if we ever extend it).
            ("readme", "readme"),
            # Lower-casing.
            ("FOO/Bar/BAZ.md", "foo/bar/baz"),
        ],
    )
    def test_simple_paths(self, rel_path: str, expected: str) -> None:
        assert normalize_path_to_slug(rel_path) == expected

    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            # Single spaces become dashes; runs of spaces collapse to a
            # single dash via the ``-{2,}`` collapse step.
            ("api notes.md", "api-notes"),
            ("foo bar baz.md", "foo-bar-baz"),
            ("foo   bar.md", "foo-bar"),
            ("foo/bar baz.md", "foo/bar-baz"),
        ],
    )
    def test_paths_with_spaces(self, rel_path: str, expected: str) -> None:
        assert normalize_path_to_slug(rel_path) == expected

    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            # Non-ascii letters get substituted with ``-`` and then any
            # leading/trailing dash is stripped per segment.
            ("über.md", "ber"),
            ("ünïcode.md", "n-code"),
            ("emoji-😀-name.md", "emoji-name"),
            # Mix of unicode + ascii inside a nested segment.
            ("notes/café/menu.md", "notes/caf/menu"),
        ],
    )
    def test_paths_with_unicode(self, rel_path: str, expected: str) -> None:
        assert normalize_path_to_slug(rel_path) == expected

    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            # ``..`` is allowed as raw characters (so we don't double-
            # encode them) but the trailing/leading-dot strip removes
            # the segment entirely, defanging the traversal attempt.
            ("../escape.md", "escape"),
            ("ok/../escape.md", "ok/escape"),
            ("./hidden.md", "hidden"),
            ("foo/./bar.md", "foo/bar"),
            # An input that is *only* traversal collapses to "".
            ("../../.md", ""),
        ],
    )
    def test_path_traversal_is_defanged(self, rel_path: str, expected: str) -> None:
        normalized = normalize_path_to_slug(rel_path)
        assert normalized == expected
        # And anything that came out non-empty must pass the safety check —
        # the whole point is that the normalizer never produces an unsafe
        # slug for a traversal input.
        if normalized:
            assert is_safe_slug(normalized)

    def test_oversize_input_normalizes_without_truncating(self) -> None:
        # The normalizer's job is not to truncate — it just produces a
        # candidate. Length enforcement is `is_safe_slug`'s job.
        big = "a" * 1000 + ".md"
        out = normalize_path_to_slug(big)
        assert out == "a" * 1000
        assert len(out) > MAX_SLUG_LEN
        assert not is_safe_slug(out)

    @pytest.mark.parametrize(
        ("rel_path", "prefix", "expected"),
        [
            # Plain prefix application.
            ("api notes.md", "openclaw", "openclaw/api-notes"),
            ("projects/yupp-agent/notes.md", "openclaw", "openclaw/projects/yupp-agent/notes"),
            # Prefix with trailing slash gets normalized too.
            ("notes.md", "openclaw/", "openclaw/notes"),
            # Prefix with weird chars / case / nested.
            ("notes.md", "Open Claw/Notes", "open-claw/notes/notes"),
            # Prefix-only (empty body).
            ("", "openclaw", "openclaw"),
            # Body-only (empty prefix string is falsy → no prefix branch).
            ("notes.md", "", "notes"),
            # Both empty.
            ("", "", ""),
            # ``None`` prefix is the default path.
            ("notes.md", None, "notes"),
            # Body normalizes to empty, prefix has content → return prefix.
            ("../../.md", "openclaw", "openclaw"),
            # Prefix normalizes to empty, body has content → return body.
            ("notes.md", "///", "notes"),
        ],
    )
    def test_prefix_application(self, rel_path: str, prefix: str | None, expected: str) -> None:
        assert normalize_path_to_slug(rel_path, prefix=prefix) == expected

    def test_windows_separators_are_normalized(self) -> None:
        # Backslashes show up if the CLI is ever run on Windows or fed a
        # path that was pasted from one.
        assert normalize_path_to_slug("foo\\bar\\baz.md") == "foo/bar/baz"

    def test_leading_and_trailing_slashes_are_dropped(self) -> None:
        assert normalize_path_to_slug("/foo/bar.md") == "foo/bar"
        assert normalize_path_to_slug("foo/bar/") == "foo/bar"
        assert normalize_path_to_slug("//foo//bar//") == "foo/bar"

    def test_segments_with_only_dots_or_dashes_are_dropped(self) -> None:
        # ``-`` and ``.`` get stripped from each segment; a segment made
        # entirely of those characters disappears, preventing the result
        # from containing ``//`` or an empty leading segment.
        assert normalize_path_to_slug("foo/---/bar.md") == "foo/bar"
        assert normalize_path_to_slug("foo/.../bar.md") == "foo/bar"
        assert normalize_path_to_slug("---.md") == ""

    def test_lone_md_input_collapses_to_empty(self) -> None:
        # ``.md`` alone strips to ``""`` and then there's nothing left.
        assert normalize_path_to_slug(".md") == ""

    def test_result_is_lowercase_even_for_extension(self) -> None:
        # An UPPERCASE extension followed by content should still strip.
        assert normalize_path_to_slug("Notes.MD") == "notes"
        # ...and content alone (no extension) is still lower-cased.
        assert normalize_path_to_slug("MixedCASE") == "mixedcase"


# ---------------------------------------------------------------------------
# is_safe_slug
# ---------------------------------------------------------------------------


class TestIsSafeSlug:
    @pytest.mark.parametrize(
        "slug",
        [
            "a",
            "foo",
            "foo_bar",
            "foo-bar",
            "foo.bar",
            "foo/bar",
            "foo/bar/baz",
            "user_preferences",
            "openclaw/projects/yupp-agent/notes",
            "0",  # leading digit is OK (regex says alphanumeric)
            "z" * MAX_SLUG_LEN,  # exactly the max length
        ],
    )
    def test_accepts_valid(self, slug: str) -> None:
        assert is_safe_slug(slug) is True

    @pytest.mark.parametrize(
        "slug",
        [
            "",  # empty
            "-foo",  # leading dash (not alphanumeric)
            "_foo",  # leading underscore
            ".foo",  # leading dot (hidden file)
            "/foo",  # leading slash (absolute path)
            "foo bar",  # whitespace
            "foo\nbar",  # embedded newline
            # Trailing whitespace / control chars: Python's ``$`` anchor
            # matches before a final ``\n``, so without the ``\A``/``\Z``
            # anchors used by ``_SLUG_RE`` these would slip through and
            # produce filenames with literal control chars.
            "foo\n",
            "\nfoo",
            "foo\r",
            "foo\x00",
            "über",  # non-ascii
            "z" * (MAX_SLUG_LEN + 1),  # one over the limit
        ],
    )
    def test_rejects_regex_violations(self, slug: str) -> None:
        assert is_safe_slug(slug) is False

    @pytest.mark.parametrize(
        "value",
        [
            None,
            0,
            42,
            b"foo",
            ["foo"],
            {"slug": "foo"},
        ],
    )
    def test_rejects_non_string_input(self, value: object) -> None:
        # Bulk-import (T6) may forward arbitrary JSON; the validator
        # must say "no" rather than raise ``TypeError``.
        assert is_safe_slug(value) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "slug",
        [
            "foo/../bar",  # embedded traversal
            "foo/./bar",  # embedded current-dir
            "foo//bar",  # empty middle segment
            "foo/",  # trailing empty segment
        ],
    )
    def test_rejects_segment_violations(self, slug: str) -> None:
        # All of these characters individually pass the regex, but the
        # segment check catches the traversal / empty-segment patterns.
        assert is_safe_slug(slug) is False

    def test_length_boundary(self) -> None:
        # The regex is ``^[alnum][alnum...]{0,254}$`` → total 1..255 chars.
        assert is_safe_slug("a" * MAX_SLUG_LEN) is True
        assert is_safe_slug("a" * (MAX_SLUG_LEN + 1)) is False
        # Sanity check on the constant itself.
        assert MAX_SLUG_LEN == 255
