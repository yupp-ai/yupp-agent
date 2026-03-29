"""Agent memory search constants and stub.

The full search implementation is in the original file from yupp-mind.
This stub provides only what AHS needs.
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

    The full implementation lives in yupp-mind. This stub exists so that
    imports don't break in yupp-agent services.
    """
    return []
