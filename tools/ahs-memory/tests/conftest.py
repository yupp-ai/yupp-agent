"""Shared fixtures.

The CLI reads its config from env + ``~/.ahs/config.toml``. To keep tests
isolated we strip the relevant env vars and point ``$HOME`` at a temp
directory before every test, so a stray developer config can't bleed
into the runs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_RESERVED_ENV = ("AHS_API_URL", "AHS_API_KEY", "AHS_USER_ID", "HOME")


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test against a fresh, isolated environment."""
    for key in _RESERVED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    yield


@pytest.fixture
def sample_workspace() -> Path:
    """Path to the committed fixture workspace under ``fixtures/``."""
    return Path(__file__).parent.parent / "fixtures" / "sample-workspace"
