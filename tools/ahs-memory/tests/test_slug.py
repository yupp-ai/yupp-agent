"""Spot-check that the vendored slug rules match T1's behaviour.

T1 has 60 dedicated unit tests in
``tests/agent_harness_service/test_memory_slug.py``. We don't re-run the
full battery here — we cover the cases the CLI actually depends on, and
:func:`test_parity_with_canonical_memory_slug` at the bottom of the file
loads the canonical ``ypl/agent_harness_service/memory_slug.py`` and
asserts both copies agree across a fixture battery, so editing one copy
without the other breaks a test. (That parity check is skipped when the
canonical module isn't on disk — e.g. when this CLI is checked out
standalone, away from the monorepo.)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

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
        # Mixed-case prefix is lower-cased and trailing slash kept consistent.
        assert normalize_path_to_slug("daily.md", prefix="Vault/") == "vault/daily"

    def test_prefix_only_returns_prefix(self) -> None:
        assert normalize_path_to_slug("", prefix="vault") == "vault"

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
    samples = ["notes/daily.md", "Notes/Weekly Plan.md", "vault/2026/05/25.md"]
    for rel in samples:
        slug = normalize_path_to_slug(rel)
        assert is_safe_slug(slug), f"{rel!r} → {slug!r} did not pass is_safe_slug"


# ---------------------------------------------------------------------------
# Drift detection against the canonical AHS implementation.
# ---------------------------------------------------------------------------

# Repo layout: tools/ahs-memory/tests/test_slug.py → parents[3] is the repo
# root, where the canonical module lives. Resolved lazily so a standalone
# checkout (no monorepo) skips rather than errors.
_CANONICAL_PATH = Path(__file__).resolve().parents[3] / "ypl" / "agent_harness_service" / "memory_slug.py"

# Inputs that exercise every branch of the normalization pipeline plus a few
# validator edge cases. Both implementations must agree on all of them.
_SLUG_FIXTURES = [
    "notes/daily.md",
    "notes/Weekly Plan.md",
    "a/b/c.md",
    "Notes.MD",
    "foo  bar.md",
    "foo--bar",
    "foo//bar",
    "./hidden/.foo.md",
    "backslash\\path.md",
    "café/münch.md",
    "openclaw/projects/yupp-agent/api-notes.md",
    "../escape/../ok.md",
    "!!!",
    "",
    "x" * 300,
    "MixedCase/Path",
    "foo/../bar",
    "foo/.",
    "trailing/slash/",
]

_PREFIX_FIXTURES = [None, "openclaw/", "Vault/", "!!!", ""]


def _load_canonical() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_canonical_memory_slug", _CANONICAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not _CANONICAL_PATH.exists(), reason="canonical memory_slug.py not present (standalone checkout)")
def test_parity_with_canonical_memory_slug() -> None:
    """Vendored slug rules must match ``ypl/.../memory_slug.py`` byte-for-byte in behaviour."""
    canonical = _load_canonical()
    for raw in _SLUG_FIXTURES:
        for prefix in _PREFIX_FIXTURES:
            vended = normalize_path_to_slug(raw, prefix=prefix)
            canon = canonical.normalize_path_to_slug(raw, prefix=prefix)
            assert vended == canon, f"normalize drift for ({raw!r}, prefix={prefix!r}): {vended!r} != {canon!r}"
        assert is_safe_slug(raw) == canonical.is_safe_slug(raw), f"is_safe_slug drift for {raw!r}"
    # The exported length cap must also stay in lockstep.
    assert MAX_SLUG_LEN == canonical.MAX_SLUG_LEN
