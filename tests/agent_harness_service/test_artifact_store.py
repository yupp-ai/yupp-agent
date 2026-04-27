"""Unit tests for ypl/agent_harness_service/artifact_store.py.

Focuses on the pure / stateless pieces of the artifact store:

  - named-slug validation
  - content-type → extension mapping
  - path derivation for content + attachments
  - attachment filename sanitization
  - ``Attachment`` size validation
  - ``read_artifact_content`` + ``read_artifact_attachment`` against an
    in-memory mock BlobStore

The slug-versioning and DB insertion paths go through SQLModel sessions
and are covered by alembic/integration tests, not this file.
"""

from __future__ import annotations
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from ypl.agent_harness_service.artifact_store import (
    CONTENT_TYPE_EXTENSIONS,
    MAX_ATTACHMENT_SIZE_BYTES,
    VALID_MEMORY_SCOPES,
    ArtifactError,
    Attachment,
    MemoryCallerContext,
    attachment_path_for,
    caller_can_write_memory,
    content_path_for,
    read_artifact_attachment,
    read_artifact_content,
    validate_memory_scope_shape,
    validate_named_slug,
)

FAKE_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# validate_named_slug
# ---------------------------------------------------------------------------


class TestValidateNamedSlug:
    @pytest.mark.parametrize(
        "slug",
        [
            "a",
            "simple",
            "with-dashes",
            "with_underscores",
            "a1",
            "alpha-123_beta",
            "x" * 63,
        ],
    )
    def test_accepts_valid_slug(self, slug: str) -> None:
        validate_named_slug(slug)  # must not raise

    @pytest.mark.parametrize(
        "slug",
        [
            "",
            "-leading-dash",
            "_leading-underscore",
            "has space",
            "has/slash",
            "has.dot",
            "x" * 64,
            "ünïcode",
        ],
    )
    def test_rejects_invalid_slug(self, slug: str) -> None:
        with pytest.raises(ArtifactError):
            validate_named_slug(slug)


# ---------------------------------------------------------------------------
# content_path_for / attachment_path_for
# ---------------------------------------------------------------------------


class TestContentPathFor:
    def test_path_shape_for_uuid(self) -> None:
        path = content_path_for(FAKE_ID, "text/markdown")
        assert path == f"{str(FAKE_ID)[:2]}/{FAKE_ID}/{FAKE_ID}.md"

    def test_path_shape_for_string_id(self) -> None:
        path = content_path_for(str(FAKE_ID), "text/plain")
        assert path == f"{str(FAKE_ID)[:2]}/{FAKE_ID}/{FAKE_ID}.txt"

    @pytest.mark.parametrize(
        "content_type,expected_ext",
        [
            ("text/plain", ".txt"),
            ("text/markdown", ".md"),
            ("text/html", ".html"),
        ],
    )
    def test_supported_extensions(self, content_type: str, expected_ext: str) -> None:
        path = content_path_for(FAKE_ID, content_type)
        assert path.endswith(expected_ext)

    def test_rejects_unsupported_content_type(self) -> None:
        with pytest.raises(ArtifactError, match="Unsupported content_type"):
            content_path_for(FAKE_ID, "application/pdf")

    def test_extension_table_matches_paths(self) -> None:
        """Sanity: each advertised content type actually resolves cleanly."""
        for ct, ext in CONTENT_TYPE_EXTENSIONS.items():
            assert content_path_for(FAKE_ID, ct).endswith(ext)


class TestAttachmentPathFor:
    def test_path_shape(self) -> None:
        path = attachment_path_for(FAKE_ID, "screenshot.png")
        assert path == f"{str(FAKE_ID)[:2]}/{FAKE_ID}/screenshot.png"

    def test_strips_leading_path(self) -> None:
        # Filenames get basename-stripped as a safety measure.
        path = attachment_path_for(FAKE_ID, "/tmp/deep/name.png")
        assert path == f"{str(FAKE_ID)[:2]}/{FAKE_ID}/name.png"

    @pytest.mark.parametrize("bad", ["..", ".", ""])
    def test_rejects_dot_names(self, bad: str) -> None:
        with pytest.raises(ArtifactError, match="Invalid attachment filename"):
            attachment_path_for(FAKE_ID, bad)

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(ArtifactError):
            attachment_path_for(FAKE_ID, "bad\x00name.png")

    def test_rejects_backslash(self) -> None:
        # basename() doesn't split on backslash on POSIX — we reject it explicitly.
        with pytest.raises(ArtifactError):
            attachment_path_for(FAKE_ID, "a\\b.png")


# ---------------------------------------------------------------------------
# Attachment dataclass-ish
# ---------------------------------------------------------------------------


class TestAttachment:
    def test_stores_fields(self) -> None:
        att = Attachment(filename="a.png", data=b"hello", content_type="image/png")
        assert att.filename == "a.png"
        assert att.data == b"hello"
        assert att.content_type == "image/png"

    def test_sanitizes_filename(self) -> None:
        att = Attachment(filename="/tmp/a.png", data=b"h", content_type="image/png")
        assert att.filename == "a.png"

    def test_rejects_oversized(self) -> None:
        with pytest.raises(ArtifactError, match="exceeds"):
            Attachment(
                filename="a.bin",
                data=b"x" * (MAX_ATTACHMENT_SIZE_BYTES + 1),
                content_type="application/octet-stream",
            )


# ---------------------------------------------------------------------------
# read_artifact_content / read_artifact_attachment
# ---------------------------------------------------------------------------


class _FakeBlobStore:
    """Minimal in-memory BlobStore stub for tests.

    Implements the full ``BlobStore`` protocol surface so mypy is happy, but
    only ``download`` is used by the code paths under test here.
    """

    def __init__(self, *, data: dict[str, bytes] | None = None) -> None:
        self._data: dict[str, bytes] = data or {}

    async def upload(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._data[path] = data

    async def download(self, path: str) -> bytes:
        if path not in self._data:
            raise FileNotFoundError(path)
        return self._data[path]

    async def get_size(self, path: str) -> int:
        if path not in self._data:
            raise FileNotFoundError(path)
        return len(self._data[path])

    async def get_access_url(self, path: str, expiry_seconds: int = 3 * 24 * 3600) -> str:
        return f"https://example.test/{path}"

    async def exists(self, path: str) -> bool:
        return path in self._data

    async def delete(self, path: str) -> None:
        if path not in self._data:
            raise FileNotFoundError(path)
        del self._data[path]


def _artifact(
    *,
    artifact_id: uuid.UUID = FAKE_ID,
    content_type: str | None = "text/markdown",
    metadata: dict[str, Any] | None = None,
    inline_content: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.agent_artifact_id = artifact_id
    m.content_type = content_type
    m.artifact_metadata = metadata
    # Explicit so ``read_artifact_content``'s inline-content short-circuit
    # doesn't trip on MagicMock's auto-attributes.
    m.inline_content = inline_content
    return m


class TestReadArtifactContent:
    async def test_returns_data_and_content_type(self) -> None:
        content_path = content_path_for(FAKE_ID, "text/markdown")
        store = _FakeBlobStore(data={content_path: b"# Hello"})
        artifact = _artifact(content_type="text/markdown")

        data, ct = await read_artifact_content(artifact, blob_store=store)

        assert data == b"# Hello"
        assert ct == "text/markdown"

    async def test_raises_when_no_content_type(self) -> None:
        store = _FakeBlobStore()
        artifact = _artifact(content_type=None)
        with pytest.raises(ArtifactError, match="has no content_type"):
            await read_artifact_content(artifact, blob_store=store)

    async def test_propagates_file_not_found(self) -> None:
        store = _FakeBlobStore()
        artifact = _artifact(content_type="text/plain")
        with pytest.raises(FileNotFoundError):
            await read_artifact_content(artifact, blob_store=store)


class TestReadArtifactAttachment:
    async def test_returns_attachment_bytes_and_recorded_content_type(self) -> None:
        att_path = attachment_path_for(FAKE_ID, "photo.png")
        store = _FakeBlobStore(data={att_path: b"PNGDATA"})
        artifact = _artifact(
            metadata={
                "attachments": [
                    {"filename": "photo.png", "content_type": "image/png", "size_bytes": 7},
                ],
            },
        )

        data, ct = await read_artifact_attachment(artifact, "photo.png", blob_store=store)

        assert data == b"PNGDATA"
        assert ct == "image/png"

    async def test_raises_when_attachment_not_recorded(self) -> None:
        store = _FakeBlobStore()
        artifact = _artifact(metadata={"attachments": []})
        with pytest.raises(ArtifactError, match="not found on artifact"):
            await read_artifact_attachment(artifact, "missing.png", blob_store=store)

    async def test_defaults_content_type_when_missing(self) -> None:
        att_path = attachment_path_for(FAKE_ID, "weird.bin")
        store = _FakeBlobStore(data={att_path: b"X"})
        artifact = _artifact(
            metadata={"attachments": [{"filename": "weird.bin"}]},
        )
        data, ct = await read_artifact_attachment(artifact, "weird.bin", blob_store=store)
        assert data == b"X"
        assert ct == "application/octet-stream"

    async def test_none_metadata_raises(self) -> None:
        store = _FakeBlobStore()
        artifact = _artifact(metadata=None)
        with pytest.raises(ArtifactError):
            await read_artifact_attachment(artifact, "any.png", blob_store=store)


# ---------------------------------------------------------------------------
# search_artifacts — shape of query input
# ---------------------------------------------------------------------------


class TestSearchArtifacts:
    async def test_empty_query_returns_empty(self) -> None:
        """A blank query short-circuits without touching the DB."""
        from ypl.agent_harness_service.artifact_store import search_artifacts

        assert await search_artifacts("") == []
        assert await search_artifacts("   ") == []


# ---------------------------------------------------------------------------
# Memory scope / authz helpers (pure, no DB)
# ---------------------------------------------------------------------------


class TestValidateMemoryScopeShape:
    def test_topic_allows_null_subject(self) -> None:
        validate_memory_scope_shape("topic", None)

    def test_topic_rejects_subject(self) -> None:
        with pytest.raises(ArtifactError, match="forbids"):
            validate_memory_scope_shape("topic", "anyone")

    @pytest.mark.parametrize("scope", ["user", "agent"])
    def test_user_agent_requires_subject(self, scope: str) -> None:
        with pytest.raises(ArtifactError, match="requires"):
            validate_memory_scope_shape(scope, None)
        with pytest.raises(ArtifactError, match="requires"):
            validate_memory_scope_shape(scope, "")

    def test_rejects_unknown_scope(self) -> None:
        with pytest.raises(ArtifactError, match="Invalid memory_scope"):
            validate_memory_scope_shape("project", "anything")

    def test_scope_constants_match_valid_set(self) -> None:
        assert VALID_MEMORY_SCOPES == frozenset({"user", "agent", "topic"})


class TestCallerCanWriteMemory:
    def test_topic_allows_anyone(self) -> None:
        assert caller_can_write_memory(MemoryCallerContext(), "topic", None)
        assert caller_can_write_memory(MemoryCallerContext(user_id="u", agent_name="a"), "topic", None)

    def test_user_requires_matching_user(self) -> None:
        caller = MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon")
        assert caller_can_write_memory(caller, "user", "USR_X") is True
        assert caller_can_write_memory(caller, "user", "USR_OTHER") is False
        # Caller with no user_id cannot write to any user scope.
        empty = MemoryCallerContext(agent_name="eng-raccoon")
        assert caller_can_write_memory(empty, "user", "USR_X") is False

    def test_agent_requires_matching_agent(self) -> None:
        caller = MemoryCallerContext(user_id="USR_X", agent_name="eng-raccoon")
        assert caller_can_write_memory(caller, "agent", "eng-raccoon") is True
        assert caller_can_write_memory(caller, "agent", "other-agent") is False
        empty = MemoryCallerContext(user_id="USR_X")
        assert caller_can_write_memory(empty, "agent", "eng-raccoon") is False

    def test_unknown_scope_refused(self) -> None:
        caller = MemoryCallerContext(user_id="u", agent_name="a")
        assert caller_can_write_memory(caller, "project", None) is False


class TestMemoryCallerContext:
    def test_has_identity_flag(self) -> None:
        assert MemoryCallerContext().has_identity is False
        assert MemoryCallerContext(user_id="u").has_identity is True
        assert MemoryCallerContext(agent_name="a").has_identity is True
        assert MemoryCallerContext(user_id="u", agent_name="a").has_identity is True


# ---------------------------------------------------------------------------
# read_artifact_content with inline_content short-circuit
# ---------------------------------------------------------------------------


class TestReadArtifactContentInline:
    async def test_inline_content_skips_blob_store(self) -> None:
        """MEMORY rows with inline_content return directly, never touching the store."""
        store = _FakeBlobStore()
        artifact = MagicMock()
        artifact.agent_artifact_id = FAKE_ID
        artifact.content_type = "text/markdown"
        artifact.inline_content = "# User preferences\nLikes tabs."
        artifact.artifact_metadata = {}

        data, ct = await read_artifact_content(artifact, blob_store=store)

        assert data == b"# User preferences\nLikes tabs."
        assert ct == "text/markdown"

    async def test_inline_content_defaults_content_type_when_missing(self) -> None:
        """Even a MEMORY row with NULL content_type still resolves."""
        store = _FakeBlobStore()
        artifact = MagicMock()
        artifact.agent_artifact_id = FAKE_ID
        artifact.content_type = None
        artifact.inline_content = "hello"
        artifact.artifact_metadata = {}

        data, ct = await read_artifact_content(artifact, blob_store=store)
        assert data == b"hello"
        assert ct == "text/markdown"


# ---------------------------------------------------------------------------
# _scope_slug_filter shape (no DB)
# ---------------------------------------------------------------------------


class TestScopeSlugFilter:
    def test_memory_topic_requires_scope(self) -> None:
        from ypl.agent_harness_service.artifact_store import _scope_slug_filter
        from ypl.db.agent_harness import AgentArtifactType

        # MEMORY lookup without scope → ArtifactError.
        with pytest.raises(ArtifactError, match="memory_scope"):
            _scope_slug_filter(
                slug="x",
                artifact_type=AgentArtifactType.MEMORY,
                memory_scope=None,
                memory_scope_subject=None,
            )

    def test_memory_user_requires_subject(self) -> None:
        from ypl.agent_harness_service.artifact_store import _scope_slug_filter
        from ypl.db.agent_harness import AgentArtifactType

        with pytest.raises(ArtifactError, match="memory_scope_subject"):
            _scope_slug_filter(
                slug="x",
                artifact_type=AgentArtifactType.MEMORY,
                memory_scope="user",
                memory_scope_subject=None,
            )

    def test_text_ignores_scope(self) -> None:
        from ypl.agent_harness_service.artifact_store import _scope_slug_filter
        from ypl.db.agent_harness import AgentArtifactType

        # TEXT lookups never touch scope columns — no error on missing scope.
        clauses = _scope_slug_filter(
            slug="x",
            artifact_type=AgentArtifactType.TEXT,
            memory_scope=None,
            memory_scope_subject=None,
        )
        assert len(clauses) == 2  # named_slug == x, artifact_type == TEXT
