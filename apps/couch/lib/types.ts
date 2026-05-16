// Wire-compatible TypeScript shapes for the AHS HTTP+WS surface.
// Keys match cmd/ahs/dto.go verbatim (snake_case JSON), so we can decode
// responses without an extra mapping layer.

// ---------- agents ----------

export interface AgentInfo {
  name: string;
  display_name: string;
  description?: string | null;
  executor_type: string;
  executor_model?: string | null;
  llm_model?: string | null;
  tool_permissions: Record<string, string>;
  allowed_subagents: string[];
  default_repo: string;
  max_turns: number;
  max_budget_usd: number;
  timeout_s: number;
  sandbox_enabled: boolean;
  allowed_gateways: string[];
  creator_user_id?: string | null;
  is_owner?: boolean | null;
}

export interface AgentListResponse {
  agents: AgentInfo[];
}

export interface AgentConfigDTO {
  skills_allowed: string[];
  skills_denied: string[];
  tools_allowed: string[];
  tools_denied: string[];
  always_load: string[];
}

export interface AgentDetailResponse {
  agent: AgentInfo;
  system_prompts?: Record<string, string>;
  additional_system_prompt?: string | null;
  config?: AgentConfigDTO | null;
}

export interface AgentCreateResponse {
  agent_id: string;
  name: string;
}

// ---------- sessions ----------

export interface SessionInfo {
  session_id: string;
  agent_name: string;
  status: string;
  trigger: string;
  model?: string | null;
  created_at?: string | null;
  slack_channel_name?: string | null;
  slack_user_id?: string | null;
  tool_permissions?: Record<string, string>;
  has_full_tool_access?: boolean | null;
  parent_session_id?: string | null;
  title?: string | null;
  message_count: number;
}

export interface SessionListResponse {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionDetailResponse {
  session: SessionInfo;
  subsessions: SessionInfo[];
}

export interface SessionCreateRequest {
  // AHS naming, kept verbatim — the field carries an agent NAME, not a UUID.
  // See cmd/ahs/sessions.go:73 ("agent_id from the TUI is actually an agent
  // NAME today"). Don't rename without coordinating an AHS-side change.
  agent_id: string;
  trigger: string;
  message?: string | null;
  user_id?: string | null;
  context?: Record<string, unknown>;
  session_id?: string | null;
  source?: string;
  force_model?: string | null;
}

export interface SessionCreateResponse {
  session_id: string;
  status: string;
}

// One element of MessageHistoryItem.tool_uses. Shape mirrors
// cmd/ahs/turn.go:27 (`recordedToolCall`) exactly — the column is
// AgentSessionMessage.RawEvents JSON marshalled from that Go struct.
export interface RecordedToolCall {
  tool_use_id: string;
  name: string;
  input: Record<string, unknown>;
  output: string;
  is_error: boolean;
  duration_ms: number;
  step: number;
}

export interface MessageHistoryItem {
  message_id: string;
  turn_number: number;
  role: "user" | "assistant" | "system" | string;
  content?: string | null;
  tool_uses?: RecordedToolCall[] | null;
  cost_usd?: number | null;
  duration_ms?: number | null;
  num_agent_turns?: number | null;
  slack_ts?: string | null;
  created_at?: string | null;
}

export interface SessionHistoryResponse {
  session_id: string;
  agent_id: string;
  status: string;
  messages: MessageHistoryItem[];
  total_messages: number;
  limit: number;
  offset: number;
}

// ---------- admin ----------

export interface AdminUserDTO {
  user_id: string;
  email: string;
  name?: string | null;
  image?: string | null;
  status: "ACTIVE" | "DEACTIVATED" | string;
  user_type: "HUMAN" | "AGENT" | "SYSTEM" | string;
  slack_user_id?: string | null;
  github_username?: string | null;
  linear_name?: string | null;
  roles: string[];
  permissions: string[];
  created_at?: string | null;
  modified_at?: string | null;
}

export interface AdminUserListResponse {
  users: AdminUserDTO[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminRoleDTO {
  role_id: string;
  name: string;
  description: string;
  permissions: string[];
  user_count: number;
}

export interface AdminRoleListResponse {
  roles: AdminRoleDTO[];
}

export interface AdminPermissionListResponse {
  permissions: string[];
}

export interface SlackAgentDTO {
  slack_agent_id: string;
  app_id: string;
  agent_name: string;
  bot_name: string;
  display_name: string;
  status: "ACTIVE" | "DISABLED" | "PENDING_APPROVAL" | string;
  created_by_user_id?: string | null;
  created_at?: string | null;
  modified_at?: string | null;
}

export interface SlackAgentListResponse {
  agents: SlackAgentDTO[];
  total: number;
}

// ---------- session debug ----------

export interface DebugToolCall {
  tool_use_id: string;
  name: string;
  input?: Record<string, unknown>;
  output?: string;
  is_error: boolean;
  duration_ms: number;
  step: number;
}

export interface DebugMessage {
  message_id: string;
  turn_number: number;
  role: "USER" | "AGENT" | "SYSTEM" | "FELLOW_AGENT" | string;
  content?: string | null;
  llm_name?: string | null;
  llm_message_id?: string | null;
  cost_usd?: number | null;
  duration_ms?: number | null;
  num_agent_turns?: number | null;
  ttfct_ms?: number | null;
  ttlct_ms?: number | null;
  completion_status: "IN_PROGRESS" | "SUCCESS" | "FAILED" | "ABORTED" | string;
  error_type: "NONE" | "ERROR_MAX_TURNS" | "ERROR_CONTEXT_OVERFLOW" | "ERROR_EXECUTOR" | "ERROR_INTERNAL" | string;
  slack_ts?: string | null;
  created_at: string;
  tool_calls?: DebugToolCall[];
  creator_user_id?: string | null;
  from_agent_name?: string | null;
}

export interface DebugTotals {
  messages: number;
  user_messages: number;
  agent_messages: number;
  tool_calls: number;
  failed_turns: number;
  total_cost_usd: number;
  total_duration_ms: number;
}

export interface DebugSessionInfo {
  session_id: string;
  agent_name: string;
  status: string;
  trigger: string;
  title?: string | null;
  model?: string | null;
  workspace?: string | null;
  llm_session_id?: string | null;
  creator_user_id?: string | null;
  parent_session_id?: string | null;
  created_at: string;
  modified_at: string;
  context?: unknown;
}

export interface DebugSessionResponse {
  session: DebugSessionInfo;
  messages: DebugMessage[];
  totals: DebugTotals;
}

// ---------- projects ----------

export interface ProjectInfo {
  project_id: string;
  name: string;
  description?: string | null;
  status: string;
  creator_user_id?: string | null;
  created_at?: string | null;
}

export interface ProjectListResponse {
  projects: ProjectInfo[];
  total: number;
  limit: number;
  offset: number;
}

// ---------- artifacts ----------

export interface ArtifactInfo {
  artifact_id: string;
  type: string;
  title: string;
  description?: string | null;
  named_slug?: string | null;
  version?: number | null;
  sha256?: string | null;
  size_bytes?: number | null;
  content_type?: string | null;
  tier: "inline" | "url" | "blob" | "unknown";
  url?: string | null;
  creator_user_id?: string | null;
  creator_agent?: string | null;
  created_at?: string | null;
  modified_at?: string | null;
}

export interface ArtifactListResponse {
  artifacts: ArtifactInfo[];
  total: number;
}

export interface ArtifactDetailResponse {
  artifact: ArtifactInfo;
  body?: string;
  body_url?: string | null;
}

export interface ArtifactVersionsResponse {
  versions: ArtifactInfo[];
}

// ---------- tasks ----------

export interface TaskInfo {
  task_id: string;
  project_id: string;
  parent_task_id?: string | null;
  title: string;
  description?: string | null;
  status: string;
  priority: "URGENT" | "HIGH" | "NORMAL" | "LOW" | string;
  agent_name?: string | null;
  depends_on?: unknown;
  result?: unknown;
  estimated_effort?: string | null;
  actual_spending_usd?: number | null;
  completed_at?: string | null;
  created_at?: string | null;
  modified_at?: string | null;
}

export interface TaskListResponse {
  tasks: TaskInfo[];
  total: number;
}

// ---------- schedules ----------

export interface ScheduleInfo {
  schedule_id: string;
  agent_name: string;
  schedule_type: "RECURRING" | "SCHEDULED" | string;
  status: string;
  message: string;
  cron_expression?: string | null;
  next_run_at?: string | null;
  last_run_at?: string | null;
  run_count: number;
  name?: string | null;
}

export interface ScheduleListResponse {
  schedules: ScheduleInfo[];
}

// /ahs/schedule/{id} returns a free-form map; we type only what we render.
export interface ScheduleDetail {
  schedule_id: string;
  agent_name?: string;
  schedule_type: string;
  status: string;
  message: string;
  cron_expression?: string | null;
  next_run_at?: string | null;
  last_run_at?: string | null;
  run_count: number;
  max_runs?: number | null;
  name?: string | null;
  description?: string | null;
}

export interface ScheduleRun {
  run_id: string;
  run_number: number;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  session_id: string;
  error?: string | null;
}

export interface ScheduleRunsResponse {
  runs: ScheduleRun[];
}

// ---------- models ----------

export interface ModelsListResponse {
  harnessed: string[];
  raw: string[];
}

// ---------- error ----------

export interface ErrorResponse {
  detail: string;
}

// ---------- WS frames ----------
// Frames AHS sends. We don't model every payload field; pages use
// what they need. See cmd/ahs/turn.go + ws.go for emitters.
export interface WsFrame {
  type: string;
  session_id?: string;
  payload?: unknown;
  // common fields seen on broadcaster events
  turn_number?: number;
  message_id?: string;
  role?: string;
  content?: string;
  delta?: string;
  tool_name?: string;
  tool_args?: unknown;
  tool_result?: unknown;
  status?: string;
  message?: string;
  cancelled?: boolean;
  [k: string]: unknown;
}
