"""Request/response models for Agent Harness Service REST API."""

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ypl.agent_harness_service.common.constants import ALL_MCP_SERVERS, RESTRICTED_HARNESS_TOOLS


@runtime_checkable
class ToolDispatcher(Protocol):
    """Protocol for dispatching tool calls to a warm proxy process.

    Implemented by ``CommandHandlerManager`` in executors/. Defined here in
    common/ so that both tools/ and executors/ can reference the type without
    creating a cross-package import.
    """

    async def call_tool(self, tool: str, args: dict[str, Any]) -> str: ...


# Known request origins for source tracking / logging.
# Using str (not Literal) to allow any source string without validation errors.
AHSSource = str


class StreamEvent(BaseModel):
    """A parsed event from Claude Code's stream-json output.

    Key types:
    - system: Initial system message with session metadata
    - assistant: Text chunks from the agent's response
    - tool_use: Agent invoking a tool
    - tool_result: Result from a tool invocation
    - result: Final message with session_id, cost, usage stats
    """

    type: str
    raw: dict = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Extract text content from assistant events."""
        if self.type == "assistant":
            content = self.raw.get("message", {}).get("content", [])
            return "".join(block.get("text", "") for block in content if block.get("type") == "text")
        return ""

    @property
    def session_id(self) -> str | None:
        """Extract session_id from system or result events."""
        if self.type == "system":
            return self.raw.get("session_id")
        if self.type == "result":
            return self.raw.get("session_id")
        return None

    @property
    def cost_usd(self) -> float | None:
        """Extract estimated cost from result events."""
        if self.type == "result":
            return self.raw.get("estimated_cost_usd") or self.raw.get("cost_usd")
        return None

    @property
    def duration_ms(self) -> int | None:
        """Extract duration from result events."""
        if self.type == "result":
            return self.raw.get("duration_ms")
        return None

    @property
    def num_turns(self) -> int | None:
        """Extract number of agent turns from result events."""
        if self.type == "result":
            return self.raw.get("num_turns")
        return None

    @property
    def subtype(self) -> str | None:
        """Extract subtype from result events (e.g. 'success', 'stopped')."""
        if self.type == "result":
            return self.raw.get("subtype")
        return None


class AHSValidationError(ValueError):
    """Raised for request validation failures (missing fields, authorization mismatches).

    Routes map this to HTTP 400, distinguishing it from plain ValueError (HTTP 404).
    """


class SessionPermissions(BaseModel):
    """User-level permission grants for an agent session.

    Stores what a session IS allowed to access (allowed-based, not denied-based).
    Constructed at session creation based on user permissions (e.g., USE_MCP).
    Stored in session_context["permissions"] and propagated unchanged to all
    subagents. Each executor resolves these against agent-level tool config
    (intersection) to derive effective permissions.
    """

    allowed_servers: list[str] = Field(default_factory=lambda: list(ALL_MCP_SERVERS))
    # ["*"] = all harness tools; otherwise lists specific allowed tool names.
    allowed_harness_tools: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def has_full_tool_access(self) -> bool:
        """True when all harness MCP tools are available (no restrictions)."""
        return "*" in self.allowed_harness_tools

    @classmethod
    def full_access(cls) -> SessionPermissions:
        """All MCP servers and tools available."""
        return cls()

    @classmethod
    def restricted(cls) -> SessionPermissions:
        """Only harness server with basic tools (no platform, no privileged tools)."""
        return cls(
            allowed_servers=["harness"],
            allowed_harness_tools=list(RESTRICTED_HARNESS_TOOLS),
        )

    @classmethod
    def from_context(cls, ctx: dict[str, Any]) -> SessionPermissions:
        """Extract SessionPermissions from session context with backward compatibility."""
        if "permissions" in ctx:
            raw = ctx["permissions"]
            # Backward compat: old format used denied_* fields
            if "denied_tools" in raw or "denied_servers" in raw:
                has_denies = bool(raw.get("denied_tools")) or bool(raw.get("denied_servers"))
                return cls.restricted() if has_denies else cls.full_access()
            return cls(**raw)
        return cls()


# --- Attachment Models ---


class AttachmentInfo(BaseModel):
    """Metadata for a file attachment stored in the configured blob store."""

    filename: str = Field(..., description="Sanitized filename")
    content_type: str = Field(..., description="MIME type (e.g., 'image/png')")
    size: int = Field(..., description="File size in bytes")
    blob_path: str = Field(
        ...,
        description="Logical blob-store path (e.g., 'attachments/{session_id}/file.png'). "
        "Resolved against the configured BlobStore backend at read time.",
    )


# --- Request Models ---


class SessionCreateRequest(BaseModel):
    """POST /session/create — create or resume a session."""

    agent_id: str = Field(..., description="Agent name (e.g., 'sre', 'code-reviewer')")
    trigger: str = Field(..., description="How the session was triggered: slack, webhook, cron, api")
    message: str | None = Field(None, description="Optional first message to process immediately")
    user_id: str | None = Field(None, description="Yupp user ID of the session creator")
    context: dict | None = Field(
        None,
        description="Session context: repo, pr_url, slack metadata, etc.",
    )
    session_id: str | None = Field(
        None,
        description="External session ID (e.g., Slack composite ID). If provided and exists, resume it.",
    )
    attachments: list[AttachmentInfo] | None = Field(
        None,
        description="File attachments stored in the blob store to download into the workspace.",
    )
    source: AHSSource = Field(
        "api",
        description="Request source: slack_gateway, cli, tui, websocket, scheduler, orchestration, api",
    )
    force_model: str | None = Field(
        None,
        description=(
            "Override the agent's default model for this session. "
            "Use a harness name (e.g., 'claude-code-cli', 'codex-cli') or a raw model in "
            "'provider/model_id' format (e.g., 'anthropic/claude-sonnet-4-6'). "
            "Stored in session context and applied on every turn."
        ),
    )


class SessionMessageRequest(BaseModel):
    """POST /session/message — send a message to an existing session."""

    session_id: str = Field(..., description="Session ID (UUID or slack_session_id)")
    message: str = Field(..., description="Message text")
    slack_ts: str | None = Field(None, description="Slack message timestamp for linking")
    slack_user_id: str | None = Field(None, description="Slack user ID of message sender")
    user_id: str | None = Field(None, description="Yupp user ID of message sender")
    attachments: list[AttachmentInfo] | None = Field(
        None,
        description="File attachments stored in the blob store to download into the workspace.",
    )
    source: AHSSource = Field(
        "api",
        description="Request source: slack_gateway, cli, tui, websocket, scheduler, orchestration, api",
    )


class SessionAttachSlackRequest(BaseModel):
    """POST /session/attach-slack — attach Slack thread context to an existing headless session."""

    session_id: str = Field(..., description="AHS session UUID to attach to")
    slack_session_id: str = Field(..., description="SAG composite session ID ({channel}:{thread_ts}:{app_id})")
    context: dict[str, Any] | None = Field(
        None,
        description="Slack context to merge into the session (channel_id, thread_ts, etc.)",
    )


class SessionAttachSlackResponse(BaseModel):
    """Response from attaching Slack context to a session."""

    session_id: str
    status: str = Field(..., description="'attached' on success")


class SessionStopRequest(BaseModel):
    """POST /session/stop — stop a running agent task."""

    session_id: str = Field(..., description="Session ID (UUID or slack_session_id)")


class SessionFeedbackRequest(BaseModel):
    """POST /session/feedback — record feedback on a session or message."""

    session_id: str = Field(..., description="Session ID (UUID or slack_session_id)")
    user_id: str | None = Field(None, description="User ID of who left the feedback. Defaults to 'SYSTEM'.")
    message_id: str | None = Field(None, description="Optional message UUID for message-level feedback")
    rating: str | None = Field(None, description="Rating: 'POSITIVE' or 'NEGATIVE'")
    structured: dict | None = Field(None, description="Structured eval payload (e.g., categories, tags, scores)")
    comment: str | None = Field(None, description="Free-text feedback")
    slack_ts: str | None = Field(None, description="Slack message timestamp that triggered this feedback")


# --- Response Models ---


class SessionMessageResponse(BaseModel):
    """Response from sending a message (returns immediately, agent runs in background).

    Status values:
    - ``"processing"`` — message accepted, agent turn started.
    - ``"queued"`` — a prior turn is still running; message is queued in-memory
      and will be combined with any other queued messages into a single turn
      once the current turn finishes. ``turn_number`` is ``-1`` until drained.
    """

    session_id: str
    turn_number: int
    status: str = "processing"


class SessionCreateResponse(BaseModel):
    """Response for session creation."""

    session_id: str = Field(..., description="Session UUID")
    status: str = Field(..., description="Session status")


class ModelsListResponse(BaseModel):
    """Response for GET /ahs/models — all selectable models grouped by executor type.

    Harnessed models are CLI wrapper names; agents run inside a managed subprocess.
    Raw models are LLM identifiers in ``provider/model_id`` format; agents call the
    provider API directly.
    """

    harnessed: list[str] = Field(
        ...,
        description="Harnessed executor names (e.g., 'claude-code-cli'). Agent runs inside a CLI wrapper.",
    )
    raw: list[str] = Field(
        ...,
        description=(
            "Raw LLM models in provider/model_id format (e.g., 'anthropic/claude-sonnet-4-6'). Direct API calls."
        ),
    )


class SessionStopResponse(BaseModel):
    """Response from stopping a session."""

    session_id: str
    status: str = Field(..., description="'stopped' or 'no_inflight_turn'")


class FeedbackResponse(BaseModel):
    """Response for feedback recording."""

    status: str = "recorded"


class ToolUseItem(BaseModel):
    """A tool invocation within an agent message."""

    tool_use_id: str
    name: str
    input: dict[str, Any] | Any | None = None
    output: str | None = None
    is_error: bool = False
    duration_ms: int | None = None
    step: int | None = None


class MessageHistoryItem(BaseModel):
    """A single message in session history."""

    message_id: str
    turn_number: int
    role: str
    content: str | None
    tool_uses: list[ToolUseItem] | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_agent_turns: int | None = None
    slack_ts: str | None = None
    created_at: datetime | None = None


class SessionHistoryResponse(BaseModel):
    """Response for GET /session/{id}/history."""

    session_id: str
    agent_id: str
    status: str
    messages: list[MessageHistoryItem]
    total_messages: int | None = None
    limit: int | None = None
    offset: int | None = None


# --- Ops Endpoint Models ---


class AgentInfo(BaseModel):
    """Summary of an agent's configuration."""

    name: str
    display_name: str
    description: str | None = None
    executor_type: str
    executor_model: str | None = None
    llm_model: str | None = None
    tool_permissions: dict[str, str]
    allowed_subagents: list[str]
    default_repo: str
    max_turns: int
    max_budget_usd: float
    timeout_s: int
    sandbox_enabled: bool
    allowed_gateways: list[str]
    creator_user_id: str | None = None
    is_owner: bool | None = None  # Set when user_id is provided in the request


class AgentListResponse(BaseModel):
    """Response for GET /agents."""

    agents: list[AgentInfo]


class AgentDetailResponse(BaseModel):
    """Response for GET /agent/{name} — single agent with optional system prompts."""

    agent: AgentInfo
    system_prompts: dict[str, str] | None = None
    additional_system_prompt: str | None = None


class SessionInfo(BaseModel):
    """Summary of an agent session."""

    session_id: str
    agent_name: str
    status: str
    trigger: str
    model: str | None = None
    created_at: datetime | None = None
    slack_channel_name: str | None = None
    slack_user_id: str | None = None
    tool_permissions: dict[str, str] | None = None
    has_full_tool_access: bool | None = None
    parent_session_id: str | None = None
    title: str | None = None
    message_count: int = 0


class SessionListResponse(BaseModel):
    """Response for GET /sessions."""

    sessions: list[SessionInfo]
    total: int
    limit: int
    offset: int


class SessionDetailResponse(BaseModel):
    """Response for GET /session/{id} — single session with all descendant subsessions (flat)."""

    session: SessionInfo
    subsessions: list[SessionInfo]


# --- Agent Create Models ---


class SandboxConfigRequest(BaseModel):
    """Sandbox configuration for an agent."""

    enabled: bool = True
    auto_allow_bash_if_sandboxed: bool = True
    bwrap_enabled: bool = False


class ExecutorConfigRequest(BaseModel):
    """Executor configuration: type and model."""

    type: Literal["harnessed", "raw"] = Field("harnessed", description="Executor type: 'harnessed' or 'raw'")
    model: str | None = Field(None, description="CLI harness name or LLM model (provider/model_id)")


class AgentCreateRequest(BaseModel):
    """POST /agent/create — create a new agent on disk and register in DB."""

    name: str = Field(
        ...,
        description="Unique agent name (lowercase, alphanumeric + hyphens, e.g., 'my-agent')",
    )
    user_id: str = Field(..., description="Yupp user ID of the agent creator")
    display_name: str = Field("", description="Human-readable display name")
    description: str | None = Field(None, description="Agent description")

    executor_config: ExecutorConfigRequest = Field(
        default_factory=ExecutorConfigRequest,
        description="Executor type and model configuration",
    )
    tool_permissions: dict[str, str] = Field(
        default_factory=lambda: {"*": "allow"},
        description="Tool permission map (e.g., {'*': 'allow'}, {'*': 'deny', '@readonly': 'allow'})",
    )
    allowed_subagents: list[str] = Field(
        default_factory=list,
        description="Agent types this agent can spawn via new_task",
    )
    default_repo: str = Field("yupp-agent", description="Default git repo to clone")
    sandbox: SandboxConfigRequest = Field(
        default_factory=SandboxConfigRequest,
        description="Sandbox configuration",
    )
    max_turns: int = Field(20, description="Maximum agent turns per session")
    max_budget_usd: float = Field(2.0, description="Maximum cost budget per session in USD")
    feedback_probability: float = Field(0.2, description="Probability of auto-feedback (0.0–1.0)")
    feedback_min_turns: int = Field(5, description="Min turns before auto-feedback")
    timeout_s: int = Field(300, description="Session timeout in seconds")
    allowed_gateways: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowed gateways (e.g., ['*'] for all, ['slack'] for Slack only)",
    )

    # Identity files (optional)
    role_md: str | None = Field(None, description="Contents of ROLE.md (agent role, expertise, and personality)")
    soul_md: str | None = Field(None, description="Deprecated — personality is now merged into ROLE.md")
    additional_system_prompt: str | None = Field(None, description="Additional system prompt appended to the agent")


class AgentEditRequest(BaseModel):
    """POST /agent/edit — edit an existing agent's configuration."""

    name: str = Field(..., description="Agent name to edit (must already exist)")
    user_id: str = Field(..., description="Yupp user ID of the requester")
    display_name: str | None = Field(None, description="New display name")
    description: str | None = Field(None, description="New description")
    executor_config: ExecutorConfigRequest | None = Field(None, description="New executor configuration")
    tool_permissions: dict[str, str] | None = Field(None, description="New tool permission map")
    allowed_subagents: list[str] | None = Field(None, description="New allowed subagent types")
    default_repo: str | None = Field(None, description="New default repo")
    sandbox: SandboxConfigRequest | None = Field(None, description="New sandbox configuration")
    max_turns: int | None = Field(None, description="New max turns")
    max_budget_usd: float | None = Field(None, description="New max budget in USD")
    timeout_s: int | None = Field(None, description="New timeout in seconds")
    allowed_gateways: list[str] | None = Field(None, description="New allowed gateways")
    role_md: str | None = Field(None, description="New ROLE.md contents")
    soul_md: str | None = Field(None, description="Deprecated — personality is now merged into ROLE.md")
    additional_system_prompt: str | None = Field(None, description="New additional system prompt")


class AgentEditResponse(BaseModel):
    """Response for POST /agent/edit."""

    name: str
    status: str = Field(..., description="'updated'")


class AgentCreateResponse(BaseModel):
    """Response for POST /agent/create."""

    name: str
    status: str = Field(..., description="'created' or 'already_exists'")


# --- Schedule Models ---


class ScheduleCreateRequest(BaseModel):
    """POST /schedules/create — create a one-time agent schedule."""

    agent_name: str = Field(..., description="Agent name to schedule")
    message: str = Field(..., description="Prompt message for the agent")
    execute_at: str = Field(..., description="ISO-8601 datetime for execution")
    timezone: str = Field("UTC", description="IANA timezone for interpreting execute_at")
    user_id: str = Field(..., description="Yupp user ID of the creator")
    context: dict[str, Any] | None = Field(None, description="Optional context to inject into the session")
    name: str | None = Field(None, description="Human-readable name for the schedule")
    description: str | None = Field(None, description="Description of what this schedule does")
    created_by_agent: str | None = Field(None, description="Agent name if created by an agent")


class RecurringScheduleCreateRequest(BaseModel):
    """POST /schedules/create-recurring — create a recurring agent schedule."""

    agent_name: str = Field(..., description="Agent name to schedule")
    message: str = Field(..., description="Prompt message for the agent")
    cron_expression: str = Field(..., description="Cron expression (5-field: minute hour day month weekday)")
    timezone: str = Field("UTC", description="IANA timezone for cron evaluation")
    user_id: str = Field(..., description="Yupp user ID of the creator")
    context: dict[str, Any] | None = Field(None, description="Optional context to inject into the session")
    name: str | None = Field(None, description="Human-readable name for the schedule")
    description: str | None = Field(None, description="Description of what this schedule does")
    max_runs: int | None = Field(None, description="Maximum number of runs (null=unlimited)")
    created_by_agent: str | None = Field(None, description="Agent name if created by an agent")


class ScheduleTriggerRequest(BaseModel):
    """POST /schedule/{id}/trigger — trigger a recurring schedule immediately."""

    user_id: str = Field(..., description="Yupp user ID of the requester (must be the creator)")


class ScheduleTriggerResponse(BaseModel):
    """Response for schedule trigger."""

    agent_schedule_id: str
    session_id: str
    run_number: int


class ScheduleEditRequest(BaseModel):
    """POST /schedule/edit — edit an existing schedule."""

    agent_schedule_id: str = Field(..., description="UUID of the schedule to edit")
    user_id: str = Field(..., description="Yupp user ID of the requester (must be the creator)")
    message: str | None = Field(None, description="New prompt message")
    cron_expression: str | None = Field(None, description="New cron expression (recurring only)")
    timezone: str | None = Field(None, description="New IANA timezone")
    name: str | None = Field(None, description="New name")
    description: str | None = Field(None, description="New description")
    context: dict[str, Any] | None = Field(None, description="New context")
    max_runs: int | None = Field(None, description="New max runs (recurring only)")
    execute_at: str | None = Field(None, description="New execution time as ISO 8601 string (one-time only)")
    agent_name: str | None = Field(None, description="Change which agent runs on this schedule")


class ScheduleInfo(BaseModel):
    """Summary of a single agent schedule."""

    agent_schedule_id: str
    agent_name: str
    schedule_type: str
    status: str
    message: str
    name: str | None = None
    description: str | None = None
    execute_at: datetime | None = None
    cron_expression: str | None = None
    cron_timezone: str | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    run_count: int = 0
    max_runs: int | None = None
    context: dict[str, Any] | None = None
    created_by_user: str | None = None
    created_by_agent: str | None = None
    created_at: datetime | None = None


class ScheduleCreateResponse(BaseModel):
    """Response for schedule creation."""

    agent_schedule_id: str
    agent_name: str
    schedule_type: str
    status: str


class ScheduleEditResponse(BaseModel):
    """Response for schedule edit."""

    agent_schedule_id: str
    status: str = "updated"


class ScheduleDeleteResponse(BaseModel):
    """Response for schedule cancellation."""

    agent_schedule_id: str
    status: str = "CANCELLED"


class ScheduleDetailResponse(BaseModel):
    """Response for GET /schedule/{id}."""

    schedule: ScheduleInfo


class ScheduleListResponse(BaseModel):
    """Response for GET /schedules."""

    schedules: list[ScheduleInfo]
    count: int


class ScheduleRunInfo(BaseModel):
    """A single past run of a schedule."""

    agent_schedule_run_id: str
    run_number: int
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    session_id: str | None = None
    error: str | None = None


class ScheduleRunsResponse(BaseModel):
    """Response for GET /schedule/{id}/runs."""

    runs: list[ScheduleRunInfo]
    count: int


class ResolveUserRequest(BaseModel):
    """POST /resolve_user — resolve a user email to a user ID."""

    email: str = Field(..., description="User email address; 404 if the ``users`` table has no row with it")


class ResolveUserResponse(BaseModel):
    """Response for POST /resolve_user."""

    user_id: str = Field(..., description="Resolved Yupp user ID")
    email: str = Field(..., description="The email that was resolved")
