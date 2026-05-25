"""Pure helpers for turning a local relative path into an AHS memory slug.

This module is shared by:

- :mod:`ypl.agent_harness_service.memory_materialization` — uses
  :func:`is_safe_slug` (and the underlying ``_SLUG_RE``) to validate the
  ``(scope, slug)`` pair received from the DB before writing a file to the
  sandbox.
- The ``ahs-memory`` CLI (``tools/ahs-memory``) — walks a local workspace
  and calls :func:`normalize_path_to_slug` to turn ``foo/bar baz.md`` into
  the canonical slug ``foo/bar-baz``.
- The optional bulk-import REST endpoint (project task T6) — receives
  slugs from arbitrary callers and re-validates them with
  :func:`is_safe_slug`.

The rules are intentionally narrow:

- Allowed characters: ``A-Z``, ``a-z``, ``0-9``, ``_``, ``.``, ``/``,
  ``-``. Everything else is rejected (in :func:`is_safe_slug`) or
  substituted with ``-`` (in :func:`normalize_path_to_slug`).
- Total length 1..255.
- No ``.``, ``..`` or empty ``/`` segments — these would let a slug
  escape the ``agent_memories/`` root once it's joined with a filesystem
  path.
- Leading character must be alphanumeric, so a hostile or weird slug
  can't pose as a hidden file (``.foo``), an absolute path (``/foo``) or
  start with an option-looking dash.

No I/O happens here. Both functions are pure and safe to call from the
server, the CLI, and tests.
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
# hyphen. The materializer relied on the same regex before this module
# was extracted; the constant is re-exported so anything that needs to
# spell out the rule (docs, validators) has one source of truth.
#
# ``\A`` / ``\Z`` (not ``^`` / ``$``) so a trailing ``\n`` cannot sneak
# past the validator — Python's ``$`` matches before a final newline,
# which would let ``"foo\n"`` produce a filename ``foo\n.md`` once
# joined.
_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_./-]{0,254}\Z")

# Any single character outside the allow-list. Matched non-greedily and
# replaced with ``-`` during normalization so that runs of arbitrary
# noise (spaces, unicode, punctuation) collapse to a predictable dash.
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

    Pipeline:

    1. Normalize OS path separators (``\\`` → ``/``).
    2. Strip a single trailing ``.md`` extension (case-insensitive).
    3. Lower-case the whole string.
    4. Replace every character outside ``[A-Za-z0-9_./-]`` with ``-``.
    5. Collapse runs of dashes and runs of slashes.
    6. Strip leading/trailing dashes and dots from each ``/``-separated
       segment, then drop any segment that ended up empty (so
       ``./hidden`` and ``../escape`` no longer carry a traversal
       segment and ``foo/`` no longer carries a trailing empty
       segment).
    7. If ``prefix`` is truthy, recursively normalize it and prepend
       ``{prefix}/`` to the result.

    The function is *best-effort*: it never raises and it does not
    guarantee that the returned slug passes :func:`is_safe_slug`. For
    example, an entirely-non-ascii input may normalize to ``""`` and an
    overlong input is not truncated. Callers (the CLI, the bulk-import
    endpoint) are expected to validate the result and report a clear
    "skip" reason when validation fails.

    Args:
        rel_path: A relative filesystem path, typically the path of a
            ``.md`` file relative to the workspace root being imported.
        prefix: Optional slug prefix to namespace the result under (e.g.
            ``openclaw/`` so every imported file lands under
            ``openclaw/...``). The prefix is normalized through the same
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
    # If exactly one of head/body is empty the join would create a
    # leading or trailing slash; return whichever side has content.
    return head or body


def _normalize_one(raw: str) -> str:
    """Apply steps 1-6 of the normalization pipeline to ``raw``.

    Shared by the path and prefix branches of
    :func:`normalize_path_to_slug` so both go through the exact same
    transformations.
    """
    # Step 1: OS separator normalization. ``str.replace`` is a no-op
    # when no backslashes are present.
    s = raw.replace("\\", "/")
    # Step 2: strip a single trailing ``.md``. We only consume one
    # extension so multi-dot stems (``notes.v2.md``) keep ``notes.v2``.
    if s.lower().endswith(".md"):
        s = s[:-3]
    # Step 3: case-fold.
    s = s.lower()
    # Step 4: substitute every disallowed character with ``-``.
    s = _DISALLOWED_RE.sub("-", s)
    # Step 5: collapse runs.
    s = _DASH_RUN_RE.sub("-", s)
    s = _SLASH_RUN_RE.sub("/", s)
    # Step 6: clean each segment and drop empties.
    cleaned: list[str] = []
    for part in s.split("/"):
        stripped = part.strip(_SEGMENT_STRIP_CHARS)
        if stripped:
            cleaned.append(stripped)
    return "/".join(cleaned)


def is_safe_slug(slug: str) -> bool:
    """Return ``True`` iff ``slug`` is safe to use as a memory artifact slug.

    Applies the same three rules the materializer enforces before
    writing a file into ``agent_memories/{scope}/``:

    1. Input is a non-empty ``str``. Non-string inputs (``None``, ``int``,
       arbitrary JSON values forwarded by the bulk-import endpoint T6)
       are rejected without raising — the materializer is defensive on
       its own path, but external callers may forward unvalidated values
       from request bodies.
    2. Matches :data:`_SLUG_RE` — leading alphanumeric, allowed chars
       throughout, total length 1..255.
    3. No segment equal to ``""``, ``"."`` or ``".."`` when split on
       ``/`` — these would let the slug escape the memory root once it
       is joined with a filesystem path.

    The regex already rejects empty input, leading ``/``/``.``/``-`` and
    trailing ``/`` (which produces an empty trailing segment after the
    next-to-last ``/``), so the segment check is a belt-and-braces
    defense aimed specifically at embedded traversal like
    ``ok/../escape``.

    Note: the validator accepts **mixed-case** slugs even though
    :func:`normalize_path_to_slug` always lower-cases its output. The
    asymmetry is deliberate — historical ``save_memory`` writes (made
    before this module existed) may have stored mixed-case slugs in the
    DB, and the materializer must still be able to validate and
    rematerialize those rows. Producers that need a canonical form
    (notably the ``ahs-memory`` CLI, T2) should call
    :func:`normalize_path_to_slug` first and treat slug comparisons as
    case-insensitive when deciding create-vs-update.
    """
    if not isinstance(slug, str) or not slug:
        return False
    if not _SLUG_RE.match(slug):
        return False
    return all(part not in ("", ".", "..") for part in slug.split("/"))
