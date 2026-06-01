"""Tiny table renderer used by every subcommand's output.

No external deps (no ``rich``/``tabulate``) on purpose — this CLI ships in
a self-contained sub-package and we want a single ``httpx`` dependency
in production. The rendering is good enough for a screenshot in a PR
description.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def human_bytes(n: int) -> str:
    """Format a byte count like ``1.2K`` / ``3.4M``. Right-justifies cleanly."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    return f"{n / (1024 * 1024 * 1024):.1f}G"


def render_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Pure ascii box-less table. Returns a single string ready to print.

    Column widths are sized to the widest cell so the output looks
    consistent regardless of input. Empty input still returns the header
    row + the separator so the user sees the shape of the table.
    """
    rows = [tuple(str(c) for c in r) for r in rows]
    headers = tuple(str(h) for h in headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def fmt(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt(headers), sep]
    lines.extend(fmt(r) for r in rows)
    return "\n".join(lines)
