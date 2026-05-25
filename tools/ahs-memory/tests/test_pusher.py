"""Pusher/diff tests against a fake :class:`AHSMemoryClient`.

A handwritten fake lets us assert *what was uploaded* alongside the
``PushOutcome`` rows, which respx/MockTransport make awkward.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ahs_memory.client import AHSAPIError, MemoryArtifact
from ahs_memory.pusher import diff_workspace, push_workspace
from ahs_memory.walker import walk_workspace


@dataclass
class _StoredRow:
    slug: str
    inline_content: str
    title: str
    description: str | None
    version: int


@dataclass
class FakeClient:
    """In-memory stand-in that mimics ``AHSMemoryClient``'s surface."""

    next_id: int = 1
    rows: dict[str, list[_StoredRow]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    force_error_on_slug: str | None = None

    def list_user_memories(self, user_id: str, *, page_size: int = 200) -> list[MemoryArtifact]:
        self.calls.append(f"list:{user_id}")
        out: list[MemoryArtifact] = []
        for slug, versions in self.rows.items():
            latest = versions[-1]
            out.append(
                MemoryArtifact(
                    artifact_id=f"id-{slug}",
                    slug=slug,
                    version=latest.version,
                    title=latest.title,
                    description=latest.description,
                    memory_scope="user",
                    memory_scope_subject=user_id,
                    created_at="2026-05-25T12:00:00Z",
                )
            )
        return out

    def get_memory_slug(self, slug: str, user_id: str) -> MemoryArtifact | None:
        self.calls.append(f"get:{slug}")
        versions = self.rows.get(slug)
        if not versions:
            return None
        latest = versions[-1]
        return MemoryArtifact(
            artifact_id=f"id-{slug}",
            slug=slug,
            version=latest.version,
            title=latest.title,
            description=latest.description,
            memory_scope="user",
            memory_scope_subject=user_id,
            created_at="2026-05-25T12:00:00Z",
        )

    def fetch_inline_content(self, artifact_id: str) -> str:
        self.calls.append(f"fetch:{artifact_id}")
        # ``id-<slug>`` reverse lookup.
        slug = artifact_id[3:]
        versions = self.rows.get(slug, [])
        if not versions:
            raise AHSAPIError(404, "missing")
        return versions[-1].inline_content

    def create_memory_artifact(
        self,
        *,
        slug: str,
        inline_content: str,
        user_id: str,
        title: str,
        description: str | None = None,
        create_new_slug: bool = False,
    ) -> dict[str, object]:
        self.calls.append(f"create:{slug}:{create_new_slug}")
        if slug == self.force_error_on_slug:
            raise AHSAPIError(500, "boom")
        existing = self.rows.get(slug)
        if existing is None:
            # Server contract: appending to an unknown slug returns 400.
            if not create_new_slug:
                raise AHSAPIError(400, f"slug {slug!r} does not exist yet; pass create_new_slug=True")
            version = 1
        else:
            version = existing[-1].version + 1
        row = _StoredRow(
            slug=slug,
            inline_content=inline_content,
            title=title,
            description=description,
            version=version,
        )
        self.rows.setdefault(slug, []).append(row)
        self.next_id += 1
        return {
            "artifact_id": f"id-{slug}",
            "named_slug": slug,
            "version": version,
            "title": title,
        }

    def close(self) -> None:  # pragma: no cover - parity with the real client
        pass

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# push_workspace
# ---------------------------------------------------------------------------


def test_push_creates_then_appends_new_version(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("first", encoding="utf-8")
    fake = FakeClient()

    candidates = walk_workspace(tmp_path)
    first = push_workspace(fake, candidates, user_id="user-1")
    assert [o.status for o in first] == ["created"]
    assert first[0].version == 1

    # Rewrite the file and push again — should create a new version.
    (tmp_path / "a.md").write_text("second", encoding="utf-8")
    candidates = walk_workspace(tmp_path)
    second = push_workspace(fake, candidates, user_id="user-1")
    assert [o.status for o in second] == ["new-version"]
    assert second[0].version == 2


def test_push_skip_unchanged_when_hash_matches(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    fake = FakeClient()

    candidates = walk_workspace(tmp_path)
    push_workspace(fake, candidates, user_id="user-1")

    # Same content + flag → no upload.
    outcomes = push_workspace(fake, candidates, user_id="user-1", skip_unchanged=True)
    assert outcomes[0].status == "skipped-unchanged"
    assert outcomes[0].detail.startswith("matches v")


def test_push_skip_unchanged_uploads_when_diff(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hi", encoding="utf-8")
    fake = FakeClient()

    candidates = walk_workspace(tmp_path)
    push_workspace(fake, candidates, user_id="user-1")

    (tmp_path / "a.md").write_text("different", encoding="utf-8")
    candidates = walk_workspace(tmp_path)
    outcomes = push_workspace(fake, candidates, user_id="user-1", skip_unchanged=True)
    assert outcomes[0].status == "new-version"


def test_push_skips_oversize_with_clear_message(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 200, encoding="utf-8")
    fake = FakeClient()

    candidates = walk_workspace(tmp_path, max_bytes=100)
    outcomes = push_workspace(fake, candidates, user_id="user-1")
    assert outcomes[0].status == "skipped-oversize"
    assert "oversize" in outcomes[0].detail
    # We did not contact the server for the oversize file.
    assert not fake.calls


def test_push_skips_unsafe_slugs(tmp_path: Path) -> None:
    (tmp_path / "!!!.md").write_text("hi", encoding="utf-8")
    fake = FakeClient()

    candidates = walk_workspace(tmp_path)
    outcomes = push_workspace(fake, candidates, user_id="user-1")
    assert outcomes[0].status == "skipped-unsafe"
    assert outcomes[0].slug == ""
    assert not fake.calls


def test_push_reports_server_errors_without_aborting(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    fake = FakeClient(force_error_on_slug="a")

    candidates = walk_workspace(tmp_path)
    outcomes = push_workspace(fake, candidates, user_id="user-1")
    statuses = {o.slug: o.status for o in outcomes}
    assert statuses == {"a": "error", "b": "created"}


def test_push_uploads_the_actual_file_body(tmp_path: Path) -> None:
    body = "# Hello\n\nbody text\n"
    (tmp_path / "page.md").write_text(body, encoding="utf-8")
    fake = FakeClient()

    push_workspace(fake, walk_workspace(tmp_path), user_id="user-1")
    stored = fake.rows["page"][-1]
    assert stored.inline_content == body
    assert stored.title == "page"


# ---------------------------------------------------------------------------
# diff_workspace
# ---------------------------------------------------------------------------


def test_diff_classifies_local_remote_changed_unchanged(tmp_path: Path, sample_workspace: Path) -> None:
    fake = FakeClient()
    # Pre-seed: one matching slug ("notes/daily"), one remote-only, one stale.
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "daily.md").write_text("daily body", encoding="utf-8")
    (tmp_path / "notes" / "monthly.md").write_text("monthly body", encoding="utf-8")

    fake.rows["notes/daily"] = [
        _StoredRow(slug="notes/daily", inline_content="daily body", title="daily", description=None, version=1)
    ]
    fake.rows["notes/monthly"] = [
        _StoredRow(slug="notes/monthly", inline_content="DIFFERENT", title="monthly", description=None, version=1)
    ]
    fake.rows["leftover"] = [
        _StoredRow(slug="leftover", inline_content="orphan", title="leftover", description=None, version=1)
    ]

    candidates = walk_workspace(tmp_path)
    rows = diff_workspace(fake, candidates, user_id="user-1")
    by_status: dict[str, list[str]] = {}
    for r in rows:
        by_status.setdefault(r.status, []).append(r.slug)

    assert sorted(by_status.get("unchanged", [])) == ["notes/daily"]
    assert sorted(by_status.get("changed", [])) == ["notes/monthly"]
    assert sorted(by_status.get("remote-only", [])) == ["leftover"]


def test_diff_treats_unreadable_local_as_changed(tmp_path: Path) -> None:
    # Bytes that can't decode as UTF-8 → "changed" so the operator notices.
    target = tmp_path / "bad.md"
    target.write_bytes(b"\xff\xfe\xfd")
    fake = FakeClient()
    fake.rows["bad"] = [_StoredRow(slug="bad", inline_content="hi", title="bad", description=None, version=1)]
    rows = diff_workspace(fake, walk_workspace(tmp_path), user_id="user-1")
    assert any(r.slug == "bad" and r.status == "changed" for r in rows)


def test_diff_local_only_for_new_files(tmp_path: Path) -> None:
    (tmp_path / "fresh.md").write_text("hi", encoding="utf-8")
    rows = diff_workspace(FakeClient(), walk_workspace(tmp_path), user_id="user-1")
    assert [(r.status, r.slug) for r in rows] == [("local-only", "fresh")]


def test_hash_match_uses_sha256_semantics() -> None:
    # Sanity test for the internal hash helper - confirms identical inputs hash equal.
    from ahs_memory.pusher import _sha256

    assert _sha256("abc") == _sha256("abc")
    assert _sha256("abc") != _sha256("abcd")
    assert _sha256("abc") == hashlib.sha256(b"abc").hexdigest()
