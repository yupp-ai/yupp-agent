"""Database models for Agent Harness Service.

Tables:
- agents: Agent definitions with config paths and metadata
- agent_sessions: Conversation sessions tied to agents
- agent_session_messages: Individual messages within sessions (user + agent)
- agent_feedbacks: Feedback signals on sessions or individual messages
- agent_schedules: Scheduled/recurring agent calls
- agent_schedule_runs: Execution history for agent schedules
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Column, Index
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, Relationship

from ypl.db.base import BaseModel


class AgentArtifactType(str, enum.Enum):
    """Type of artifact tracked in the agent artifact registry."""

    YUPPASTE = "YUPPASTE"  # Text content (investigations, reports, summaries)
    CODE_REVIEW = "CODE_REVIEW"  # GitHub PR review
    OTHER = "OTHER"  # Catch-all for future types


class AgentSessionTrigger(str, enum.Enum):
    """How the session was initiated."""

    SLACK = "SLACK"
    WEBHOOK = "WEBHOOK"
    CRON = "CRON"
    API = "API"
    TASK = "TASK"  # Triggered by a project task executor


class AgentSessionStatus(str, enum.Enum):
    """Lifecycle status of a session."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    STALE = "STALE"


class AgentSessionMessageRole(str, enum.Enum):
    """Who authored the message."""

    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class AgentSessionMessageCompletionStatus(str, enum.Enum):
    """Lifecycle state of an individual message row.

    - IN_PROGRESS: AGENT eager-persist draft written at turn start for streaming;
      the CLI is still running.  Must not be counted as a completed response.
    - SUCCESS: Message is fully written and represents a successful outcome.
    - FAILED: Turn completed with an error condition (see error_type for details).
    - ABORTED: Turn was stopped mid-execution by a user /stop command.

    USER and SYSTEM messages default to SUCCESS — they have no draft state.
    """

    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class AgentSessionMessageErrorType(str, enum.Enum):
    """Reason a turn failed.  Only meaningful when completion_status == FAILED.

    - NONE: No error (used for IN_PROGRESS, SUCCESS, ABORTED).
    - ERROR_MAX_TURNS: Agent hit the configured max_turns limit.
    - ERROR_CONTEXT_OVERFLOW: Agent ran out of context window space.
    - ERROR_EXECUTOR: Exception thrown by the CLI or raw executor.
    - ERROR_INTERNAL: AHS-internal failure (bad config, dispatch error, etc.).
    """

    NONE = "NONE"
    ERROR_MAX_TURNS = "ERROR_MAX_TURNS"
    ERROR_CONTEXT_OVERFLOW = "ERROR_CONTEXT_OVERFLOW"
    ERROR_EXECUTOR = "ERROR_EXECUTOR"
    ERROR_INTERNAL = "ERROR_INTERNAL"


class AgentFeedbackRating(str, enum.Enum):
    """Predefined feedback rating values for easy aggregation."""

    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class AgentExecutorType(str, enum.Enum):
    """How the agent is executed."""

    HARNESSED = "HARNESSED"
    RAW = "RAW"


class AgentScheduleType(str, enum.Enum):
    """Type of agent schedule."""

    SCHEDULED = "SCHEDULED"  # One-time execution at a specific time
    RECURRING = "RECURRING"  # Recurring execution via cron expression


class AgentScheduleStatus(str, enum.Enum):
    """Status of an agent schedule (the schedule itself, not individual runs)."""

    PENDING = "PENDING"  # Ready for next run
    IN_PROGRESS = "IN_PROGRESS"  # Currently executing (use AgentScheduleRunStatus for run-level status)
    COMPLETED = "COMPLETED"  # Terminal: SCHEDULED finished or RECURRING max_runs reached
    FAILED = "FAILED"  # Terminal failure (use AgentScheduleRunStatus for run-level errors)
    CANCELLED = "CANCELLED"  # User cancelled
    PAUSED = "PAUSED"  # Temporarily paused (for RECURRING jobs)


class AgentScheduleRunStatus(str, enum.Enum):
    """Status of a single execution of an agent schedule."""

    PENDING = "PENDING"  # Claimed, waiting to execute
    IN_PROGRESS = "IN_PROGRESS"  # Currently executing
    COMPLETED = "COMPLETED"  # Finished successfully
    FAILED = "FAILED"  # Finished with error


class AgentProjectStatus(str, enum.Enum):
    """Lifecycle status of a project."""

    ACTIVE = "ACTIVE"  # Project is actively being worked on
    PAUSED = "PAUSED"  # Temporarily paused (also the default for newly created projects)
    COMPLETED = "COMPLETED"  # All tasks completed, goal achieved
    ARCHIVED = "ARCHIVED"  # No longer active, kept for reference


class AgentTaskStatus(str, enum.Enum):
    """Lifecycle status of a task within a project."""

    PENDING = "PENDING"  # Created, not yet ready to start
    BLOCKED = "BLOCKED"  # Waiting on dependencies
    READY = "READY"  # Dependencies met, can be claimed
    IN_PROGRESS = "IN_PROGRESS"  # Currently being executed
    COMPLETED = "COMPLETED"  # Successfully finished
    FAILED = "FAILED"  # Finished with error
    CANCELLED = "CANCELLED"  # Manually cancelled
    IN_REVIEW = "IN_REVIEW"  # Agent finished, awaiting human review


class AgentTaskPriority(int, enum.Enum):
    """Priority levels for tasks."""

    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


class Agent(BaseModel, table=True):
    """An agent definition with its config and identity."""

    __tablename__ = "agents"

    agent_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    name: str = Field(nullable=False, sa_type=sa.Text, unique=True, index=True)
    display_name: str = Field(nullable=False, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)

    executor_type: AgentExecutorType = Field(
        default=AgentExecutorType.HARNESSED,
        sa_column=Column(
            sa.Enum(AgentExecutorType),
            nullable=False,
            server_default=AgentExecutorType.HARNESSED.name,
        ),
    )
    executor_model: str | None = Field(default=None, sa_type=sa.Text)

    config: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    sessions: list["AgentSession"] = Relationship(back_populates="agent")

    # User who created this agent (no FK constraint — user may not exist in this DB)
    creator_user_id: str | None = Field(default=None, nullable=True, sa_type=sa.Text, index=True)

    additional_system_prompt: str | None = Field(default=None, sa_type=sa.Text)


class AgentSession(BaseModel, table=True):
    """A conversation session between a user and an agent."""

    __tablename__ = "agent_sessions"

    agent_session_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_id: uuid.UUID = Field(foreign_key="agents.agent_id", nullable=False, index=True)
    parent_session_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("agent_sessions.agent_session_id"), nullable=True, index=True),
    )
    # User who created this session (no FK constraint — user may not exist in this DB)
    creator_user_id: str | None = Field(default=None, nullable=True, sa_type=sa.Text, index=True)
    slack_session_id: str | None = Field(default=None, sa_type=sa.Text, unique=True)
    trigger: AgentSessionTrigger = Field(sa_column=Column(sa.Enum(AgentSessionTrigger), nullable=False))
    context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    workspace: str | None = Field(default=None, sa_type=sa.Text)
    llm_session_id: str | None = Field(default=None, sa_type=sa.Text)
    extra_dirs: list[str] | None = Field(default=None, sa_column=Column(ARRAY(sa.Text), nullable=True))
    model: str | None = Field(default=None, sa_type=sa.Text)
    status: AgentSessionStatus = Field(
        default=AgentSessionStatus.ACTIVE,
        sa_column=Column(
            sa.Enum(AgentSessionStatus),
            nullable=False,
            server_default=AgentSessionStatus.ACTIVE.name,
        ),
    )

    agent: Agent = Relationship(back_populates="sessions")
    messages: list["AgentSessionMessage"] = Relationship(back_populates="session")
    feedbacks: list["AgentFeedback"] = Relationship(back_populates="session")

    title: str | None = Field(default=None, sa_type=sa.Text)


class AgentSessionMessage(BaseModel, table=True):
    """A single message within a session."""

    __tablename__ = "agent_session_messages"

    agent_session_message_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_session_id: uuid.UUID = Field(foreign_key="agent_sessions.agent_session_id", nullable=False, index=True)
    turn_number: int = Field(nullable=False)
    role: AgentSessionMessageRole = Field(sa_column=Column(sa.Enum(AgentSessionMessageRole), nullable=False))
    # User who created this message (no FK constraint — user may not exist in this DB)
    creator_user_id: str | None = Field(default=None, nullable=True, sa_type=sa.Text, index=True)
    content: str | None = Field(default=None, sa_type=sa.Text)
    raw_events: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    llm_name: str | None = Field(default=None, sa_type=sa.Text)
    llm_message_id: str | None = Field(default=None, sa_type=sa.Text)
    cost_usd: Decimal | None = Field(default=None, sa_column=Column(sa.Numeric(precision=10, scale=6), nullable=True))
    duration_ms: int | None = Field(default=None, sa_type=sa.Integer)
    num_agent_turns: int | None = Field(default=None, sa_type=sa.Integer)
    slack_ts: str | None = Field(default=None, sa_type=sa.Text)
    # Per-turn inference latency metrics derived from wall-clock timing in the event loop.
    ttfct_ms: int | None = Field(default=None, sa_type=sa.Integer)
    ttlct_ms: int | None = Field(default=None, sa_type=sa.Integer)
    completion_status: AgentSessionMessageCompletionStatus = Field(
        default=AgentSessionMessageCompletionStatus.SUCCESS,
        sa_column=Column(
            sa.Enum(AgentSessionMessageCompletionStatus),
            nullable=False,
            server_default=AgentSessionMessageCompletionStatus.SUCCESS.value,
        ),
    )
    error_type: AgentSessionMessageErrorType = Field(
        default=AgentSessionMessageErrorType.NONE,
        sa_column=Column(
            sa.Enum(AgentSessionMessageErrorType),
            nullable=False,
            server_default=AgentSessionMessageErrorType.NONE.value,
        ),
    )

    session: AgentSession = Relationship(back_populates="messages")
    feedbacks: list["AgentFeedback"] = Relationship(back_populates="message")

    __table_args__ = (Index("ix_agent_session_messages_session_turn", "agent_session_id", "turn_number"),)


class AgentFeedback(BaseModel, table=True):
    """Feedback signal on a session or individual message."""

    __tablename__ = "agent_feedbacks"

    agent_feedback_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_session_id: uuid.UUID = Field(foreign_key="agent_sessions.agent_session_id", nullable=False, index=True)
    agent_session_message_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent_session_messages.agent_session_message_id",
        index=True,
    )
    # User who left feedback (no FK constraint — user may not exist in this DB)
    user_id: str = Field(nullable=False, sa_type=sa.Text, index=True)
    rating: AgentFeedbackRating | None = Field(
        default=None, sa_column=Column(sa.Enum(AgentFeedbackRating), nullable=True)
    )
    structured: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    comment: str | None = Field(default=None, sa_type=sa.Text)
    slack_ts: str | None = Field(default=None, sa_type=sa.Text)

    session: AgentSession = Relationship(back_populates="feedbacks")
    message: AgentSessionMessage | None = Relationship(back_populates="feedbacks")


class AgentSchedule(BaseModel, table=True):
    """A scheduled call to an agent."""

    __tablename__ = "agent_schedules"

    agent_schedule_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_id: uuid.UUID = Field(foreign_key="agents.agent_id", nullable=False, index=True)
    schedule_type: AgentScheduleType = Field(sa_column=Column(sa.Enum(AgentScheduleType), nullable=False))
    message: str = Field(nullable=False, sa_type=sa.Text)
    context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    status: AgentScheduleStatus = Field(
        default=AgentScheduleStatus.PENDING,
        sa_column=Column(
            sa.Enum(AgentScheduleStatus),
            nullable=False,
            server_default=AgentScheduleStatus.PENDING.name,
        ),
    )
    execute_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    cron_expression: str | None = Field(default=None, sa_type=sa.Text)
    cron_timezone: str = Field(default="UTC", nullable=False, sa_type=sa.Text)
    next_run_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    last_run_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    run_count: int = Field(default=0, nullable=False, sa_type=sa.Integer)
    max_runs: int | None = Field(default=None, sa_type=sa.Integer)
    last_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_sessions.agent_session_id", nullable=True
    )
    last_error: str | None = Field(default=None, sa_type=sa.Text)
    name: str | None = Field(default=None, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)
    created_by_user: str = Field(nullable=False, sa_type=sa.Text)
    created_by_agent: str | None = Field(default=None, sa_type=sa.Text)

    agent: Agent = Relationship()
    runs: list["AgentScheduleRun"] = Relationship(back_populates="schedule")

    __table_args__ = (
        Index("ix_agent_schedules_status_next_run", "status", "next_run_at"),
        sa.CheckConstraint(
            "(schedule_type = 'SCHEDULED' AND execute_at IS NOT NULL) OR "
            "(schedule_type = 'RECURRING' AND cron_expression IS NOT NULL)",
            name="agent_schedules_type_fields_required",
        ),
    )


class AgentScheduleRun(BaseModel, table=True):
    """A single execution of an agent schedule."""

    __tablename__ = "agent_schedule_runs"

    agent_schedule_run_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_schedule_id: uuid.UUID = Field(foreign_key="agent_schedules.agent_schedule_id", nullable=False, index=True)
    run_number: int = Field(nullable=False, sa_type=sa.Integer)
    status: AgentScheduleRunStatus = Field(
        default=AgentScheduleRunStatus.PENDING,
        sa_column=Column(
            sa.Enum(AgentScheduleRunStatus),
            nullable=False,
            server_default=AgentScheduleRunStatus.PENDING.name,
        ),
    )
    started_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    completed_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    session_id: uuid.UUID | None = Field(default=None, foreign_key="agent_sessions.agent_session_id", nullable=True)
    error: str | None = Field(default=None, sa_type=sa.Text)

    schedule: AgentSchedule = Relationship(back_populates="runs")


class AgentProject(BaseModel, table=True):
    """A project groups related work under a common goal."""

    __tablename__ = "agent_projects"

    agent_project_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    name: str = Field(nullable=False, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)
    status: AgentProjectStatus = Field(
        default=AgentProjectStatus.PAUSED,
        sa_column=Column(
            sa.Enum(AgentProjectStatus),
            nullable=False,
            server_default=AgentProjectStatus.PAUSED.name,
        ),
    )
    # User who created this project (no FK constraint — user may not exist in this DB)
    creator_user_id: str | None = Field(default=None, nullable=True, sa_type=sa.Text, index=True)
    default_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.agent_id", nullable=True, index=True)
    project_data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    shared_state: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    budget_usd: Decimal | None = Field(default=None, sa_column=Column(sa.Numeric(precision=10, scale=2), nullable=True))
    budget_spent_usd: Decimal = Field(
        default=Decimal(0), sa_column=Column(sa.Numeric(precision=10, scale=2), nullable=False, server_default="0")
    )
    slack_channel: str | None = Field(default=None, sa_type=sa.Text)

    tasks: list["AgentTask"] = Relationship(back_populates="project")


class AgentTask(BaseModel, table=True):
    """A task within a project."""

    __tablename__ = "agent_tasks"

    agent_task_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_project_id: uuid.UUID = Field(foreign_key="agent_projects.agent_project_id", nullable=False, index=True)
    parent_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("agent_tasks.agent_task_id"), nullable=True, index=True),
    )
    title: str = Field(nullable=False, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)
    status: AgentTaskStatus = Field(
        default=AgentTaskStatus.PENDING,
        sa_column=Column(
            sa.Enum(AgentTaskStatus),
            nullable=False,
            server_default=AgentTaskStatus.PENDING.name,
        ),
    )
    priority: AgentTaskPriority = Field(
        default=AgentTaskPriority.NORMAL,
        sa_column=Column(
            sa.Enum(AgentTaskPriority),
            nullable=False,
            server_default=AgentTaskPriority.NORMAL.name,
        ),
    )
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.agent_id", nullable=True, index=True)
    assigned_session_ids: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    task_data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    estimated_effort: str | None = Field(default=None, sa_type=sa.Text)
    actual_spending_usd: Decimal | None = Field(
        default=None, sa_column=Column(sa.Numeric(precision=10, scale=6), nullable=True)
    )
    completed_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]

    project: AgentProject = Relationship(back_populates="tasks")

    depends_on: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    __table_args__ = (
        Index("ix_agent_tasks_project_status", "agent_project_id", "status"),
        sa.CheckConstraint("parent_task_id != agent_task_id", name="no_self_parent"),
    )


class AgentSecurityIncidentType(str, enum.Enum):
    """Type of security incident detected by an agent."""

    SECRETS_PROBE = "SECRETS_PROBE"
    MEMORY_MANIPULATION = "MEMORY_MANIPULATION"
    SCOPE_MANIPULATION = "SCOPE_MANIPULATION"
    SOCIAL_ENGINEERING = "SOCIAL_ENGINEERING"


class AgentSecuritySeverity(str, enum.Enum):
    """Severity level of a security incident."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentSecurityResolution(str, enum.Enum):
    """Resolution status of a reviewed security incident."""

    FALSE_POSITIVE = "FALSE_POSITIVE"
    CONFIRMED = "CONFIRMED"
    MITIGATED = "MITIGATED"


class AgentSecurityIncident(BaseModel, table=True):
    """A security incident detected by an agent during a session.

    Written by the report_security_incident MCP tool. Reviewed by humans
    via the Soul admin dashboard.
    """

    __tablename__ = "agent_security_incidents"

    incident_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_session_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("agent_sessions.agent_session_id"), nullable=True),
    )
    agent_name: str = Field(nullable=False, sa_type=sa.Text)
    incident_type: AgentSecurityIncidentType = Field(
        sa_column=Column(
            sa.Enum(AgentSecurityIncidentType, create_type=False),
            nullable=False,
        )
    )
    severity: AgentSecuritySeverity = Field(
        sa_column=Column(
            sa.Enum(AgentSecuritySeverity, create_type=False),
            nullable=False,
        )
    )
    description: str = Field(nullable=False, sa_type=sa.Text)
    evidence: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    turn_number: int | None = Field(default=None, nullable=True)
    offense_number: int = Field(default=1, nullable=False)
    auto_detected: bool = Field(default=True, nullable=False)
    reported_at: datetime | None = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    reviewed_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    reviewed_by: str | None = Field(default=None, sa_type=sa.Text)
    resolution: AgentSecurityResolution | None = Field(
        default=None,
        sa_column=Column(sa.Enum(AgentSecurityResolution, create_type=False), nullable=True),
    )

    __table_args__ = (
        Index("ix_agent_security_incidents_session", "agent_session_id"),
        Index("ix_agent_security_incidents_type_severity", "incident_type", "severity"),
        Index("ix_agent_security_incidents_reported_at", "reported_at"),
        Index(
            "ix_agent_security_incidents_unreviewed",
            "reviewed_at",
            postgresql_where=sa.text("reviewed_at IS NULL"),
        ),
    )


class AgentArtifact(BaseModel, table=True):
    """A pointer to an artifact created by or shared with agents.

    Artifacts are lightweight metadata records pointing to external resources
    (yuppastes, PR reviews, documents, etc.). No content is stored here — only
    the URL and enough context to find, filter, and attribute the artifact.
    """

    __tablename__ = "agent_artifacts"

    agent_artifact_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    artifact_type: AgentArtifactType = Field(sa_column=Column(sa.Enum(AgentArtifactType), nullable=False))
    title: str = Field(nullable=False, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)
    # The canonical pointer to the artifact (yuppaste URL, PR link, etc.)
    url: str = Field(nullable=False, sa_type=sa.Text)

    # Attribution: who/what created this artifact (no FK constraint — user may not exist in this DB)
    creator_user_id: str | None = Field(default=None, nullable=True, sa_type=sa.Text, index=True)
    creator_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.agent_id", nullable=True, index=True)

    # Context: where/why this artifact was created
    agent_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_sessions.agent_session_id", nullable=True
    )
    agent_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_tasks.agent_task_id", nullable=True, index=True
    )

    # Flexible bag for type-specific data (e.g. {"pr_number": 123, "repo": "yupp-mind"})
    artifact_metadata: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    __table_args__ = (
        Index("ix_agent_artifacts_type", "artifact_type"),
        Index("ix_agent_artifacts_session_type", "agent_session_id", "artifact_type"),
    )
