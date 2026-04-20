"""Agent Projects — view and manage agent projects and tasks."""

from __future__ import annotations
import html
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import col, select
from ypl.agent_harness_service.projects.task_utils import TERMINAL_TASK_STATUSES, complete_task, restart_task
from ypl.agent_harness_service.task_executor import (
    RESUMABLE_ERROR_SUBTYPES,
    resolve_task_dependencies,
    resume_task,
)
from ypl.backend.db import get_async_session, get_async_session_read_replica, retry_db
from ypl.backend.llm.constants import TEAM_DIRECTORY
from ypl.backend.utils.streamlit_utils import run_coroutine_in_lit_worker
from ypl.db.agent_harness import (
    Agent,
    AgentArtifact,
    AgentArtifactType,
    AgentProject,
    AgentProjectStatus,
    AgentSession,
    AgentSessionStatus,
    AgentTask,
    AgentTaskPriority,
    AgentTaskStatus,
)
from ypl.db.users import User
from ypl.streamlit_server.auth import require_auth
from ypl.streamlit_server.permissions import get_current_user_email
from ypl.structured_logger import get_logger

# Linear workspace slug used to build project URLs
_LINEAR_WORKSPACE_SLUG = "yupp"
_LINEAR_TEAM_ID_YUP = "75eb453f-2fed-478c-989e-e96e2cf1024d"

logger = get_logger()


def _internal_link(text: str, url: str) -> str:
    """Return an HTML anchor that navigates in the same tab (target=_self)."""
    return f'<a href="{html.escape(url, quote=True)}" target="_self">{text}</a>'


st.set_page_config(page_title="Agent Projects", page_icon="📁", layout="wide")
require_auth()

_current_email = get_current_user_email()
_current_username = _current_email.split("@")[0] if _current_email else None

# ── Timezone helper ──────────────────────────────────────────────────────────

_PACIFIC = ZoneInfo("America/Los_Angeles")
_DT_MIN_UTC = datetime.min.replace(tzinfo=UTC)


def _to_local(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(_PACIFIC)
    return local.strftime(f"%Y-%m-%d %H:%M:%S {local.strftime('%Z')}")


def _time_ago(dt: datetime | None) -> str:
    """Return a human-readable relative time string like '3 min ago', '2 hr ago', '5 days ago'."""
    if dt is None:
        return ""
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return f"{months} mo ago"


# ── Status styling ───────────────────────────────────────────────────────────

_PROJECT_STATUS_EMOJI: dict[AgentProjectStatus, str] = {
    AgentProjectStatus.ACTIVE: "🟢",
    AgentProjectStatus.PAUSED: "⏸️",
    AgentProjectStatus.COMPLETED: "✅",
    AgentProjectStatus.ARCHIVED: "📦",
}

_TASK_STATUS_EMOJI: dict[AgentTaskStatus, str] = {
    AgentTaskStatus.PENDING: "⏳",
    AgentTaskStatus.BLOCKED: "🚫",
    AgentTaskStatus.READY: "▶️",
    AgentTaskStatus.IN_PROGRESS: "✍️",
    AgentTaskStatus.COMPLETED: "✅",
    AgentTaskStatus.FAILED: "❌",
    AgentTaskStatus.CANCELLED: "⚫",
    AgentTaskStatus.IN_REVIEW: "👤",
}

_TASK_PRIORITY_BADGE: dict[AgentTaskPriority, str] = {
    AgentTaskPriority.URGENT: "🔥",
    AgentTaskPriority.HIGH: "⬆️",
    AgentTaskPriority.NORMAL: "",
    AgentTaskPriority.LOW: "⬇️",
}

_TASK_PRIORITY_COLOR: dict[AgentTaskPriority, str] = {
    AgentTaskPriority.URGENT: "#D32F2F",
    AgentTaskPriority.HIGH: "#F57C00",
    AgentTaskPriority.NORMAL: "#757575",
    AgentTaskPriority.LOW: "#9E9E9E",
}

_TASK_STATUS_COLOR: dict[AgentTaskStatus, str] = {
    AgentTaskStatus.PENDING: "#E0E0E0",
    AgentTaskStatus.BLOCKED: "#BDBDBD",
    AgentTaskStatus.READY: "#BBDEFB",
    AgentTaskStatus.IN_PROGRESS: "#FFF9C4",
    AgentTaskStatus.COMPLETED: "#C8E6C9",
    AgentTaskStatus.FAILED: "#EF9A9A",
    AgentTaskStatus.CANCELLED: "#8B0000",
    AgentTaskStatus.IN_REVIEW: "#CE93D8",
}

# Statuses that need white text on dark backgrounds
_TASK_STATUS_LIGHT_TEXT: set[AgentTaskStatus] = {AgentTaskStatus.CANCELLED}

_SESSION_STATUS_EMOJI: dict[AgentSessionStatus, str] = {
    AgentSessionStatus.ACTIVE: "🟢",
    AgentSessionStatus.COMPLETED: "✅",
    AgentSessionStatus.STALE: "⚪",
}


# ── PR link helpers ───────────────────────────────────────────────────────────


def _get_pr_link_parts(result: dict[str, Any] | None) -> tuple[str, str] | None:
    """Extract and validate PR URL from task result, returning (safe_url, pr_label) or None."""
    pr_url = result.get("pr_url") if result else None
    if not isinstance(pr_url, str) or not pr_url.startswith("https://"):
        return None
    # Escape parentheses in URL for markdown link syntax
    safe_url = pr_url.replace(")", "%29")
    # Extract PR number, stripping query strings and fragments
    pr_number = ""
    if "/pull/" in pr_url:
        pr_number = pr_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    pr_label = f"PR #{pr_number}" if pr_number.isdigit() else "Pull Request"
    return safe_url, pr_label


# ── DB queries ───────────────────────────────────────────────────────────────


@retry_db
async def fetch_all_projects(
    status: str | None = None,
    creator_user_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch projects with optional filters and task counts."""
    async with get_async_session_read_replica() as session:
        task_count_subq = (
            select(
                col(AgentTask.agent_project_id),
                func.count(col(AgentTask.agent_task_id)).label("task_count"),
                func.count(col(AgentTask.agent_task_id))
                .filter(col(AgentTask.status) == AgentTaskStatus.COMPLETED)
                .label("completed_count"),
            )
            .where(col(AgentTask.deleted_at).is_(None))
            .group_by(col(AgentTask.agent_project_id))
            .subquery()
        )

        query = (
            select(
                AgentProject,
                func.coalesce(task_count_subq.c.task_count, 0).label("task_count"),
                func.coalesce(task_count_subq.c.completed_count, 0).label("completed_count"),
            )
            .outerjoin(task_count_subq, col(AgentProject.agent_project_id) == task_count_subq.c.agent_project_id)
            .where(col(AgentProject.deleted_at).is_(None))
        )

        if status and status != "(all)":
            query = query.where(col(AgentProject.status) == AgentProjectStatus(status))

        if creator_user_id:
            query = query.where(col(AgentProject.creator_user_id) == creator_user_id)

        query = query.order_by(col(AgentProject.created_at).desc()).limit(limit)

        result = await session.exec(query)
        rows = list(result.all())

    return [
        {
            "project": row[0],
            "task_count": row[1],
            "completed_count": row[2],
            "creator_name": row[0].creator_user_id or "—",
        }
        for row in rows
    ]


@retry_db
async def fetch_attention_tasks(creator_email: str) -> list[dict[str, Any]]:
    """Fetch in-review and in-progress tasks from projects owned by the given user.

    Returns a list of dicts with task and project info, grouped by project.
    """
    attention_statuses = [AgentTaskStatus.IN_REVIEW, AgentTaskStatus.IN_PROGRESS]
    async with get_async_session_read_replica() as session:
        # Look up user by email
        user_result = await session.exec(
            select(User.user_id).where(col(User.email) == creator_email).where(col(User.deleted_at).is_(None))
        )
        user_id = user_result.first()
        if not user_id:
            return []

        query = (
            select(AgentTask, AgentProject.name, AgentProject.agent_project_id)
            .join(AgentProject, col(AgentTask.agent_project_id) == col(AgentProject.agent_project_id))
            .where(col(AgentProject.creator_user_id) == str(user_id))
            .where(col(AgentProject.deleted_at).is_(None))
            .where(col(AgentProject.status) == AgentProjectStatus.ACTIVE)
            .where(col(AgentTask.deleted_at).is_(None))
            .where(col(AgentTask.status).in_(attention_statuses))
            .order_by(AgentProject.name, col(AgentTask.priority), col(AgentTask.created_at))
        )
        result = await session.exec(query)
        rows = list(result.all())

    return [
        {
            "task": row[0],
            "project_name": row[1],
            "project_id": str(row[2]),
        }
        for row in rows
    ]


@retry_db
async def fetch_project_with_tasks(project_id: uuid.UUID) -> dict[str, Any] | None:
    """Fetch a single project with all its tasks."""
    async with get_async_session_read_replica() as session:
        project_query = (
            select(AgentProject)
            .where(col(AgentProject.agent_project_id) == project_id)
            .where(col(AgentProject.deleted_at).is_(None))
        )
        project_result = await session.exec(project_query)
        project = project_result.one_or_none()

        if project is None:
            return None

        tasks_query = (
            select(AgentTask)
            .where(col(AgentTask.agent_project_id) == project_id)
            .where(col(AgentTask.deleted_at).is_(None))
            .order_by(col(AgentTask.priority), col(AgentTask.created_at))
        )
        tasks_result = await session.exec(tasks_query)
        tasks = list(tasks_result.all())

        # Fetch default agent name if set
        default_agent_name: str | None = None
        if project.default_agent_id:
            agent_result = await session.exec(select(Agent.name).where(col(Agent.agent_id) == project.default_agent_id))
            default_agent_name = agent_result.one_or_none()

    return {
        "project": project,
        "tasks": tasks,
        "creator_name": project.creator_user_id or "—",
        "default_agent_name": default_agent_name,
    }


@retry_db
async def update_project_status(project_id: uuid.UUID, new_status: AgentProjectStatus) -> bool:
    """Update a project's status. Returns True on success."""
    async with get_async_session() as session:
        query = select(AgentProject).where(col(AgentProject.agent_project_id) == project_id).with_for_update()
        result = await session.exec(query)
        project = result.one_or_none()
        if project is None:
            return False
        project.status = new_status
        session.add(project)
        await session.commit()
        return True


@retry_db
async def fetch_task_artifacts(task_id: uuid.UUID, session_ids: list[str] | None = None) -> list[AgentArtifact]:
    """Fetch artifacts for a task (by task_id) and optionally its related sessions."""
    async with get_async_session_read_replica() as session:
        stmt = (
            select(AgentArtifact)
            .where(col(AgentArtifact.deleted_at).is_(None))
            .where(col(AgentArtifact.agent_task_id) == task_id)
            .order_by(col(AgentArtifact.created_at).asc())
        )
        result = await session.exec(stmt)
        task_artifacts = list(result.all())

        # Also fetch artifacts linked to associated sessions (not already in task_artifacts)
        if session_ids:
            known_ids = {a.agent_artifact_id for a in task_artifacts}
            sid_uuids = []
            for sid in session_ids:
                try:
                    sid_uuids.append(uuid.UUID(sid))
                except ValueError:
                    pass
            if sid_uuids:
                sess_stmt = (
                    select(AgentArtifact)
                    .where(col(AgentArtifact.deleted_at).is_(None))
                    .where(col(AgentArtifact.agent_session_id).in_(sid_uuids))
                    .order_by(col(AgentArtifact.created_at).asc())
                )
                sess_result = await session.exec(sess_stmt)
                for a in sess_result.all():
                    if a.agent_artifact_id not in known_ids:
                        task_artifacts.append(a)
                        known_ids.add(a.agent_artifact_id)

        task_artifacts.sort(key=lambda a: a.created_at or datetime.min.replace(tzinfo=UTC))
        return task_artifacts


@retry_db
async def set_task_forced_pickup(task_id: uuid.UUID) -> bool:
    """Set forced_pickup flag in task_data to allow manual triggering.

    This enables the task executor to pick up this READY task even if
    the project is paused. Only allowed for READY tasks in PAUSED projects —
    ACTIVE projects execute READY tasks automatically without needing this
    flag, and allowing it there would risk a stale flag bypassing pause
    semantics if the project is paused before the task is claimed.

    Returns True on success, False if task not found, not in READY state,
    or project is not PAUSED.
    """
    async with get_async_session() as session:
        query = select(AgentTask).where(col(AgentTask.agent_task_id) == task_id).with_for_update()
        result = await session.exec(query)
        task = result.one_or_none()
        if task is None:
            return False

        # Only allow setting forced_pickup for READY tasks; the executor queries
        # status == READY so setting this flag on PENDING tasks would be a no-op.
        if task.status != AgentTaskStatus.READY:
            return False

        # Only allow forced_pickup for PAUSED projects. ACTIVE projects execute
        # READY tasks automatically; a flag set while ACTIVE and not yet claimed
        # would silently bypass pause semantics if the project is later paused.
        project_query = select(AgentProject).where(col(AgentProject.agent_project_id) == task.agent_project_id)
        project_result = await session.exec(project_query)
        project = project_result.one_or_none()
        if project is None or project.status != AgentProjectStatus.PAUSED:
            return False

        # Merge forced_pickup into existing task_data
        task_data = dict(task.task_data or {})
        task_data["forced_pickup"] = True
        task.task_data = task_data
        session.add(task)
        await session.commit()
        return True


@retry_db
async def update_task_status(task_id: uuid.UUID, new_status: AgentTaskStatus) -> bool:
    """Update a task's status. Returns True on success.

    For terminal statuses (COMPLETED, FAILED, CANCELLED), uses complete_task to handle
    cost aggregation, completed_at timestamp, and other side effects. Also triggers
    dependency resolution when a task is completed.
    """
    async with get_async_session() as session:
        query = select(AgentTask).where(col(AgentTask.agent_task_id) == task_id).with_for_update()
        result = await session.exec(query)
        task = result.one_or_none()
        if task is None:
            return False

        if new_status in TERMINAL_TASK_STATUSES:
            # Pass result=None to preserve existing task.result data
            await complete_task(session, task, new_status, result=None)
        else:
            task.status = new_status
            session.add(task)
        await session.commit()

    # Run dependency resolution outside the transaction to avoid holding locks
    # Best-effort: don't fail the status update if dependency resolution fails
    if new_status == AgentTaskStatus.COMPLETED:
        try:
            await resolve_task_dependencies()
        except Exception:
            logger.exception("Failed to resolve task dependencies after completing task %s", task_id)

    return True


@retry_db
async def fetch_available_agents() -> list[dict[str, Any]]:
    """Fetch all available agents for selection dropdowns.

    Returns:
        List of dicts with agent_id, name, and display_name.
    """
    async with get_async_session_read_replica() as session:
        result = await session.exec(
            select(Agent.agent_id, Agent.name, Agent.display_name)
            .where(col(Agent.deleted_at).is_(None))
            .order_by(Agent.name)
        )
        return [{"agent_id": str(row[0]), "name": row[1], "display_name": row[2]} for row in result.all()]


@retry_db
async def update_project_agent(project_id: uuid.UUID, agent_name: str | None) -> bool:
    """Update a project's default agent. Returns True on success.

    Args:
        project_id: The project UUID.
        agent_name: The agent name to set, or None to clear.
    """
    async with get_async_session() as session:
        # Look up agent_id from name if provided
        agent_id: uuid.UUID | None = None
        if agent_name:
            agent_result = await session.exec(select(Agent.agent_id).where(Agent.name == agent_name))
            agent_id = agent_result.one_or_none()
            if agent_id is None:
                return False

        query = select(AgentProject).where(col(AgentProject.agent_project_id) == project_id).with_for_update()
        result = await session.exec(query)
        project = result.one_or_none()
        if project is None:
            return False
        project.default_agent_id = agent_id
        session.add(project)
        await session.commit()
        return True


@retry_db
async def update_task_agent(task_id: uuid.UUID, agent_name: str | None) -> bool:
    """Update a task's assigned agent. Returns True on success.

    Args:
        task_id: The task UUID.
        agent_name: The agent name to set, or None to clear.
    """
    async with get_async_session() as session:
        # Look up agent_id from name if provided
        agent_id: uuid.UUID | None = None
        if agent_name:
            agent_result = await session.exec(select(Agent.agent_id).where(Agent.name == agent_name))
            agent_id = agent_result.one_or_none()
            if agent_id is None:
                return False

        query = select(AgentTask).where(col(AgentTask.agent_task_id) == task_id).with_for_update()
        result = await session.exec(query)
        task = result.one_or_none()
        if task is None:
            return False
        task.agent_id = agent_id
        session.add(task)
        await session.commit()
        return True


@retry_db
async def fetch_team_members() -> list[dict[str, Any]]:
    """Fetch team members with their user_ids from the database.

    Uses TEAM_DIRECTORY emails to look up users.

    Returns:
        List of dicts with user_id, name, and email.
    """
    team_emails = [m.email for m in TEAM_DIRECTORY if m.email]
    async with get_async_session_read_replica() as session:
        result = await session.exec(
            select(User.user_id, User.name, User.email)
            .where(col(User.email).in_(team_emails))
            .where(col(User.deleted_at).is_(None))
            .order_by(User.name)
        )
        return [{"user_id": row[0], "name": row[1] or row[2], "email": row[2]} for row in result.all()]


@retry_db
async def update_project_creator(project_id: uuid.UUID, user_id: str | None) -> bool:
    """Update a project's creator. Returns True on success.

    Args:
        project_id: The project UUID.
        user_id: The user_id to set as creator, or None to clear.
    """
    async with get_async_session() as session:
        query = select(AgentProject).where(col(AgentProject.agent_project_id) == project_id).with_for_update()
        result = await session.exec(query)
        project = result.one_or_none()
        if project is None:
            return False
        project.creator_user_id = user_id
        session.add(project)
        await session.commit()
        return True


def _get_linear_project_ref(project: AgentProject) -> dict[str, str] | None:
    """Extract the ``linear_ref`` dict from ``project_data``, if present."""
    data = project.project_data or {}
    ref = data.get("linear_ref")
    if not isinstance(ref, dict) or not ref.get("linear_project_id"):
        return None
    return ref


def _linear_project_url(linear_project_id: str) -> str:
    """Build a Linear project URL from a project UUID."""
    return f"https://linear.app/{_LINEAR_WORKSPACE_SLUG}/project/{linear_project_id}"


@retry_db
async def fetch_project_sessions(
    project_id: uuid.UUID,
    status_filter: AgentSessionStatus | None = None,
) -> list[dict[str, Any]]:
    """Fetch all sessions associated with tasks in a project.

    Returns session info with task title for context.
    """
    async with get_async_session_read_replica() as session:
        # First get all tasks with assigned sessions
        tasks_query = (
            select(AgentTask)
            .where(col(AgentTask.agent_project_id) == project_id)
            .where(col(AgentTask.deleted_at).is_(None))
            .where(col(AgentTask.assigned_session_ids).isnot(None))
        )
        tasks_result = await session.exec(tasks_query)
        tasks = list(tasks_result.all())

        # Collect all session IDs and map to task info.
        # Note: If a session is assigned to multiple tasks, only the last task is shown in the UI.
        # This is acceptable for the current use case where sessions typically map to one task.
        session_to_task: dict[str, AgentTask] = {}
        for task in tasks:
            if task.assigned_session_ids:
                for sid in task.assigned_session_ids:
                    session_to_task[sid] = task

        if not session_to_task:
            return []

        # Fetch the sessions
        session_ids = [uuid.UUID(sid) for sid in session_to_task]
        sessions_query = (
            select(AgentSession)
            .options(selectinload(AgentSession.agent))  # type: ignore[arg-type]
            .where(col(AgentSession.agent_session_id).in_(session_ids))
            .where(col(AgentSession.deleted_at).is_(None))
        )
        if status_filter:
            sessions_query = sessions_query.where(col(AgentSession.status) == status_filter)
        sessions_query = sessions_query.order_by(col(AgentSession.created_at).desc())

        sessions_result = await session.exec(sessions_query)
        sessions = list(sessions_result.all())

    return [
        {
            "session": s,
            "task": session_to_task.get(str(s.agent_session_id)),
            "agent_name": s.agent.display_name if s.agent else "—",
        }
        for s in sessions
    ]


# ── Artifact helpers ─────────────────────────────────────────────────────────

_ARTIFACT_TYPE_ICON: dict[AgentArtifactType, str] = {
    AgentArtifactType.YUPPASTE: "📝",
    AgentArtifactType.CODE_REVIEW: "🔍",
    AgentArtifactType.OTHER: "📦",
}


def _md_cell(s: str) -> str:
    """Escape pipe characters and newlines so the string is safe in a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


def _render_task_artifacts(task_id: uuid.UUID, session_ids: list[str] | None) -> None:
    """Fetch and render artifacts for a task and its sessions."""
    artifacts = run_coroutine_in_lit_worker(
        fetch_task_artifacts(task_id, session_ids),
        timeout=30,
    )
    if not artifacts:
        st.caption("No artifacts.")
        return
    rows = []
    for a in artifacts or []:
        icon = _ARTIFACT_TYPE_ICON.get(a.artifact_type, "📦")
        type_str = f"{icon} {a.artifact_type.value}"
        title_link = f"[{_md_cell(a.title)}]({a.url})"
        src = ""
        if a.agent_session_id:
            sid = str(a.agent_session_id)
            src = f"[session {sid[:8]}](/agent_harness_console?session_id={sid})"
        elif a.agent_task_id:
            src = "task"
        raw_desc = (a.description or "")[:60] + ("…" if a.description and len(a.description) > 60 else "")
        desc = _md_cell(raw_desc)
        rows.append(f"| {type_str} | {title_link} | {desc} | {src} |")
    header = "| Type | Title | Description | Source |"
    sep = "|------|-------|-------------|--------|"
    st.markdown("\n".join([header, sep] + rows))


# ── Tree / topo helpers ──────────────────────────────────────────────────────


def _build_children_map(
    tasks: list[AgentTask],
) -> tuple[dict[str | None, list[AgentTask]], set[str]]:
    """Group tasks by parent_task_id.

    Returns (children_by_parent, all_task_ids).
    Key None = root tasks (no parent or orphaned parent).
    """
    all_task_ids = {str(t.agent_task_id) for t in tasks}
    children_by_parent: dict[str | None, list[AgentTask]] = defaultdict(list)

    for task in tasks:
        if task.parent_task_id:
            parent_id = str(task.parent_task_id)
            if parent_id in all_task_ids:
                children_by_parent[parent_id].append(task)
            else:
                children_by_parent[None].append(task)
        else:
            children_by_parent[None].append(task)

    return dict(children_by_parent), all_task_ids


def _topo_sort_key(task: AgentTask) -> tuple[int, datetime]:
    return (task.priority.value, task.created_at or _DT_MIN_UTC)


def _topological_sort_siblings(
    tasks: list[AgentTask],
    all_task_ids: set[str],
) -> list[AgentTask]:
    """Sort a list of sibling tasks in topological order based on depends_on."""
    if not tasks:
        return []

    task_map = {str(t.agent_task_id): t for t in tasks}
    sibling_ids = set(task_map.keys())

    in_degree: dict[str, int] = dict.fromkeys(sibling_ids, 0)
    dependents: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        if task.depends_on:
            for dep_id in task.depends_on:
                if dep_id in sibling_ids:
                    in_degree[str(task.agent_task_id)] += 1
                    dependents[dep_id].append(str(task.agent_task_id))

    queue: deque[str] = deque(
        sorted(
            (tid for tid in sibling_ids if in_degree[tid] == 0),
            key=lambda tid: _topo_sort_key(task_map[tid]),
        )
    )

    sorted_ids: list[str] = []
    while queue:
        tid = queue.popleft()
        sorted_ids.append(tid)
        children = sorted(
            dependents.get(tid, []),
            key=lambda c: _topo_sort_key(task_map[c]),
        )
        for child_id in children:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                queue.append(child_id)

    remaining = [tid for tid in sibling_ids if tid not in set(sorted_ids)]
    sorted_ids.extend(sorted(remaining, key=lambda tid: _topo_sort_key(task_map[tid])))

    return [task_map[tid] for tid in sorted_ids]


def _compute_sibling_topo_levels(
    siblings: list[AgentTask],
) -> dict[str, int]:
    """Compute topo levels among a set of sibling tasks (based on their mutual dependencies)."""
    sibling_ids = {str(t.agent_task_id) for t in siblings}
    levels: dict[str, int] = {}
    task_map = {str(t.agent_task_id): t for t in siblings}

    def _get_level(tid: str, visited: set[str]) -> int:
        if tid in levels:
            return levels[tid]
        if tid in visited:
            return 0
        visited.add(tid)
        task = task_map.get(tid)
        if not task or not task.depends_on:
            levels[tid] = 0
            return 0
        max_dep = 0
        for dep_id in task.depends_on:
            if dep_id in sibling_ids:
                max_dep = max(max_dep, _get_level(dep_id, visited) + 1)
        levels[tid] = max_dep
        return max_dep

    for tid in sibling_ids:
        _get_level(tid, set())

    return levels


def _count_all_descendants(
    task_id: str,
    children_map: dict[str | None, list[AgentTask]],
    visited: set[str] | None = None,
) -> int:
    """Count all descendants (all levels) of a task."""
    if visited is None:
        visited = set()
    if task_id in visited:
        return 0
    visited.add(task_id)
    direct = children_map.get(task_id, [])
    total = len(direct)
    for child in direct:
        total += _count_all_descendants(str(child.agent_task_id), children_map, visited)
    return total


def _get_task_depth(
    task_id: str,
    task_map: dict[str, AgentTask],
    all_task_ids: set[str],
) -> int:
    """Get the depth of a task in the parent hierarchy (0 = root)."""
    depth = 0
    current_id = task_id
    visited: set[str] = set()
    while current_id in task_map:
        if current_id in visited:
            break
        visited.add(current_id)
        task = task_map[current_id]
        if task.parent_task_id:
            parent_id = str(task.parent_task_id)
            if parent_id in all_task_ids:
                depth += 1
                current_id = parent_id
            else:
                break
        else:
            break
    return depth


def _get_max_depth(tasks: list[AgentTask]) -> int:
    """Get the maximum depth in the parent hierarchy."""
    if not tasks:
        return 0
    _, all_task_ids = _build_children_map(tasks)
    task_map = {str(t.agent_task_id): t for t in tasks}
    return max(_get_task_depth(str(t.agent_task_id), task_map, all_task_ids) for t in tasks)


def _compute_unique_ranks(
    children_map: dict[str | None, list[AgentTask]],
    all_task_ids: set[str],
) -> dict[str, str]:
    """Compute unique rank strings for all tasks.

    Tasks at the same topo level get letter suffixes (1a, 1b, 1c).
    Children inherit parent rank as prefix (1a.0, 1a.1b).
    """
    rank_map: dict[str, str] = {}

    def _assign_ranks(parent_id: str | None, prefix: str) -> None:
        siblings = children_map.get(parent_id, [])
        if not siblings:
            return

        sorted_siblings = _topological_sort_siblings(siblings, all_task_ids)
        sibling_topo = _compute_sibling_topo_levels(sorted_siblings)

        # Group by topo level to detect ties
        level_groups: dict[int, list[AgentTask]] = defaultdict(list)
        for task in sorted_siblings:
            level = sibling_topo.get(str(task.agent_task_id), 0)
            level_groups[level].append(task)

        for level in sorted(level_groups.keys()):
            group = level_groups[level]
            needs_suffix = len(group) > 1
            for idx, task in enumerate(group):
                tid = str(task.agent_task_id)
                display_level = level + 1
                if needs_suffix:
                    suffix = chr(ord("a") + idx) if idx < 26 else f"_{idx}"
                    rank_str = f"{prefix}{display_level}{suffix}"
                else:
                    rank_str = f"{prefix}{display_level}"
                rank_map[tid] = rank_str
                if tid in children_map:
                    _assign_ranks(tid, f"{rank_str}.")

    _assign_ranks(None, "")
    return rank_map


# ── CSS ──────────────────────────────────────────────────────────────────────

_PAGE_CSS = """
<style>
.task-list-container [data-testid="stExpander"] {
    margin-bottom: 2px !important;
    border-radius: 4px !important;
}
.task-list-container [data-testid="stExpander"] > div:first-child {
    padding: 0.3rem 0.5rem !important;
}
.task-list-container [data-testid="stExpander"] summary {
    padding: 0.2rem 0 !important;
}
.task-list-container [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
    padding: 0.5rem 0.75rem !important;
}
</style>
"""


# ── Dependency DAG rendering ─────────────────────────────────────────────────


def _collect_tasks_up_to_depth(
    tasks: list[AgentTask],
    max_depth: int,
) -> list[AgentTask]:
    """Filter tasks to only include those up to a given parent-hierarchy depth."""
    _, all_task_ids = _build_children_map(tasks)
    task_map = {str(t.agent_task_id): t for t in tasks}
    return [task for task in tasks if _get_task_depth(str(task.agent_task_id), task_map, all_task_ids) <= max_depth]


def _render_dependency_dag(tasks: list[AgentTask], depth_limit: int | None) -> None:
    """Render a Graphviz DAG of task dependencies — flat nodes with topo-level rank alignment."""
    if not tasks:
        st.info("No tasks to display.")
        return

    # Filter by depth if needed
    if depth_limit is not None:
        tasks = _collect_tasks_up_to_depth(tasks, depth_limit)
        if not tasks:
            st.info("No tasks at this depth level.")
            return

    task_map = {str(t.agent_task_id): t for t in tasks}

    # Group visible tasks by their parent_task_id
    tasks_by_parent: dict[str | None, list[AgentTask]] = defaultdict(list)
    for task in tasks:
        if task.parent_task_id:
            tasks_by_parent[str(task.parent_task_id)].append(task)
        else:
            tasks_by_parent[None].append(task)

    def _compute_group_topo_levels(group_tasks: list[AgentTask]) -> dict[int, list[str]]:
        """Compute topo levels for a group of sibling tasks, returning level→task_ids."""
        group_ids = {str(t.agent_task_id) for t in group_tasks}
        group_levels: dict[str, int] = {}

        def _get_lvl(tid: str, visited: set[str]) -> int:
            if tid in group_levels:
                return group_levels[tid]
            if tid in visited:
                return 0
            visited.add(tid)
            t = task_map.get(tid)
            if not t or not t.depends_on:
                group_levels[tid] = 0
                return 0
            max_dep = 0
            for dep_id in t.depends_on:
                if dep_id in group_ids:
                    max_dep = max(max_dep, _get_lvl(dep_id, visited) + 1)
            group_levels[tid] = max_dep
            return max_dep

        for tid in group_ids:
            _get_lvl(tid, set())

        grp: dict[int, list[str]] = defaultdict(list)
        for tid, lvl in group_levels.items():
            grp[lvl].append(tid)
        return grp

    def _dot_header() -> list[str]:
        return [
            "digraph {",
            "    rankdir=TB;",
            '    graph [size="10,8", ratio="compress"];',
            '    node [shape=box, style=filled, fontsize=10, fontname="Helvetica", margin="0.15,0.07"];',
            '    edge [color="#666666"];',
        ]

    def _emit_node(lines: list[str], tid: str, task: AgentTask, indent: str) -> None:
        color = _TASK_STATUS_COLOR.get(task.status, "#E0E0E0")
        emoji = _TASK_STATUS_EMOJI.get(task.status, "")
        font_color = "white" if task.status in _TASK_STATUS_LIGHT_TEXT else "black"
        label = f"{emoji} {task.title}".replace('"', '\\"')
        lines.append(f'{indent}"{tid}" [label="{label}", fillcolor="{color}", fontcolor="{font_color}"];')

    # Build ordered list of groups: (parent_id_or_None, tasks_in_group)
    groups: list[tuple[str | None, list[AgentTask]]] = []
    root_tasks = tasks_by_parent.get(None, [])
    if root_tasks:
        groups.append((None, root_tasks))
    groups.extend((parent_id, tasks_by_parent[parent_id]) for parent_id in tasks_by_parent if parent_id is not None)

    # Legend on the left, DAG batches on the right
    legend_col, dag_col = st.columns([0.15, 0.85])

    with legend_col:
        for status, color in _TASK_STATUS_COLOR.items():
            emoji = _TASK_STATUS_EMOJI.get(status, "")
            text_color = "#fff" if status in _TASK_STATUS_LIGHT_TEXT else "#333"
            st.markdown(
                f'<span style="background:{color};color:{text_color};padding:2px 6px;'
                f'border-radius:3px;font-size:11px;white-space:nowrap;">'
                f"{emoji} {status.value}</span>",
                unsafe_allow_html=True,
            )

    with dag_col:
        _MAX_GROUPS_PER_ROW = 3
        for batch_start in range(0, len(groups), _MAX_GROUPS_PER_ROW):
            batch = groups[batch_start : batch_start + _MAX_GROUPS_PER_ROW]
            batch_task_ids: set[str] = set()
            for _, group_tasks in batch:
                for t in group_tasks:
                    batch_task_ids.add(str(t.agent_task_id))

            lines = _dot_header()

            for parent_id, group_tasks in batch:
                if parent_id is None:
                    for task in group_tasks:
                        _emit_node(lines, str(task.agent_task_id), task, "    ")
                    root_level_groups = _compute_group_topo_levels(group_tasks)
                    for level in sorted(root_level_groups.keys()):
                        tids = root_level_groups[level]
                        if len(tids) > 1:
                            node_refs = " ".join(f'"{tid}"' for tid in tids)
                            lines.append(f"    {{ rank=same; {node_refs} }}")
                else:
                    parent_task = task_map.get(parent_id)
                    if parent_task:
                        p_emoji = _TASK_STATUS_EMOJI.get(parent_task.status, "")
                        cluster_label = f"{p_emoji} {parent_task.title}".replace('"', '\\"')
                    else:
                        cluster_label = parent_id[:8]
                    cid = parent_id.replace("-", "_")
                    lines.append(f"    subgraph cluster_{cid} {{")
                    lines.append(f'        label="{cluster_label}";')
                    lines.append("        labeljust=l;")
                    lines.append("        style=dashed;")
                    lines.append('        color="#999999";')
                    lines.append("        fontsize=11;")
                    lines.append('        fontname="Helvetica";')
                    lines.append("        margin=10;")
                    for task in group_tasks:
                        _emit_node(lines, str(task.agent_task_id), task, "        ")
                    child_level_groups = _compute_group_topo_levels(group_tasks)
                    for level in sorted(child_level_groups.keys()):
                        tids = child_level_groups[level]
                        if len(tids) > 1:
                            node_refs = " ".join(f'"{tid}"' for tid in tids)
                            lines.append(f"        {{ rank=same; {node_refs} }}")
                    lines.append("    }")

            # Proxy nodes for cross-batch dependencies (deps not rendered in this chart)
            cross_batch_dep_ids: set[str] = {
                dep_id
                for task in tasks
                if str(task.agent_task_id) in batch_task_ids
                if task.depends_on
                for dep_id in task.depends_on
                if dep_id not in batch_task_ids
            }
            for dep_id in sorted(cross_batch_dep_ids):
                dep_task = task_map.get(dep_id)
                if dep_task:
                    emoji = _TASK_STATUS_EMOJI.get(dep_task.status, "")
                    proxy_label = f"{emoji} {dep_task.title}".replace('"', '\\"')
                else:
                    proxy_label = dep_id[:8]
                lines.append(
                    f'    "{dep_id}" [label="{proxy_label}", style="filled,dashed", '
                    f'fillcolor="#F5F5F5", fontcolor="#AAAAAA", color="#AAAAAA", fontsize=9];'
                )

            # Edges — all deps where the target task is in this batch
            lines.extend(
                f'    "{dep_id}" -> "{str(task.agent_task_id)}";'
                for task in tasks
                if str(task.agent_task_id) in batch_task_ids
                if task.depends_on
                for dep_id in task.depends_on
            )

            lines.append("}")
            st.graphviz_chart("\n".join(lines), use_container_width=False)


# ── Task list rendering (hierarchical with indentation) ──────────────────────

_MAX_RENDER_DEPTH = 10


def _render_task_list_recursive(
    parent_id: str | None,
    children_map: dict[str | None, list[AgentTask]],
    all_task_ids: set[str],
    task_title_map: dict[str, str],
    rank_map: dict[str, str],
    project_id: str,
    depth: int = 0,
) -> None:
    """Render tasks grouped by parent, topo-sorted within each sibling group, with columns."""
    if depth >= _MAX_RENDER_DEPTH:
        st.caption(f"⚠️ Max nesting depth reached ({_MAX_RENDER_DEPTH})")
        return

    siblings = children_map.get(parent_id, [])
    if not siblings:
        return

    sorted_siblings = _topological_sort_siblings(siblings, all_task_ids)

    for task in sorted_siblings:
        task_id = str(task.agent_task_id)
        status_emoji_t = _TASK_STATUS_EMOJI.get(task.status, "")
        has_children = task_id in children_map

        # Depends on (show unique ranks)
        deps_str = ""
        if task.depends_on:
            dep_ranks = [rank_map.get(dep_id, dep_id[:8]) for dep_id in task.depends_on]
            deps_str = ", ".join(dep_ranks)

        # Description preview
        desc_preview = ""
        if task.description:
            desc_preview = task.description[:200]
            if len(task.description) > 200:
                desc_preview += "..."

        created_str = _time_ago(task.created_at)

        # Count all descendants (all levels), not just direct children
        if has_children:
            descendant_count = _count_all_descendants(task_id, children_map)
            children_str = (
                f'<span style="color:#888;font-size:0.85em;">({descendant_count} child task'
                f"{'s' if descendant_count != 1 else ''})</span>"
            )
        else:
            children_str = ""

        rank_display = rank_map.get(task_id, "?")

        # Row: status | rank | dep on | task title | description | PR | priority | created | session
        row_cols = st.columns([0.7, 0.6, 1, 2.5, 2.5, 0.7, 0.8, 0.8, 1])

        with row_cols[0]:
            st.caption(f"{status_emoji_t} {task.status.value}")

        with row_cols[1]:
            st.caption(rank_display)

        with row_cols[2]:
            if deps_str:
                st.caption(deps_str)

        with row_cols[3]:
            task_url = f"?project_id={project_id}&task_id={task_id}"
            title_md = _internal_link(html.escape(task.title), task_url)
            if children_str:
                title_md += f" {children_str}"
            # Use sub-columns for indentation: depth 0 = full, depth 1 = [1,7], depth 2+ = [2,6]
            if depth == 0:
                st.markdown(title_md, unsafe_allow_html=True)
            elif depth == 1:
                _spacer, _content = st.columns([1, 7])
                with _content:
                    st.markdown(title_md, unsafe_allow_html=True)
            else:
                _spacer, _content = st.columns([2, 6])
                with _content:
                    st.markdown(title_md, unsafe_allow_html=True)

        with row_cols[4]:
            if desc_preview:
                st.caption(desc_preview)

        with row_cols[5]:
            if pr_link := _get_pr_link_parts(task.result):
                st.markdown(f"[🔗 {pr_link[1]}]({pr_link[0]})")

        with row_cols[6]:
            pri_color = _TASK_PRIORITY_COLOR.get(task.priority, "#757575")
            pri_badge = _TASK_PRIORITY_BADGE.get(task.priority, "")
            st.markdown(
                f'<span style="color:{pri_color};font-size:0.85em;font-weight:600;">'
                f"{pri_badge} {task.priority.name}</span>",
                unsafe_allow_html=True,
            )

        with row_cols[7]:
            st.caption(created_str)

        with row_cols[8]:
            if task.assigned_session_ids:
                session_links = []
                for sid in task.assigned_session_ids:
                    short_id = sid[:8]
                    session_links.append(f"[{short_id}](/agent_harness_console?session_id={sid})")
                st.caption(", ".join(session_links))

        # Recurse into children
        if has_children:
            _render_task_list_recursive(
                task_id,
                children_map,
                all_task_ids,
                task_title_map,
                rank_map,
                project_id,
                depth=depth + 1,
            )


# ── Task detail view ─────────────────────────────────────────────────────────


def _render_task_detail(
    project_id: uuid.UUID,
    task_id: str,
    tasks: list[AgentTask],
    project_name: str,
    project_status: AgentProjectStatus,
) -> None:
    """Render the detail view for a single task."""
    task_map = {str(t.agent_task_id): t for t in tasks}
    task = task_map.get(task_id)

    if task is None:
        st.error(f"Task not found: `{task_id}`")
        return

    task_title_map = {str(t.agent_task_id): t.title for t in tasks}
    children_map, all_task_ids = _build_children_map(tasks)
    rank_map = _compute_unique_ranks(children_map, all_task_ids)

    # Breadcrumb: Project > Task
    project_url = f"?project_id={project_id}"
    st.markdown(
        f"[All Projects](?/) · [{html.escape(project_name)}]({project_url}) · {html.escape(task.title)}",
    )

    status_emoji = _TASK_STATUS_EMOJI.get(task.status, "")
    st.markdown(f"## {status_emoji} {html.escape(task.title)}")

    left_col, right_col = st.columns([1, 1])

    with left_col:
        st.markdown("### Details")

        rank_display = rank_map.get(task_id, "?")

        # Status display
        st.markdown(f"**Status:** {status_emoji} {task.status.value}")

        # Action buttons row - show relevant buttons based on status
        # IN_PROGRESS is blocked due to executor race conditions.
        # PENDING is blocked because the task has unresolved dependencies and must transition
        # to READY before any manual action is valid (no legal transition exists from PENDING).
        if task.status not in {AgentTaskStatus.PENDING, AgentTaskStatus.IN_PROGRESS}:
            restart_key = f"restart_{task_id}"
            confirm_restart_key = f"confirm_restart_{task_id}"
            confirm_complete_key = f"confirm_complete_{task_id}"
            confirm_failed_key = f"confirm_failed_{task_id}"
            confirm_resume_key = f"confirm_resume_{task_id}"
            confirm_start_key = f"confirm_start_{task_id}"

            def _clear_confirmations() -> None:
                """Clear all confirmation states."""
                st.session_state.pop(confirm_restart_key, None)
                st.session_state.pop(confirm_complete_key, None)
                st.session_state.pop(confirm_failed_key, None)
                st.session_state.pop(confirm_resume_key, None)
                st.session_state.pop(confirm_start_key, None)

            def _do_restart() -> None:
                """Helper to run restart logic and update UI."""
                with st.spinner("Restarting task..."):
                    success, msg = run_coroutine_in_lit_worker(
                        restart_task(uuid.UUID(task_id)),
                        timeout=30,
                    )
                if success:
                    _clear_confirmations()
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

            def _do_mark_complete() -> None:
                """Helper to mark task as complete."""
                with st.spinner("Marking as complete..."):
                    success = run_coroutine_in_lit_worker(
                        update_task_status(uuid.UUID(task_id), AgentTaskStatus.COMPLETED),
                        timeout=30,
                    )
                if success:
                    _clear_confirmations()
                    st.success("Task marked as completed")
                    st.rerun()
                else:
                    st.error("Failed to update task status")

            def _do_mark_failed() -> None:
                """Helper to mark task as failed."""
                with st.spinner("Marking as failed..."):
                    success = run_coroutine_in_lit_worker(
                        update_task_status(uuid.UUID(task_id), AgentTaskStatus.FAILED),
                        timeout=30,
                    )
                if success:
                    _clear_confirmations()
                    st.success("Task marked as failed")
                    st.rerun()
                else:
                    st.error("Failed to update task status")

            def _do_resume() -> None:
                """Helper to resume a failed task."""
                with st.spinner("Resuming task..."):
                    result = run_coroutine_in_lit_worker(
                        resume_task(uuid.UUID(task_id)),
                        timeout=30,
                    )
                if result.get("success"):
                    st.success("Task queued for resumption")
                    st.rerun()
                else:
                    st.error(f"Failed to resume: {result.get('error')}")

            # Build button row based on status (right-aligned)
            if task.status == AgentTaskStatus.IN_REVIEW:
                spacer, btn_col1, btn_col2, btn_col3 = st.columns([2, 1, 1, 1])
                with btn_col1:
                    if st.button("✅ Complete", key=f"complete_{task_id}", type="primary"):
                        _clear_confirmations()
                        st.session_state[confirm_complete_key] = True
                        st.rerun()
                with btn_col2:
                    if st.button("❌ Failed", key=f"mark_failed_{task_id}", type="secondary"):
                        _clear_confirmations()
                        st.session_state[confirm_failed_key] = True
                        st.rerun()
                with btn_col3:
                    if st.button("🔄 Restart", key=restart_key, type="secondary"):
                        _clear_confirmations()
                        st.session_state[confirm_restart_key] = True
                        st.rerun()

                # Confirmation dialogs (right-aligned)
                if st.session_state.get(confirm_complete_key, False):
                    st.info("Mark this task as completed?")
                    spacer, col1, col2 = st.columns([5, 1, 1])
                    with col1:
                        if st.button("Yes, complete", key=f"yes_complete_{task_id}", type="primary"):
                            _do_mark_complete()
                    with col2:
                        if st.button("Cancel", key=f"cancel_complete_{task_id}"):
                            _clear_confirmations()
                            st.rerun()

                if st.session_state.get(confirm_failed_key, False):
                    st.warning("Mark this task as failed?")
                    spacer, col1, col2 = st.columns([5, 1, 1])
                    with col1:
                        if st.button("Yes, mark failed", key=f"yes_failed_{task_id}", type="primary"):
                            _do_mark_failed()
                    with col2:
                        if st.button("Cancel", key=f"cancel_failed_{task_id}"):
                            _clear_confirmations()
                            st.rerun()

                if st.session_state.get(confirm_restart_key, False):
                    st.warning("Restart this task? It will be reset to READY status.")
                    spacer, col1, col2 = st.columns([5, 1, 1])
                    with col1:
                        if st.button("Yes, restart", key=f"yes_restart_{task_id}", type="primary"):
                            _do_restart()
                    with col2:
                        if st.button("Cancel", key=f"cancel_restart_{task_id}"):
                            _clear_confirmations()
                            st.rerun()

            elif task.status == AgentTaskStatus.FAILED:
                task_data = task.task_data or {}
                error_subtype = task_data.get("error_subtype")
                is_resumable = error_subtype in RESUMABLE_ERROR_SUBTYPES

                if is_resumable:
                    spacer, btn_col1, btn_col2 = st.columns([5, 1, 1])
                    with btn_col1:
                        if st.button("▶️ Resume", key=f"resume_{task_id}", type="primary"):
                            _clear_confirmations()
                            st.session_state[confirm_resume_key] = True
                            st.rerun()
                    with btn_col2:
                        if st.button("🔄 Restart", key=restart_key, type="secondary"):
                            _clear_confirmations()
                            st.session_state[confirm_restart_key] = True
                            st.rerun()

                    # Confirmation dialogs for FAILED tasks (right-aligned)
                    if st.session_state.get(confirm_resume_key, False):
                        st.info("Resume this task from where it left off?")
                        spacer, col1, col2 = st.columns([5, 1, 1])
                        with col1:
                            if st.button("Yes, resume", key=f"yes_resume_{task_id}", type="primary"):
                                _do_resume()
                        with col2:
                            if st.button("Cancel", key=f"cancel_resume_{task_id}"):
                                _clear_confirmations()
                                st.rerun()

                    if st.session_state.get(confirm_restart_key, False):
                        st.warning("Restart this task? It will be reset to READY status.")
                        spacer, col1, col2 = st.columns([5, 1, 1])
                        with col1:
                            if st.button("Yes, restart", key=f"yes_restart_failed_{task_id}", type="primary"):
                                _do_restart()
                        with col2:
                            if st.button("Cancel", key=f"cancel_restart_failed_{task_id}"):
                                _clear_confirmations()
                                st.rerun()
                else:
                    spacer, btn_col = st.columns([4, 1])
                    with btn_col:
                        if st.button("🔄 Restart", key=restart_key, type="secondary"):
                            _clear_confirmations()
                            st.session_state[confirm_restart_key] = True
                            st.rerun()

                    if st.session_state.get(confirm_restart_key, False):
                        st.warning("Restart this task? It will be reset to READY status.")
                        spacer, col1, col2 = st.columns([5, 1, 1])
                        with col1:
                            if st.button("Yes, restart", key=f"yes_restart_failed_{task_id}", type="primary"):
                                _do_restart()
                        with col2:
                            if st.button("Cancel", key=f"cancel_restart_failed_{task_id}"):
                                _clear_confirmations()
                                st.rerun()

            elif task.status == AgentTaskStatus.COMPLETED:
                spacer, btn_col = st.columns([4, 1])
                with btn_col:
                    if st.button("🔄 Restart", key=restart_key, type="secondary"):
                        st.session_state[confirm_restart_key] = True
                        st.rerun()

                if st.session_state.get(confirm_restart_key, False):
                    st.warning("⚠️ This task is marked as COMPLETED. Are you sure you want to restart it?")
                    spacer, col1, col2 = st.columns([5, 1, 1])
                    with col1:
                        if st.button("Yes, restart", key=f"yes_restart_completed_{task_id}", type="primary"):
                            _do_restart()
                    with col2:
                        if st.button("Cancel", key=f"cancel_restart_completed_{task_id}"):
                            _clear_confirmations()
                            st.rerun()

            elif task.status == AgentTaskStatus.READY:
                # READY tasks can be manually started only when the project is PAUSED.
                # In ACTIVE projects the executor picks up READY tasks automatically;
                # allowing forced_pickup there would risk a stale flag bypassing pause
                # semantics if the project is paused before the task is claimed.
                # PENDING tasks are excluded by the outer guard above and shown no buttons,
                # since no valid transition exists until dependencies resolve to READY.
                if project_status != AgentProjectStatus.PAUSED:
                    st.caption("Task will be picked up automatically when the executor runs.")
                else:

                    def _do_start() -> None:
                        """Set forced_pickup flag to allow manual task execution."""
                        with st.spinner("Starting task..."):
                            success = run_coroutine_in_lit_worker(
                                set_task_forced_pickup(uuid.UUID(task_id)),
                                timeout=30,
                            )
                        if success:
                            _clear_confirmations()
                            st.success("Task queued for execution")
                            st.rerun()
                        else:
                            st.error("Failed to start task")

                    spacer, btn_col = st.columns([4, 1])
                    with btn_col:
                        if st.button("🚀 Start", key=f"start_{task_id}", type="primary"):
                            _clear_confirmations()
                            st.session_state[confirm_start_key] = True
                            st.rerun()

                    if st.session_state.get(confirm_start_key, False):
                        st.info("Start this task manually? It will be picked up by the executor.")
                        spacer, col1, col2 = st.columns([5, 1, 1])
                        with col1:
                            if st.button("Yes, start", key=f"yes_start_{task_id}", type="primary"):
                                _do_start()
                        with col2:
                            if st.button("Cancel", key=f"cancel_start_{task_id}"):
                                _clear_confirmations()
                                st.rerun()

            elif task.status == AgentTaskStatus.BLOCKED:
                # BLOCKED tasks - just show info, no actions
                st.caption("Task is blocked by dependencies.")

            else:
                # For other statuses (CANCELLED, etc.)
                spacer, btn_col = st.columns([4, 1])
                with btn_col:
                    if st.button("🔄 Restart", key=restart_key, type="secondary"):
                        _clear_confirmations()
                        st.session_state[confirm_restart_key] = True
                        st.rerun()

                if st.session_state.get(confirm_restart_key, False):
                    st.warning("Restart this task? It will be reset to READY status.")
                    spacer, col1, col2 = st.columns([5, 1, 1])
                    with col1:
                        if st.button("Yes, restart", key=f"yes_restart_other_{task_id}", type="primary"):
                            _do_restart()
                    with col2:
                        if st.button("Cancel", key=f"cancel_restart_other_{task_id}"):
                            _clear_confirmations()
                            st.rerun()

        st.markdown(f"**Rank:** {rank_display}")

        pri_color = _TASK_PRIORITY_COLOR.get(task.priority, "#757575")
        pri_badge = _TASK_PRIORITY_BADGE.get(task.priority, "")
        st.markdown(
            f'**Priority:** <span style="color:{pri_color};font-weight:600;">{pri_badge} {task.priority.name}</span>',
            unsafe_allow_html=True,
        )

        if task.estimated_effort:
            st.markdown(f"**Effort:** {task.estimated_effort}")

        # Agent assignment
        st.markdown("**Agent:**")
        try:
            agents = run_coroutine_in_lit_worker(fetch_available_agents(), timeout=30)
        except Exception:
            agents = []

        # Build options: "(none)" + agent names
        agent_options = ["(none)"] + [a["name"] for a in agents]
        agent_display = {a["name"]: f"{a['name']} ({a['display_name']})" for a in agents}
        agent_display["(none)"] = "(none)"

        # Find current agent name from agent_id
        current_agent_name = "(none)"
        if task.agent_id:
            for a in agents:
                if a["agent_id"] == str(task.agent_id):
                    current_agent_name = a["name"]
                    break

        current_index = agent_options.index(current_agent_name) if current_agent_name in agent_options else 0

        selected_agent = st.selectbox(
            "Assigned agent",
            options=agent_options,
            index=current_index,
            format_func=lambda x: agent_display.get(x, x),
            key=f"task_agent_{task_id}",
            label_visibility="collapsed",
        )

        if selected_agent != current_agent_name:
            new_agent = selected_agent if selected_agent != "(none)" else None
            with st.spinner("Updating agent..."):
                success = run_coroutine_in_lit_worker(
                    update_task_agent(uuid.UUID(task_id), new_agent),
                    timeout=30,
                )
            if success:
                st.success(f"Agent updated to {selected_agent}")
                st.rerun()
            else:
                st.error("Failed to update agent")

        st.divider()

        if task.depends_on:
            dep_lines = []
            for dep_id in task.depends_on:
                dep_title = task_title_map.get(dep_id, dep_id[:8])
                dep_rank = rank_map.get(dep_id, "?")
                dep_url = f"?project_id={project_id}&task_id={dep_id}"
                dep_lines.append(f"- [{dep_rank}: {html.escape(dep_title)}]({dep_url})")
            st.markdown("**Depends on:**")
            st.markdown("\n".join(dep_lines))

        # Show dependents (tasks that depend on this one)
        dependents = [t for t in tasks if t.depends_on and task_id in t.depends_on]
        if dependents:
            dep_lines = []
            for dep_task in dependents:
                dep_tid = str(dep_task.agent_task_id)
                dep_rank = rank_map.get(dep_tid, "?")
                dep_url = f"?project_id={project_id}&task_id={dep_tid}"
                dep_lines.append(f"- [{dep_rank}: {html.escape(dep_task.title)}]({dep_url})")
            st.markdown("**Depended on by:**")
            st.markdown("\n".join(dep_lines))

        st.divider()

        if task.assigned_session_ids:
            st.markdown("**Sessions:**")
            for sid in task.assigned_session_ids:
                short_id = sid[:8]
                lit_link = _internal_link(f"Lit {short_id}", f"/agent_harness_console?session_id={sid}")
                wr_link = f'<a href="https://war-room.yuppster.ai/session/{sid}" target="_blank">WR</a>'
                st.markdown(f"- {lit_link} · {wr_link}", unsafe_allow_html=True)

        if pr_link := _get_pr_link_parts(task.result):
            st.markdown(f"**PR:** [{pr_link[1]}]({pr_link[0]})")

        # Linear issue link
        task_linear_ref = (task.task_data or {}).get("linear_ref") or {}
        linear_identifier = task_linear_ref.get("linear_identifier", "")
        linear_issue_id = task_linear_ref.get("linear_issue_id", "")
        if linear_identifier and linear_issue_id:
            linear_issue_url = f"https://linear.app/{_LINEAR_WORKSPACE_SLUG}/issue/{linear_identifier}"
            st.markdown(f"**Linear:** [{linear_identifier}]({linear_issue_url})")

        st.divider()

        created_ago = _time_ago(task.created_at)
        st.markdown(f"**Created:** {_to_local(task.created_at)} ({created_ago})")
        if task.completed_at:
            completed_ago = _time_ago(task.completed_at)
            st.markdown(f"**Completed:** {_to_local(task.completed_at)} ({completed_ago})")
        if task.actual_spending_usd:
            st.markdown(f"**Spent:** ${task.actual_spending_usd:.4f}")

        # Parent task link
        if task.parent_task_id:
            parent_tid = str(task.parent_task_id)
            parent_title = task_title_map.get(parent_tid, parent_tid[:8])
            parent_url = f"?project_id={project_id}&task_id={parent_tid}"
            link = _internal_link(html.escape(parent_title), parent_url)
            st.markdown(f"**Parent task:** {link}", unsafe_allow_html=True)

        # Child tasks
        child_tasks = children_map.get(task_id, [])
        if child_tasks:
            st.markdown(f"**Child tasks:** {len(child_tasks)}")
            for child in child_tasks:
                child_tid = str(child.agent_task_id)
                child_rank = rank_map.get(child_tid, "?")
                child_emoji = _TASK_STATUS_EMOJI.get(child.status, "")
                child_url = f"?project_id={project_id}&task_id={child_tid}"
                link = _internal_link(f"{child_rank}: {html.escape(child.title)}", child_url)
                st.markdown(f"- {child_emoji} {link}", unsafe_allow_html=True)

        st.caption(f"Task ID: `{task_id}`")

    with right_col:
        st.markdown("### Description / Prompt")
        if task.description:
            st.markdown(task.description)
        else:
            st.info("No description.")

        if task.task_data:
            with st.expander("📥 Task Data", expanded=False):
                st.json(task.task_data)

        if task.result:
            with st.expander("📄 Result", expanded=False):
                st.json(task.result)

        with st.expander("📦 Artifacts", expanded=True):
            _render_task_artifacts(
                uuid.UUID(task_id),
                task.assigned_session_ids or [],
            )


# ── Project detail view ──────────────────────────────────────────────────────


def _render_project_detail(project_id: uuid.UUID, task_id: str | None) -> None:
    """Render the detail view for a single project."""
    with st.spinner("Loading project..."):
        data = run_coroutine_in_lit_worker(fetch_project_with_tasks(project_id), timeout=60)

    if data is None:
        st.error(f"Project not found: `{project_id}`")
        return

    project: AgentProject = data["project"]
    tasks: list[AgentTask] = data["tasks"]
    default_agent_name: str | None = data.get("default_agent_name")

    # If a specific task is selected, render task detail view
    if task_id:
        _render_task_detail(project_id, task_id, tasks, project_name=project.name, project_status=project.status)
        return

    # Breadcrumb
    st.markdown(f"{_internal_link('All Projects', '?/')} · {html.escape(project.name)}", unsafe_allow_html=True)

    status_emoji = _PROJECT_STATUS_EMOJI.get(project.status, "")
    st.markdown(f"## {status_emoji} {html.escape(project.name)}")

    col1, col2, col3 = st.columns(3)
    with col1:
        # Editable status dropdown
        status_options = list(AgentProjectStatus)
        status_display = {s: f"{_PROJECT_STATUS_EMOJI.get(s, '')} {s.value}" for s in status_options}
        current_status_index = status_options.index(project.status)

        status_label_col, status_select_col = st.columns([1, 2])
        with status_label_col:
            st.markdown("**Status:**")
        with status_select_col:
            selected_status = st.selectbox(
                "Status",
                options=status_options,
                index=current_status_index,
                format_func=lambda x: status_display.get(x, x.value),
                key=f"project_status_{project.agent_project_id}",
                label_visibility="collapsed",
            )

        if selected_status != project.status:
            with st.spinner("Updating status..."):
                success = run_coroutine_in_lit_worker(
                    update_project_status(project.agent_project_id, selected_status),
                    timeout=30,
                )
            if success:
                st.rerun()
            else:
                st.error("Failed to update status")

        # Inline creator selector (label and dropdown on same line)
        try:
            team_members = run_coroutine_in_lit_worker(fetch_team_members(), timeout=30)
        except Exception:
            team_members = []

        creator_options = ["(none)"] + [m["user_id"] for m in team_members]
        creator_display = {m["user_id"]: m["name"] for m in team_members}
        creator_display["(none)"] = "(none)"

        # Resolve creator to a comparable value - if not in team list, treat as "(none)"
        # to prevent silent erasure when the current value isn't representable
        current_creator = "(none)"
        if project.creator_user_id and project.creator_user_id in creator_options:
            current_creator = project.creator_user_id
        creator_index = creator_options.index(current_creator)

        creator_label_col, creator_select_col = st.columns([1, 2])
        with creator_label_col:
            st.markdown("**Creator:**")
        with creator_select_col:
            selected_creator = st.selectbox(
                "Creator",
                options=creator_options,
                index=creator_index,
                format_func=lambda x: creator_display.get(x, x),
                key=f"project_creator_{project.agent_project_id}",
                label_visibility="collapsed",
            )

        # Only update if user explicitly changed the selection
        if selected_creator != current_creator:
            new_creator = selected_creator if selected_creator != "(none)" else None
            with st.spinner("Updating..."):
                success = run_coroutine_in_lit_worker(
                    update_project_creator(project.agent_project_id, new_creator),
                    timeout=30,
                )
            if success:
                st.rerun()
            else:
                st.error("Failed to update")
    with col2:
        if project.budget_usd:
            spent = project.budget_spent_usd or Decimal(0)
            pct = (spent / project.budget_usd * 100) if project.budget_usd > 0 else 0
            st.markdown(f"**Budget:** ${project.budget_usd:.2f}")
            st.markdown(f"**Spent:** ${spent:.2f} ({pct:.1f}%)")
        else:
            st.markdown("**Budget:** No limit")
    with col3:
        created_ago = _time_ago(project.created_at)
        st.markdown(f"**Created:** {_to_local(project.created_at)} ({created_ago})")
        if project.slack_channel:
            st.markdown(f"**Slack:** #{project.slack_channel}")

        # Linear project link or export button
        linear_ref = _get_linear_project_ref(project)
        if linear_ref:
            linear_url = _linear_project_url(linear_ref["linear_project_id"])
            last_synced = linear_ref.get("last_synced_at", "")
            synced_label = f" (synced {last_synced[:10]})" if last_synced else ""
            st.markdown(f"**Linear:** [{html.escape(project.name)}]({linear_url}){synced_label}")
        else:
            if st.button("Export to Linear", key=f"export_linear_{project.agent_project_id}"):
                with st.spinner("Exporting project to Linear..."):
                    try:
                        from ypl.agent_harness_service.tools.linear_sync.export_to_linear import (
                            export_project_to_linear,
                        )

                        linear_project_id, sync_result = run_coroutine_in_lit_worker(
                            export_project_to_linear(
                                project_id=str(project.agent_project_id),
                                linear_team_id=_LINEAR_TEAM_ID_YUP,
                            ),
                            timeout=120,
                        )
                        st.success(
                            f"Exported to Linear! Created: {sync_result.created}, "
                            f"Updated: {sync_result.updated}, Errors: {sync_result.errors}"
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Export failed: {e}")

        # Inline default agent selector (label and dropdown on same line)
        try:
            agents = run_coroutine_in_lit_worker(fetch_available_agents(), timeout=30)
        except Exception:
            agents = []

        agent_options = ["(none)"] + [a["name"] for a in agents]
        agent_display = {a["name"]: f"{a['name']} ({a['display_name']})" for a in agents}
        agent_display["(none)"] = "(none)"

        # Resolve agent to a comparable value - if not in agent list, treat as "(none)"
        # to prevent silent erasure when the current value isn't representable
        current_agent = "(none)"
        if default_agent_name and default_agent_name in agent_options:
            current_agent = default_agent_name
        current_index = agent_options.index(current_agent)

        agent_label_col, agent_select_col = st.columns([1, 2])
        with agent_label_col:
            st.markdown("**Default Agent:**")
        with agent_select_col:
            selected_agent = st.selectbox(
                "Default agent",
                options=agent_options,
                index=current_index,
                format_func=lambda x: agent_display.get(x, x),
                key=f"project_default_agent_{project.agent_project_id}",
                label_visibility="collapsed",
            )

        # Only update if user explicitly changed the selection
        if selected_agent != current_agent:
            new_agent = selected_agent if selected_agent != "(none)" else None
            with st.spinner("Updating..."):
                success = run_coroutine_in_lit_worker(
                    update_project_agent(project.agent_project_id, new_agent),
                    timeout=30,
                )
            if success:
                st.rerun()
            else:
                st.error("Failed to update")

    if project.description:
        st.markdown(f"**Description:** {project.description}")

    st.caption(f"Project ID: `{project.agent_project_id}`")
    st.divider()

    task_title_map = {str(t.agent_task_id): t.title for t in tasks}
    children_map, all_task_ids = _build_children_map(tasks)
    rank_map = _compute_unique_ranks(children_map, all_task_ids)

    tab_tasks, tab_sessions, tab_deps, tab_shared_state, tab_budget, tab_settings = st.tabs(
        ["📋 Tasks", "🖥️ Sessions", "🔗 Dependencies", "🗄️ Shared State", "💰 Budget", "⚙️ Settings"]
    )

    with tab_tasks:
        if not tasks:
            st.info("No tasks in this project.")
        else:
            total = len(tasks)
            completed = sum(1 for t in tasks if t.status == AgentTaskStatus.COMPLETED)
            in_progress = sum(1 for t in tasks if t.status == AgentTaskStatus.IN_PROGRESS)
            in_review = sum(1 for t in tasks if t.status == AgentTaskStatus.IN_REVIEW)
            ready = sum(1 for t in tasks if t.status == AgentTaskStatus.READY)
            blocked = sum(1 for t in tasks if t.status == AgentTaskStatus.BLOCKED)

            summary_cols = st.columns(6)
            with summary_cols[0]:
                st.metric("Total", total)
            with summary_cols[1]:
                st.metric("Completed", completed)
            with summary_cols[2]:
                st.metric("In Progress", in_progress)
            with summary_cols[3]:
                st.metric("In Review", in_review)
            with summary_cols[4]:
                st.metric("Ready", ready)
            with summary_cols[5]:
                st.metric("Blocked", blocked)

            st.divider()

            st.markdown(_PAGE_CSS, unsafe_allow_html=True)
            st.markdown('<div class="task-list-container">', unsafe_allow_html=True)

            # Header row
            hdr_cols = st.columns([0.7, 0.6, 1, 2.5, 2.5, 0.7, 0.8, 0.8, 1])
            with hdr_cols[0]:
                st.markdown("**Status**")
            with hdr_cols[1]:
                st.markdown("**Rank**")
            with hdr_cols[2]:
                st.markdown("**Dep on**")
            with hdr_cols[3]:
                st.markdown("**Task**")
            with hdr_cols[4]:
                st.markdown("**Description**")
            with hdr_cols[5]:
                st.markdown("**PR**")
            with hdr_cols[6]:
                st.markdown("**Priority**")
            with hdr_cols[7]:
                st.markdown("**Created**")
            with hdr_cols[8]:
                st.markdown("**Session**")

            _render_task_list_recursive(
                parent_id=None,
                children_map=children_map,
                all_task_ids=all_task_ids,
                task_title_map=task_title_map,
                rank_map=rank_map,
                project_id=str(project.agent_project_id),
                depth=0,
            )

            st.markdown("</div>", unsafe_allow_html=True)

    with tab_sessions:
        session_status_options = ["(all)"] + [s.value for s in AgentSessionStatus]
        selected_session_status = st.selectbox(
            "Filter by status",
            session_status_options,
            index=0,
            key="session_status_filter",
        )

        status_filter = None
        if selected_session_status != "(all)":
            status_filter = AgentSessionStatus(selected_session_status)

        with st.spinner("Loading sessions..."):
            sessions_data = run_coroutine_in_lit_worker(
                fetch_project_sessions(project.agent_project_id, status_filter),
                timeout=60,
            )

        if not sessions_data:
            st.info("No sessions found for this project.")
        else:
            # Count by status
            active_count = sum(1 for s in sessions_data if s["session"].status == AgentSessionStatus.ACTIVE)
            completed_count = sum(1 for s in sessions_data if s["session"].status == AgentSessionStatus.COMPLETED)

            summary_cols = st.columns(3)
            with summary_cols[0]:
                st.metric("Total", len(sessions_data))
            with summary_cols[1]:
                st.metric("Active", active_count)
            with summary_cols[2]:
                st.metric("Completed", completed_count)

            st.divider()

            # Header row
            hdr_cols = st.columns([1.5, 1.5, 2, 1, 1.5])
            with hdr_cols[0]:
                st.markdown("**Session**")
            with hdr_cols[1]:
                st.markdown("**Agent**")
            with hdr_cols[2]:
                st.markdown("**Task**")
            with hdr_cols[3]:
                st.markdown("**Status**")
            with hdr_cols[4]:
                st.markdown("**Started**")

            for session_data in sessions_data:
                sess: AgentSession = session_data["session"]
                task: AgentTask | None = session_data["task"]
                agent_name: str = session_data["agent_name"]

                session_id_str = str(sess.agent_session_id)
                session_url = f"/agent_harness_console?session_id={session_id_str}"

                # Status styling
                status_emoji = _SESSION_STATUS_EMOJI.get(sess.status, "⚪")

                row_cols = st.columns([1.5, 1.5, 2, 1, 1.5])
                with row_cols[0]:
                    title_display = sess.title or session_id_str[:8]
                    st.markdown(_internal_link(html.escape(title_display), session_url), unsafe_allow_html=True)
                with row_cols[1]:
                    st.caption(html.escape(agent_name))
                with row_cols[2]:
                    if task:
                        st.caption(html.escape(task.title))
                    else:
                        st.caption("—")
                with row_cols[3]:
                    st.caption(f"{status_emoji} {sess.status.value}")
                with row_cols[4]:
                    st.caption(_time_ago(sess.created_at))

    with tab_deps:
        if not tasks:
            st.info("No tasks in this project.")
        else:
            has_deps = any(t.depends_on for t in tasks)
            if not has_deps:
                st.info("No task dependencies defined. All tasks are independent.")
            else:
                max_parent_depth = _get_max_depth(tasks)
                level_options = [str(lvl) for lvl in range(1, max_parent_depth + 2)] + ["all"]

                selected_level = st.radio(
                    "Show up to level",
                    level_options,
                    index=0,
                    horizontal=True,
                    key="dag_depth",
                )

                if selected_level == "all":
                    depth_limit = None
                else:
                    depth_limit = int(selected_level) - 1

                _render_dependency_dag(tasks, depth_limit)

    with tab_shared_state:
        if project.shared_state:
            st.json(project.shared_state)
        else:
            st.info("No shared state.")

    with tab_budget:
        if project.budget_usd:
            spent = project.budget_spent_usd or Decimal(0)
            remaining = project.budget_usd - spent
            pct_spent = (spent / project.budget_usd) if project.budget_usd > 0 else Decimal(0)
            pct_clamped = min(float(pct_spent), 1.0)

            st.progress(pct_clamped, text=f"${spent:.2f} / ${project.budget_usd:.2f}")

            budget_cols = st.columns(3)
            with budget_cols[0]:
                st.metric("Budget", f"${project.budget_usd:.2f}")
            with budget_cols[1]:
                st.metric("Spent", f"${spent:.2f}")
            with budget_cols[2]:
                st.metric("Remaining", f"${remaining:.2f}")

            st.divider()

            st.markdown("### Per-Task Spending")
            tasks_with_spending = [t for t in tasks if t.actual_spending_usd and t.actual_spending_usd > 0]
            if tasks_with_spending:
                for task in sorted(tasks_with_spending, key=lambda t: t.actual_spending_usd or 0, reverse=True):
                    s_emoji = _TASK_STATUS_EMOJI.get(task.status, "")
                    st.markdown(f"- {s_emoji} **{html.escape(task.title)}**: ${task.actual_spending_usd:.4f}")
            else:
                st.info("No task-level spending recorded yet.")
        else:
            st.info("This project has no budget limit configured.")

    with tab_settings:
        if project.project_data:
            with st.expander("📥 Project Data", expanded=True):
                st.json(project.project_data)
        else:
            st.info("No project data.")


# ── Project list view ────────────────────────────────────────────────────────


def _render_at_a_glance() -> None:
    """Render the 'At a Glance' section showing tasks needing attention from the current user's projects."""
    if not _current_email:
        return

    with st.spinner("Loading tasks needing attention..."):
        try:
            attention_tasks = run_coroutine_in_lit_worker(
                fetch_attention_tasks(_current_email),
                timeout=60,
            )
        except Exception as e:
            st.error(f"Error loading attention tasks: {e}")
            return

    if not attention_tasks:
        st.caption("No tasks needing attention.")
        return

    # Group by project
    tasks_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in attention_tasks:
        tasks_by_project[item["project_name"]].append(item)

    st.caption(f"{len(attention_tasks)} task(s) needing attention across {len(tasks_by_project)} project(s)")

    # Header row
    header_cols = st.columns([1.5, 0.8, 2.5, 0.7, 1, 1])
    with header_cols[0]:
        st.markdown("**Project**")
    with header_cols[1]:
        st.markdown("**Status**")
    with header_cols[2]:
        st.markdown("**Task**")
    with header_cols[3]:
        st.markdown("**PR**")
    with header_cols[4]:
        st.markdown("**Created**")
    with header_cols[5]:
        st.markdown("**Session**")

    for project_name, items in tasks_by_project.items():
        for idx, item in enumerate(items):
            task: AgentTask = item["task"]
            project_id = item["project_id"]
            task_id = str(task.agent_task_id)
            status_emoji = _TASK_STATUS_EMOJI.get(task.status, "")
            created_str = _time_ago(task.created_at)

            row_cols = st.columns([1.5, 0.8, 2.5, 0.7, 1, 1])

            with row_cols[0]:
                if idx == 0:
                    project_url = f"?project_id={project_id}"
                    st.markdown(_internal_link(html.escape(project_name), project_url), unsafe_allow_html=True)
                # Show nothing for subsequent tasks in same project group

            with row_cols[1]:
                st.caption(f"{status_emoji} {task.status.value}")

            with row_cols[2]:
                task_url = f"?project_id={project_id}&task_id={task_id}"
                title_md = _internal_link(html.escape(task.title), task_url)
                st.markdown(title_md, unsafe_allow_html=True)

            with row_cols[3]:
                if pr_link := _get_pr_link_parts(task.result):
                    st.markdown(f"[🔗 {pr_link[1]}]({pr_link[0]})")

            with row_cols[4]:
                st.caption(created_str)

            with row_cols[5]:
                if task.assigned_session_ids:
                    session_links = []
                    for sid in task.assigned_session_ids:
                        short_id = sid[:8]
                        lit_link = _internal_link(f"Lit {short_id}", f"/agent_harness_console?session_id={sid}")
                        wr_link = f'<a href="https://war-room.yuppster.ai/session/{sid}" target="_blank">WR</a>'
                        session_links.append(f"{lit_link} {wr_link}")
                    st.caption(" · ".join(session_links), unsafe_allow_html=True)


def _render_project_list() -> None:
    """Render the list view of all projects."""
    st.caption(f"Viewing all projects (admin: {_current_username or 'local dev'})")

    # At a Glance section
    st.subheader("At a Glance")
    _render_at_a_glance()

    # All Projects section
    st.subheader("All Projects")
    _render_project_list_filtered(show_mine_only=False, current_user_id=None)


def _render_project_list_filtered(show_mine_only: bool, current_user_id: str | None) -> None:
    """Render a filtered list of projects."""
    filter_cols = st.columns([2, 1, 1])

    with filter_cols[0]:
        status_options = ["(all)"] + [s.value for s in AgentProjectStatus]
        selected_status = st.selectbox(
            "Status",
            status_options,
            key=f"proj_status_{'mine' if show_mine_only else 'all'}",
        )

    with filter_cols[1]:
        limit_options = [20, 50, 100, 200]
        limit = st.selectbox(
            "Limit",
            limit_options,
            index=1,
            key=f"proj_limit_{'mine' if show_mine_only else 'all'}",
        )

    with filter_cols[2]:
        st.write("")

    creator_filter = current_user_id if show_mine_only else None

    with st.spinner("Loading projects..."):
        try:
            projects_data = run_coroutine_in_lit_worker(
                fetch_all_projects(
                    status=selected_status if selected_status != "(all)" else None,
                    creator_user_id=creator_filter,
                    limit=int(limit),
                ),
                timeout=60,
            )
        except Exception as e:
            st.error(f"Error loading projects: {e}")
            projects_data = []

    st.caption(f"{len(projects_data)} project(s) loaded")

    if not projects_data:
        st.info("No projects found matching the filters.")
        return

    show_details = st.checkbox(
        "Show Details",
        value=False,
        key=f"proj_details_{'mine' if show_mine_only else 'all'}",
    )

    # Header row
    header_cols = st.columns([1, 1.5, 3, 1, 1, 1])
    with header_cols[0]:
        st.markdown("**Status**")
    with header_cols[1]:
        st.markdown("**Project**")
    with header_cols[2]:
        st.markdown("**Description**")
    with header_cols[3]:
        st.markdown("**Created by**")
    with header_cols[4]:
        st.markdown("**Progress**")
    with header_cols[5]:
        st.markdown("**Budget**")

    for proj_data in projects_data:
        project: AgentProject = proj_data["project"]
        task_count: int = proj_data["task_count"]
        completed_count: int = proj_data["completed_count"]
        creator_name: str = proj_data["creator_name"]

        status_emoji = _PROJECT_STATUS_EMOJI.get(project.status, "")
        progress_str = f"{completed_count}/{task_count} tasks" if task_count > 0 else "No tasks"

        budget_str = "No limit"
        if project.budget_usd:
            spent = project.budget_spent_usd or Decimal(0)
            budget_str = f"${spent:.2f}/${project.budget_usd:.2f}"

        desc_preview = ""
        if project.description:
            desc_preview = project.description[:120]
            if len(project.description) > 120:
                desc_preview += "..."

        row_cols = st.columns([1, 1.5, 3, 1, 1, 1])

        with row_cols[0]:
            st.write(f"{status_emoji} {project.status.value}")

        with row_cols[1]:
            project_url = f"?project_id={project.agent_project_id}"
            st.markdown(_internal_link(html.escape(project.name), project_url), unsafe_allow_html=True)

        with row_cols[2]:
            if desc_preview:
                st.caption(desc_preview)

        with row_cols[3]:
            st.caption(html.escape(creator_name))

        with row_cols[4]:
            st.caption(progress_str)

        with row_cols[5]:
            st.caption(budget_str)

        if show_details:
            detail_col1, detail_col2 = st.columns(2)

            with detail_col1:
                st.markdown(f"**Status:** {project.status.value}")
                if project.slack_channel:
                    st.markdown(f"**Slack:** #{project.slack_channel}")

            with detail_col2:
                if project.budget_usd:
                    spent = project.budget_spent_usd or Decimal(0)
                    pct = (spent / project.budget_usd * 100) if project.budget_usd > 0 else 0
                    st.markdown(f"**Budget:** ${project.budget_usd:.2f}")
                    st.markdown(f"**Spent:** ${spent:.2f} ({pct:.1f}%)")
                else:
                    st.markdown("**Budget:** No limit")

                if task_count > 0:
                    pct_complete = completed_count / task_count
                    st.progress(pct_complete, text=f"{completed_count}/{task_count} completed")

            if project.description:
                st.markdown(f"**Description:** {project.description}")

            created_ago = _time_ago(project.created_at)
            st.caption(f"Created: {_to_local(project.created_at)} ({created_ago})")
            st.caption(f"Project ID: `{project.agent_project_id}`")
            st.divider()


# ── Main page logic ──────────────────────────────────────────────────────────

st.title("Agent Projects")

qp = st.query_params
url_project_id = qp.get("project_id", None)
url_task_id = qp.get("task_id", None)

if url_project_id:
    try:
        project_uuid = uuid.UUID(url_project_id.strip())
    except ValueError:
        st.error(f"Invalid project ID: `{url_project_id}`")
        st.stop()

    _render_project_detail(project_uuid, task_id=url_task_id)
else:
    _render_project_list()
