"""IQL query models, per-type result models, and response types for AHS search.

Search flow:
  SearchRequest  →  (parsed to)  SearchQuery  →  SearchResponse
                                                       └── dict[SearchType, list[<Result>]]

Each result type carries a ``score`` (additive relevance, typically 0.0–2.1) and a ``created_at``
timestamp alongside type-specific metadata so callers can render rich snippets
without a second round-trip.
"""

from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# SearchType enum
# ---------------------------------------------------------------------------


class SearchType(StrEnum):
    """Searchable entity types across the Agent Harness Service."""

    SESSIONS = "sessions"
    SESSION_MESSAGES = "session_messages"
    PROJECTS = "projects"
    TASKS = "tasks"
    SCHEDULES = "schedules"
    ARTIFACT_PR = "artifact_pr"
    ARTIFACT_DOC = "artifact_doc"


# ---------------------------------------------------------------------------
# SearchQuery (IQL)
# ---------------------------------------------------------------------------


class SearchQuery(BaseModel):
    """Structured search query (Internal Query Language).

    All filter fields are optional and combined with AND semantics.
    ``q`` is the free-text search term, analysed with BM25 + vector similarity.
    """

    q: str = Field(..., description="Free-text search term (required)")
    types: list[SearchType] | None = Field(
        None,
        description="Entity types to search. Searches all types when omitted.",
    )
    agent: str | None = Field(
        None,
        description="Filter by agent name (e.g. 'eng-raccoon', 'sre').",
    )
    creator_user_id: str | None = Field(
        None,
        description="Filter by the Yupp user ID of the entity creator.",
    )
    status: str | None = Field(
        None,
        description=(
            "Filter by entity status. Interpretation is type-specific "
            "(e.g. 'COMPLETED' for tasks, 'active' for schedules)."
        ),
    )
    date_from: datetime | None = Field(
        None,
        description="Inclusive lower bound on created_at (UTC).",
    )
    date_to: datetime | None = Field(
        None,
        description="Inclusive upper bound on created_at (UTC).",
    )
    limit_per_type: Annotated[int, Field(ge=1, le=50)] = Field(
        10,
        description="Maximum results to return per entity type (1–50, default 10).",
    )

    @model_validator(mode="after")
    def _validate_q_not_empty(self) -> SearchQuery:
        if not self.q.strip():
            raise ValueError("Search query 'q' cannot be empty or whitespace-only.")
        return self


# ---------------------------------------------------------------------------
# Per-type result models
# ---------------------------------------------------------------------------


class SessionResult(BaseModel):
    """A single session matched by search."""

    id: str = Field(..., description="Session UUID")
    title: str | None = Field(None, description="Session title (auto-generated or user-set)")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field — must be Literal for discriminated union to work.
    type: Literal[SearchType.SESSIONS] = SearchType.SESSIONS

    # Type-specific metadata
    agent_name: str | None = None
    status: str | None = None
    trigger: str | None = Field(None, description="How the session was triggered: slack, api, cron, …")
    model: str | None = None
    message_count: int = 0
    parent_session_id: str | None = None


class SessionMessageResult(BaseModel):
    """A single session message matched by search, with a text snippet."""

    id: str = Field(..., description="Message UUID")
    title: str | None = Field(None, description="Synthesised label, e.g. 'Turn 3 — assistant'")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.SESSION_MESSAGES] = SearchType.SESSION_MESSAGES

    # Type-specific metadata
    session_id: str = Field(..., description="Parent session UUID")
    session_title: str | None = None
    role: str | None = Field(None, description="'user' or 'assistant'")
    turn_number: int | None = None
    snippet: str | None = Field(
        None,
        description="Short excerpt of matching message content (≤300 chars, highlights query terms).",
    )


class ProjectResult(BaseModel):
    """A single project matched by search."""

    id: str = Field(..., description="Project UUID (agent_project_id)")
    name: str = Field(..., description="Project name")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.PROJECTS] = SearchType.PROJECTS

    # Type-specific metadata
    description: str | None = None
    status: str | None = None
    creator_user_id: str | None = None
    slack_channel: str | None = None
    task_counts: dict[str, int] | None = Field(
        None,
        description="Task counts by status, e.g. {'COMPLETED': 4, 'IN_PROGRESS': 1}.",
    )


class TaskResult(BaseModel):
    """A single task matched by search, nested under its parent project."""

    id: str = Field(..., description="Task UUID (agent_task_id)")
    title: str = Field(..., description="Task title")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.TASKS] = SearchType.TASKS

    # Type-specific metadata
    project_id: str = Field(..., description="Parent project UUID")
    project_name: str | None = Field(None, description="Parent project name (denormalised for display)")
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    agent_name: str | None = None
    depends_on: list[str] | None = None
    completed_at: datetime | None = None


class ScheduleResult(BaseModel):
    """A single agent schedule matched by search."""

    id: str = Field(..., description="Schedule UUID (agent_schedule_id)")
    name: str | None = Field(None, description="Human-readable schedule name")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.SCHEDULES] = SearchType.SCHEDULES

    # Type-specific metadata
    agent_name: str | None = None
    schedule_type: str | None = Field(None, description="'one_time' or 'recurring'")
    status: str | None = None
    cron_expression: str | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    created_by_user: str | None = None


class ArtifactPRResult(BaseModel):
    """A single GitHub PR artifact matched by search."""

    id: str = Field(..., description="Artifact UUID or PR identifier")
    title: str | None = Field(None, description="PR title")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.ARTIFACT_PR] = SearchType.ARTIFACT_PR

    # Type-specific metadata
    url: str | None = Field(None, description="Public URL to the PR")
    session_id: str | None = Field(None, description="Session that produced this artifact")
    project_id: str | None = Field(None, description="Project this artifact belongs to")
    pr_number: int | None = Field(None, description="GitHub PR number")
    repo: str | None = Field(None, description="GitHub repository (owner/name)")
    extra: dict[str, Any] | None = Field(
        None,
        description="PR-specific metadata (e.g. state, merged_at).",
    )


class ArtifactDocResult(BaseModel):
    """A single document artifact (textual artifact, file, report, …) matched by search."""

    id: str = Field(..., description="Artifact UUID or external identifier")
    title: str | None = Field(None, description="Document title or filename")
    score: float = Field(
        ...,
        description="Additive relevance score (max ~2.1: title +1.0, desc +0.5, data +0.3, agent +0.2, recency +0.1)",
    )
    created_at: datetime | None = None

    # Discriminator field
    type: Literal[SearchType.ARTIFACT_DOC] = SearchType.ARTIFACT_DOC

    # Type-specific metadata
    url: str | None = Field(None, description="Public or internal URL to the document")
    session_id: str | None = Field(None, description="Session that produced this artifact")
    project_id: str | None = Field(None, description="Project this artifact belongs to")
    size_bytes: int | None = None
    extra: dict[str, Any] | None = Field(
        None,
        description="Document-type-specific metadata (e.g. paste language, file extension).",
    )


# Discriminated union of all per-type result models.
# Each model uses a Literal ``type`` field so Pydantic can resolve the correct
# model unambiguously without falling back to left-to-right coercion.
AnySearchResult = Annotated[
    SessionResult
    | SessionMessageResult
    | ProjectResult
    | TaskResult
    | ScheduleResult
    | ArtifactPRResult
    | ArtifactDocResult,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# SearchResponse
# ---------------------------------------------------------------------------


class SearchResponse(BaseModel):
    """Top-level response from a search request.

    ``results`` maps each searched ``SearchType`` to the list of matching
    entities for that type.  Types with zero matches are omitted from the dict.
    """

    query: SearchQuery
    results: dict[SearchType, list[AnySearchResult]] = Field(
        default_factory=dict,
        description="Per-type result lists. Keys are SearchType values present in the response.",
    )
    total_count: int = Field(
        0,
        description="Total number of results across all types.",
    )

    @model_validator(mode="after")
    def _validate_result_types_match_keys(self) -> SearchResponse:
        """Ensure every item in a bucket has a ``type`` matching the bucket key.

        This enforces the documented invariant that ``results[SearchType.X]``
        contains only items whose ``type == SearchType.X``, catching
        mis-bucketed payloads before they reach consumers.
        """
        for key, items in self.results.items():
            for item in items:
                if item.type != key:
                    raise ValueError(
                        f"Result bucket '{key}' contains an item with type '{item.type}'. "
                        "Each item's type must match its containing bucket key."
                    )
        return self


# ---------------------------------------------------------------------------
# SearchRequest
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """Entry-point request model for POST /search.

    Callers supply *either* a pre-built ``SearchQuery`` *or* a plain ``text``
    string (which is promoted to a ``SearchQuery`` with all defaults).
    At least one of the two fields must be present.
    """

    query: SearchQuery | None = Field(
        None,
        description="Fully structured IQL query. Takes precedence over 'text' if both are provided.",
    )
    text: str | None = Field(
        None,
        description="Free-text shorthand. Promoted to SearchQuery(q=text) with all filter defaults.",
    )

    @model_validator(mode="after")
    def _require_query_or_text(self) -> SearchRequest:
        if self.query is None and (self.text is None or self.text.strip() == ""):
            raise ValueError("SearchRequest requires at least one of 'query' or 'text'.")
        # Promote bare text to a full SearchQuery so downstream code always
        # works with a SearchQuery object.
        if self.query is None and self.text:
            self.query = SearchQuery(q=self.text.strip())
        return self

    def resolved_query(self) -> SearchQuery:
        """Return the effective SearchQuery (always populated after validation)."""
        if self.query is None:
            raise RuntimeError("query must be set after model validation")
        return self.query
