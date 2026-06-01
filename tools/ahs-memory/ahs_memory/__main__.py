"""Entry point so ``python -m ahs_memory`` works the same as the console script."""

from __future__ import annotations

from ahs_memory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
