"""REST API routes for Agent Project & Task management.

Endpoints:
- GET    /projects                                  — list projects
- GET    /projects/{project_id}                     — get project detail
- PATCH  /projects/{project_id}                     — update project fields
- POST   /projects/{project_id}/status              — change project status
- GET    /projects/{project_id}/tasks               — list tasks in project
- GET    /projects/{project_id}/tasks/{task_id}     — get task detail
- PATCH  /projects/{project_id}/tasks/{task_id}     — update task fields
- POST   /projects/{project_id}/tasks/{task_id}/status        — change task status
- PUT    /projects/{project_id}/tasks/{task_id}/dependencies  — set task dependencies
- POST   /projects/{project_id}/tasks/{task_id}/restart       — restart task (clear state, reset to READY)
- POST   /projects/{project_id}/tasks/{task_id}/resume        — resume failed task
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from ypl.agent_harness_service.common.auth import verify_api_key
from ypl.agent_harness_service.projects.project_service import (
    get_project_service,
    get_task_service,
    list_projects_service,
    list_tasks_service,
    restart_task_service,
    resume_task_service,
    set_project_status_service,
    set_task_dependencies_service,
    set_task_status_service,
    update_project_service,
    update_task_service,
)
from ypl.agent_harness_service.projects.project_types import (
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectStatusRequest,
    ProjectUpdateRequest,
    TaskDependenciesRequest,
    TaskListResponse,
    TaskResponse,
    TaskResumeResponse,
    TaskStatusRequest,
    TaskStatusResponse,
    TaskUpdateRequest,
)

project_router = APIRouter(prefix="/projects", tags=["agent-projects"])


# ============================================================================
# Projects
# ============================================================================


@project_router.get(
    "",
    dependencies=[Depends(verify_api_key)],
)
async def list_projects_route(
    status: str | None = Query(None, description="Filter by project status: ACTIVE, PAUSED, COMPLETED, ARCHIVED"),
    creator_user_id: str | None = Query(None, description="Filter by creator user ID"),
    limit: int = Query(20, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> ProjectListResponse:
    """List projects with optional filters and pagination."""
    try:
        return await list_projects_service(status=status, creator_user_id=creator_user_id, limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@project_router.get(
    "/{project_id}",
    dependencies=[Depends(verify_api_key)],
)
async def get_project_route(project_id: str) -> ProjectDetailResponse:
    """Get project detail with shared_state and task summary."""
    try:
        return await get_project_service(project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.patch(
    "/{project_id}",
    dependencies=[Depends(verify_api_key)],
)
async def update_project_route(project_id: str, request: ProjectUpdateRequest) -> ProjectDetailResponse:
    """Update a project's mutable fields (name, description, slack_channel)."""
    try:
        return await update_project_service(project_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.post(
    "/{project_id}/status",
    dependencies=[Depends(verify_api_key)],
)
async def set_project_status_route(project_id: str, request: ProjectStatusRequest) -> ProjectDetailResponse:
    """Change a project's status."""
    try:
        return await set_project_status_service(project_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


# ============================================================================
# Tasks
# ============================================================================


@project_router.get(
    "/{project_id}/tasks",
    dependencies=[Depends(verify_api_key)],
)
async def list_tasks_route(
    project_id: str,
    status: str | None = Query(None, description="Filter by task status"),
    parent_task_id: str | None = Query(None, description="Filter by parent task ID"),
    limit: int = Query(100, ge=1, le=500, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> TaskListResponse:
    """List all tasks in a project with optional filters and pagination."""
    try:
        return await list_tasks_service(
            project_id, status=status, parent_task_id=parent_task_id, limit=limit, offset=offset
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.get(
    "/{project_id}/tasks/{task_id}",
    dependencies=[Depends(verify_api_key)],
)
async def get_task_route(project_id: str, task_id: str) -> TaskResponse:
    """Get task detail."""
    try:
        return await get_task_service(project_id, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.patch(
    "/{project_id}/tasks/{task_id}",
    dependencies=[Depends(verify_api_key)],
)
async def update_task_route(project_id: str, task_id: str, request: TaskUpdateRequest) -> TaskResponse:
    """Update a task's mutable fields."""
    try:
        return await update_task_service(project_id, task_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.post(
    "/{project_id}/tasks/{task_id}/status",
    dependencies=[Depends(verify_api_key)],
)
async def set_task_status_route(project_id: str, task_id: str, request: TaskStatusRequest) -> TaskStatusResponse:
    """Change a task's status with validation and dependency cascade."""
    try:
        return await set_task_status_service(project_id, task_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.put(
    "/{project_id}/tasks/{task_id}/dependencies",
    dependencies=[Depends(verify_api_key)],
)
async def set_task_dependencies_route(project_id: str, task_id: str, request: TaskDependenciesRequest) -> TaskResponse:
    """Replace a task's dependency list."""
    try:
        return await set_task_dependencies_service(project_id, task_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.post(
    "/{project_id}/tasks/{task_id}/restart",
    dependencies=[Depends(verify_api_key)],
)
async def restart_task_route(project_id: str, task_id: str) -> TaskResponse:
    """Restart a task: clear result/spending/sessions and reset to READY (or PENDING if deps unmet)."""
    try:
        return await restart_task_service(project_id, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@project_router.post(
    "/{project_id}/tasks/{task_id}/resume",
    dependencies=[Depends(verify_api_key)],
)
async def resume_task_route(project_id: str, task_id: str) -> TaskResumeResponse:
    """Resume a failed task that hit the turn limit."""
    try:
        return await resume_task_service(project_id, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
