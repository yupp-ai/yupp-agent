"""Integration tests for the Linear ↔ AHS sync pipeline.

These tests exercise the full round-trip through the real Linear GraphQL API
and the real application database.  They are automatically skipped when
``LINEAR_API_KEY`` is not configured (CI without credentials, local dev without
a `.env`).

Each test creates temporary Linear projects / issues and AHS project / task
rows, then deletes / archives them in a try/finally block so that cleanup is
guaranteed even on test failure.

Run manually::

    pytest tests/agent_harness_service/linear_sync/test_integration.py -v -s

"""

from __future__ import annotations
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Skip guard — skip the entire module if credentials are not available
# ---------------------------------------------------------------------------

_linear_skip_reason = "LINEAR_API_KEY not configured — skipping integration tests"


def _linear_available() -> bool:
    """Return True only if LinearClient can be instantiated (key present)."""
    try:
        from ypl.backend.config import settings

        return bool(settings.LINEAR_API_KEY)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _linear_available(), reason=_linear_skip_reason)


# ---------------------------------------------------------------------------
# Test prefix — every test object uses this so we can identify and clean up
# ---------------------------------------------------------------------------

TEST_PREFIX = "[INTEGRATION-TEST]"


# ---------------------------------------------------------------------------
# GraphQL helpers for cleanup (archive / delete)
# ---------------------------------------------------------------------------


def _archive_linear_project(client: Any, project_id: str) -> None:
    """Archive a Linear project via the projectArchive mutation."""
    mutation = """
        mutation ArchiveProject($id: String!) {
            projectArchive(id: $id) {
                success
            }
        }
    """
    try:
        client.post({"query": mutation, "variables": {"id": project_id}})
    except Exception as exc:
        import warnings

        warnings.warn(f"Failed to archive Linear project {project_id}: {exc}", stacklevel=2)


def _delete_linear_issue(client: Any, issue_id: str) -> None:
    """Delete a Linear issue via the issueDelete mutation."""
    mutation = """
        mutation DeleteIssue($id: String!) {
            issueDelete(id: $id) {
                success
            }
        }
    """
    try:
        client.post({"query": mutation, "variables": {"id": issue_id}})
    except Exception as exc:
        import warnings

        warnings.warn(f"Failed to delete Linear issue {issue_id}: {exc}", stacklevel=2)


def _create_linear_project_with_issues(
    client: Any,
    team_id: str,
    project_name: str,
    issues: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Create a Linear project and issues, return (project_id, [issue_ids]).

    Args:
        client: LinearClient instance.
        team_id: Linear team UUID.
        project_name: Display name for the project.
        issues: List of dicts with ``title``, ``description``, ``priority``,
            and optional ``parent_idx`` (0-based index into *issues* list for
            setting the parent issue).

    Returns:
        ``(linear_project_id, [linear_issue_id, ...])`` in creation order.
    """
    create_resp = client.create_project(name=project_name, team_ids=[team_id])
    errors = create_resp.get("errors")
    if errors:
        raise RuntimeError(f"create_project failed: {errors}")
    proj = create_resp["data"]["projectCreate"]["project"]
    linear_project_id: str = proj["id"]

    created_ids: list[str] = []
    try:
        for spec in issues:
            parent_id: str | None = None
            parent_idx = spec.get("parent_idx")
            if parent_idx is not None:
                parent_id = created_ids[parent_idx]
            resp = client.create_issue(
                title=spec["title"],
                team_id=team_id,
                description=spec.get("description", ""),
                priority=spec.get("priority", 0),
                project_id=linear_project_id,
                parent_id=parent_id,
            )
            errors = resp.get("errors")
            if errors:
                raise RuntimeError(f"create_issue failed: {errors}")
            issue = resp["data"]["issueCreate"]["issue"]
            created_ids.append(issue["id"])
    except Exception:
        # Rollback: archive the partially-created project to avoid leaking test data
        _archive_linear_project(client, linear_project_id)
        raise

    return linear_project_id, created_ids


def _add_blocked_by_relation(client: Any, issue_id: str, blocker_id: str) -> None:
    """Create a ``blocked`` relation from *issue_id* to *blocker_id*."""
    mutation = """
        mutation CreateIssueRelation($input: IssueRelationCreateInput!) {
            issueRelationCreate(input: $input) {
                success
            }
        }
    """
    client.post(
        {
            "query": mutation,
            "variables": {
                "input": {
                    "issueId": issue_id,
                    "relatedIssueId": blocker_id,
                    "type": "blocked",
                }
            },
        }
    )


async def _delete_ahs_project(project_id: uuid.UUID) -> None:
    """Soft-delete an AHS project and all its tasks by setting deleted_at."""
    from sqlmodel import col, select
    from ypl.backend.db import get_async_session
    from ypl.db.agent_harness import AgentProject, AgentTask

    now = datetime.now(UTC)
    async with get_async_session() as db:
        task_result = await db.execute(
            select(AgentTask)
            .where(col(AgentTask.agent_project_id) == project_id)
            .where(col(AgentTask.deleted_at).is_(None))
        )
        for task in task_result.scalars().all():
            task.deleted_at = now
            db.add(task)

        proj_result = await db.execute(
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == project_id)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        project = proj_result.scalars().first()
        if project:
            project.deleted_at = now
            db.add(project)

        await db.commit()


# ---------------------------------------------------------------------------
# Helper: run async coroutines synchronously in tests
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously (test helper)."""
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def linear_client() -> Any:
    """Instantiate a real LinearClient (skips if key missing)."""
    from ypl.backend.utils.linear import LinearClient

    try:
        return LinearClient()
    except ValueError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def linear_team_id(linear_client: Any) -> str:
    """Return the first available Linear team ID.

    Prefers teams whose name or key contains "test" or "sandbox".
    Skips if no teams are accessible.
    """
    teams = linear_client.get_teams()
    nodes = teams.get("data", {}).get("teams", {}).get("nodes", [])
    if not nodes:
        pytest.skip("No Linear teams accessible — cannot run integration tests")
    for team in nodes:
        name_lower = (team.get("name") or "").lower()
        key_lower = (team.get("key") or "").lower()
        if "test" in name_lower or "sandbox" in name_lower or "test" in key_lower:
            return str(team["id"])
    pytest.skip("No test/sandbox team found — refusing to run integration tests against production teams")


# ---------------------------------------------------------------------------
# Test 1: Create Linear project → import to AHS → verify data matches
# ---------------------------------------------------------------------------


class TestImportFromLinear:
    """Linear → AHS import integration tests."""

    def test_basic_import_data_matches(self, linear_client: Any, linear_team_id: str) -> None:
        """Import a simple Linear project into AHS and verify title / description."""
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear

        project_name = f"{TEST_PREFIX} Import Basic {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            linear_project_id, _ = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[
                    {"title": "Task Alpha", "description": "Alpha description", "priority": 2},
                    {"title": "Task Beta", "description": "Beta description", "priority": 4},
                ],
            )

            project, sync_result = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id

            # Project metadata matches
            assert project.name == project_name
            assert project.project_data is not None
            linear_ref = project.project_data.get("linear_ref") or {}
            assert linear_ref.get("linear_project_id") == linear_project_id
            assert linear_ref.get("linear_team_id") == linear_team_id

            # All issues imported, no errors
            assert sync_result.created == 2
            assert sync_result.errors == 0

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_import_maps_priority_correctly(self, linear_client: Any, linear_team_id: str) -> None:
        """Imported issues preserve Linear priority → AHS priority mapping."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentTask, AgentTaskPriority

        project_name = f"{TEST_PREFIX} Import Priority {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            linear_project_id, _ = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[
                    {"title": "Urgent Task", "priority": 1},  # Linear 1 → AHS URGENT
                    {"title": "High Task", "priority": 2},  # Linear 2 → AHS HIGH
                    {"title": "Medium Task", "priority": 3},  # Linear 3 → AHS MEDIUM
                    {"title": "Low Task", "priority": 4},  # Linear 4 → AHS LOW
                    {"title": "No Prio Task", "priority": 0},  # Linear 0 → AHS NO_PRIORITY
                ],
            )

            project, _ = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id

            async def _fetch_priorities() -> dict[str, AgentTaskPriority]:
                async with get_async_session() as db:
                    result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    return {t.title: t.priority for t in result.scalars().all()}

            priority_map = _run(_fetch_priorities())

            assert priority_map["Urgent Task"] == AgentTaskPriority.URGENT
            assert priority_map["High Task"] == AgentTaskPriority.HIGH
            # "MEDIUM" is not in AgentTaskPriority enum, falls back to NORMAL
            assert priority_map["Medium Task"] == AgentTaskPriority.NORMAL
            assert priority_map["Low Task"] == AgentTaskPriority.LOW
            # "NO_PRIORITY" is not in AgentTaskPriority enum, falls back to NORMAL
            assert priority_map["No Prio Task"] == AgentTaskPriority.NORMAL

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_incremental_sync_picks_up_new_issues(self, linear_client: Any, linear_team_id: str) -> None:
        """sync_tasks_from_linear() picks up issues added after initial import."""
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import (
            import_project_from_linear,
            sync_tasks_from_linear,
        )

        project_name = f"{TEST_PREFIX} Incremental Sync {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        new_issue_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            linear_project_id, _ = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[{"title": "Original Issue", "priority": 3}],
            )

            project, first_sync = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id
            assert first_sync.created == 1

            # Add a second issue to Linear after the initial import
            resp = linear_client.create_issue(
                title="New Issue Post-Import",
                team_id=linear_team_id,
                project_id=linear_project_id,
            )
            new_issue_id = resp["data"]["issueCreate"]["issue"]["id"]

            # Use a watermark far enough in the past to guarantee the new issue is included.
            since = datetime.now(UTC) - timedelta(hours=1)
            incremental = _run(sync_tasks_from_linear(ahs_project_id, since=since))

            # The new issue should be created; the original may be updated or skipped.
            assert incremental.created >= 1
            assert incremental.errors == 0

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if new_issue_id:
                _delete_linear_issue(linear_client, new_issue_id)
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)


# ---------------------------------------------------------------------------
# Test 2: Create AHS project → export to Linear → verify via Linear API
# ---------------------------------------------------------------------------


class TestExportToLinear:
    """AHS → Linear export integration tests."""

    def test_basic_export_creates_linear_project_and_issues(self, linear_client: Any, linear_team_id: str) -> None:
        """Export an AHS project and verify the Linear project and issues exist."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Export Basic {uuid.uuid4().hex[:6]}"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        description="Integration test project",
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title="Export Task One",
                            description="First exported task",
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.HIGH,
                        )
                    )
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title="Export Task Two",
                            description="Second exported task",
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.LOW,
                        )
                    )
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())

            linear_project_id, sync_result = _run(
                export_project_to_linear(
                    project_id=str(ahs_project_id),
                    linear_team_id=linear_team_id,
                )
            )

            assert sync_result.created == 2
            assert sync_result.errors == 0
            assert linear_project_id

            # Verify Linear project name
            proj_resp = linear_client.get_project(linear_project_id)
            linear_proj = proj_resp.get("data", {}).get("project") or {}
            assert linear_proj.get("name") == project_name

            # Verify issue titles
            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            titles = {i["title"] for i in issues}
            assert "Export Task One" in titles
            assert "Export Task Two" in titles

            # Verify AHS task_data was updated with Linear refs
            async def _check_refs() -> list[dict[str, Any]]:
                async with get_async_session() as db:
                    result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    return [t.task_data or {} for t in result.scalars().all()]

            task_data_list = _run(_check_refs())
            for td in task_data_list:
                linear_ref = td.get("linear_ref", {})
                assert "linear_issue_id" in linear_ref, f"task_data.linear_ref missing linear_issue_id: {td}"
                assert "linear_identifier" in linear_ref, f"task_data.linear_ref missing linear_identifier: {td}"

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_export_idempotent_on_second_call(self, linear_client: Any, linear_team_id: str) -> None:
        """Calling export twice does not duplicate Linear issues."""
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Export Idempotent {uuid.uuid4().hex[:6]}"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        description="Idempotency test",
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title="Idempotent Task",
                            description="Should only appear once in Linear",
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.NORMAL,
                        )
                    )
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())

            # First export
            linear_project_id, first_result = _run(
                export_project_to_linear(
                    project_id=str(ahs_project_id),
                    linear_team_id=linear_team_id,
                )
            )
            assert first_result.created == 1

            # Second export — must update, not create
            _, second_result = _run(
                export_project_to_linear(
                    project_id=str(ahs_project_id),
                    linear_team_id=linear_team_id,
                )
            )
            assert second_result.created == 0
            assert second_result.updated == 1
            assert second_result.errors == 0

            # Exactly one issue in Linear
            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            assert len(issues) == 1

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)


# ---------------------------------------------------------------------------
# Test 3: Bidirectional sync + conflict resolution
# ---------------------------------------------------------------------------


class TestBidirectionalSync:
    """Bidirectional sync conflict-resolution integration tests."""

    def test_latest_wins_linear_edit_beats_stale_ahs(self, linear_client: Any, linear_team_id: str) -> None:
        """LATEST_WINS: Linear change (newer) wins over unchanged AHS title."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.bidirectional import (
            ConflictResolution,
            sync_bidirectional,
        )
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Bidi Latest Wins {uuid.uuid4().hex[:6]}"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        description="Bidirectional test",
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title="Bidi Task Original",
                            description="Original description",
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.NORMAL,
                        )
                    )
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())
            linear_project_id, _ = _run(export_project_to_linear(str(ahs_project_id), linear_team_id))

            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            assert len(issues) == 1
            linear_issue_id = issues[0]["id"]

            # Modify Linear side (becomes "newer" than the AHS sync watermark)
            linear_client.update_issue(
                issue_id=linear_issue_id,
                title="Bidi Task Updated In Linear",
                description="Updated by Linear side",
            )

            result = _run(
                sync_bidirectional(
                    project_id=ahs_project_id,
                    conflict_resolution=ConflictResolution.LATEST_WINS,
                )
            )
            assert result.errors == 0

            async def _get_task_title() -> str:
                async with get_async_session() as db:
                    task_result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    task = task_result.scalars().first()
                    return task.title if task else ""

            assert _run(_get_task_title()) == "Bidi Task Updated In Linear"

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_ahs_wins_overrides_linear_changes(self, linear_client: Any, linear_team_id: str) -> None:
        """AHS_WINS: AHS title is always pushed to Linear, overwriting Linear edits."""
        from ypl.agent_harness_service.tools.linear_sync.bidirectional import (
            ConflictResolution,
            sync_bidirectional,
        )
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Bidi AHS Wins {uuid.uuid4().hex[:6]}"
        ahs_title = "AHS Wins Task"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title=ahs_title,
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.NORMAL,
                        )
                    )
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())
            linear_project_id, _ = _run(export_project_to_linear(str(ahs_project_id), linear_team_id))

            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            linear_issue_id = issues[0]["id"]
            linear_client.update_issue(issue_id=linear_issue_id, title="Linear Changed This Title")

            result = _run(
                sync_bidirectional(
                    project_id=ahs_project_id,
                    conflict_resolution=ConflictResolution.AHS_WINS,
                )
            )
            assert result.errors == 0

            # Linear issue should have the AHS title restored
            refreshed = linear_client.get_issue(linear_issue_id)
            linear_title = refreshed.get("data", {}).get("issue", {}).get("title", "")
            assert linear_title == ahs_title

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_linear_wins_always_overwrites_ahs(self, linear_client: Any, linear_team_id: str) -> None:
        """LINEAR_WINS: Linear title always replaces the AHS task title."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.bidirectional import (
            ConflictResolution,
            sync_bidirectional,
        )
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Bidi Linear Wins {uuid.uuid4().hex[:6]}"
        linear_updated_title = "Linear Wins This Round"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id
                    db.add(
                        AgentTask(
                            agent_project_id=pid,
                            title="Original AHS Title",
                            status=AgentTaskStatus.READY,
                            priority=AgentTaskPriority.NORMAL,
                        )
                    )
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())
            linear_project_id, _ = _run(export_project_to_linear(str(ahs_project_id), linear_team_id))

            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            linear_issue_id = issues[0]["id"]
            linear_client.update_issue(issue_id=linear_issue_id, title=linear_updated_title)

            result = _run(
                sync_bidirectional(
                    project_id=ahs_project_id,
                    conflict_resolution=ConflictResolution.LINEAR_WINS,
                )
            )
            assert result.errors == 0

            async def _get_title() -> str:
                async with get_async_session() as db:
                    task_result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    task = task_result.scalars().first()
                    return task.title if task else ""

            assert _run(_get_title()) == linear_updated_title

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)


# ---------------------------------------------------------------------------
# Test 4: Subtask hierarchy preservation
# ---------------------------------------------------------------------------


class TestSubtaskHierarchy:
    """Verify parent-child subtask relationships survive import and export."""

    def test_import_preserves_parent_child_hierarchy(self, linear_client: Any, linear_team_id: str) -> None:
        """Importing a Linear project with sub-issues sets parent_task_id on AHS tasks."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentTask

        project_name = f"{TEST_PREFIX} Hierarchy Import {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            # Parent at index 0; children at indices 1 and 2 via parent_idx
            linear_project_id, _ = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[
                    {"title": "Parent Task", "description": "Root task"},
                    {"title": "Child Task A", "description": "First subtask", "parent_idx": 0},
                    {"title": "Child Task B", "description": "Second subtask", "parent_idx": 0},
                ],
            )

            project, sync_result = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id
            assert sync_result.created == 3
            assert sync_result.errors == 0

            async def _fetch_tasks() -> list[AgentTask]:
                async with get_async_session() as db:
                    result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    return list(result.scalars().all())

            tasks = _run(_fetch_tasks())
            by_title = {t.title: t for t in tasks}

            assert "Parent Task" in by_title
            assert "Child Task A" in by_title
            assert "Child Task B" in by_title

            parent = by_title["Parent Task"]
            child_a = by_title["Child Task A"]
            child_b = by_title["Child Task B"]

            assert parent.parent_task_id is None
            assert child_a.parent_task_id == parent.agent_task_id
            assert child_b.parent_task_id == parent.agent_task_id

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_export_preserves_parent_child_hierarchy(self, linear_client: Any, linear_team_id: str) -> None:
        """Exporting AHS tasks with parent_task_id creates Linear sub-issues."""
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Hierarchy Export {uuid.uuid4().hex[:6]}"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id

                    parent = AgentTask(
                        agent_project_id=pid,
                        title="AHS Parent",
                        description="Root level task",
                        status=AgentTaskStatus.READY,
                        priority=AgentTaskPriority.HIGH,
                    )
                    db.add(parent)
                    await db.flush()

                    child = AgentTask(
                        agent_project_id=pid,
                        title="AHS Child",
                        description="Subtask",
                        status=AgentTaskStatus.READY,
                        priority=AgentTaskPriority.NORMAL,
                        parent_task_id=parent.agent_task_id,
                    )
                    db.add(child)
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())

            linear_project_id, sync_result = _run(export_project_to_linear(str(ahs_project_id), linear_team_id))
            assert sync_result.errors == 0

            # Verify issues exist in Linear
            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            by_title = {i["title"]: i for i in issues}

            assert "AHS Parent" in by_title
            assert "AHS Child" in by_title

            # list_project_issues returns `parent { id }` — verify it is set on child
            parent_issue_id = by_title["AHS Parent"]["id"]
            child_from_list = by_title["AHS Child"]
            parent_field = child_from_list.get("parent")

            # The child issue must have a parent reference after export
            assert parent_field is not None, "The parent field for the child issue should not be None"
            assert parent_field["id"] == parent_issue_id, (
                f"Child issue parent_id={parent_field['id']} != expected {parent_issue_id}"
            )

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)


# ---------------------------------------------------------------------------
# Test 5: Dependency graph preservation
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    """Verify blocker/blocked dependency relationships survive import and export."""

    def test_import_preserves_blocked_by_dependencies(self, linear_client: Any, linear_team_id: str) -> None:
        """Import a project whose issues have blocked_by relations."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentTask, AgentTaskStatus

        project_name = f"{TEST_PREFIX} Deps Import {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            # Create blocker first, then the blocked issue
            linear_project_id, issue_ids = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[
                    {"title": "Blocker Task", "description": "Must finish first"},
                    {"title": "Blocked Task", "description": "Cannot start yet"},
                ],
            )
            blocker_id, blocked_id = issue_ids[0], issue_ids[1]

            # blocked_id is blocked_by blocker_id
            _add_blocked_by_relation(linear_client, blocked_id, blocker_id)

            project, sync_result = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id
            assert sync_result.created == 2
            assert sync_result.errors == 0

            async def _fetch_tasks() -> list[AgentTask]:
                async with get_async_session() as db:
                    result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    return list(result.scalars().all())

            tasks = _run(_fetch_tasks())
            by_title = {t.title: t for t in tasks}

            blocker_task = by_title["Blocker Task"]
            blocked_task = by_title["Blocked Task"]

            # Blocked task depends on the blocker task
            assert blocked_task.depends_on is not None
            assert str(blocker_task.agent_task_id) in blocked_task.depends_on

            # Blocked task starts as BLOCKED (has unmet deps)
            assert blocked_task.status == AgentTaskStatus.BLOCKED

            # Blocker has no deps → not BLOCKED
            assert blocker_task.status != AgentTaskStatus.BLOCKED
            assert not blocker_task.depends_on

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_export_preserves_dependency_graph(self, linear_client: Any, linear_team_id: str) -> None:
        """Exporting AHS tasks with depends_on sets blocked_by relations in Linear."""
        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import export_project_to_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentProject, AgentProjectStatus, AgentTask, AgentTaskPriority, AgentTaskStatus

        ahs_project_id: uuid.UUID | None = None
        linear_project_id: str | None = None
        project_name = f"{TEST_PREFIX} Deps Export {uuid.uuid4().hex[:6]}"

        try:

            async def _setup() -> uuid.UUID:
                async with get_async_session() as db:
                    project = AgentProject(
                        name=project_name,
                        status=AgentProjectStatus.PAUSED,
                        creator_user_id="test-integration-user",
                    )
                    db.add(project)
                    await db.flush()
                    pid = project.agent_project_id

                    blocker = AgentTask(
                        agent_project_id=pid,
                        title="AHS Blocker",
                        description="Must complete first",
                        status=AgentTaskStatus.READY,
                        priority=AgentTaskPriority.HIGH,
                    )
                    db.add(blocker)
                    await db.flush()

                    blocked = AgentTask(
                        agent_project_id=pid,
                        title="AHS Blocked",
                        description="Depends on blocker",
                        status=AgentTaskStatus.BLOCKED,
                        priority=AgentTaskPriority.NORMAL,
                        depends_on=[str(blocker.agent_task_id)],
                    )
                    db.add(blocked)
                    await db.commit()
                    return pid

            ahs_project_id = _run(_setup())

            linear_project_id, sync_result = _run(export_project_to_linear(str(ahs_project_id), linear_team_id))
            assert sync_result.errors == 0
            assert sync_result.created == 2

            # Verify blocked_by relation in Linear
            issues = linear_client.list_project_issues(linear_project_id, include_completed=True)
            by_title = {i["title"]: i for i in issues}

            assert "AHS Blocker" in by_title
            assert "AHS Blocked" in by_title

            blocked_issue = by_title["AHS Blocked"]
            blocker_issue = by_title["AHS Blocker"]

            # list_project_issues includes relations.nodes with type/relatedIssue
            blocked_relations = blocked_issue.get("relations", {}).get("nodes", [])
            blocker_ids_in_relations = [
                rel.get("relatedIssue", {}).get("id") for rel in blocked_relations if rel.get("type") == "blocked_by"
            ]
            assert blocker_issue["id"] in blocker_ids_in_relations, (
                f"AHS Blocked has no blocked_by relation pointing to AHS Blocker. Relations found: {blocked_relations}"
            )

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)

    def test_topological_order_multi_level_deps(self, linear_client: Any, linear_team_id: str) -> None:
        """Import a three-task chain A→B→C and verify correct dependency ordering."""
        from sqlmodel import col, select
        from ypl.agent_harness_service.tools.linear_sync.import_from_linear import import_project_from_linear
        from ypl.backend.db import get_async_session
        from ypl.db.agent_harness import AgentTask, AgentTaskStatus

        project_name = f"{TEST_PREFIX} Deps Chain {uuid.uuid4().hex[:6]}"
        linear_project_id: str | None = None
        ahs_project_id: uuid.UUID | None = None

        try:
            linear_project_id, issue_ids = _create_linear_project_with_issues(
                client=linear_client,
                team_id=linear_team_id,
                project_name=project_name,
                issues=[
                    {"title": "Task A (root)"},
                    {"title": "Task B (mid)"},
                    {"title": "Task C (leaf)"},
                ],
            )
            task_a_id, task_b_id, task_c_id = issue_ids

            # B is blocked by A; C is blocked by B
            _add_blocked_by_relation(linear_client, task_b_id, task_a_id)
            _add_blocked_by_relation(linear_client, task_c_id, task_b_id)

            project, sync_result = _run(
                import_project_from_linear(
                    linear_project_id=linear_project_id,
                    linear_team_id=linear_team_id,
                    creator_user_id="test-integration-user",
                    include_completed=True,
                )
            )
            ahs_project_id = project.agent_project_id
            assert sync_result.created == 3

            async def _fetch_tasks() -> list[AgentTask]:
                async with get_async_session() as db:
                    result = await db.execute(
                        select(AgentTask)
                        .where(col(AgentTask.agent_project_id) == ahs_project_id)
                        .where(col(AgentTask.deleted_at).is_(None))
                    )
                    return list(result.scalars().all())

            tasks = _run(_fetch_tasks())
            by_title = {t.title: t for t in tasks}

            task_a = by_title["Task A (root)"]
            task_b = by_title["Task B (mid)"]
            task_c = by_title["Task C (leaf)"]

            # A: no deps, not BLOCKED
            assert not task_a.depends_on
            assert task_a.status != AgentTaskStatus.BLOCKED

            # B depends on A
            assert task_b.depends_on is not None
            assert str(task_a.agent_task_id) in task_b.depends_on
            assert task_b.status == AgentTaskStatus.BLOCKED

            # C depends on B
            assert task_c.depends_on is not None
            assert str(task_b.agent_task_id) in task_c.depends_on
            assert task_c.status == AgentTaskStatus.BLOCKED

        finally:
            if ahs_project_id:
                _run(_delete_ahs_project(ahs_project_id))
            if linear_project_id:
                _archive_linear_project(linear_client, linear_project_id)
