"""End-to-end regression harness for the memory-as-artifact pipeline.

Exercises the full stack against a *deployed* AHS through SSH tunnels — DB
schema (asyncpg :5433), REST surface (httpx :8090 ``/ahs/artifacts``), MCP
tools (httpx :8090 ``/mcp/platform``), and a static repo grep that guards
against the legacy GCS-backed memory subsystem creeping back. Returns
non-zero if any assertion fails so the script doubles as a CI smoke test.

This is **not** a unit test: it talks to the real Postgres + the real
service. Slugs are namespaced ``e2e_memory_<unix_ts>_*`` so cleanup is
trivial (a best-effort sweep runs in ``finally``).

Prerequisites
-------------
1. ``ahs-tunnel.sh`` is running and forwarding:

   * ``localhost:8090``  → AHS + MCP + SAG
   * ``localhost:5433``  → Postgres (read + the few synthetic rows phase A
                                       writes inside a rolled-back txn)

2. Environment variables:

   * ``AHS_API_KEY``        — AHS REST ``X-API-Key`` (required).
   * ``MCP_DEV_TOKEN``      — ``Bearer yupp_dev_*`` token with
                              ``MANAGE_AGENT_SESSIONS`` (required for phase
                              C; without it phase C is skipped with a
                              loud SKIP message but the rest still runs).
   * ``MEMORY_E2E_DB_URL``  — Postgres URL. Defaults to
                              ``postgresql://test:test@localhost:5433/agentdb``.
   * ``AHS_HOST``           — ``host:port`` for AHS. Defaults to
                              ``localhost:8090``.

3. The ``SYSTEM`` user exists in ``users`` (it's the AHS service principal
   that holds ``MANAGE_AGENT_SESSIONS``). This is true on every standard
   monolith deploy seeded by ``python -m ypl.mono_server.setup``.

Usage
-----
::

    poetry run python scripts/test_memory_e2e.py
    poetry run python scripts/test_memory_e2e.py --skip-mcp
    poetry run python scripts/test_memory_e2e.py --skip-grep
    poetry run python scripts/test_memory_e2e.py --keep-artifacts

Exit codes
----------
0  All assertions passed.
1  One or more assertions failed (or a phase blew up — partial PASS/FAIL
   counts are still printed).

Phases
------
A. **DB sanity** (asyncpg). Enum value, columns, partial-WHERE indexes,
   and direct-INSERT CHECK violations (transaction is rolled back so the
   DB stays clean).
B. **REST surface** (httpx). MEMORY create/read/list/search/delete across
   all three scopes, version-bump, cross-scope read+write rejection, body
   validation.
C. **MCP tools** (httpx JSON-RPC over ``/mcp/platform``). save / load /
   search / list happy paths, default-subject behaviour, cross-scope
   rejection, and a regression guard that the legacy
   ``get_agent_memory`` / ``store_agent_memory`` / ``search_agent_memory``
   tools are no longer registered.
D. **Static-grep regression** of the repo at HEAD for the legacy GCS
   memory symbols (must return zero hits outside ``tests/`` and
   ``ypl/db/alembic/versions/``).
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_AHS_HOST = "localhost:8090"
DEFAULT_DB_URL = "postgresql://test:test@localhost:5433/agentdb"

# Caller identity used by every phase B / C scoped call.
TEST_USER_ID = "SYSTEM"
TEST_AGENT_NAME = "eng-raccoon"
OTHER_USER_ID = "E2E_OTHER_USER"
OTHER_AGENT_NAME = "e2e_other_agent"

# Regression-grep targets. Must produce zero hits in non-test, non-alembic
# code once task [6] (delete legacy GCS-backed MCP memory tools) ships.
LEGACY_SYMBOLS: tuple[str, ...] = (
    "_AGENT_MEMORY_PREFIX",
    "AHS_GCS_MEMORY_BUCKET",
    "AHS_GCS_MEMORY_PREFIX",
    "PRIVATE_TOPIC_PREFIX",
    "gcs_memory_sync_manifest",
    "private_memory_index_manifest",
    "memory_persistence",
    "agent_memory_search",
)

# Path prefixes excluded from the regression grep.
GREP_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "tests/",
    "ypl/db/alembic/versions/",
    # Don't flag the script itself for documenting the legacy symbols.
    "scripts/test_memory_e2e.py",
)

# Legacy MCP tool names that must NOT be registered.
LEGACY_MCP_TOOLS: tuple[str, ...] = (
    "get_agent_memory",
    "store_agent_memory",
    "search_agent_memory",
)

# CHECK constraint names from migration b227eabd88f2.
CK_CONTENT_LOCATION = "ck_agent_artifacts_content_location"
CK_SCOPE_MATCHES_TYPE = "ck_agent_artifacts_memory_scope_matches_type"
CK_SUBJECT_PRESENCE = "ck_agent_artifacts_memory_subject_presence"


# ---------------------------------------------------------------------------
# Result tracker
# ---------------------------------------------------------------------------


@dataclass
class Results:
    """Accumulate per-assertion PASS/FAIL with a final summary."""

    verbose: bool = True
    items: list[tuple[str, bool, str]] = field(default_factory=list)
    skipped_phases: list[str] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.items.append((name, passed, detail))
        if self.verbose:
            tag = "PASS" if passed else "FAIL"
            line = f"  [{tag}] {name}"
            if detail and (not passed or self.verbose):
                line += f" — {detail}"
            print(line, flush=True)
        return passed

    def expect(self, name: str, condition: bool, detail: str = "") -> bool:
        return self.record(name, bool(condition), detail)

    def expect_eq(self, name: str, actual: Any, expected: Any) -> bool:
        ok = actual == expected
        return self.record(name, ok, "" if ok else f"actual={actual!r}, expected={expected!r}")

    def section(self, title: str) -> None:
        if self.verbose:
            print(f"\n=== {title} ===", flush=True)

    def skip_phase(self, phase: str, reason: str) -> None:
        self.skipped_phases.append(phase)
        print(f"\n=== {phase} SKIPPED — {reason} ===", flush=True)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def n_failed(self) -> int:
        return sum(1 for _, ok, _ in self.items if not ok)

    @property
    def n_passed(self) -> int:
        return sum(1 for _, ok, _ in self.items if ok)

    def summary(self) -> str:
        lines = [
            "",
            f"Total assertions: {self.total}",
            f"  PASS: {self.n_passed}",
            f"  FAIL: {self.n_failed}",
        ]
        if self.skipped_phases:
            lines.append(f"  SKIPPED phases: {', '.join(self.skipped_phases)}")
        if self.n_failed:
            lines.append("")
            lines.append("Failed assertions:")
            for name, ok, detail in self.items:
                if not ok:
                    lines.append(f"  - {name}{(' — ' + detail) if detail else ''}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------


def _api_base(ahs_host: str) -> str:
    return f"http://{ahs_host}/ahs/artifacts"


def _api_headers(
    api_key: str, *, caller: bool = False, alt_user: str | None = None, alt_agent: str | None = None
) -> dict[str, str]:
    """Build REST headers. ``caller=True`` adds the SYSTEM/eng-raccoon identity.

    ``alt_user`` / ``alt_agent`` override the identity (used to test
    cross-scope writes against a different subject).
    """
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if caller:
        headers["X-User-ID"] = alt_user if alt_user is not None else TEST_USER_ID
        headers["X-AHS-Agent-Name"] = alt_agent if alt_agent is not None else TEST_AGENT_NAME
    return headers


async def _post_artifact(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    return await client.post(base, headers=headers, content=json.dumps(body))


# ---------------------------------------------------------------------------
# Phase A: DB sanity
# ---------------------------------------------------------------------------


async def phase_a_db(results: Results, db_url: str) -> None:
    results.section("PHASE A — DB sanity (asyncpg)")
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(db_url)
    except Exception as exc:
        results.expect("A.connect", False, f"asyncpg.connect failed: {type(exc).__name__}: {exc}")
        return

    try:
        # A1. AgentArtifactType enum contains MEMORY.
        enum_values = await conn.fetch(
            """
            SELECT enumlabel
            FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = 'agentartifacttype'
            ORDER BY e.enumsortorder
            """
        )
        labels = [row["enumlabel"] for row in enum_values]
        results.expect("A.enum_has_MEMORY", "MEMORY" in labels, f"labels={labels}")

        # A2. Required columns exist.
        cols = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'agent_artifacts'
              AND column_name IN ('inline_content', 'memory_scope', 'memory_scope_subject')
            """
        )
        col_set = {row["column_name"] for row in cols}
        for needed in ("inline_content", "memory_scope", "memory_scope_subject"):
            results.expect(f"A.column_present[{needed}]", needed in col_set)

        # A3. Indexes with the correct partial WHERE clauses.
        idx_rows = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'agent_artifacts'
              AND indexname IN ('uix_memory_scope_slug_version', 'ix_memory_scope_subject')
            """
        )
        idx_def = {row["indexname"]: row["indexdef"] for row in idx_rows}

        results.expect(
            "A.index[uix_memory_scope_slug_version]_present",
            "uix_memory_scope_slug_version" in idx_def,
        )
        if "uix_memory_scope_slug_version" in idx_def:
            d = idx_def["uix_memory_scope_slug_version"]
            results.expect(
                "A.index[uix_memory_scope_slug_version].partial_where",
                "artifact_type = 'MEMORY'" in d and "named_slug IS NOT NULL" in d and "version IS NOT NULL" in d,
                detail=d,
            )
            results.expect(
                "A.index[uix_memory_scope_slug_version].nulls_not_distinct",
                "NULLS NOT DISTINCT" in d.upper(),
                detail=d,
            )

        results.expect(
            "A.index[ix_memory_scope_subject]_present",
            "ix_memory_scope_subject" in idx_def,
        )
        if "ix_memory_scope_subject" in idx_def:
            d = idx_def["ix_memory_scope_subject"]
            results.expect(
                "A.index[ix_memory_scope_subject].partial_where",
                "artifact_type = 'MEMORY'" in d,
                detail=d,
            )

        # A4. CHECK constraint violations are rejected by direct SQL inserts.
        # We do all violating inserts inside a single rolled-back transaction
        # so we don't pollute the DB even on partial failure.
        async def _violation_test(name: str, sql: str, args: tuple[Any, ...], expected_constraint: str) -> None:
            tx = conn.transaction()
            await tx.start()
            try:
                try:
                    await conn.execute(sql, *args)
                except asyncpg.IntegrityConstraintViolationError as exc:
                    msg = str(exc)
                    ok = expected_constraint in msg
                    results.expect(name, ok, f"got: {msg.splitlines()[0]!r}")
                else:
                    results.expect(name, False, "INSERT succeeded — constraint did NOT fire")
            finally:
                await tx.rollback()

        # ck_agent_artifacts_memory_scope_matches_type: MEMORY without scope.
        await _violation_test(
            "A.ck_violation[scope_mismatch_MEMORY_no_scope]",
            """
            INSERT INTO agent_artifacts (
                agent_artifact_id, artifact_type, title, inline_content,
                content_type, created_at
            )
            VALUES ($1, 'MEMORY', 'e2e-violation', 'x', 'text/markdown', NOW())
            """,
            (uuid.uuid4(),),
            CK_SCOPE_MATCHES_TYPE,
        )

        # ck_agent_artifacts_memory_subject_presence: scope=user without subject.
        await _violation_test(
            "A.ck_violation[subject_shape_user_no_subject]",
            """
            INSERT INTO agent_artifacts (
                agent_artifact_id, artifact_type, title, inline_content,
                content_type, memory_scope, memory_scope_subject, created_at
            )
            VALUES ($1, 'MEMORY', 'e2e-violation', 'x', 'text/markdown', 'user', NULL, NOW())
            """,
            (uuid.uuid4(),),
            CK_SUBJECT_PRESENCE,
        )

        # Also check scope=topic WITH subject violates the same constraint.
        await _violation_test(
            "A.ck_violation[subject_shape_topic_with_subject]",
            """
            INSERT INTO agent_artifacts (
                agent_artifact_id, artifact_type, title, inline_content,
                content_type, memory_scope, memory_scope_subject, created_at
            )
            VALUES ($1, 'MEMORY', 'e2e-violation', 'x', 'text/markdown', 'topic', 'should-be-null', NOW())
            """,
            (uuid.uuid4(),),
            CK_SUBJECT_PRESENCE,
        )

        # ck_agent_artifacts_content_location: both inline_content AND url set.
        await _violation_test(
            "A.ck_violation[content_location_both_set]",
            """
            INSERT INTO agent_artifacts (
                agent_artifact_id, artifact_type, title, inline_content, url,
                content_type, memory_scope, memory_scope_subject, created_at
            )
            VALUES ($1, 'MEMORY', 'e2e-violation', 'x', 'http://example.com',
                    'text/markdown', 'topic', NULL, NOW())
            """,
            (uuid.uuid4(),),
            CK_CONTENT_LOCATION,
        )

        # And neither set (TEXT row with no url and no inline_content).
        await _violation_test(
            "A.ck_violation[content_location_neither_set]",
            """
            INSERT INTO agent_artifacts (
                agent_artifact_id, artifact_type, title, content_type, created_at
            )
            VALUES ($1, 'TEXT', 'e2e-violation', 'text/markdown', NOW())
            """,
            (uuid.uuid4(),),
            CK_CONTENT_LOCATION,
        )

    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Phase B: REST surface
# ---------------------------------------------------------------------------


@dataclass
class CreatedSlug:
    """Track a slug + scope so cleanup can target it."""

    slug: str
    scope: str
    subject: str | None


async def phase_b_rest(
    results: Results,
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    slug_prefix: str,
    created: list[CreatedSlug],
) -> None:
    results.section("PHASE B — REST surface (httpx :8090)")

    # Headers used throughout the phase.
    h_admin = _api_headers(api_key)  # no caller identity → admin view
    h_caller = _api_headers(api_key, caller=True)  # SYSTEM + eng-raccoon

    # ------------------------------------------------------------------
    # B1. POST happy path — all three scopes (topic/user=SYSTEM/agent=eng-raccoon).
    # ------------------------------------------------------------------
    topic_slug = f"{slug_prefix}_topic"
    user_slug = f"{slug_prefix}_user"
    agent_slug = f"{slug_prefix}_agent"

    # Topic scope.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": topic_slug,
            "inline_content": "topic content v1 widget-foxtrot",
            "memory_scope": "topic",
            "named_slug": topic_slug,
            "create_new_slug": True,
        },
    )
    results.expect_eq("B.create.topic.status", r.status_code, 201)
    if r.status_code == 201:
        body = r.json()
        results.expect_eq("B.create.topic.scope", body["memory_scope"], "topic")
        results.expect_eq("B.create.topic.subject", body["memory_scope_subject"], None)
        results.expect_eq("B.create.topic.version", body["version"], 1)
        created.append(CreatedSlug(topic_slug, "topic", None))

    # User scope.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": user_slug,
            "inline_content": "user content v1",
            "memory_scope": "user",
            "memory_scope_subject": TEST_USER_ID,
            "named_slug": user_slug,
            "create_new_slug": True,
        },
    )
    results.expect_eq("B.create.user.status", r.status_code, 201)
    if r.status_code == 201:
        body = r.json()
        results.expect_eq("B.create.user.scope", body["memory_scope"], "user")
        results.expect_eq("B.create.user.subject", body["memory_scope_subject"], TEST_USER_ID)
        created.append(CreatedSlug(user_slug, "user", TEST_USER_ID))

    # Agent scope.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": agent_slug,
            "inline_content": "agent content v1",
            "memory_scope": "agent",
            "memory_scope_subject": TEST_AGENT_NAME,
            "named_slug": agent_slug,
            "create_new_slug": True,
        },
    )
    results.expect_eq("B.create.agent.status", r.status_code, 201)
    if r.status_code == 201:
        body = r.json()
        results.expect_eq("B.create.agent.scope", body["memory_scope"], "agent")
        results.expect_eq("B.create.agent.subject", body["memory_scope_subject"], TEST_AGENT_NAME)
        created.append(CreatedSlug(agent_slug, "agent", TEST_AGENT_NAME))

    # ------------------------------------------------------------------
    # B2. Version bump on re-save (same scope, subject, slug).
    # ------------------------------------------------------------------
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": topic_slug,
            "inline_content": "topic content v2 widget-foxtrot",
            "memory_scope": "topic",
            "named_slug": topic_slug,
            "create_new_slug": False,
        },
    )
    results.expect_eq("B.version_bump.status", r.status_code, 201)
    if r.status_code == 201:
        results.expect_eq("B.version_bump.version", r.json()["version"], 2)

    # ------------------------------------------------------------------
    # B3. List as admin (no caller headers) returns all three rows.
    # ------------------------------------------------------------------
    r = await client.get(base, headers=h_admin, params={"type": "MEMORY", "limit": 200})
    results.expect_eq("B.list.admin.status", r.status_code, 200)
    if r.status_code == 200:
        slugs = {a["named_slug"] for a in r.json().get("artifacts", [])}
        for s in (topic_slug, user_slug, agent_slug):
            results.expect(f"B.list.admin.contains[{s}]", s in slugs)

    # ------------------------------------------------------------------
    # B4. List with caller headers — gets topic ∪ own user ∪ own agent only.
    # We seed an "other-user" memory directly via admin headers + the
    # ``creator_user_id`` field, then assert the caller view doesn't see it.
    # ------------------------------------------------------------------
    other_slug = f"{slug_prefix}_otheruser"
    # Admin (no caller headers) can write any scope/subject.
    r_seed = await _post_artifact(
        client,
        base,
        h_admin,
        {
            "type": "MEMORY",
            "title": other_slug,
            "inline_content": "other user's secret",
            "memory_scope": "user",
            "memory_scope_subject": OTHER_USER_ID,
            "named_slug": other_slug,
            "create_new_slug": True,
        },
    )
    if r_seed.status_code == 201:
        created.append(CreatedSlug(other_slug, "user", OTHER_USER_ID))

    r = await client.get(base, headers=h_caller, params={"type": "MEMORY", "limit": 200})
    results.expect_eq("B.list.caller.status", r.status_code, 200)
    if r.status_code == 200:
        slugs = {a["named_slug"] for a in r.json().get("artifacts", [])}
        # caller's three slugs are visible.
        for s in (topic_slug, user_slug, agent_slug):
            results.expect(f"B.list.caller.visible[{s}]", s in slugs)
        # other-user slug NOT visible.
        results.expect(f"B.list.caller.cross_user_invisible[{other_slug}]", other_slug not in slugs)

    # ------------------------------------------------------------------
    # B5. by-slug latest non-archived + by-slug?version=N specific version.
    # ------------------------------------------------------------------
    r = await client.get(
        f"{base}/by-slug/{topic_slug}",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "topic"},
    )
    results.expect_eq("B.by_slug.latest.status", r.status_code, 200)
    if r.status_code == 200:
        results.expect_eq("B.by_slug.latest.version", r.json()["version"], 2)

    r = await client.get(
        f"{base}/by-slug/{topic_slug}",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "topic", "version": 1},
    )
    results.expect_eq("B.by_slug.specific_version.status", r.status_code, 200)
    if r.status_code == 200:
        results.expect_eq("B.by_slug.specific_version.value", r.json()["version"], 1)

    # ------------------------------------------------------------------
    # B6. by-slug/versions returns the version list.
    # ------------------------------------------------------------------
    r = await client.get(
        f"{base}/by-slug/{topic_slug}/versions",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "topic"},
    )
    results.expect_eq("B.by_slug.versions.status", r.status_code, 200)
    if r.status_code == 200:
        versions = sorted(v["version"] for v in r.json().get("versions", []))
        results.expect("B.by_slug.versions.has_v1_and_v2", versions == [1, 2], detail=str(versions))

    # ------------------------------------------------------------------
    # B7. Search matches title + inline_content; cross-scope rows never appear.
    # ------------------------------------------------------------------
    r = await client.get(
        f"{base}/search",
        headers=h_caller,
        params={"q": "widget-foxtrot", "type": "MEMORY", "limit": 50},
    )
    results.expect_eq("B.search.unique_token.status", r.status_code, 200)
    if r.status_code == 200:
        slugs = {a["named_slug"] for a in r.json().get("artifacts", [])}
        results.expect("B.search.unique_token.hits_topic_slug", topic_slug in slugs, detail=str(slugs))

    # Cross-scope: search the other-user's content — caller must not see it.
    r = await client.get(
        f"{base}/search",
        headers=h_caller,
        params={"q": "secret", "type": "MEMORY", "limit": 50},
    )
    results.expect_eq("B.search.cross_user.status", r.status_code, 200)
    if r.status_code == 200:
        slugs = {a["named_slug"] for a in r.json().get("artifacts", [])}
        results.expect(
            "B.search.cross_user.invisible",
            other_slug not in slugs,
            detail=f"other_slug leaked into caller search: {slugs}",
        )

    # Search with scope filter — only that scope's rows.
    r = await client.get(
        f"{base}/search",
        headers=h_caller,
        params={"q": slug_prefix, "type": "MEMORY", "scope": "topic", "limit": 50},
    )
    results.expect_eq("B.search.scope_filter.status", r.status_code, 200)
    if r.status_code == 200:
        scopes = {a["memory_scope"] for a in r.json().get("artifacts", [])}
        results.expect(
            "B.search.scope_filter.only_topic",
            scopes <= {"topic"},
            detail=f"unexpected scopes in scope=topic search: {scopes}",
        )

    # ------------------------------------------------------------------
    # B8. Cross-scope read filter — caller asking for ANOTHER user's subject
    # is rejected (403) or returns empty (the route layer raises 403; a stale
    # build that just filters silently would 404 the by-slug lookup).
    # ------------------------------------------------------------------
    r = await client.get(
        base,
        headers=h_caller,
        params={"type": "MEMORY", "scope": "user", "subject": OTHER_USER_ID, "limit": 50},
    )
    results.expect(
        "B.cross_scope_read.list_rejected",
        r.status_code in (403, 404),
        detail=f"expected 403/404, got {r.status_code}: {r.text[:200]}",
    )

    r = await client.get(
        f"{base}/by-slug/{other_slug}",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "user", "subject": OTHER_USER_ID},
    )
    results.expect(
        "B.cross_scope_read.by_slug_rejected",
        r.status_code in (403, 404),
        detail=f"expected 403/404, got {r.status_code}: {r.text[:200]}",
    )

    # ------------------------------------------------------------------
    # B9. Cross-scope WRITE rejection — caller's subject mismatches identity.
    # ------------------------------------------------------------------
    bad_slug = f"{slug_prefix}_should_not_exist"
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": bad_slug,
            "inline_content": "should be rejected",
            "memory_scope": "user",
            "memory_scope_subject": OTHER_USER_ID,
            "named_slug": bad_slug,
            "create_new_slug": True,
        },
    )
    results.expect_eq("B.cross_scope_write.user.status", r.status_code, 403)

    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": bad_slug,
            "inline_content": "should be rejected",
            "memory_scope": "agent",
            "memory_scope_subject": OTHER_AGENT_NAME,
            "named_slug": bad_slug,
            "create_new_slug": True,
        },
    )
    results.expect_eq("B.cross_scope_write.agent.status", r.status_code, 403)

    # ------------------------------------------------------------------
    # B10. Body validation — should be 400 for each bad request.
    # ------------------------------------------------------------------
    # Missing inline_content on MEMORY.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": "x",
            "memory_scope": "topic",
        },
    )
    results.expect_eq("B.validation.memory_missing_inline.status", r.status_code, 400)

    # Missing memory_scope on MEMORY.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "MEMORY",
            "title": "x",
            "inline_content": "x",
        },
    )
    results.expect_eq("B.validation.memory_missing_scope.status", r.status_code, 400)

    # inline_content on TEXT type.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "TEXT",
            "title": "x",
            "content": "ok",
            "inline_content": "should not be allowed here",
        },
    )
    results.expect_eq("B.validation.text_with_inline.status", r.status_code, 400)

    # memory_scope set on non-MEMORY.
    r = await _post_artifact(
        client,
        base,
        h_caller,
        {
            "type": "TEXT",
            "title": "x",
            "content": "ok",
            "memory_scope": "topic",
        },
    )
    results.expect_eq("B.validation.text_with_scope.status", r.status_code, 400)

    # ------------------------------------------------------------------
    # B11. DELETE by-slug → soft-archive; subsequent by-slug 404.
    # We pick the user-scope slug because it's owned by the caller and we
    # don't want to interfere with later phases that need the topic slug.
    # ------------------------------------------------------------------
    r = await client.delete(
        f"{base}/by-slug/{user_slug}",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "user", "subject": TEST_USER_ID},
    )
    results.expect_eq("B.delete.by_slug.status", r.status_code, 200)

    r = await client.get(
        f"{base}/by-slug/{user_slug}",
        headers=h_caller,
        params={"type": "MEMORY", "scope": "user", "subject": TEST_USER_ID},
    )
    results.expect_eq("B.delete.by_slug.subsequent_404", r.status_code, 404)


# ---------------------------------------------------------------------------
# Phase C: MCP tools
# ---------------------------------------------------------------------------


class McpClient:
    """Minimal JSON-RPC client for FastMCP's ``streamable-http`` endpoint.

    FastMCP exposes ``tools/list`` and ``tools/call``; the wire format is
    standard MCP. We don't bother with sessions because the deployment is
    set up with ``stateless_http=True``.
    """

    def __init__(self, client: httpx.AsyncClient, url: str, base_headers: dict[str, str]) -> None:
        self.client = client
        self.url = url
        self.base_headers = base_headers
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: dict[str, Any], extra_headers: dict[str, str] | None = None) -> Any:
        headers = {
            **self.base_headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        r = await self.client.post(self.url, headers=headers, content=json.dumps(payload))
        r.raise_for_status()
        body = r.text
        # FastMCP can return either application/json or text/event-stream
        # depending on the client's Accept header. We requested both, but
        # ``json_response=True`` on the deployed app makes JSON the norm.
        if body.startswith(("event:", "data:")):
            data_lines = [line[5:].lstrip() for line in body.splitlines() if line.startswith("data:")]
            body = data_lines[-1] if data_lines else "{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MCP returned non-JSON body: {body[:500]!r}") from exc
        if "error" in data and data["error"]:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._rpc("tools/call", {"name": name, "arguments": args})
        # FastMCP wraps the tool's dict return in {content: [{text: "..."}], isError: bool}.
        if isinstance(result, dict):
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        return {"raw_text": text}
        return {"raw_result": result}


async def phase_c_mcp(
    results: Results,
    client: httpx.AsyncClient,
    ahs_host: str,
    dev_token: str,
    slug_prefix: str,
    rest_base: str,
    api_key: str,
    created: list[CreatedSlug],
) -> None:
    results.section("PHASE C — MCP tools (httpx :8090 /mcp/platform)")

    mcp_url = f"http://{ahs_host}/mcp/platform/"
    base_headers = {
        "Authorization": f"Bearer {dev_token}",
        "X-User-ID": TEST_USER_ID,
        "X-AHS-Agent-Name": TEST_AGENT_NAME,
    }
    mcp = McpClient(client, mcp_url, base_headers)

    # ------------------------------------------------------------------
    # C1. Regression guard: legacy tool names are NOT registered.
    # ------------------------------------------------------------------
    try:
        tools = await mcp.list_tools()
    except Exception as exc:
        results.expect("C.tools_list", False, f"tools/list failed: {type(exc).__name__}: {exc}")
        return

    tool_names: set[str] = {n for n in (t.get("name") for t in tools) if isinstance(n, str)}
    results.expect(
        "C.tools_list.contains_save_memory",
        "save_memory" in tool_names,
        detail=f"missing save_memory; got {sorted(tool_names)[:25]}...",
    )
    results.expect(
        "C.tools_list.contains_load_memory",
        "load_memory" in tool_names,
    )
    results.expect(
        "C.tools_list.contains_search_memory",
        "search_memory" in tool_names,
    )
    results.expect(
        "C.tools_list.contains_list_memory",
        "list_memory" in tool_names,
    )
    for legacy in LEGACY_MCP_TOOLS:
        results.expect(
            f"C.regression.legacy_tool_absent[{legacy}]",
            legacy not in tool_names,
            detail=f"legacy tool {legacy!r} is still registered",
        )

    # ------------------------------------------------------------------
    # C2. save_memory happy paths for each scope.
    # ------------------------------------------------------------------
    mcp_topic = f"{slug_prefix}_mcp_topic"
    mcp_user = f"{slug_prefix}_mcp_user"
    mcp_agent = f"{slug_prefix}_mcp_agent"

    save_topic = await mcp.call_tool(
        "save_memory",
        {
            "topic": mcp_topic,
            "content": "mcp topic body",
            "scope": "topic",
        },
    )
    if results.expect_eq("C.save_memory.topic.success", save_topic.get("success"), True):
        results.expect_eq("C.save_memory.topic.scope", save_topic.get("scope"), "topic")
        results.expect_eq("C.save_memory.topic.subject", save_topic.get("subject"), None)
        created.append(CreatedSlug(mcp_topic, "topic", None))

    # User scope — explicit subject = caller's identity.
    save_user = await mcp.call_tool(
        "save_memory",
        {
            "topic": mcp_user,
            "content": "mcp user body",
            "scope": "user",
            "subject": TEST_USER_ID,
        },
    )
    if results.expect_eq("C.save_memory.user.success", save_user.get("success"), True):
        results.expect_eq("C.save_memory.user.subject", save_user.get("subject"), TEST_USER_ID)
        created.append(CreatedSlug(mcp_user, "user", TEST_USER_ID))

    # Agent scope, default scope=agent (omit `scope` param).
    save_agent = await mcp.call_tool(
        "save_memory",
        {
            "topic": mcp_agent,
            "content": "mcp agent body — defaults",
        },
    )
    if results.expect_eq("C.save_memory.agent_default.success", save_agent.get("success"), True):
        results.expect_eq("C.save_memory.agent_default.scope", save_agent.get("scope"), "agent")
        results.expect_eq("C.save_memory.agent_default.subject", save_agent.get("subject"), TEST_AGENT_NAME)
        created.append(CreatedSlug(mcp_agent, "agent", TEST_AGENT_NAME))

    # ------------------------------------------------------------------
    # C3. Default-subject behaviour: omit subject for user scope.
    # ------------------------------------------------------------------
    mcp_user_default = f"{slug_prefix}_mcp_user_default"
    save_user_default = await mcp.call_tool(
        "save_memory",
        {
            "topic": mcp_user_default,
            "content": "mcp user default body",
            "scope": "user",
        },
    )
    if results.expect_eq("C.save_memory.user_default.success", save_user_default.get("success"), True):
        results.expect_eq(
            "C.save_memory.user_default.subject_defaults_to_caller",
            save_user_default.get("subject"),
            TEST_USER_ID,
        )
        created.append(CreatedSlug(mcp_user_default, "user", TEST_USER_ID))

    # ------------------------------------------------------------------
    # C4. load_memory happy path.
    # ------------------------------------------------------------------
    load_topic = await mcp.call_tool("load_memory", {"topic": mcp_topic, "scope": "topic"})
    if results.expect_eq("C.load_memory.topic.success", load_topic.get("success"), True):
        results.expect_eq("C.load_memory.topic.content", load_topic.get("content"), "mcp topic body")

    load_agent = await mcp.call_tool("load_memory", {"topic": mcp_agent})  # default scope=agent
    if results.expect_eq("C.load_memory.agent_default.success", load_agent.get("success"), True):
        results.expect_eq(
            "C.load_memory.agent_default.subject",
            load_agent.get("subject"),
            TEST_AGENT_NAME,
        )

    # ------------------------------------------------------------------
    # C5. search_memory + list_memory happy paths.
    # ------------------------------------------------------------------
    search_res = await mcp.call_tool("search_memory", {"query": "mcp topic body"})
    if results.expect_eq("C.search_memory.success", search_res.get("success"), True):
        slugs = {r.get("slug") for r in search_res.get("results", [])}
        results.expect("C.search_memory.hits_topic_slug", mcp_topic in slugs, detail=str(slugs))

    list_res = await mcp.call_tool("list_memory", {})
    if results.expect_eq("C.list_memory.success", list_res.get("success"), True):
        slugs = {r.get("slug") for r in list_res.get("memories", [])}
        for s in (mcp_topic, mcp_user, mcp_agent, mcp_user_default):
            results.expect(f"C.list_memory.contains[{s}]", s in slugs)

    # ------------------------------------------------------------------
    # C6. Cross-user / cross-agent reads → success=False.
    # ------------------------------------------------------------------
    cross_user = await mcp.call_tool(
        "load_memory",
        {"topic": "anything", "scope": "user", "subject": OTHER_USER_ID},
    )
    results.expect_eq("C.load_memory.cross_user.success", cross_user.get("success"), False)

    cross_agent = await mcp.call_tool(
        "load_memory",
        {"topic": "anything", "scope": "agent", "subject": OTHER_AGENT_NAME},
    )
    results.expect_eq("C.load_memory.cross_agent.success", cross_agent.get("success"), False)

    cross_user_search = await mcp.call_tool(
        "search_memory",
        {"query": "x", "scope": "user", "subject": OTHER_USER_ID},
    )
    results.expect_eq("C.search_memory.cross_user.success", cross_user_search.get("success"), False)

    cross_user_list = await mcp.call_tool(
        "list_memory",
        {"scope": "user", "subject": OTHER_USER_ID},
    )
    results.expect_eq("C.list_memory.cross_user.success", cross_user_list.get("success"), False)

    # ------------------------------------------------------------------
    # C7. Cross-scope WRITE rejection: success=False with "Cross-scope write rejected:".
    # ------------------------------------------------------------------
    write_rejected = await mcp.call_tool(
        "save_memory",
        {
            "topic": f"{slug_prefix}_must_fail",
            "content": "x",
            "scope": "user",
            "subject": OTHER_USER_ID,
        },
    )
    results.expect_eq("C.save_memory.cross_user.success", write_rejected.get("success"), False)
    results.expect(
        "C.save_memory.cross_user.error_prefix",
        "Cross-scope write rejected:" in str(write_rejected.get("error", "")),
        detail=f"got error={write_rejected.get('error')!r}",
    )

    write_rejected_agent = await mcp.call_tool(
        "save_memory",
        {
            "topic": f"{slug_prefix}_must_fail_agent",
            "content": "x",
            "scope": "agent",
            "subject": OTHER_AGENT_NAME,
        },
    )
    results.expect_eq("C.save_memory.cross_agent.success", write_rejected_agent.get("success"), False)
    results.expect(
        "C.save_memory.cross_agent.error_prefix",
        "Cross-scope write rejected:" in str(write_rejected_agent.get("error", "")),
        detail=f"got error={write_rejected_agent.get('error')!r}",
    )


# ---------------------------------------------------------------------------
# Phase D: GCS-isolation regression check (offline grep)
# ---------------------------------------------------------------------------


def phase_d_grep(results: Results, repo_root: Path) -> None:
    results.section("PHASE D — GCS-isolation regression grep")

    # Walk the repo's source tree once. We only look under known source
    # directories to avoid scanning ``.venv``, ``node_modules``, etc.
    scan_roots = [
        repo_root / "ypl",
        repo_root / "scripts",
        repo_root / "apps",
        repo_root / "tools",
    ]

    files: list[Path] = []
    for root in scan_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root).as_posix()
            if any(rel.startswith(prefix) for prefix in GREP_EXCLUDE_PREFIXES):
                continue
            # Only scan sources we expect to host these symbols.
            if path.suffix not in (".py", ".md", ".yml", ".yaml", ".json", ".txt", ".sh"):
                continue
            files.append(path)

    # Build a regex that matches any of the legacy symbols (word boundaries).
    pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in LEGACY_SYMBOLS) + r")\b")

    hits_per_symbol: dict[str, list[str]] = {sym: [] for sym in LEGACY_SYMBOLS}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(repo_root).as_posix()
        for m in pattern.finditer(text):
            sym = m.group(1)
            hits_per_symbol[sym].append(rel)

    for sym in LEGACY_SYMBOLS:
        hits = sorted(set(hits_per_symbol[sym]))
        results.expect(
            f"D.legacy_symbol_zero_hits[{sym}]",
            len(hits) == 0,
            detail=f"hits in: {hits[:5]}{'…' if len(hits) > 5 else ''}" if hits else "",
        )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def cleanup_artifacts(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    created: list[CreatedSlug],
) -> tuple[int, int]:
    """Best-effort archive of every slug we created. Returns (deleted, errors)."""

    headers = _api_headers(api_key, caller=False)  # admin
    deleted = 0
    errors = 0
    # Deduplicate (slug, scope, subject).
    unique = {(c.slug, c.scope, c.subject) for c in created}
    for slug, scope, subject in unique:
        params: dict[str, Any] = {"type": "MEMORY", "scope": scope}
        if subject is not None:
            params["subject"] = subject
        try:
            r = await client.delete(f"{base}/by-slug/{slug}", headers=headers, params=params)
            if r.status_code in (200, 204, 404):
                deleted += 1
            else:
                errors += 1
                print(f"  cleanup: {slug} → HTTP {r.status_code}: {r.text[:120]}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"  cleanup: {slug} → {type(exc).__name__}: {exc}", flush=True)
    return deleted, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _open_client(timeout: float):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(timeout=timeout) as client:
        yield client


def _resolve_repo_root() -> Path:
    """Repo root = parent of ``scripts/`` since this file lives there."""
    return Path(__file__).resolve().parent.parent


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("AHS_API_KEY", "")
    if not api_key:
        print("ERROR: AHS_API_KEY environment variable is required.", file=sys.stderr)
        return 1

    db_url = os.environ.get("MEMORY_E2E_DB_URL", DEFAULT_DB_URL)
    ahs_host = os.environ.get("AHS_HOST", DEFAULT_AHS_HOST)
    dev_token = os.environ.get("MCP_DEV_TOKEN", "")

    slug_prefix = f"e2e_memory_{int(time.time())}"
    print(f"Slug prefix for this run: {slug_prefix}")
    print(f"AHS host:                {ahs_host}")
    print(f"Postgres URL:            {_redact_db_url(db_url)}")
    print(f"MCP dev token:           {'<provided>' if dev_token else '<missing — phase C will skip>'}")

    results = Results(verbose=True)
    base = _api_base(ahs_host)
    created: list[CreatedSlug] = []

    try:
        # Phase A — DB sanity.
        if args.skip_db:
            results.skip_phase("PHASE A", "--skip-db requested")
        else:
            await phase_a_db(results, db_url)

        async with _open_client(timeout=args.timeout) as client:
            # Phase B — REST surface.
            if args.skip_rest:
                results.skip_phase("PHASE B", "--skip-rest requested")
            else:
                await phase_b_rest(results, client, base, api_key, slug_prefix, created)

            # Phase C — MCP tools.
            if args.skip_mcp:
                results.skip_phase("PHASE C", "--skip-mcp requested")
            elif not dev_token:
                results.skip_phase("PHASE C", "MCP_DEV_TOKEN not set")
            else:
                await phase_c_mcp(results, client, ahs_host, dev_token, slug_prefix, base, api_key, created)

            # Cleanup before printing summary so DB stays clean even on failure.
            if not args.keep_artifacts and created:
                results.section("CLEANUP — archiving e2e_* slugs")
                deleted, errors = await cleanup_artifacts(client, base, api_key, created)
                print(f"  archived {deleted} slugs ({errors} errors)", flush=True)

        # Phase D — offline grep (no network needed).
        if args.skip_grep:
            results.skip_phase("PHASE D", "--skip-grep requested")
        else:
            phase_d_grep(results, _resolve_repo_root())

    except Exception as exc:
        print(f"\nFATAL: phase raised {type(exc).__name__}: {exc}", file=sys.stderr)
        # Always try to clean up before exiting on a fatal error.
        if not args.keep_artifacts and created:
            try:
                async with _open_client(timeout=args.timeout) as client:
                    await cleanup_artifacts(client, base, api_key, created)
            except Exception:
                pass
        print(results.summary())
        return 1

    print(results.summary())
    return 0 if results.n_failed == 0 else 1


def _redact_db_url(url: str) -> str:
    """Strip credentials from the printed URL (helps when pasting logs)."""
    return re.sub(r"://[^@]*@", "://<redacted>@", url)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="End-to-end regression harness for the memory-as-artifact pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip phase A (DB sanity / asyncpg).",
    )
    parser.add_argument(
        "--skip-rest",
        action="store_true",
        help="Skip phase B (REST surface).",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Skip phase C (MCP tools). Implied when MCP_DEV_TOKEN is unset.",
    )
    parser.add_argument(
        "--skip-grep",
        action="store_true",
        help="Skip phase D (offline regression grep).",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Don't archive the test slugs at the end (useful for debugging a failure).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15).",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
