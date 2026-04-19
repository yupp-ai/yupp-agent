"""Architectural lint for Agent Harness Service.

Enforces the layering constraints defined in ARCHITECTURE.md.
These tests run on every CI build to prevent architectural drift.

The AHS layering model:

    Layer 0: common/         -- zero AHS deps, imported by everyone
    Layer 1: core/, gateway/, executors/, tools/, projects/
                             -- each depends ONLY on common/
                             -- NEVER import from each other at module level
                             -- NEVER import from root wiring files at module level
    Wiring:  root .py files  -- may import from any layer

Lazy imports (inside function/method bodies) are allowed for runtime
callbacks where the calling direction is inherently upward — e.g.,
an MCP tool lazily importing service.create_agent. These do not
create import-time cycles. Module-level imports are the hard constraint.

See ypl/agent_harness_service/ARCHITECTURE.md for full details.
"""

from __future__ import annotations
import ast
import os
from pathlib import Path

import pytest

AHS_ROOT = Path(__file__).resolve().parents[2] / "ypl" / "agent_harness_service"
AHS_IMPORT_PREFIX = "ypl.agent_harness_service"

# ---------------------------------------------------------------------------
# Layer definitions
# ---------------------------------------------------------------------------

LAYER_0 = {"common"}

LAYER_1 = {"core", "gateway", "executors", "tools", "projects"}

# Root wiring files (may import from anywhere)
ROOT_WIRING = {"service", "server", "routes", "orchestration", "task_executor", "scheduler", "lifespan", "middleware"}

# Directories excluded from architectural lint (UI, scripts, deploy, etc.)
EXCLUDED_DIRS = {"tui", "scripts", "deploy", "docs", "__pycache__", "service"}


# ---------------------------------------------------------------------------
# Import extraction (AST-based, distinguishes module-level from lazy)
# ---------------------------------------------------------------------------


def _extract_top_level_ahs_imports(filepath: Path) -> list[tuple[int, str]]:
    """Extract module-level AHS imports (not inside functions/methods).

    Only returns imports that are direct children of the Module node,
    or nested inside if/try blocks at the module level. Does NOT return
    imports inside function or method bodies (those are "lazy imports").
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []

    def _visit_top_level(node: ast.AST) -> None:
        """Recursively visit nodes at module level (not inside functions)."""
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(AHS_IMPORT_PREFIX + "."):
            results.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            results.extend(
                (node.lineno, alias.name) for alias in node.names if alias.name.startswith(AHS_IMPORT_PREFIX + ".")
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # Stop recursion — anything inside a function is a lazy import
            return
        elif isinstance(node, ast.ClassDef):
            # Class-level imports are still module-level (executed at import time)
            for child in ast.iter_child_nodes(node):
                _visit_top_level(child)
            return

        for child in ast.iter_child_nodes(node):
            _visit_top_level(child)

    _visit_top_level(tree)
    return results


def _extract_all_ahs_imports(filepath: Path) -> list[tuple[int, str]]:
    """Extract ALL AHS imports (both module-level and lazy)."""
    try:
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(AHS_IMPORT_PREFIX + "."):
            results.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            results.extend(
                (node.lineno, alias.name) for alias in node.names if alias.name.startswith(AHS_IMPORT_PREFIX + ".")
            )
    return results


def _module_to_package(module_path: str) -> str | None:
    """Extract the first-level package from a full module path.

    'ypl.agent_harness_service.common.constants' -> 'common'
    'ypl.agent_harness_service.service' -> 'service'  (root file)
    'ypl.agent_harness_service.tools.linear_sync.mapping' -> 'tools'
    """
    suffix = module_path.removeprefix(AHS_IMPORT_PREFIX + ".")
    parts = suffix.split(".")
    return parts[0] if parts else None


def _collect_python_files(directory: Path) -> list[Path]:
    """Collect all .py files under a directory, excluding EXCLUDED_DIRS."""
    files: list[Path] = []
    for root, dirs, filenames in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        files.extend(Path(root) / name for name in filenames if name.endswith(".py") and name != "__init__.py")
    return sorted(files)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLayer0NoDeps:
    """common/ (Layer 0) must not import from any other AHS package."""

    def test_common_has_no_ahs_deps_outside_common(self) -> None:
        violations: list[str] = []
        for f in _collect_python_files(AHS_ROOT / "common"):
            for lineno, module in _extract_all_ahs_imports(f):
                pkg = _module_to_package(module)
                if pkg != "common":
                    violations.append(f"  {f.relative_to(AHS_ROOT)}:{lineno} imports {module}")

        assert not violations, (
            "ARCHITECTURE VIOLATION: common/ is Layer 0 and must have zero AHS "
            "dependencies outside of common/ itself.\n"
            "common/ is the foundation layer -- every other package imports from it. "
            "If common/ imports from another package, it creates a circular dependency "
            "that breaks the entire layering model.\n"
            "Violations:\n" + "\n".join(violations)
        )


class TestLayer1ModuleLevelIndependence:
    """Layer 1 packages must not have module-level imports from each other or root.

    Lazy imports inside function bodies are allowed — they are runtime callbacks
    that don't create import-time cycles (e.g., an MCP tool lazily importing
    service.create_agent when the tool is invoked).
    """

    FORBIDDEN_SOURCES = LAYER_1 | ROOT_WIRING

    @pytest.fixture(autouse=True)
    def _files(self) -> None:
        self.package_files: dict[str, list[Path]] = {}
        for pkg in LAYER_1:
            pkg_dir = AHS_ROOT / pkg
            if pkg_dir.is_dir():
                self.package_files[pkg] = _collect_python_files(pkg_dir)

    @pytest.mark.parametrize("package", sorted(LAYER_1))
    def test_layer1_no_module_level_cross_imports(self, package: str) -> None:
        violations: list[str] = []
        files = self.package_files.get(package, [])
        for f in files:
            for lineno, module in _extract_top_level_ahs_imports(f):
                target_pkg = _module_to_package(module)
                if target_pkg is None:
                    continue
                if target_pkg == "common" or target_pkg == package:
                    continue
                violations.append(f"  {f.relative_to(AHS_ROOT)}:{lineno} imports {module} (from '{target_pkg}/')")

        assert not violations, (
            f"ARCHITECTURE VIOLATION: {package}/ has module-level imports from "
            f"outside common/ and {package}/.\n"
            f"Layer 1 packages are independent at import time -- they must only "
            f"have module-level imports from common/ (Layer 0) or from within "
            f"{package}/ itself. This ensures any Layer 1 package can be "
            f"understood, tested, and modified without knowledge of others.\n"
            f"If {package}/ needs functionality from another package at runtime, "
            f"use a lazy import inside the function body, or use dependency "
            f"injection (see register_orchestration_callbacks in ARCHITECTURE.md).\n"
            f"Violations:\n" + "\n".join(violations)
        )


class TestNoModuleLevelUpwardImports:
    """Layer 0/1 must not have module-level imports of root wiring files."""

    def test_no_module_level_root_wiring_imports(self) -> None:
        all_layer_files: list[tuple[str, Path]] = [("common", f) for f in _collect_python_files(AHS_ROOT / "common")]
        for pkg in LAYER_1:
            pkg_dir = AHS_ROOT / pkg
            if pkg_dir.is_dir():
                all_layer_files.extend((pkg, f) for f in _collect_python_files(pkg_dir))

        violations: list[str] = []
        for _pkg, f in all_layer_files:
            for lineno, module in _extract_top_level_ahs_imports(f):
                target_pkg = _module_to_package(module)
                if target_pkg in ROOT_WIRING:
                    violations.append(
                        f"  {f.relative_to(AHS_ROOT)}:{lineno} imports {module} (root wiring file '{target_pkg}.py')"
                    )

        assert not violations, (
            "ARCHITECTURE VIOLATION: Layer 0/1 packages must never have module-level "
            "imports of root wiring files (service.py, server.py, orchestration.py, etc.).\n"
            "Root wiring files sit at the top of the dependency graph -- they import "
            "from layers below, never the reverse at module level. Lazy imports inside "
            "function bodies are acceptable for runtime callbacks, but module-level "
            "imports would create circular dependencies.\n"
            "If a Layer 1 package needs wiring-layer functionality, use dependency "
            "injection (see register_orchestration_callbacks pattern in ARCHITECTURE.md).\n"
            "Violations:\n" + "\n".join(violations)
        )


class TestExecutorIsolation:
    """Executors must not import service.py or write to the database."""

    def test_executors_do_not_import_service(self) -> None:
        """No imports of service.py at all (not even lazy) — executors are isolated."""
        violations: list[str] = []
        for f in _collect_python_files(AHS_ROOT / "executors"):
            for lineno, module in _extract_all_ahs_imports(f):
                if "service" in module.split("."):
                    violations.append(f"  {f.relative_to(AHS_ROOT)}:{lineno} imports {module}")

        assert not violations, (
            "ARCHITECTURE VIOLATION: executors/ must never import service.py.\n"
            "Executors are side-effect-limited: they run agent logic and return an "
            "ExecutorResult. All session state management (DB writes, status "
            "transitions) happens in service.py AFTER the executor returns. "
            "If an executor imported service.py, it could bypass this contract "
            "and mutate session state directly, making failures non-recoverable.\n"
            "Violations:\n" + "\n".join(violations)
        )

    def test_executors_do_not_import_db_session(self) -> None:
        """Executors must not use database sessions directly."""
        violations: list[str] = []
        for f in _collect_python_files(AHS_ROOT / "executors"):
            source = f.read_text()
            for i, line in enumerate(source.splitlines(), 1):
                if "get_async_session" in line and not line.lstrip().startswith("#"):
                    violations.append(f"  {f.relative_to(AHS_ROOT)}:{i} uses get_async_session")

        assert not violations, (
            "ARCHITECTURE VIOLATION: executors/ must never use get_async_session.\n"
            "Executors must not write to the database. Token/cost accounting, "
            "session status updates, and message persistence all flow through "
            "the ExecutorResult return value back to service.py, which owns the "
            "DB writes. Direct DB access from executors would bypass the session "
            "state machine and create inconsistencies.\n"
            "Violations:\n" + "\n".join(violations)
        )


class TestMCPServerIsolation:
    """local_mcp_server.py must not import service.py or orchestration.py at module level."""

    def test_no_top_level_service_import(self) -> None:
        mcp_file = AHS_ROOT / "tools" / "local_mcp_server.py"
        violations: list[str] = []
        for lineno, module in _extract_top_level_ahs_imports(mcp_file):
            if _module_to_package(module) == "service":
                violations.append(f"  line {lineno}: {module}")

        assert not violations, (
            "ARCHITECTURE VIOLATION: tools/local_mcp_server.py must not import "
            "service.py at module level.\n"
            "The MCP server runs in-process and is imported by service.py. A "
            "module-level import of service.py would create a circular import. "
            "Lazy imports inside function bodies are acceptable for runtime "
            "callbacks (e.g., create_agent), but top-level imports are forbidden.\n"
            "Violations:\n" + "\n".join(violations)
        )

    def test_no_top_level_orchestration_import(self) -> None:
        mcp_file = AHS_ROOT / "tools" / "local_mcp_server.py"
        violations: list[str] = []
        for lineno, module in _extract_top_level_ahs_imports(mcp_file):
            if "orchestration" in module:
                violations.append(f"  line {lineno}: {module}")

        assert not violations, (
            "ARCHITECTURE VIOLATION: tools/local_mcp_server.py must not import "
            "orchestration.py directly at module level.\n"
            "Orchestration functions (run_subagent, route_model_stub) are injected "
            "via register_orchestration_callbacks() at startup. This DI pattern "
            "keeps tools/ at Layer 1 (no root wiring deps). Adding a direct import "
            "would break the layering and re-introduce the tools->root dependency "
            "cycle that was explicitly eliminated.\n"
            "Violations:\n" + "\n".join(violations)
        )


class TestNoModuleLevelCircularDeps:
    """No module-level circular dependencies between Layer 1 packages."""

    def test_no_module_level_cross_deps(self) -> None:
        import_graph: dict[str, set[str]] = {pkg: set() for pkg in LAYER_1}

        for pkg in LAYER_1:
            pkg_dir = AHS_ROOT / pkg
            if not pkg_dir.is_dir():
                continue
            for f in _collect_python_files(pkg_dir):
                for _, module in _extract_top_level_ahs_imports(f):
                    target = _module_to_package(module)
                    if target and target in LAYER_1 and target != pkg:
                        import_graph[pkg].add(target)

        edges = [f"  {pkg}/ -> {dep}/" for pkg, deps in sorted(import_graph.items()) for dep in sorted(deps)]

        assert not edges, (
            "ARCHITECTURE VIOLATION: Layer 1 packages have module-level "
            "cross-dependencies.\n"
            "Each Layer 1 package (core/, gateway/, executors/, tools/, projects/) "
            "must be fully independent at import time -- module-level imports only "
            "from common/ (Layer 0). Cross-package dependencies make it impossible "
            "to understand, test, or modify one package without considering all "
            "the others.\n"
            "Move shared code to common/, or use dependency injection for "
            "cross-cutting concerns.\n"
            "Dependency edges found:\n" + "\n".join(edges)
        )
