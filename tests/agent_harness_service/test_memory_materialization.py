"""Unit tests for ypl/agent_harness_service/memory_materialization.py.

Covers the pure / disk-only pieces of the materialization module:

  - ``_safe_relpath_for`` rejects path-traversal and unknown scopes
  - ``write_memory_file`` performs an atomic temp+rename
  - ``write_memory_file`` returns ``None`` (no I/O) on invalid input
  - ``materialize_memory_for_session`` writes one .md per artifact and
    creates the three scope subdirs even when the listing is empty

The DB / artifact-store interaction is exercised via patched
``list_artifacts`` / ``read_artifact_content`` so the test stays
in-process and doesn't require Postgres.
"""

from __future__ import annotations
import os
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from ypl.agent_harness_service.memory_materialization import (
    MEMORY_ROOT,
    SCOPE_SUBDIRS,
    _safe_relpath_for,
    materialize_memory_for_session,
    memory_file_path,
    write_memory_file,
)
from ypl.agent_harness_service.memory_store import MemoryCallerContext

# ---------------------------------------------------------------------------
# _safe_relpath_for
# ---------------------------------------------------------------------------


class TestSafeRelpathFor:
    @pytest.mark.parametrize(
        ("scope", "slug", "expected"),
        [
            ("topic", "routing_tips", os.path.join("topic", "routing_tips.md")),
            ("user", "user_preferences", os.path.join("user", "user_preferences.md")),
            ("agent", "feedback-style", os.path.join("agent", "feedback-style.md")),
            ("topic", "nested/folder/slug", os.path.join("topic", "nested/folder/slug.md")),
        ],
    )
    def test_accepts_valid(self, scope: str, slug: str, expected: str) -> None:
        assert _safe_relpath_for(scope, slug) == expected

    @pytest.mark.parametrize(
        ("scope", "slug"),
        [
            ("project", "any"),  # unknown scope
            ("topic", ""),  # empty slug
            ("topic", None),  # None slug
            (None, "x"),  # None scope
            ("topic", "../escape"),  # path traversal
            ("topic", "ok/../escape"),  # nested traversal
            ("topic", "./hidden"),  # leading dot segment
            ("topic", "trailing/"),  # trailing empty segment
            ("topic", "/leading"),  # leading empty segment
            ("topic", "a" * 256),  # too long
            ("topic", "ünïcode"),  # non-ascii
            ("topic", "has space"),  # whitespace
        ],
    )
    def test_rejects_invalid(self, scope: Any, slug: Any) -> None:
        assert _safe_relpath_for(scope, slug) is None


# ---------------------------------------------------------------------------
# memory_file_path / write_memory_file
# ---------------------------------------------------------------------------


class TestMemoryFilePath:
    def test_builds_absolute_path(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        got = memory_file_path(ws, "user", "preferences")
        assert got == os.path.join(ws, MEMORY_ROOT, "user", "preferences.md")

    def test_returns_none_for_invalid(self, tmp_path: Any) -> None:
        assert memory_file_path(str(tmp_path), "elsewhere", "x") is None


class TestWriteMemoryFile:
    def test_atomic_write_creates_file_with_content(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        path = write_memory_file(ws, "agent", "notes", "hello world\n")
        assert path is not None
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            assert f.read() == "hello world\n"

    def test_overwrites_existing(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        write_memory_file(ws, "topic", "routing_tips", "v1\n")
        path = write_memory_file(ws, "topic", "routing_tips", "v2\n")
        assert path is not None
        with open(path, encoding="utf-8") as f:
            assert f.read() == "v2\n"

    def test_invalid_input_returns_none_and_writes_nothing(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        assert write_memory_file(ws, "elsewhere", "slug", "data") is None
        assert write_memory_file(ws, "topic", "../escape", "data") is None
        # Nothing should have been created.
        assert not os.path.exists(os.path.join(ws, MEMORY_ROOT))

    def test_no_lingering_tmp_files_on_success(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        write_memory_file(ws, "topic", "x", "body")
        contents = os.listdir(os.path.join(ws, MEMORY_ROOT, "topic"))
        # Only the final file remains; the .tmp.* file is renamed away.
        assert contents == ["x.md"]


# ---------------------------------------------------------------------------
# materialize_memory_for_session
# ---------------------------------------------------------------------------


def _make_artifact(scope: str, subject: str | None, slug: str, body: str) -> Any:
    """Build a minimal stand-in for AgentArtifact with only the fields
    materialize_memory_for_session reads."""

    class _Stub:
        agent_artifact_id = uuid.uuid4()
        memory_scope = scope
        memory_scope_subject = subject
        named_slug = slug
        inline_content = body
        content_type = "text/markdown"

    return _Stub()


class TestMaterializeMemoryForSession:
    async def test_creates_scope_subdirs_even_when_empty(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        with patch(
            "ypl.agent_harness_service.memory_materialization.list_artifacts",
            return_value=[],
        ):
            written = await materialize_memory_for_session(
                workspace=ws,
                caller=MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon"),
            )
        assert written == 0
        for sub in SCOPE_SUBDIRS:
            assert os.path.isdir(os.path.join(ws, MEMORY_ROOT, sub))

    async def test_writes_one_file_per_artifact(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        artifacts = [
            _make_artifact("topic", None, "routing_tips", "# Routing\nbody A\n"),
            _make_artifact("user", "USR_X", "user_preferences", "# Prefs\nbody B\n"),
            _make_artifact("agent", "eng-raccoon", "feedback_style", "# Style\nbody C\n"),
        ]
        with patch(
            "ypl.agent_harness_service.memory_materialization.list_artifacts",
            return_value=artifacts,
        ):
            written = await materialize_memory_for_session(
                workspace=ws,
                caller=MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon"),
            )

        assert written == 3
        with open(os.path.join(ws, MEMORY_ROOT, "topic", "routing_tips.md")) as f:
            assert f.read() == "# Routing\nbody A\n"
        with open(os.path.join(ws, MEMORY_ROOT, "user", "user_preferences.md")) as f:
            assert f.read() == "# Prefs\nbody B\n"
        with open(os.path.join(ws, MEMORY_ROOT, "agent", "feedback_style.md")) as f:
            assert f.read() == "# Style\nbody C\n"

    async def test_skips_artifacts_with_invalid_scope_or_slug(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        # Mix one good with several intentionally-bad rows. The bad ones
        # are dropped silently; the good one still lands.
        artifacts = [
            _make_artifact("topic", None, "good", "ok\n"),
            _make_artifact("nope", None, "x", "skip me\n"),  # bad scope
            _make_artifact("topic", None, "../escape", "skip me\n"),  # traversal
            _make_artifact("topic", None, "", "skip me\n"),  # empty slug
        ]
        with patch(
            "ypl.agent_harness_service.memory_materialization.list_artifacts",
            return_value=artifacts,
        ):
            written = await materialize_memory_for_session(
                workspace=ws,
                caller=MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon"),
            )
        assert written == 1
        assert os.path.isfile(os.path.join(ws, MEMORY_ROOT, "topic", "good.md"))

    async def test_db_failure_returns_zero(self, tmp_path: Any) -> None:
        ws = str(tmp_path)
        with patch(
            "ypl.agent_harness_service.memory_materialization.list_artifacts",
            side_effect=RuntimeError("DB down"),
        ):
            written = await materialize_memory_for_session(
                workspace=ws,
                caller=MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon"),
            )
        # Even on DB failure the scope subdirs are still created so the
        # agent's prompt rendering doesn't trip on a missing path.
        assert written == 0
        for sub in SCOPE_SUBDIRS:
            assert os.path.isdir(os.path.join(ws, MEMORY_ROOT, sub))
