"""Database models for Agent Harness Service.

Tables:
- agents: Agent definitions with config paths and metadata
- agent_sessions: Conversation sessions tied to agents
- agent_session_messages: Individual messages within sessions (user + agent)
- agent_messages: Agent-to-agent messages (A2A messaging)
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
from sqlalchemy import CheckConstraint, Column, Index, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, Relationship

from ypl.db.base import BaseModel


class AgentArtifactType(str, enum.Enum):
    """Type of artifact tracked in the agent artifact registry."""

    TEXT = "TEXT"  # Text content (investigations, reports, summaries)
    CODE_REVIEW = "CODE_REVIEW"  # GitHub PR review
    OTHER = "OTHER"  # Catch-all for future types
    MEMORY = "MEMORY"  # Agent memory (inline content + scope/subject)
    SKILL = "SKILL"  # Agent skill (inline markdown + scope/subject; parsed frontmatter in artifact_metadata)


# Types whose body lives in ``inline_content`` and is qualified by the
# ``memory_scope`` / ``memory_scope_subject`` columns. Both MEMORY and SKILL
# use this pattern — the scope columns are reused for SKILL (the historical
# ``memory_`` prefix predates that; the columns themselves are generic).
SCOPED_INLINE_ARTIFACT_TYPES: frozenset[AgentArtifactType] = frozenset(
    {AgentArtifactType.MEMORY, AgentArtifactType.SKILL}
)


class AgentSessionTrigger(str, enum.Enum):
    """How the session was initiated."""

    SLACK = "SLACK"
    WEBHOOK = "WEBHOOK"
    CRON = "CRON"
    API = "API"
    TASK = "TASK"  # Triggered by a project task executor
    AGENT = "AGENT"  # Session initiated by another agent (A2A messaging)


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
    FELLOW_AGENT = "FELLOW_AGENT"  # Turn originated from a peer agent (A2A messaging)


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


class AgentMessageStatus(str, enum.Enum):
    """Delivery status of an agent-to-agent message."""

    QUEUED = "QUEUED"  # Persisted, waiting to be dispatched
    DELIVERING = "DELIVERING"  # Claimed by a worker; in-flight
    DELIVERED = "DELIVERED"  # Successfully injected into target session
    FAILED = "FAILED"  # Exhausted max_attempts without successful delivery


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

    # Corresponding user identity row in the users table (set at agent creation time)
    agent_user_id: str | None = Field(
        default=None, foreign_key="users.user_id", nullable=True, sa_type=sa.Text, index=True
    )


class AgentSession(BaseModel, table=True):
    """A conversation session between a user and an agent."""

    __tablename__ = "agent_sessions"

    agent_session_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    agent_id: uuid.UUID = Field(foreign_key="agents.agent_id", nullable=False, index=True)
    parent_session_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid, sa.ForeignKey("agent_sessions.agent_session_id"), nullable=True, index=True),
    )
    # Lineage pointer for /fork — peer session, not a subagent child. Distinct from
    # parent_session_id so fork lineage doesn't inherit subagent semantics (cost
    # cascade, stop-signal propagation). NULL for sessions that weren't forked.
    forked_from_session_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid,
            sa.ForeignKey("agent_sessions.agent_session_id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
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


class AgentMessage(BaseModel, table=True):
    """A message sent from one agent to another (A2A messaging).

    Supports two delivery scenarios:
    - Scenario A (to_session_id IS NULL): create a new session for the target agent.
    - Scenario B (to_session_id IS NOT NULL): inject into an existing session.

    Delivery is at-least-once with crash-safe recovery via the claimed_at lease.
    """

    __tablename__ = "agent_messages"

    agent_message_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)

    # Sender and recipient agents
    from_agent_id: uuid.UUID = Field(foreign_key="agents.agent_id", nullable=False, index=True)
    to_agent_id: uuid.UUID = Field(foreign_key="agents.agent_id", nullable=False, index=True)

    # Sender context — nullable (agent may send outside any active session)
    from_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_sessions.agent_session_id", nullable=True
    )

    # Delivery target:
    #   NULL → create a new session for to_agent    (Scenario A)
    #   set  → inject into this existing session    (Scenario B)
    to_session_id: uuid.UUID | None = Field(default=None, foreign_key="agent_sessions.agent_session_id", nullable=True)

    content: str = Field(nullable=False, sa_type=sa.Text)
    message_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))

    status: AgentMessageStatus = Field(
        default=AgentMessageStatus.QUEUED,
        sa_column=Column(sa.Enum(AgentMessageStatus), nullable=False),
    )

    queued_at: datetime | None = Field(
        default=None,
        sa_column=Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
    )
    delivered_at: datetime | None = Field(default=None, nullable=True, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]

    # Lease tracking for crash recovery
    claimed_at: datetime | None = Field(default=None, nullable=True, sa_type=sa.DateTime(timezone=True))  # type: ignore[call-overload]
    attempt_count: int = Field(default=0, nullable=False)
    max_attempts: int = Field(default=3, nullable=False)

    # Populated after successful delivery — the session that received the message
    resolved_session_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_sessions.agent_session_id", nullable=True
    )
    error: str | None = Field(default=None, nullable=True, sa_type=sa.Text)

    __table_args__ = (
        # Fast queue drain: find pending new-session messages for a target agent
        Index(
            "ix_agent_message_to_agent_queued",
            "to_agent_id",
            "created_at",
            postgresql_where=text("status = 'QUEUED' AND to_session_id IS NULL"),
        ),
        # Fast session inbox drain
        Index(
            "ix_agent_message_to_session_queued",
            "to_session_id",
            "status",
            "created_at",
            postgresql_where=text("status IN ('QUEUED', 'DELIVERING') AND to_session_id IS NOT NULL"),
        ),
        # Stale-claim recovery: find hung DELIVERING rows by lease age
        Index(
            "ix_agent_message_stale_delivering",
            "claimed_at",
            postgresql_where=text("status = 'DELIVERING'"),
        ),
        Index("ix_agent_message_from_session", "from_session_id"),
        sa.CheckConstraint("attempt_count <= max_attempts", name="ck_agent_message_attempt_count"),
    )


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

    # A2A provenance — set when role=FELLOW_AGENT
    from_agent_id: uuid.UUID | None = Field(default=None, foreign_key="agents.agent_id", nullable=True, index=True)
    agent_message_id_ref: uuid.UUID | None = Field(
        default=None, foreign_key="agent_messages.agent_message_id", nullable=True, index=True
    )

    session: AgentSession = Relationship(back_populates="messages")
    feedbacks: list["AgentFeedback"] = Relationship(back_populates="message")

    __table_args__ = (
        Index("ix_agent_session_messages_session_turn", "agent_session_id", "turn_number"),
        sa.CheckConstraint(
            "(role = 'FELLOW_AGENT') = (from_agent_id IS NOT NULL)",
            name="ck_agent_session_message_fellow_agent_provenance",
        ),
    )


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
    (artifacts, PR reviews, documents, etc.). No content is stored here — only
    the URL and enough context to find, filter, and attribute the artifact.
    """

    __tablename__ = "agent_artifacts"

    agent_artifact_id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, nullable=False)
    artifact_type: AgentArtifactType = Field(sa_column=Column(sa.Enum(AgentArtifactType), nullable=False))
    title: str = Field(nullable=False, sa_type=sa.Text)
    description: str | None = Field(default=None, sa_type=sa.Text)
    # The canonical pointer to the artifact (artifact URL, PR link, etc.).
    # Nullable for MEMORY artifacts, which store their body in ``inline_content``
    # instead of pointing at an external blob. Every non-MEMORY artifact still
    # sets this — enforced by ck_agent_artifacts_content_location.
    url: str | None = Field(default=None, nullable=True, sa_type=sa.Text)

    # Inline content body — used by MEMORY artifacts (markdown notes). Exactly
    # one of (inline_content, url) is non-NULL per row.
    inline_content: str | None = Field(default=None, nullable=True, sa_type=sa.Text)

    # Scope qualifier — populated for MEMORY and SKILL artifacts.
    # (Historical naming: the columns were introduced for MEMORY first, then
    # SKILL adopted the same scoping pattern. The columns themselves are
    # type-agnostic; the ``ck_agent_artifacts_memory_scope_matches_type``
    # check constraint controls which artifact types may set them.)
    #   memory_scope         ∈ {'user', 'agent', 'topic'}
    #   memory_scope_subject = users.user_id (scope=user)
    #                        | agents.name    (scope=agent)
    #                        | NULL           (scope=topic)
    memory_scope: str | None = Field(default=None, nullable=True, sa_type=sa.Text)
    memory_scope_subject: str | None = Field(default=None, nullable=True, sa_type=sa.Text)

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

    # Flexible bag for type-specific data (e.g. {"pr_number": 123, "repo": "yupp-agent"})
    artifact_metadata: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Unify Artifact and Artifact: stable human-readable identity and versioning
    named_slug: str | None = Field(default=None, nullable=True, sa_type=sa.Text)
    version: int | None = Field(default=None, nullable=True)
    content_type: str | None = Field(default=None, nullable=True, sa_type=sa.Text)

    __table_args__ = (
        Index("ix_agent_artifacts_type", "artifact_type"),
        Index("ix_agent_artifacts_session_type", "agent_session_id", "artifact_type"),
        # Partial unique index mirrors the migration: only enforces uniqueness when both are non-NULL.
        # MEMORY and SKILL artifacts are excluded — their uniqueness is scope-qualified
        # via uix_memory_scope_slug_version / uix_skill_scope_slug_version below, so
        # the same slug can live in (e.g.) both a user scope and an agent scope.
        Index(
            "uix_agent_artifacts_slug_version",
            "named_slug",
            "version",
            unique=True,
            postgresql_where=text(
                "named_slug IS NOT NULL AND version IS NOT NULL AND artifact_type NOT IN ('MEMORY', 'SKILL')"
            ),
        ),
        # Per-scope slug/version uniqueness for MEMORY artifacts. Parallel MEMORY
        # saves across different (scope, subject) tuples don't collide, while
        # each (scope, subject, slug) sequence stays monotonic.
        #
        # NULLS NOT DISTINCT (PG 15+): topic-scope rows have
        # ``memory_scope_subject IS NULL``; without this flag Postgres would
        # treat each NULL as distinct and let duplicate
        # ``(topic, NULL, slug, version)`` rows through, breaking topic-scope
        # uniqueness.
        Index(
            "uix_memory_scope_slug_version",
            "memory_scope",
            "memory_scope_subject",
            "named_slug",
            "version",
            unique=True,
            postgresql_where=text("artifact_type = 'MEMORY' AND named_slug IS NOT NULL AND version IS NOT NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        # Per-scope slug/version uniqueness for SKILL artifacts. Mirrors the
        # MEMORY index — SKILL artifacts share the scope columns but live in
        # their own (scope, subject, slug) namespace, so the same slug can
        # appear as both a MEMORY topic and a SKILL topic without colliding.
        Index(
            "uix_skill_scope_slug_version",
            "memory_scope",
            "memory_scope_subject",
            "named_slug",
            "version",
            unique=True,
            postgresql_where=text("artifact_type = 'SKILL' AND named_slug IS NOT NULL AND version IS NOT NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        # Fast scope-filtered reads (e.g. "all memory for user X").
        Index(
            "ix_memory_scope_subject",
            "memory_scope",
            "memory_scope_subject",
            postgresql_where=text("artifact_type = 'MEMORY'"),
        ),
        # Fast scope-filtered reads for SKILL artifacts (catalog merging in
        # the system-prompt builder uses these).
        Index(
            "ix_skill_scope_subject",
            "memory_scope",
            "memory_scope_subject",
            postgresql_where=text("artifact_type = 'SKILL'"),
        ),
        CheckConstraint(
            "content_type IN ('text/plain', 'text/markdown', 'text/html')",
            name="ck_agent_artifacts_content_type",
        ),
        # Exactly one of (inline_content, url) must be populated on any row.
        CheckConstraint(
            "(inline_content IS NOT NULL) <> (url IS NOT NULL)",
            name="ck_agent_artifacts_content_location",
        ),
        # MEMORY or SKILL <=> scope is set. Other types must have both scope columns NULL.
        # (The constraint name is kept for back-compat with the original MEMORY-only
        # migration; SKILL was added by a follow-up migration that broadens the predicate.)
        CheckConstraint(
            "(artifact_type IN ('MEMORY', 'SKILL') AND memory_scope IN ('user', 'agent', 'topic')) "
            "OR (artifact_type NOT IN ('MEMORY', 'SKILL') "
            "    AND memory_scope IS NULL AND memory_scope_subject IS NULL)",
            name="ck_agent_artifacts_memory_scope_matches_type",
        ),
        # topic scope has no subject; user/agent scopes require one.
        CheckConstraint(
            "memory_scope IS NULL "
            "OR (memory_scope = 'topic' AND memory_scope_subject IS NULL) "
            "OR (memory_scope IN ('user', 'agent') AND memory_scope_subject IS NOT NULL)",
            name="ck_agent_artifacts_memory_subject_presence",
        ),
    )
