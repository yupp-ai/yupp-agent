"""Path-to-slug helpers — vendored verbatim from T1.

The canonical implementation lives at
``ypl/agent_harness_service/memory_slug.py`` (PR #315). This CLI is a
self-contained sub-package under ``tools/ahs-memory/`` with its own
``pyproject.toml`` — it does not import from the AHS monorepo, so we keep
a byte-for-byte copy of the slug rules here. Both the AHS module and this
file rely on the same regex, the same character class and the same
segment-cleanup pipeline; keep them in sync. ``tests/test_slug.py``
(``test_parity_with_canonical_memory_slug``) loads the canonical module
off disk and asserts both copies agree across a fixture battery, so
editing one without the other trips a test (the check is skipped only in a
standalone checkout where the canonical module isn't present).
"""

from __future__ import annotations

import re

# The maximum allowed length of a slug after normalization. Mirrors the
# upper bound encoded in :data:`_SLUG_RE` (1 leading char + up to 254
# trailing chars). Exposed as a constant so callers can size buffers or
# warn before the regex rejects something.
MAX_SLUG_LEN = 255

# Canonical slug character class. A valid slug starts with an
# alphanumeric and consists of alphanumerics, underscore, dot, slash and
# hyphen. ``\A`` / ``\Z`` (not ``^`` / ``$``) so a trailing ``\n`` cannot
# sneak past the validator.
_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_./-]{0,254}\Z")

# Any single character outside the allow-list. Replaced with ``-`` during
# normalization so that runs of arbitrary noise (spaces, unicode,
# punctuation) collapse to a predictable dash.
_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9_./-]")

# Collapse runs of dashes / slashes that show up after substitution or
# from sloppy input (``foo//bar``, ``foo  bar`` → ``foo--bar``).
_DASH_RUN_RE = re.compile(r"-{2,}")
_SLASH_RUN_RE = re.compile(r"/{2,}")

# Characters that should not appear at the start or end of an individual
# path segment. Leading dots would create hidden filenames; leading or
# trailing dashes are ugly and (for a leading dash) would fail the
# leading-alphanumeric rule of :data:`_SLUG_RE`.
_SEGMENT_STRIP_CHARS = "-."


def normalize_path_to_slug(rel_path: str, prefix: str | None = None) -> str:
    """Convert a local relative path into a candidate memory slug.

    Best-effort: never raises; does not guarantee the returned slug
    passes :func:`is_safe_slug`. Callers (the CLI) are expected to
    validate the result and report a clear "skip" reason when validation
    fails.

    Args:
        rel_path: Relative filesystem path, typically the path of a
            ``.md`` file relative to the workspace root being imported.
        prefix: Optional slug prefix to namespace the result under (e.g.
            ``notes/``). The prefix is normalized through the same
            pipeline so callers don't have to pre-clean it.

    Returns:
        The candidate slug. May be the empty string if the input
        contained no allowed characters.
    """
    body = _normalize_one(rel_path)
    if not prefix:
        return body
    head = _normalize_one(prefix)
    if head and body:
        return f"{head}/{body}"
    return head or body


def _normalize_one(raw: str) -> str:
    """Apply the normalization pipeline to ``raw``."""
    # OS separator normalization.
    s = raw.replace("\\", "/")
    # Strip a single trailing ``.md`` (case-insensitive).
    if s.lower().endswith(".md"):
        s = s[:-3]
    s = s.lower()
    s = _DISALLOWED_RE.sub("-", s)
    s = _DASH_RUN_RE.sub("-", s)
    s = _SLASH_RUN_RE.sub("/", s)
    cleaned: list[str] = []
    for part in s.split("/"):
        stripped = part.strip(_SEGMENT_STRIP_CHARS)
        if stripped:
            cleaned.append(stripped)
    return "/".join(cleaned)


def is_safe_slug(slug: str) -> bool:
    """Return ``True`` iff ``slug`` is safe to use as a memory artifact slug.

    Same three rules the AHS materializer enforces:

    1. Non-empty ``str``.
    2. Matches :data:`_SLUG_RE` — leading alphanumeric, allowed chars
       throughout, total length 1..255.
    3. No segment equal to ``""``, ``"."`` or ``".."`` when split on
       ``/`` — defense-in-depth against embedded path traversal.
    """
    if not isinstance(slug, str) or not slug:
        return False
    if not _SLUG_RE.match(slug):
        return False
    return all(part not in ("", ".", "..") for part in slug.split("/"))
