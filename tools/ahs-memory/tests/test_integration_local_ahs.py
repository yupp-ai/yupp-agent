"""End-to-end test against a real AHS instance (skipped by default).

Run with::

    AHS_INTEGRATION=1 AHS_API_URL=http://localhost:8090 \\
    AHS_API_KEY=$AGENT_HARNESS_SERVICE_API_KEY \\
    AHS_USER_ID=<some-test-user-id> \\
    pytest tests/test_integration_local_ahs.py

The test creates a uniquely-slugged MEMORY artifact per run
(``ahs_memory_e2e/<unix_ts>/...``) so repeated invocations don't
collide. Cleanup happens in the finally block via the artifact archive
endpoint, but a stray run that crashes leaves the rows behind as
``ahs_memory_e2e/...`` — easy to spot in the artifact viewer.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from ahs_memory.client import AHSAPIError, AHSMemoryClient
from ahs_memory.pusher import push_workspace
from ahs_memory.walker import walk_workspace

pytestmark = pytest.mark.skipif(
    os.environ.get("AHS_INTEGRATION") != "1",
    reason="set AHS_INTEGRATION=1 to run against a real AHS instance",
)


def _archive_slug(client: AHSMemoryClient, slug: str, user_id: str) -> None:
    """Best-effort cleanup. Swallows 404 so a partially-failed test still cleans up."""
    try:
        resp = client._http.delete(
            f"/ahs/artifacts/by-slug/{slug}",
            params={"type": "MEMORY", "scope": "user", "subject": user_id},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            print(f"warning: archive {slug!r} returned {resp.status_code}: {resp.text}")
    except httpx.HTTPError as exc:
        print(f"warning: archive {slug!r} raised {exc}")


def test_push_then_skip_unchanged(tmp_path: Path) -> None:
    api_url = os.environ["AHS_API_URL"]
    api_key = os.environ["AHS_API_KEY"]
    user_id = os.environ["AHS_USER_ID"]
    stamp = int(time.time())
    prefix = f"ahs_memory_e2e/{stamp}"

    # Build a tiny workspace under tmp_path.
    (tmp_path / "alpha.md").write_text("# Alpha\n\nfirst write\n", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "two.md").write_text("# Beta two\n\nsecond file\n", encoding="utf-8")
    candidates = walk_workspace(tmp_path, prefix=prefix)
    assert len(candidates) == 2

    slugs = [c.slug for c in candidates]

    with AHSMemoryClient(api_url, api_key, user_id=user_id) as client:
        try:
            # First push: both files should be created.
            outcomes = push_workspace(client, candidates, user_id=user_id)
            statuses = {o.slug: o.status for o in outcomes}
            assert statuses == {slugs[0]: "created", slugs[1]: "created"}, statuses

            # Re-push with skip-unchanged: both should skip.
            candidates = walk_workspace(tmp_path, prefix=prefix)
            outcomes = push_workspace(client, candidates, user_id=user_id, skip_unchanged=True)
            statuses = {o.slug: o.status for o in outcomes}
            assert all(s == "skipped-unchanged" for s in statuses.values()), statuses

            # Modify one file → that one becomes a new version, the other still skips.
            (tmp_path / "alpha.md").write_text("# Alpha\n\nrewritten\n", encoding="utf-8")
            candidates = walk_workspace(tmp_path, prefix=prefix)
            outcomes = push_workspace(client, candidates, user_id=user_id, skip_unchanged=True)
            statuses = {o.slug: o.status for o in outcomes}
            assert statuses[slugs[0]] == "new-version"
            assert statuses[slugs[1]] == "skipped-unchanged"

            # Server-side verification.
            for slug in slugs:
                m = client.get_memory_slug(slug, user_id=user_id)
                assert m is not None
                assert m.memory_scope == "user"
                assert m.memory_scope_subject == user_id
        finally:
            for slug in slugs:
                _archive_slug(client, slug, user_id)


def test_oversize_skipped_without_request(tmp_path: Path) -> None:
    api_url = os.environ["AHS_API_URL"]
    api_key = os.environ["AHS_API_KEY"]
    user_id = os.environ["AHS_USER_ID"]
    (tmp_path / "big.md").write_text("x" * 5000, encoding="utf-8")
    candidates = walk_workspace(tmp_path, max_bytes=1000)
    with AHSMemoryClient(api_url, api_key, user_id=user_id) as client:
        outcomes = push_workspace(client, candidates, user_id=user_id)
    assert outcomes[0].status == "skipped-oversize"


def test_invalid_user_id_returns_clear_error(tmp_path: Path) -> None:
    api_url = os.environ["AHS_API_URL"]
    api_key = os.environ["AHS_API_KEY"]
    (tmp_path / "page.md").write_text("hi", encoding="utf-8")
    # Use a deliberately-wrong user_id — server should refuse.
    bogus_user = "bogus-user-does-not-exist"
    candidates = walk_workspace(tmp_path, prefix=f"ahs_memory_e2e/badauth/{int(time.time())}")
    with AHSMemoryClient(api_url, api_key, user_id=bogus_user) as client:
        outcomes = push_workspace(client, candidates, user_id=bogus_user)
    # Either a 403 (refused) or 404 (no such user) is acceptable — the
    # important thing is the outcome is ``error`` with a meaningful detail.
    assert outcomes[0].status == "error"
    assert isinstance(AHSAPIError, type)  # sanity import touch
