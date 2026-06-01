"""Walk a local workspace and turn its ``*.md`` files into ``Candidate`` rows.

Pure I/O on top of :mod:`ahs_memory.slug` — no network, no AHS state.
Both the ``walk`` subcommand and the ``push`` subcommand consume the
output of :func:`walk_workspace`.

Filtering model:

- Walks every regular file under ``root``.
- Only ``*.md`` files (case-insensitive extension) are emitted; everything
  else is skipped silently because the spec is markdown-only.
- ``include`` / ``exclude`` glob lists (matched with :mod:`fnmatch` against
  the *relative POSIX path* of each file) further trim the set.
- Files that produce a slug that fails :func:`is_safe_slug` are still
  emitted, but with ``skip_reason`` populated so the operator sees why
  they would be excluded from ``push``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ahs_memory.slug import MAX_SLUG_LEN, is_safe_slug, normalize_path_to_slug


@dataclass(frozen=True)
class Candidate:
    """One markdown file that the CLI would consider importing."""

    # Absolute filesystem path. Used for reading content during ``push``.
    path: Path
    # Path relative to the walk root, using ``/`` separators on every OS.
    # Stored verbatim so the diff output is portable.
    rel_path: str
    # Canonical slug derived from ``rel_path`` + optional ``--prefix``.
    # May be the empty string if normalization stripped everything.
    slug: str
    # Size in bytes (``os.stat`` result), captured at walk time.
    size_bytes: int
    # Human-readable reason this candidate would be skipped at push time;
    # empty string means "would push". Filled in by the walker for
    # static reasons (slug unsafe, oversize); ``push`` may also stamp
    # additional reasons (skipped-unchanged) downstream.
    skip_reason: str = ""
    # Tags propagated through the report. ``ok`` rows have an empty list.
    tags: tuple[str, ...] = field(default_factory=tuple)


# Default cap on per-file upload size. Matches the AHS server's
# ``MAX_CONTENT_SIZE_BYTES`` (10 MiB) so a push request that's accepted
# locally is never rejected with a 413 by the server. The walk and push
# subcommands expose this via ``--max-bytes``.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def walk_workspace(
    root: Path,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    prefix: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Candidate]:
    """Walk ``root`` and return a list of :class:`Candidate` rows.

    Args:
        root: Workspace root. Must be an existing directory.
        include: Glob patterns; if non-empty, only relative paths matching
            any of these are considered.
        exclude: Glob patterns; relative paths matching any of these are
            dropped entirely (no row in the output).
        prefix: Optional slug prefix, passed through to
            :func:`normalize_path_to_slug`.
        max_bytes: Files larger than this get ``skip_reason="oversize"``
            so the operator sees them in the walk table but ``push`` won't
            attempt them.

    The output is sorted by ``rel_path`` so the walk/diff tables are
    deterministic across runs.

    Raises:
        FileNotFoundError: if ``root`` does not exist.
        NotADirectoryError: if ``root`` exists but is not a directory.
    """
    if not root.exists():
        raise FileNotFoundError(f"workspace root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"workspace root is not a directory: {root}")

    out: list[Candidate] = []
    for path in _iter_markdown_files(root):
        rel = path.relative_to(root).as_posix()
        if exclude and _matches_any(rel, exclude):
            continue
        if include and not _matches_any(rel, include):
            continue
        size = path.stat().st_size
        slug = normalize_path_to_slug(rel, prefix=prefix)
        skip_reason, tags = _classify(slug, size, max_bytes)
        out.append(
            Candidate(
                path=path,
                rel_path=rel,
                slug=slug,
                size_bytes=size,
                skip_reason=skip_reason,
                tags=tags,
            )
        )

    # Stable ordering for table output / diffs.
    out.sort(key=lambda c: c.rel_path)
    return out


def _iter_markdown_files(root: Path) -> Iterable[Path]:
    """Yield every ``*.md`` file under ``root`` in deterministic order.

    Case-insensitive extension match so ``.MD`` / ``.Md`` are also picked
    up — common on case-insensitive filesystems. Hidden files (``.foo``)
    and hidden directories are skipped — we never import dotfiles.
    """
    for entry in sorted(root.rglob("*")):
        if entry.is_dir():
            continue
        if not entry.is_file():
            # Skip symlinks-to-nowhere, sockets, etc.
            continue
        # Skip dotfile paths anywhere in the tree.
        rel_parts = entry.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if entry.suffix.lower() != ".md":
            continue
        yield entry


def _matches_any(rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def _classify(slug: str, size_bytes: int, max_bytes: int) -> tuple[str, tuple[str, ...]]:
    """Compute (skip_reason, tags) for a candidate.

    The tags surface in tables to highlight rows; the skip_reason is the
    operator-visible string explaining why the row wouldn't push.
    """
    if not slug:
        return ("slug-empty (path normalizes to nothing)", ("unsafe",))
    if not is_safe_slug(slug):
        if len(slug) > MAX_SLUG_LEN:
            return (f"slug-too-long ({len(slug)} > {MAX_SLUG_LEN})", ("unsafe",))
        return ("slug-unsafe (failed validator)", ("unsafe",))
    if size_bytes > max_bytes:
        return (f"oversize ({size_bytes} > {max_bytes} bytes)", ("oversize",))
    return ("", ())
