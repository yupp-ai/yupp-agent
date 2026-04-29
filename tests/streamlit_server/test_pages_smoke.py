"""Smoke tests for the Streamlit page modules.

These tests guard the canonical "Table 'X' is already defined for this
MetaData instance" failure mode by exercising two complementary paths:

1. ``test_page_loads_cleanly`` — runs every ``ypl/streamlit_server/pages/*.py``
   script through ``streamlit.testing.v1.AppTest`` and asserts the run
   produces no SQLAlchemy ``InvalidRequestError``. This catches the case
   where a page introduces a *second import path* for one of the
   ``ypl.db.*`` modules (e.g. an alias / sys-path quirk that re-executes
   the ``class X(SQLModel, table=True)`` body).

2. ``test_module_reload_repros_duplicate_table`` — proves the *other*
   failure mode (Streamlit's ``LocalSourcesWatcher`` purging
   ``sys.modules`` mid-process) is still relevant. If this assertion
   ever stops firing, SQLModel has either gained idempotency for table
   redefinition or our model files have grown a guard, and the file-watcher
   fix in the production entrypoints is no longer load-bearing.

If both tests are green the running Streamlit server should never see the
bug: pages import without conflict, and the production entrypoints are
launched with ``--server.fileWatcherType=none`` (covered by
``test_entrypoint_disables_file_watcher.py``).

See ``ypl/streamlit_server/AGENTS.md`` for the full investigation playbook.
"""

from __future__ import annotations
import sys
from pathlib import Path

import pytest
import sqlalchemy.exc
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES_DIR = REPO_ROOT / "ypl" / "streamlit_server" / "pages"

PAGE_FILES = sorted(p for p in PAGES_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("page_path", PAGE_FILES, ids=lambda p: p.name)
def test_page_loads_cleanly(page_path: Path) -> None:
    """Every Streamlit page must execute without SQLAlchemy metadata errors.

    ``AppTest.from_file`` runs the page like the real Streamlit runtime
    would, so the page's module-level imports (``from ypl.db.rbac import …``,
    etc.) all execute. If any page has introduced a duplicate import path
    that double-registers a table, we'll surface a
    ``sqlalchemy.exc.InvalidRequestError`` here and fail loudly — the same
    error users would see hitting the page in a browser.
    """
    assert PAGE_FILES, "Expected at least one page module to test."

    at = AppTest.from_file(str(page_path), default_timeout=30)
    at.run()

    invalid_request_errors = [
        exc
        for exc in at.exception
        # AppTest exposes Exception proto messages — match by name to avoid
        # importing the proto class.
        if "InvalidRequestError" in str(exc.value) or "is already defined for this MetaData" in str(exc.value)
    ]
    assert not invalid_request_errors, (
        f"Page {page_path.name} raised a SQLAlchemy duplicate-table error during import. "
        f"This is the 'Table X is already defined for this MetaData instance' bug — "
        f"see ypl/streamlit_server/AGENTS.md. Errors: "
        f"{[e.value for e in invalid_request_errors]}"
    )


def test_module_reload_repros_duplicate_table() -> None:
    """Confirm that purging ``ypl.db.*`` from ``sys.modules`` and re-importing
    really does raise ``InvalidRequestError`` on the current SQLModel version.

    This is a *negative-control* / load-bearing-fix test: it proves that the
    ``--server.fileWatcherType=none`` flag in the production entrypoints
    (covered by ``test_entrypoint_disables_file_watcher.py``) is still
    plugging an active footgun. If this test ever fails — i.e. the
    re-import stops raising — SQLModel has either gained idempotent table
    registration or someone has added a guard in ``ypl/db/*.py`` to skip a
    second class body. Either way, revisit ``AGENTS.md`` and decide whether
    the watcher-disable flag is still required.
    """
    # Make sure ypl.db.* are loaded once (mimics app.py's ``from
    # ypl.db.all_models import *``).
    import ypl.db.all_models

    assert "ypl.db.rbac" in sys.modules, "ypl.db.rbac should be loaded after all_models"

    # Simulate what Streamlit's LocalSourcesWatcher does on a watched-file
    # mtime change: ``del sys.modules[<name>]`` for every watched module.
    # We only need to evict the ypl.db.* modules to provoke re-execution of
    # the ``class X(SQLModel, table=True)`` bodies on the next import.
    purged: list[str] = [m for m in list(sys.modules) if m.startswith("ypl.db.")]
    assert purged, "Expected ypl.db.* modules to evict for the simulation"

    saved = {name: sys.modules[name] for name in purged}
    for name in purged:
        del sys.modules[name]

    try:
        with pytest.raises(sqlalchemy.exc.InvalidRequestError, match="already defined for this MetaData"):
            import ypl.db.rbac  # noqa: F401
    finally:
        # Restore the original module objects so we don't poison other tests
        # in the same pytest session.
        for name, module in saved.items():
            sys.modules[name] = module
