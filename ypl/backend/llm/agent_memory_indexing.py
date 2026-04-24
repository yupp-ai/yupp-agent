"""Agent memory indexing stub.

Pgvector/embedding-based indexing is not wired up in yupp-agent yet. GCS is
the source of truth for memory content; this stub keeps imports working and
returns zero so callers can treat indexing as best-effort.
"""


async def index_topic_sections(
    topic: str,
    content: str,
    agent_name: str | None = None,
    source_generation: int | None = None,
) -> int:
    """No-op stub; returns 0 sections indexed."""
    return 0
