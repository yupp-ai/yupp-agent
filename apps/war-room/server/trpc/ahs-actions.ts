import { z } from 'zod'
import {
  ahsCreateScheduleRequestSchema,
  ahsEditScheduleRequestSchema,
} from '@/lib/ahs/request-schemas'
import {
  cancelSchedule,
  createAgent,
  createRecurringSchedule,
  createSchedule,
  createSession,
  editAgent,
  editSchedule,
  getAgent,
  getProject,
  getProjectTask,
  getSchedule,
  getSession,
  getSessionHistory,
  listAgents,
  listProjects,
  listProjectTasks,
  listSchedules,
  listSessions,
  recordFeedback,
  resumeProjectTask,
  sendMessage,
  setProjectTaskStatus,
  stopSession,
  triggerSchedule,
} from '@/lib/ahs/server/client'
import { getAhsApiKey, getAhsHost } from '@/lib/ahs/server/config'
import { AHS_SESSION_TRIGGER_VALUES } from '@/lib/ahs/session-triggers'
import {
  AHS_FEEDBACK_RATING_VALUES,
  AHS_PROJECT_STATUS_VALUES,
  AHS_REQUEST_TRIGGER_VALUES,
  AHS_SCHEDULE_STATUS_VALUES,
  AHS_SCHEDULE_TYPE_VALUES,
  AHS_SOURCE_VALUES,
  AHS_TASK_STATUS_VALUES,
} from '@/lib/ahs/types'
import { operatorAction, trpcActions } from './init'

function buildWsUrl(sessionId: string): string {
  const host = getAhsHost()
  if (!host) throw new Error('AHS host is not configured')
  const normalized = host.replace(/\/$/, '')
  let wsHost: string
  if (normalized.startsWith('http')) {
    wsHost = normalized.replace(/^http/, 'ws')
  } else {
    const isIp = /^\d+\.\d+\.\d+\.\d+/.test(normalized)
    wsHost = `${isIp ? 'ws' : 'wss'}://${normalized}`
  }
  const apiKey = getAhsApiKey()
  return `${wsHost}/ahs/session/${encodeURIComponent(sessionId)}/ws?api_key=${encodeURIComponent(apiKey)}`
}

const attachmentSchema = z.object({
  filename: z.string(),
  content_type: z.string(),
  size: z.number(),
  gcs_url: z.string(),
})

const executorConfigSchema = z.object({
  type: z.string().optional(),
  model: z.string().optional(),
})

const sandboxSchema = z
  .object({ enabled: z.boolean().optional() })
  .catchall(z.unknown())

const legacyTriggerSchema = z.enum(['SLACK', 'WEBHOOK', 'CRON', 'API'])

function normalizeTriggerValue(
  trigger:
    | z.infer<typeof legacyTriggerSchema>
    | (typeof AHS_REQUEST_TRIGGER_VALUES)[number]
): (typeof AHS_REQUEST_TRIGGER_VALUES)[number] {
  return trigger.toLowerCase() as (typeof AHS_REQUEST_TRIGGER_VALUES)[number]
}

export const ahsActions = trpcActions({
  getWsUrl: operatorAction
    .input(z.object({ sessionId: z.string() }))
    .query(({ input }) => ({ url: buildWsUrl(input.sessionId) })),

  listAgents: operatorAction
    .input(
      z
        .object({
          include_all: z.boolean().optional(),
        })
        .optional()
    )
    .query(({ ctx, input }) => {
      return listAgents({
        include_all: input?.include_all ?? true,
        ...(input?.include_all === false
          ? { user_id: ctx.session.user.id }
          : {}),
      })
    }),

  getAgent: operatorAction
    .input(
      z.object({
        name: z.string(),
        include_system_prompts: z.boolean().optional(),
      })
    )
    .query(({ input }) =>
      getAgent(input.name, {
        include_system_prompts: input.include_system_prompts,
      })
    ),

  listProjects: operatorAction
    .input(
      z
        .object({
          status: z.enum(AHS_PROJECT_STATUS_VALUES).optional(),
          mine_only: z.boolean().optional(),
          limit: z.number().min(1).max(100).optional(),
          offset: z.number().min(0).optional(),
        })
        .optional()
    )
    .query(({ ctx, input }) =>
      listProjects({
        status: input?.status,
        limit: input?.limit,
        offset: input?.offset,
        ...(input?.mine_only ? { creator_user_id: ctx.session.user.id } : {}),
      })
    ),

  getProject: operatorAction
    .input(z.object({ projectId: z.string() }))
    .query(({ input }) => getProject(input.projectId)),

  getProjectTask: operatorAction
    .input(z.object({ projectId: z.string(), taskId: z.string() }))
    .query(({ input }) => getProjectTask(input.projectId, input.taskId)),

  listProjectTasks: operatorAction
    .input(
      z.object({
        projectId: z.string(),
        status: z.enum(AHS_TASK_STATUS_VALUES).optional(),
        parent_task_id: z.string().optional(),
        limit: z.number().min(1).max(500).optional(),
        offset: z.number().min(0).optional(),
      })
    )
    .query(({ input }) =>
      listProjectTasks(input.projectId, {
        status: input.status,
        parent_task_id: input.parent_task_id,
        limit: input.limit,
        offset: input.offset,
      })
    ),

  setProjectTaskStatus: operatorAction
    .input(
      z.object({
        projectId: z.string(),
        taskId: z.string(),
        status: z.enum(AHS_TASK_STATUS_VALUES),
        result: z.record(z.string(), z.unknown()).nullish(),
      })
    )
    .mutation(({ input }) =>
      setProjectTaskStatus(input.projectId, input.taskId, {
        status: input.status,
        result: input.result,
      })
    ),

  resumeProjectTask: operatorAction
    .input(z.object({ projectId: z.string(), taskId: z.string() }))
    .mutation(({ input }) => resumeProjectTask(input.projectId, input.taskId)),

  listSessions: operatorAction
    .input(
      z
        .object({
          status: z.enum(['ACTIVE', 'COMPLETED', 'STALE']).optional(),
          agent_name: z.string().optional(),
          trigger: z.enum(AHS_SESSION_TRIGGER_VALUES).optional(),
          since: z.string().optional(),
          until: z.string().optional(),
          root_sessions_only: z.boolean().optional(),
          include_all: z.boolean().optional(),
          limit: z.number().optional(),
          offset: z.number().optional(),
        })
        .optional()
    )
    .query(({ ctx, input }) => {
      return listSessions({
        status: input?.status,
        agent_name: input?.agent_name,
        trigger: input?.trigger,
        since: input?.since,
        until: input?.until,
        root_sessions_only: input?.root_sessions_only,
        include_all: input?.include_all ?? true,
        limit: input?.limit,
        offset: input?.offset,
        ...(input?.include_all === false
          ? { user_id: ctx.session.user.id }
          : {}),
      })
    }),

  getSession: operatorAction
    .input(z.object({ id: z.string() }))
    .query(({ input }) => getSession(input.id)),

  getSessionHistory: operatorAction
    .input(
      z.object({
        id: z.string(),
        limit: z.number().optional(),
        offset: z.number().optional(),
      })
    )
    .query(({ input }) =>
      getSessionHistory(input.id, input.limit, input.offset)
    ),

  createSession: operatorAction
    .input(
      z.object({
        agent_id: z.string(),
        trigger: legacyTriggerSchema
          .or(z.enum(AHS_REQUEST_TRIGGER_VALUES))
          .transform((trigger) => normalizeTriggerValue(trigger)),
        message: z.string().optional(),
        context: z.record(z.string(), z.unknown()).optional(),
        session_id: z.string().optional(),
        attachments: z.array(attachmentSchema).optional(),
        source: z.enum(AHS_SOURCE_VALUES).optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      createSession({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  sendMessage: operatorAction
    .input(
      z.object({
        session_id: z.string(),
        message: z.string(),
        slack_ts: z.string().nullish(),
        slack_user_id: z.string().nullish(),
        attachments: z.array(attachmentSchema).optional(),
        source: z.enum(AHS_SOURCE_VALUES).optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      sendMessage({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  stopSession: operatorAction
    .input(z.object({ sessionId: z.string() }))
    .mutation(({ input }) => stopSession(input.sessionId)),

  recordFeedback: operatorAction
    .input(
      z.object({
        session_id: z.string(),
        message_id: z.string().optional(),
        rating: z.enum(AHS_FEEDBACK_RATING_VALUES).optional(),
        structured: z.record(z.string(), z.unknown()).optional(),
        comment: z.string().optional(),
        slack_ts: z.string().optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      recordFeedback({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  createAgent: operatorAction
    .input(
      z.object({
        name: z.string(),
        display_name: z.string().optional(),
        description: z.string().optional(),
        executor_config: executorConfigSchema.optional(),
        tool_permissions: z.record(z.string(), z.unknown()).optional(),
        allowed_subagents: z.array(z.string()).optional(),
        default_repo: z.string().optional(),
        sandbox: sandboxSchema.optional(),
        max_turns: z.number().optional(),
        max_budget_usd: z.number().optional(),
        timeout_s: z.number().optional(),
        allowed_gateways: z.array(z.string()).optional(),
        role_md: z.string().optional(),
        soul_md: z.string().optional(),
        additional_system_prompt: z.string().optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      createAgent({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  createSchedule: operatorAction
    .input(ahsCreateScheduleRequestSchema.omit({ user_id: true }))
    .mutation(({ ctx, input }) =>
      createSchedule({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  createRecurringSchedule: operatorAction
    .input(
      z.object({
        agent_name: z.string(),
        message: z.string(),
        cron_expression: z.string(),
        timezone: z.string().optional(),
        name: z.string().optional(),
        description: z.string().optional(),
        context: z.record(z.string(), z.unknown()).optional(),
        max_runs: z.number().optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      createRecurringSchedule({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  editAgent: operatorAction
    .input(
      z.object({
        name: z.string(),
        display_name: z.string().optional(),
        description: z.string().optional(),
        executor_config: executorConfigSchema.optional(),
        tool_permissions: z.record(z.string(), z.unknown()).optional(),
        allowed_subagents: z.array(z.string()).optional(),
        default_repo: z.string().optional(),
        sandbox: sandboxSchema.optional(),
        max_turns: z.number().optional(),
        max_budget_usd: z.number().optional(),
        timeout_s: z.number().optional(),
        allowed_gateways: z.array(z.string()).optional(),
        role_md: z.string().optional(),
        soul_md: z.string().optional(),
        additional_system_prompt: z.string().optional(),
      })
    )
    .mutation(({ ctx, input }) =>
      editAgent({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  listSchedules: operatorAction
    .input(
      z
        .object({
          agent_name: z.string().optional(),
          status: z.enum(AHS_SCHEDULE_STATUS_VALUES).optional(),
          schedule_type: z.enum(AHS_SCHEDULE_TYPE_VALUES).optional(),
          mine_only: z.boolean().optional(),
          limit: z.number().min(1).max(200).optional(),
        })
        .optional()
    )
    .query(({ ctx, input }) =>
      listSchedules({
        agent_name: input?.agent_name,
        status: input?.status,
        schedule_type: input?.schedule_type,
        limit: input?.limit,
        ...(input?.mine_only ? { created_by: ctx.session.user.id } : {}),
      })
    ),

  getSchedule: operatorAction
    .input(z.object({ id: z.string() }))
    .query(({ input }) => getSchedule(input.id)),

  editSchedule: operatorAction
    .input(ahsEditScheduleRequestSchema.omit({ user_id: true }))
    .mutation(({ ctx, input }) =>
      editSchedule({
        ...input,
        user_id: ctx.session.user.id,
      })
    ),

  triggerSchedule: operatorAction
    .input(z.object({ agent_schedule_id: z.string() }))
    .mutation(({ ctx, input }) =>
      triggerSchedule(input.agent_schedule_id, {
        user_id: ctx.session.user.id,
      })
    ),

  cancelSchedule: operatorAction
    .input(z.object({ agent_schedule_id: z.string() }))
    .mutation(({ ctx, input }) =>
      cancelSchedule(input.agent_schedule_id, ctx.session.user.id)
    ),
})
