import 'server-only'

import { z } from 'zod'
import { AHS_SESSION_TRIGGER_VALUES } from '../session-triggers'

// Server-side runtime validators for AHS responses.
// We parse backend payloads here to fail fast on schema drift.
export const ahsSessionStatusSchema = z.enum(['ACTIVE', 'COMPLETED', 'STALE'])
export const ahsProjectStatusSchema = z.enum([
  'ACTIVE',
  'PAUSED',
  'COMPLETED',
  'ARCHIVED',
])
export const ahsTaskStatusSchema = z.enum([
  'PENDING',
  'BLOCKED',
  'READY',
  'IN_PROGRESS',
  'IN_REVIEW',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
])
export const ahsTaskPrioritySchema = z.enum(['URGENT', 'HIGH', 'NORMAL', 'LOW'])
export const ahsTriggerTypeSchema = z.enum(AHS_SESSION_TRIGGER_VALUES)
export const ahsToolPermissionsSchema = z.record(z.string(), z.unknown())
export const ahsScheduleTypeSchema = z.enum(['SCHEDULED', 'RECURRING'])
export const ahsScheduleStatusSchema = z.enum([
  'PENDING',
  'IN_PROGRESS',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'PAUSED',
])

export const ahsAgentInfoSchema = z.object({
  name: z.string(),
  display_name: z.string(),
  description: z.string().nullable(),
  executor_type: z.string(),
  executor_model: z.string().nullable(),
  llm_model: z.string().nullable(),
  tool_permissions: ahsToolPermissionsSchema,
  allowed_subagents: z.array(z.string()),
  default_repo: z.string(),
  max_turns: z.number(),
  max_budget_usd: z.number(),
  timeout_s: z.number(),
  sandbox_enabled: z.boolean(),
  allowed_gateways: z.array(z.string()),
  creator_user_id: z.string().nullable().optional(),
})

export const ahsAgentDetailResponseSchema = z.object({
  agent: ahsAgentInfoSchema,
  system_prompts: z.record(z.string(), z.string()).nullable(),
})

export const ahsListAgentsResponseSchema = z.object({
  agents: z.array(ahsAgentInfoSchema),
})

export const ahsSessionInfoSchema = z.object({
  session_id: z.string(),
  title: z.string().nullable().optional(),
  agent_name: z.string(),
  status: ahsSessionStatusSchema,
  trigger: ahsTriggerTypeSchema,
  model: z.string().nullable(),
  created_at: z.string().nullable(),
  slack_channel_name: z.string().nullable(),
  slack_user_id: z.string().nullable(),
  tool_permissions: ahsToolPermissionsSchema.nullable(),
  has_full_tool_access: z.boolean().nullable(),
  parent_session_id: z.string().nullable(),
  message_count: z.number(),
})

const AHS_HISTORY_ROLE_VALUES = [
  'user',
  'USER',
  'assistant',
  'ASSISTANT',
  'agent',
  'AGENT',
  'system',
  'SYSTEM',
] as const

export const ahsHistoryRoleSchema = z
  .enum(AHS_HISTORY_ROLE_VALUES)
  .transform((role): 'user' | 'assistant' => {
    switch (role) {
      case 'user':
      case 'USER':
        return 'user'
      case 'assistant':
      case 'ASSISTANT':
      case 'agent':
      case 'AGENT':
      case 'system':
      case 'SYSTEM':
        return 'assistant'
    }
  })

export const ahsToolUseSchema = z.object({
  tool_use_id: z.string(),
  name: z.string(),
  input: z.unknown().optional(),
  output: z.unknown().optional(),
  is_error: z.boolean(),
  duration_ms: z.number().nullable().optional(),
  step: z.number().nullable().optional(),
})

const ahsDecimalSchema = z
  .union([z.number(), z.string()])
  .transform((value, ctx) => {
    const parsed = typeof value === 'number' ? value : Number(value)

    if (!Number.isFinite(parsed)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Invalid decimal value',
      })
      return z.NEVER
    }

    return parsed
  })

const EMPTY_AHS_TASK_SUMMARY = {
  PENDING: 0,
  BLOCKED: 0,
  READY: 0,
  IN_PROGRESS: 0,
  IN_REVIEW: 0,
  COMPLETED: 0,
  FAILED: 0,
  CANCELLED: 0,
  total: 0,
} as const

const ahsNullableStringSchema = z
  .string()
  .nullish()
  .transform((value) => value ?? null)

const ahsNullableStringArraySchema = z
  .array(z.string())
  .nullish()
  .transform((value) => value ?? null)

const ahsNullableUnknownSchema = z
  .unknown()
  .nullish()
  .transform((value) => value ?? null)

const ahsNullableDecimalSchema = ahsDecimalSchema
  .nullish()
  .transform((value) => value ?? null)

export const ahsTaskSummarySchema = z
  .object({
    PENDING: z.number().optional(),
    BLOCKED: z.number().optional(),
    READY: z.number().optional(),
    IN_PROGRESS: z.number().optional(),
    IN_REVIEW: z.number().optional(),
    COMPLETED: z.number().optional(),
    FAILED: z.number().optional(),
    CANCELLED: z.number().optional(),
    total: z.number().optional(),
  })
  .transform((value) => ({
    ...EMPTY_AHS_TASK_SUMMARY,
    ...value,
  }))

export const ahsProjectResponseSchema = z.object({
  agent_project_id: z.string(),
  name: z.string(),
  description: ahsNullableStringSchema,
  status: ahsProjectStatusSchema,
  creator_user_id: ahsNullableStringSchema,
  creator_user_name: ahsNullableStringSchema,
  slack_channel: ahsNullableStringSchema,
  budget_usd: ahsNullableDecimalSchema,
  budget_spent_usd: ahsDecimalSchema.nullish().transform((value) => value ?? 0),
  created_at: ahsNullableStringSchema,
  task_summary: ahsTaskSummarySchema
    .nullish()
    .transform((value) => value ?? EMPTY_AHS_TASK_SUMMARY),
})

export const ahsProjectDetailResponseSchema = ahsProjectResponseSchema.extend({
  shared_state: ahsNullableUnknownSchema,
})

export const ahsProjectListResponseSchema = z.object({
  items: z.array(ahsProjectResponseSchema),
  total: z.number(),
  offset: z.number(),
  limit: z.number(),
})

export const ahsTaskResponseSchema = z.object({
  agent_task_id: z.string(),
  agent_project_id: z.string(),
  title: z.string(),
  description: ahsNullableStringSchema,
  status: ahsTaskStatusSchema,
  priority: ahsTaskPrioritySchema,
  parent_task_id: ahsNullableStringSchema,
  depends_on: ahsNullableStringArraySchema,
  agent_name: ahsNullableStringSchema,
  agent_id: ahsNullableStringSchema,
  assigned_session_ids: ahsNullableStringArraySchema,
  result: ahsNullableUnknownSchema,
  task_data: ahsNullableUnknownSchema,
  estimated_effort: ahsNullableStringSchema,
  actual_spending_usd: ahsNullableDecimalSchema,
  completed_at: ahsNullableStringSchema,
  created_at: ahsNullableStringSchema,
  creator_user_id: ahsNullableStringSchema,
  creator_user_name: ahsNullableStringSchema,
})

export const ahsTaskStatusResponseSchema = z.object({
  agent_task_id: z.string(),
  status: ahsTaskStatusSchema,
  newly_ready_tasks: z.array(z.string()).default([]),
})

export const ahsTaskResumeResponseSchema = z.object({
  agent_task_id: z.string(),
  status: ahsTaskStatusSchema,
  session_to_resume: ahsNullableStringSchema,
})

export const ahsTaskListResponseSchema = z.object({
  items: z.array(ahsTaskResponseSchema),
  total: z.number(),
  offset: z.number(),
  limit: z.number(),
  task_summary: ahsTaskSummarySchema
    .nullish()
    .transform((value) => value ?? EMPTY_AHS_TASK_SUMMARY),
})

export const ahsMessageHistoryItemSchema = z.object({
  message_id: z.string(),
  turn_number: z.number(),
  role: ahsHistoryRoleSchema,
  content: z.string().nullable(),
  tool_uses: z
    .array(ahsToolUseSchema)
    .nullish()
    .transform((toolUses) => toolUses ?? []),
  cost_usd: z.number().nullable(),
  duration_ms: z.number().nullable(),
  num_agent_turns: z.number().nullable(),
  slack_ts: z.string().nullable(),
  created_at: z.string().nullable(),
})

export const ahsListSessionsResponseSchema = z.object({
  sessions: z.array(ahsSessionInfoSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
})

export const ahsCreateSessionResponseSchema = z.object({
  session_id: z.string(),
  status: z.string(),
})

export const ahsSendMessageResponseSchema = z.object({
  session_id: z.string(),
  turn_number: z.number(),
  status: z.enum(['processing', 'queued']),
})

export const ahsSessionDetailResponseSchema = z.object({
  session: ahsSessionInfoSchema,
  subsessions: z.array(ahsSessionInfoSchema),
})

export const ahsGetSessionHistoryResponseSchema = z.object({
  session_id: z.string(),
  agent_id: z.string(),
  status: ahsSessionStatusSchema,
  messages: z.array(ahsMessageHistoryItemSchema),
  total_messages: z.number(),
  limit: z.number(),
  offset: z.number(),
})

export const ahsStopSessionResponseSchema = z.object({
  session_id: z.string(),
  status: z.enum(['stopped', 'no_inflight_turn']),
})

export const ahsFeedbackResponseSchema = z.object({
  status: z.literal('recorded'),
})

export const ahsCreateAgentResponseSchema = z.object({
  name: z.string(),
  status: z.literal('created'),
})

export const ahsCreateScheduleResponseSchema = z.object({
  agent_schedule_id: z.string(),
  agent_name: z.string(),
  schedule_type: ahsScheduleTypeSchema,
  status: ahsScheduleStatusSchema,
})

export const ahsEditAgentResponseSchema = z.object({
  name: z.string(),
  status: z.literal('updated'),
})

export const ahsResolveUserResponseSchema = z.object({
  user_id: z.string(),
  email: z.string().email(),
})

export const ahsScheduleInfoSchema = z.object({
  agent_schedule_id: z.string(),
  agent_name: z.string(),
  schedule_type: ahsScheduleTypeSchema,
  status: ahsScheduleStatusSchema,
  message: z.string(),
  name: z.string().nullable(),
  description: z.string().nullable(),
  execute_at: z.string().nullable(),
  cron_expression: z.string().nullable(),
  cron_timezone: z.string().nullable(),
  next_run_at: z.string().nullable(),
  last_run_at: z.string().nullable(),
  run_count: z.number(),
  max_runs: z.number().nullable(),
  context: z.record(z.string(), z.unknown()).nullable(),
  created_by_user: z.string().nullable(),
  created_by_agent: z.string().nullable(),
  created_at: z.string().nullable(),
})

export const ahsListSchedulesResponseSchema = z.object({
  schedules: z.array(ahsScheduleInfoSchema),
  count: z.number(),
})

export const ahsScheduleDetailResponseSchema = z.object({
  schedule: ahsScheduleInfoSchema,
})

export const ahsEditScheduleResponseSchema = z.object({
  agent_schedule_id: z.string(),
  status: z.literal('updated'),
})

export const ahsTriggerScheduleResponseSchema = z.object({
  agent_schedule_id: z.string(),
  session_id: z.string(),
  run_number: z.number(),
})

export const ahsDeleteScheduleResponseSchema = z.object({
  agent_schedule_id: z.string(),
  status: z.literal('CANCELLED'),
})

export type AhsSessionStatus = z.infer<typeof ahsSessionStatusSchema>
export type AhsProjectStatus = z.infer<typeof ahsProjectStatusSchema>
export type AhsTaskStatus = z.infer<typeof ahsTaskStatusSchema>
export type AhsTaskPriority = z.infer<typeof ahsTaskPrioritySchema>
export type AhsTriggerType = z.infer<typeof ahsTriggerTypeSchema>
export type AhsAgentInfo = z.infer<typeof ahsAgentInfoSchema>
export type AhsAgentDetailResponse = z.infer<
  typeof ahsAgentDetailResponseSchema
>
export type AhsScheduleType = z.infer<typeof ahsScheduleTypeSchema>
export type AhsScheduleStatus = z.infer<typeof ahsScheduleStatusSchema>
export type AhsListAgentsResponse = z.infer<typeof ahsListAgentsResponseSchema>
export type AhsSessionInfo = z.infer<typeof ahsSessionInfoSchema>
export type AhsToolUse = z.infer<typeof ahsToolUseSchema>
export type AhsMessageHistoryItem = z.infer<typeof ahsMessageHistoryItemSchema>
export type AhsMessageRole = AhsMessageHistoryItem['role']
export type AhsListSessionsResponse = z.infer<
  typeof ahsListSessionsResponseSchema
>
export type AhsCreateSessionResponse = z.infer<
  typeof ahsCreateSessionResponseSchema
>
export type AhsSendMessageResponse = z.infer<
  typeof ahsSendMessageResponseSchema
>
export type AhsSessionDetailResponse = z.infer<
  typeof ahsSessionDetailResponseSchema
>
export type AhsGetSessionHistoryResponse = z.infer<
  typeof ahsGetSessionHistoryResponseSchema
>
export type AhsStopSessionResponse = z.infer<
  typeof ahsStopSessionResponseSchema
>
export type AhsFeedbackResponse = z.infer<typeof ahsFeedbackResponseSchema>
export type AhsCreateAgentResponse = z.infer<
  typeof ahsCreateAgentResponseSchema
>
export type AhsCreateScheduleResponse = z.infer<
  typeof ahsCreateScheduleResponseSchema
>
export type AhsTaskSummary = z.infer<typeof ahsTaskSummarySchema>
export type AhsProjectResponse = z.infer<typeof ahsProjectResponseSchema>
export type AhsProjectDetailResponse = z.infer<
  typeof ahsProjectDetailResponseSchema
>
export type AhsProjectListResponse = z.infer<
  typeof ahsProjectListResponseSchema
>
export type AhsTaskResponse = z.infer<typeof ahsTaskResponseSchema>
export type AhsTaskStatusResponse = z.infer<typeof ahsTaskStatusResponseSchema>
export type AhsTaskResumeResponse = z.infer<typeof ahsTaskResumeResponseSchema>
export type AhsTaskListResponse = z.infer<typeof ahsTaskListResponseSchema>
export type AhsEditAgentResponse = z.infer<typeof ahsEditAgentResponseSchema>
export type AhsResolveUserResponse = z.infer<
  typeof ahsResolveUserResponseSchema
>
export type AhsScheduleInfo = z.infer<typeof ahsScheduleInfoSchema>
export type AhsListSchedulesResponse = z.infer<
  typeof ahsListSchedulesResponseSchema
>
export type AhsScheduleDetailResponse = z.infer<
  typeof ahsScheduleDetailResponseSchema
>
export type AhsEditScheduleResponse = z.infer<
  typeof ahsEditScheduleResponseSchema
>
export type AhsTriggerScheduleResponse = z.infer<
  typeof ahsTriggerScheduleResponseSchema
>
export type AhsDeleteScheduleResponse = z.infer<
  typeof ahsDeleteScheduleResponseSchema
>
