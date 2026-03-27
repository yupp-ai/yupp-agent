"""Stub debugging_utils for yupp-agent.

Provides the minimal interface used by health.py and taskiq.py.
"""

from pydantic import BaseModel


class AsyncTaskList(BaseModel):
    """Minimal stub."""

    tasks: list[str] = []
    num_active_tasks: int = 0


def get_async_task_statuses() -> AsyncTaskList:
    return AsyncTaskList()


def get_resource_leak_info(top_n: int = 50) -> dict:
    return {}


async def log_resource_info(*args: object, **kwargs: object) -> None:
    pass
