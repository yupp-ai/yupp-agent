"""Stub debugging_utils for yupp-agent.

Provides the minimal interface used by health.py and taskiq.py.
"""

from pydantic import BaseModel


class AsyncTaskList(BaseModel):
    """Minimal stub."""

    tasks: list[str] = []


async def get_async_task_statuses() -> AsyncTaskList:
    return AsyncTaskList()


async def get_resource_leak_info() -> dict:
    return {}


async def log_resource_info() -> None:
    pass
