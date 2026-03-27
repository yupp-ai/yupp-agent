"""Request/response models for Agent Project & Task REST API endpoints."""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# Shared response shapes
# ============================================================================


class TaskSummary(BaseModel):
    """Counts of tasks by status for a project."""

    PENDING: int = 0
    BLOCKED: int = 0
    READY: int = 0
    IN_PROGRESS: int = 0
    IN_REVIEW: int = 0
    COMPLETED: int = 0
    FAILED: int = 0
    CANCELLED: int = 0
    total: int = 0


class ProjectResponse(BaseModel):
    """A project with creator info and task summary."""

    agent_project_id: str
    name: str
    description: str | None = None
    status: str
    creator_user_id: str | None = None
    creator_user_name: str | None = None
    slack_channel: str | None = None
    budget_usd: Decimal | None = None
    budget_spent_usd: Decimal = Decimal(0)
    created_at: datetime | None = None
    task_summary: TaskSummary = Field(default_factory=TaskSummary)


class ProjectDetailResponse(ProjectResponse):
    """Project detail includes shared_state."""

    shared_state: dict[str, Any] | None = None


class TaskResponse(BaseModel):
    """A task with agent and creator info."""

    agent_task_id: str
    agent_project_id: str
    title: str
    description: str | None = None
    status: str
    priority: str
    parent_task_id: str | None = None
    depends_on: list[str] | None = None
    agent_name: str | None = None
    agent_id: str | None = None
    assigned_session_ids: list[str] | None = None
    result: dict[str, Any] | None = None
    task_data: dict[str, Any] | None = None
    estimated_effort: str | None = None
    actual_spending_usd: Decimal | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    creator_user_id: str | None = None
    creator_user_name: str | None = None


# ============================================================================
# List responses (paginated)
# ============================================================================


class ProjectListResponse(BaseModel):
    """Paginated list of projects."""

    items: list[ProjectResponse]
    total: int
    offset: int
    limit: int


class TaskListResponse(BaseModel):
    """Paginated list of tasks with summary."""

    items: list[TaskResponse]
    total: int
    offset: int
    limit: int
    task_summary: TaskSummary = Field(default_factory=TaskSummary)


# ============================================================================
# Mutation requests
# ============================================================================


class ProjectUpdateRequest(BaseModel):
    """PATCH /ahs/projects/{project_id} — partial update."""

    name: str | None = None
    description: str | None = None
    slack_channel: str | None = None


class ProjectStatusRequest(BaseModel):
    """POST /ahs/projects/{project_id}/status — change project status."""

    status: str = Field(..., description="ACTIVE | PAUSED | COMPLETED | ARCHIVED")


class TaskUpdateRequest(BaseModel):
    """PATCH /ahs/projects/{project_id}/tasks/{task_id} — partial update."""

    title: str | None = None
    description: str | None = None
    priority: str | None = Field(None, description="URGENT | HIGH | NORMAL | LOW")
    agent_name: str | None = Field(None, description="Agent name, empty string to unassign")
    task_data: dict[str, Any] | None = None
    estimated_effort: str | None = None


class TaskStatusRequest(BaseModel):
    """POST /ahs/projects/{project_id}/tasks/{task_id}/status — change task status."""

    status: str = Field(
        ..., description="PENDING | BLOCKED | READY | IN_PROGRESS | IN_REVIEW | COMPLETED | FAILED | CANCELLED"
    )
    result: dict[str, Any] | None = None


class TaskStatusResponse(BaseModel):
    """Response from changing task status."""

    agent_task_id: str
    status: str
    newly_ready_tasks: list[str] = Field(default_factory=list)


class TaskDependenciesRequest(BaseModel):
    """PUT /ahs/projects/{project_id}/tasks/{task_id}/dependencies."""

    depends_on: list[str] = Field(..., description="Task UUIDs to depend on, empty list to clear")


class TaskResumeResponse(BaseModel):
    """Response from resuming a failed task."""

    agent_task_id: str
    status: str
    session_to_resume: str | None = None
