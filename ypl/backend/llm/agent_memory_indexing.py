"""Agent memory indexing stub.

The full indexing implementation lives in yupp-mind and requires its DB/embedding
stack (pgvector, Gemini embeddings, ypl.db.agent_memory_index models).

This stub provides a no-op so that imports in yupp-agent services don't crash.
GCS remains the source of truth; search indexing is handled by yupp-mind.
"""


async def index_topic_sections(
    topic: str,
    content: str,
    agent_name: str | None = None,
    source_generation: int | None = None,
) -> int:
    """No-op stub. Indexing is handled by yupp-mind; returns 0 sections indexed."""
    return 0
