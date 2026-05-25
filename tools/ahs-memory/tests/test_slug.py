"""Spot-check that the vendored slug rules match T1's behaviour.

T1 has 60 dedicated unit tests in
``tests/agent_harness_service/test_memory_slug.py``. We don't re-run the
full battery here — we cover the cases the CLI actually depends on and
trust the parity assertion at the bottom of the file to flag drift if
someone edits one copy without the other.
"""

from __future__ import annotations

import pytest
from ahs_memory.slug import MAX_SLUG_LEN, is_safe_slug, normalize_path_to_slug


class TestNormalizePathToSlug:
    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            ("notes/daily.md", "notes/daily"),
            ("notes/Weekly Plan.md", "notes/weekly-plan"),
            ("a/b/c.md", "a/b/c"),
            ("Notes.MD", "notes"),
            ("foo  bar.md", "foo-bar"),
            ("foo--bar", "foo-bar"),
            ("foo//bar", "foo/bar"),
            ("./hidden/.foo.md", "hidden/foo"),
            ("backslash\\path.md", "backslash/path"),
            ("", ""),
        ],
    )
    def test_canonical_forms(self, rel_path: str, expected: str) -> None:
        assert normalize_path_to_slug(rel_path) == expected

    def test_unicode_collapses_to_dashes(self) -> None:
        assert normalize_path_to_slug("café/münch.md") == "caf/m-nch"

    def test_prefix_prepended_and_normalized(self) -> None:
        assert normalize_path_to_slug("daily.md", prefix="OpenClaw/") == "openclaw/daily"

    def test_prefix_only_returns_prefix(self) -> None:
        assert normalize_path_to_slug("", prefix="openclaw") == "openclaw"

    def test_path_only_returns_path_when_prefix_normalizes_to_empty(self) -> None:
        assert normalize_path_to_slug("daily.md", prefix="!!!") == "daily"


class TestIsSafeSlug:
    @pytest.mark.parametrize("slug", ["foo", "foo/bar", "a", "a1/b_2/c-3.md"])
    def test_accepts_valid(self, slug: str) -> None:
        assert is_safe_slug(slug) is True

    @pytest.mark.parametrize(
        "slug",
        [
            "",
            "/foo",
            "-foo",
            ".foo",
            "foo/../bar",
            "foo//bar",
            "foo/.",
            "foo\n",
            "foo bar",
        ],
    )
    def test_rejects_invalid(self, slug: str) -> None:
        assert is_safe_slug(slug) is False

    def test_rejects_non_strings(self) -> None:
        assert is_safe_slug(None) is False  # type: ignore[arg-type]
        assert is_safe_slug(42) is False  # type: ignore[arg-type]

    def test_length_cap(self) -> None:
        assert is_safe_slug("a" * MAX_SLUG_LEN) is True
        assert is_safe_slug("a" * (MAX_SLUG_LEN + 1)) is False


def test_normalized_slugs_pass_validator() -> None:
    """Normalizer output should be safe (modulo the empty / overlong cases)."""
    samples = ["notes/daily.md", "Notes/Weekly Plan.md", "openclaw/2026/05/25.md"]
    for rel in samples:
        slug = normalize_path_to_slug(rel)
        assert is_safe_slug(slug), f"{rel!r} → {slug!r} did not pass is_safe_slug"
