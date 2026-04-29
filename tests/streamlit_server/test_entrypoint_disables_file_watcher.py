"""Regression test: production Streamlit entrypoints must disable the file watcher.

Streamlit's ``LocalSourcesWatcher`` reloads watched modules by deleting them
from ``sys.modules`` whenever a watched ``.py`` file's mtime changes. On a
SQLModel codebase that purge causes the next request to redefine
table-mapped classes (``class Agent(BaseModel, table=True): ...``) against
``SQLModel.metadata`` — which still holds the old ``Table`` objects — and
every page 500s with::

    sqlalchemy.exc.InvalidRequestError:
      Table 'agents' is already defined for this MetaData instance.

The fix is to pass ``--server.fileWatcherType=none`` in production. Local
dev keeps the default. See ``ypl/streamlit_server/AGENTS.md``.

This test guards both production-shaped entrypoints. If you add a new one,
add it here too.
"""

from __future__ import annotations
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCTION_ENTRYPOINTS = [
    REPO_ROOT / "ypl" / "streamlit_server" / "selfhosted_entrypoint.sh",
    REPO_ROOT / "ypl" / "streamlit_server" / "streamlit_server_entrypoint.sh",
]


def _strip_comments(shell_text: str) -> str:
    """Drop ``#`` comments so the assertion looks at executable lines only.

    Naive but sufficient for these scripts: we don't have ``#`` characters
    inside quoted strings on the streamlit invocation line. The first ``#``
    on each line starts a comment unless it's the shebang.
    """
    cleaned: list[str] = []
    for line in shell_text.splitlines():
        if line.startswith("#!"):
            cleaned.append(line)
            continue
        idx = line.find("#")
        if idx >= 0:
            line = line[:idx]
        cleaned.append(line)
    return "\n".join(cleaned)


@pytest.mark.parametrize(
    "entrypoint",
    PRODUCTION_ENTRYPOINTS,
    ids=lambda p: p.name,
)
def test_production_entrypoint_disables_streamlit_file_watcher(entrypoint: Path) -> None:
    assert entrypoint.is_file(), f"Expected entrypoint at {entrypoint}"
    executable_text = _strip_comments(entrypoint.read_text())

    # Tolerate either ``--server.fileWatcherType=none`` (Cloud Run style,
    # equals-form) or ``--server.fileWatcherType none`` (selfhosted style,
    # space-form). Either spelling is fine; the flag must be set to ``none``
    # on a real (non-comment) line so it actually reaches the streamlit CLI.
    accepted = (
        "--server.fileWatcherType=none",
        "--server.fileWatcherType none",
    )
    assert any(form in executable_text for form in accepted), (
        f"{entrypoint.name} must launch streamlit with "
        "--server.fileWatcherType=none. Without it, Streamlit's source "
        "watcher purges sys.modules on file mtime changes (e.g. mid-deploy "
        "git pull) and the next page load redefines SQLModel tables, "
        "raising InvalidRequestError on every page. "
        "See ypl/streamlit_server/AGENTS.md."
    )
