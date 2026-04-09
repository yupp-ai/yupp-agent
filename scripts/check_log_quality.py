"""CI log-quality checker.

Rejects WARNING- and ERROR-level logger calls that have an empty or missing
message argument.  Catches the most common sources of null-message log
entries before they reach production.

Checks performed
----------------
1. ``logger.warning()`` / ``logger.error()`` called with no positional args.
2. ``logger.warning("")`` / ``logger.error("")`` — explicit empty string.
3. ``logger.warning(None)`` / ``logger.error(None)`` — explicit None.

Usage
-----
    python scripts/check_log_quality.py [paths...]

    paths  One or more files or directories to scan.
           Defaults to ``ypl/`` when no paths are given.

Exit codes
----------
0  No issues found.
1  One or more violations found (details printed to stdout).
"""

from __future__ import annotations
import ast
import sys
from pathlib import Path

# Logger method names that require a non-empty message
_CHECKED_METHODS = frozenset({"warning", "error", "critical"})

# Names that are treated as logger objects (common patterns in this repo)
_LOGGER_NAMES = frozenset({"logger", "log"})


def _is_logger_call(node: ast.Call) -> bool:
    """Return True if *node* looks like ``<logger>.warning(...)`` etc."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _CHECKED_METHODS:
        return False
    # Accept any object.warning() — we can't always know the type statically,
    # but filtering to known logger names keeps false-positives low.
    obj = func.value
    if isinstance(obj, ast.Name) and obj.id in _LOGGER_NAMES:
        return True
    # Also catch ``get_logger().warning(...)`` and similar chained calls.
    if isinstance(obj, ast.Call):
        return True
    return False


def _message_is_empty(node: ast.Call) -> bool:
    """Return True when the first positional argument is missing, empty, or None."""
    if not node.args:
        return True  # no positional args at all: logger.warning()
    first = node.args[0]
    if isinstance(first, ast.Constant):
        # Catches "" and None passed as a literal
        return first.value is None or first.value == ""
    return False


def check_file(path: Path) -> list[tuple[int, str]]:
    """Parse *path* and return a list of (lineno, description) violations."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_logger_call(node) and _message_is_empty(node):
            method = node.func.attr  # type: ignore[union-attr]
            violations.append(
                (
                    node.lineno,
                    f"logger.{method}() called with empty/missing/None message",
                )
            )
    return violations


def scan(roots: list[Path]) -> dict[Path, list[tuple[int, str]]]:
    """Walk *roots* recursively and collect violations from every .py file."""
    results: dict[Path, list[tuple[int, str]]] = {}
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            v = check_file(root)
            if v:
                results[root] = v
        elif root.is_dir():
            for py_file in sorted(root.rglob("*.py")):
                v = check_file(py_file)
                if v:
                    results[py_file] = v
    return results


def main() -> int:
    args = sys.argv[1:]
    roots = [Path(a) for a in args] if args else [Path("ypl")]

    results = scan(roots)
    if not results:
        print("check_log_quality: OK — no empty-message WARNING/ERROR calls found.")
        return 0

    total = sum(len(v) for v in results.values())
    print(f"check_log_quality: FAIL — {total} violation(s) in {len(results)} file(s):\n")
    for path, violations in sorted(results.items()):
        for lineno, desc in violations:
            print(f"  {path}:{lineno}: {desc}")
    print()
    print("Fix: ensure every logger.warning() / logger.error() call includes a descriptive message string.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
