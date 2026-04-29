"""Agent memory search stub.

Only the callable signature used by AHS is exposed here; hybrid/semantic
search is not yet wired up in yupp-agent. The follow-up "memory section
search" project replaces this stub once section-level search is
re-implemented on top of MEMORY artifacts.
"""

from typing import Any


async def search_agent_memory(
    query: str,
    mode: str = "hybrid",
    top_k: int = 20,
    topic: str | None = None,
    full_content: bool = False,
    scope: str = "all",
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    """Stub for agent memory search. Returns empty results.

    Hybrid/semantic search is not yet wired up in yupp-agent; this stub
    keeps callers working until it is.
    """
    return []
