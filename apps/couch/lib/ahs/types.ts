import type {
  AhsProjectStatus,
  AhsScheduleStatus,
  AhsScheduleType,
  AhsSessionStatus,
  AhsTaskStatus,
  AhsTriggerType,
} from './server/schemas'

export type {
  AhsCreateScheduleRequest,
  AhsEditScheduleRequest,
} from './request-schemas'
export type {
  AhsAgentDetailResponse,
  AhsAgentInfo,
  AhsCreateAgentResponse,
  AhsCreateScheduleResponse,
  AhsCreateSessionResponse,
  AhsDeleteScheduleResponse,
  AhsEditAgentResponse,
  AhsEditScheduleResponse,
  AhsFeedbackResponse,
  AhsGetSessionHistoryResponse,
  AhsListAgentsResponse,
  AhsListSchedulesResponse,
  AhsListSessionsResponse,
  AhsMessageHistoryItem,
  AhsMessageRole,
  AhsProjectDetailResponse,
  AhsProjectListResponse,
  AhsProjectResponse,
  AhsProjectStatus,
  AhsResolveUserResponse,
  AhsScheduleDetailResponse,
  AhsScheduleInfo,
  AhsScheduleStatus,
  AhsScheduleType,
  AhsSendMessageResponse,
  AhsSessionDetailResponse,
  AhsSessionInfo,
  AhsSessionStatus,
  AhsStopSessionResponse,
  AhsTaskListResponse,
  AhsTaskPriority,
  AhsTaskResponse,
  AhsTaskResumeResponse,
  AhsTaskStatus,
  AhsTaskStatusResponse,
  AhsTaskSummary,
  AhsToolUse,
  AhsTriggerScheduleResponse,
  AhsTriggerType,
} from './server/schemas'

// AHS (Agent Harness Service) TypeScript types.
// REST response types are inferred from zod schemas in server/schemas.ts so
// parsing and static types stay in sync.
export const AHS_REQUEST_TRIGGER_VALUES = [
  'slack',
  'webhook',
  'cron',
  'api',
] as const

export const AHS_SOURCE_VALUES = [
  'slack_gateway',
  'cli',
  'tui',
  'websocket',
  'scheduler',
  'orchestration',
  'api',
] as const

export const AHS_FEEDBACK_RATING_VALUES = ['POSITIVE', 'NEGATIVE'] as const
export const AHS_PROJECT_STATUS_VALUES = [
  'ACTIVE',
  'PAUSED',
  'COMPLETED',
  'ARCHIVED',
] as const
export const AHS_SCHEDULE_TYPE_VALUES = ['SCHEDULED', 'RECURRING'] as const
export const AHS_SCHEDULE_STATUS_VALUES = [
  'PENDING',
  'IN_PROGRESS',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'PAUSED',
] as const
export const AHS_TASK_STATUS_VALUES = [
  'PENDING',
  'BLOCKED',
  'READY',
  'IN_PROGRESS',
  'IN_REVIEW',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
] as const
export const AHS_TASK_PRIORITY_VALUES = [
  'URGENT',
  'HIGH',
  'NORMAL',
  'LOW',
] as const

export type AhsRequestTrigger = (typeof AHS_REQUEST_TRIGGER_VALUES)[number]
export type AhsRequestSource = (typeof AHS_SOURCE_VALUES)[number]
export type AhsFeedbackRating = (typeof AHS_FEEDBACK_RATING_VALUES)[number]
export type AhsProjectStatusValue = (typeof AHS_PROJECT_STATUS_VALUES)[number]
export type AhsScheduleStatusValue = (typeof AHS_SCHEDULE_STATUS_VALUES)[number]
export type AhsScheduleTypeValue = (typeof AHS_SCHEDULE_TYPE_VALUES)[number]
export type AhsTaskStatusValue = (typeof AHS_TASK_STATUS_VALUES)[number]
export type AhsTaskPriorityValue = (typeof AHS_TASK_PRIORITY_VALUES)[number]

// ---------------------------------------------------------------------------
// REST request types
// ---------------------------------------------------------------------------

export interface AhsAttachmentInfo {
  filename: string
  content_type: string
  size: number
  gcs_url: string
}

export interface AhsListAgentsParams {
  user_id?: string
  include_all?: boolean
}

export interface AhsGetAgentParams {
  include_system_prompts?: boolean
}

export interface AhsListSessionsParams {
  status?: AhsSessionStatus
  agent_name?: string
  trigger?: AhsTriggerType
  since?: string
  until?: string
  root_sessions_only?: boolean
  user_id?: string
  include_all?: boolean
  limit?: number
  offset?: number
}

export interface AhsListSchedulesParams {
  agent_name?: string
  status?: AhsScheduleStatus
  schedule_type?: AhsScheduleType
  created_by?: string
  limit?: number
}

export interface AhsListProjectsParams {
  status?: AhsProjectStatus
  creator_user_id?: string
  limit?: number
  offset?: number
}

export interface AhsListProjectTasksParams {
  status?: AhsTaskStatus
  parent_task_id?: string
  limit?: number
  offset?: number
}

export interface AhsSetProjectTaskStatusRequest {
  status: AhsTaskStatus
  result?: Record<string, unknown> | null
}

export interface AhsCreateSessionRequest {
  agent_id: string
  trigger: AhsRequestTrigger
  message?: string
  user_id?: string
  context?: Record<string, unknown>
  session_id?: string
  attachments?: AhsAttachmentInfo[]
  source?: AhsRequestSource
}

export interface AhsSendMessageRequest {
  session_id: string
  message: string
  user_id?: string
  slack_ts?: string | null
  slack_user_id?: string | null
  attachments?: AhsAttachmentInfo[]
  source?: AhsRequestSource
}

export interface AhsFeedbackRequest {
  session_id: string
  user_id: string
  message_id?: string
  rating?: AhsFeedbackRating
  structured?: Record<string, unknown>
  comment?: string
  slack_ts?: string
}

export interface AhsExecutorConfig {
  type?: string
  model?: string
}

export interface AhsSandboxConfig {
  enabled?: boolean
  [key: string]: unknown
}

export interface AhsCreateAgentRequest {
  name: string
  user_id: string
  display_name?: string
  description?: string
  executor_config?: AhsExecutorConfig
  tool_permissions?: Record<string, unknown>
  allowed_subagents?: string[]
  default_repo?: string
  sandbox?: AhsSandboxConfig
  max_turns?: number
  max_budget_usd?: number
  timeout_s?: number
  allowed_gateways?: string[]
  role_md?: string
  soul_md?: string
  additional_system_prompt?: string
}

export interface AhsEditAgentRequest
  extends Partial<Omit<AhsCreateAgentRequest, 'name' | 'user_id'>> {
  name: string
  user_id: string
}

interface AhsBaseCreateScheduleRequest {
  agent_name: string
  message: string
  timezone?: string
  user_id: string
  context?: Record<string, unknown>
  name?: string
  description?: string
}

export interface AhsCreateRecurringScheduleRequest
  extends AhsBaseCreateScheduleRequest {
  cron_expression: string
  max_runs?: number
}

export interface AhsTriggerScheduleRequest {
  user_id: string
}

export interface AhsResolveUserRequest {
  email: string
}

// ---------------------------------------------------------------------------
// WebSocket: server → client events
// ---------------------------------------------------------------------------

export interface AhsThreadStartedEvent {
  type: 'thread/started'
  thread_id: string
  event_id: string | number
}

export interface AhsTurnStartedEvent {
  type: 'turn/started'
  turn_id: string
  event_id: string | number
}

export interface AhsTurnUsage {
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  cost_usd?: number
  duration_ms?: number
  num_agent_turns?: number
}

export interface AhsTurnCompletedEvent {
  type: 'turn/completed'
  turn_id: string
  usage?: AhsTurnUsage
  status?: 'completed' | 'failed'
  error?: { message: string }
  event_id: string | number
}

export interface AhsItemBase {
  id: string
  type: string
  status?: string
}

export interface AhsUserMessageItem extends AhsItemBase {
  type: 'user_message'
  text?: string
}

export interface AhsAgentMessageItem extends AhsItemBase {
  type: 'agent_message'
  text?: string
}

export interface AhsCommandExecutionItem extends AhsItemBase {
  type: 'command_execution'
  command?: string
  aggregated_output?: string
  exit_code?: number
}

export interface AhsFileChangeItem extends AhsItemBase {
  type: 'file_change'
  changes?: Array<{ path: string; type?: string; kind?: string }>
}

export interface AhsMcpToolCallItem extends AhsItemBase {
  type: 'mcp_tool_call'
  server?: string
  tool?: string
  arguments?: Record<string, unknown>
  result?: unknown
  error?: { message: string }
}

export type AhsItem =
  | AhsUserMessageItem
  | AhsAgentMessageItem
  | AhsCommandExecutionItem
  | AhsFileChangeItem
  | AhsMcpToolCallItem
  | AhsItemBase

export interface AhsItemStartedEvent {
  type: 'item/started'
  item: AhsItem
  thread_id?: string
  turn_id?: string
  event_id: string | number
}

export interface AhsItemCompletedEvent {
  type: 'item/completed'
  item: AhsItem
  thread_id?: string
  turn_id?: string
  event_id: string | number
}

export interface AhsAgentMessageDeltaEvent {
  type: 'item/agentMessage/delta'
  item_id: string
  delta: string
  thread_id?: string
  turn_id?: string
  event_id: string | number
}

export interface AhsHeartbeatEvent {
  type: 'heartbeat'
}

export interface AhsErrorEvent {
  type: 'error'
  message: string
}

export type AhsServerEvent =
  | AhsThreadStartedEvent
  | AhsTurnStartedEvent
  | AhsTurnCompletedEvent
  | AhsItemStartedEvent
  | AhsItemCompletedEvent
  | AhsAgentMessageDeltaEvent
  | AhsHeartbeatEvent
  | AhsErrorEvent

// ---------------------------------------------------------------------------
// WebSocket: client → server messages
// ---------------------------------------------------------------------------

export interface AhsUserMessage {
  type: 'user_message'
  session_id: string
  content: string
  user_id: string
  source?: AhsRequestSource
}

export interface AhsStopMessage {
  type: 'stop'
  session_id: string
}

export interface AhsPingMessage {
  type: 'ping'
}

export type AhsClientMessage = AhsUserMessage | AhsStopMessage | AhsPingMessage
