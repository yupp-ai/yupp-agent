import uuid
from datetime import datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import UUID, Column, ForeignKey
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlmodel import Field, Relationship

from ypl.db.base import BaseModel


class AgentMemorySection(BaseModel, table=True):
    """A section within an agent memory topic, used as a search index.

    GCS remains the source of truth for agent memory files.
    This table is a search index populated via dual-write.
    """

    __tablename__ = "agent_memory_sections"

    # Needed for TSVECTOR type
    class Config:
        arbitrary_types_allowed = True

    agent_memory_section_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    topic: str = Field(nullable=False, index=True)
    section_key: str = Field(nullable=False)
    section_title: str | None = Field(default=None, nullable=True)
    content: str = Field(nullable=False, sa_type=sa.Text)
    content_tsvector: TSVECTOR | None = Field(default=None, sa_column=Column(TSVECTOR, nullable=True))
    agent_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(UUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True),
    )
    source_generation: int | None = Field(default=None, sa_type=sa.BigInteger, nullable=True)
    last_indexed_at: datetime | None = Field(default=None, nullable=True, sa_type=sa.DateTime(timezone=True))  # type: ignore

    embeddings: list["AgentMemorySectionEmbedding"] = Relationship(back_populates="section")

    __table_args__ = (
        sa.UniqueConstraint("topic", "section_key", name="uq_agent_memory_sections_topic_section_key"),
        sa.Index("ix_agent_memory_sections_content_tsvector", "content_tsvector", postgresql_using="gin"),
        sa.Index(
            "ix_agent_memory_sections_topic_section_key_live",
            "topic",
            "section_key",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
    )


class AgentMemorySectionEmbedding(BaseModel, table=True):
    """Embedding for an agent memory section, used for semantic search."""

    __tablename__ = "agent_memory_section_embeddings"

    agent_memory_section_embedding_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_memory_section_id: uuid.UUID = Field(
        sa_column=Column(
            UUID(as_uuid=True),
            ForeignKey("agent_memory_sections.agent_memory_section_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    section: AgentMemorySection = Relationship(back_populates="embeddings")
    embedding: Vector | list[float] = Field(sa_column=Column(Vector(1536), nullable=False))
    embedding_model_name: str = Field(nullable=False)

    class Config:
        arbitrary_types_allowed = True

    __table_args__ = (
        sa.UniqueConstraint(
            "agent_memory_section_id",
            "embedding_model_name",
            name="uq_agent_memory_section_embeddings_section_model",
        ),
        sa.Index("ix_agent_memory_section_embeddings_model_name", "embedding_model_name"),
    )
