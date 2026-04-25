"""Agent memory search constants and stub.

Only the constants and the callable signature used by AHS are exposed here;
hybrid/semantic search is not yet wired up in yupp-agent.
"""

from typing import Any

PRIVATE_TOPIC_PREFIX = "PRIVATE"


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
