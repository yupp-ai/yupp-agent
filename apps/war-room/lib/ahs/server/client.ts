/**
 * Server-side AHS REST client.
 *
 * This module uses AHS credentials and must only be consumed by server code.
 */
import 'server-only'

import type { z } from 'zod'
import type {
  AhsAgentDetailResponse,
  AhsAgentInfo,
  AhsCreateAgentRequest,
  AhsCreateAgentResponse,
  AhsCreateRecurringScheduleRequest,
  AhsCreateScheduleRequest,
  AhsCreateScheduleResponse,
  AhsCreateSessionRequest,
  AhsCreateSessionResponse,
  AhsDeleteScheduleResponse,
  AhsEditAgentRequest,
  AhsEditAgentResponse,
  AhsEditScheduleRequest,
  AhsEditScheduleResponse,
  AhsFeedbackRequest,
  AhsFeedbackResponse,
  AhsGetAgentParams,
  AhsGetSessionHistoryResponse,
  AhsListAgentsParams,
  AhsListAgentsResponse,
  AhsListProjectsParams,
  AhsListProjectTasksParams,
  AhsListSchedulesParams,
  AhsListSchedulesResponse,
  AhsListSessionsParams,
  AhsListSessionsResponse,
  AhsProjectDetailResponse,
  AhsProjectListResponse,
  AhsResolveUserResponse,
  AhsScheduleDetailResponse,
  AhsSendMessageRequest,
  AhsSendMessageResponse,
  AhsSessionDetailResponse,
  AhsSetProjectTaskStatusRequest,
  AhsStopSessionResponse,
  AhsTaskListResponse,
  AhsTaskResponse,
  AhsTaskResumeResponse,
  AhsTaskStatusResponse,
  AhsTriggerScheduleRequest,
  AhsTriggerScheduleResponse,
} from '../types'
import { getAhsApiKey, getAhsHost } from './config'
import {
  ahsAgentDetailResponseSchema,
  ahsCreateAgentResponseSchema,
  ahsCreateScheduleResponseSchema,
  ahsCreateSessionResponseSchema,
  ahsDeleteScheduleResponseSchema,
  ahsEditAgentResponseSchema,
  ahsEditScheduleResponseSchema,
  ahsFeedbackResponseSchema,
  ahsGetSessionHistoryResponseSchema,
  ahsListAgentsResponseSchema,
  ahsListSchedulesResponseSchema,
  ahsListSessionsResponseSchema,
  ahsProjectDetailResponseSchema,
  ahsProjectListResponseSchema,
  ahsResolveUserResponseSchema,
  ahsScheduleDetailResponseSchema,
  ahsSendMessageResponseSchema,
  ahsSessionDetailResponseSchema,
  ahsStopSessionResponseSchema,
  ahsTaskListResponseSchema,
  ahsTaskResponseSchema,
  ahsTaskResumeResponseSchema,
  ahsTaskStatusResponseSchema,
  ahsTriggerScheduleResponseSchema,
} from './schemas'

export class AhsHttpError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'AhsHttpError'
    this.status = status
  }
}

export function isAhsHttpError(
  error: unknown,
  status?: number
): error is AhsHttpError {
  return (
    error instanceof AhsHttpError &&
    (status === undefined || error.status === status)
  )
}

function baseUrl(): string {
  const host = getAhsHost()
  if (!host) throw new Error('AHS host is not configured')
  const normalized = host.replace(/\/$/, '')
  if (!/^https?:\/\//.test(normalized)) {
    throw new Error('AHS host must include http:// or https://')
  }
  return normalized
}

function buildQueryString(
  values: Record<string, boolean | number | string | undefined>
): string {
  const query = new URLSearchParams()

  for (const [key, value] of Object.entries(values)) {
    if (value == null) continue
    query.set(key, String(value))
  }

  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

async function ahsFetch<T>(
  path: string,
  schema: z.ZodType<T>,
  init?: RequestInit
): Promise<T> {
  const url = `${baseUrl()}/ahs${path}`
  const apiKey = getAhsApiKey()
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'x-api-key': apiKey } : {}),
      ...init?.headers,
    },
  })

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new AhsHttpError(res.status, `AHS ${res.status}: ${text}`)
  }

  const data: unknown = await res.json()
  return schema.parse(data)
}

export async function listAgents(
  params?: AhsListAgentsParams
): Promise<AhsAgentInfo[]> {
  const data = await ahsFetch<AhsListAgentsResponse>(
    `/agents${buildQueryString({
      user_id: params?.user_id,
      include_all: params?.include_all,
    })}`,
    ahsListAgentsResponseSchema
  )
  return data.agents
}

export async function getAgent(
  agentName: string,
  params?: AhsGetAgentParams
): Promise<AhsAgentDetailResponse> {
  return ahsFetch<AhsAgentDetailResponse>(
    `/agent/${encodeURIComponent(agentName)}${buildQueryString({
      include_system_prompts: params?.include_system_prompts,
    })}`,
    ahsAgentDetailResponseSchema
  )
}

export async function listSessions(
  params?: AhsListSessionsParams
): Promise<AhsListSessionsResponse> {
  return ahsFetch<AhsListSessionsResponse>(
    `/sessions${buildQueryString({
      status: params?.status,
      agent_name: params?.agent_name,
      trigger: params?.trigger,
      since: params?.since,
      until: params?.until,
      root_sessions_only: params?.root_sessions_only,
      user_id: params?.user_id,
      include_all: params?.include_all,
      limit: params?.limit,
      offset: params?.offset,
    })}`,
    ahsListSessionsResponseSchema
  )
}

export async function getSession(
  id: string
): Promise<AhsSessionDetailResponse> {
  return ahsFetch<AhsSessionDetailResponse>(
    `/session/${encodeURIComponent(id)}`,
    ahsSessionDetailResponseSchema
  )
}

export async function listProjects(
  params?: AhsListProjectsParams
): Promise<AhsProjectListResponse> {
  return ahsFetch<AhsProjectListResponse>(
    `/projects${buildQueryString({
      status: params?.status,
      creator_user_id: params?.creator_user_id,
      limit: params?.limit,
      offset: params?.offset,
    })}`,
    ahsProjectListResponseSchema
  )
}

export async function getProject(
  projectId: string
): Promise<AhsProjectDetailResponse> {
  return ahsFetch<AhsProjectDetailResponse>(
    `/projects/${encodeURIComponent(projectId)}`,
    ahsProjectDetailResponseSchema
  )
}

export async function listProjectTasks(
  projectId: string,
  params?: AhsListProjectTasksParams
): Promise<AhsTaskListResponse> {
  return ahsFetch<AhsTaskListResponse>(
    `/projects/${encodeURIComponent(projectId)}/tasks${buildQueryString({
      status: params?.status,
      parent_task_id: params?.parent_task_id,
      limit: params?.limit,
      offset: params?.offset,
    })}`,
    ahsTaskListResponseSchema
  )
}

export async function getProjectTask(
  projectId: string,
  taskId: string
): Promise<AhsTaskResponse> {
  return ahsFetch<AhsTaskResponse>(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`,
    ahsTaskResponseSchema
  )
}

export async function setProjectTaskStatus(
  projectId: string,
  taskId: string,
  req: AhsSetProjectTaskStatusRequest
): Promise<AhsTaskStatusResponse> {
  return ahsFetch<AhsTaskStatusResponse>(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/status`,
    ahsTaskStatusResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function resumeProjectTask(
  projectId: string,
  taskId: string
): Promise<AhsTaskResumeResponse> {
  return ahsFetch<AhsTaskResumeResponse>(
    `/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/resume`,
    ahsTaskResumeResponseSchema,
    {
      method: 'POST',
    }
  )
}

export async function getSessionHistory(
  id: string,
  limit = 50,
  offset = 0
): Promise<AhsGetSessionHistoryResponse> {
  return ahsFetch<AhsGetSessionHistoryResponse>(
    `/session/${encodeURIComponent(id)}/history${buildQueryString({
      limit,
      offset,
    })}`,
    ahsGetSessionHistoryResponseSchema
  )
}

export async function createSession(
  req: AhsCreateSessionRequest
): Promise<AhsCreateSessionResponse> {
  return ahsFetch<AhsCreateSessionResponse>(
    '/session/create',
    ahsCreateSessionResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function sendMessage(
  req: AhsSendMessageRequest
): Promise<AhsSendMessageResponse> {
  return ahsFetch<AhsSendMessageResponse>(
    '/session/message',
    ahsSendMessageResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function stopSession(
  sessionId: string
): Promise<AhsStopSessionResponse> {
  return ahsFetch<AhsStopSessionResponse>(
    '/session/stop',
    ahsStopSessionResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    }
  )
}

export async function recordFeedback(
  req: AhsFeedbackRequest
): Promise<AhsFeedbackResponse> {
  return ahsFetch<AhsFeedbackResponse>(
    '/session/feedback',
    ahsFeedbackResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function resolveUserByEmail(
  email: string
): Promise<AhsResolveUserResponse> {
  return ahsFetch<AhsResolveUserResponse>(
    '/resolve_user',
    ahsResolveUserResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify({ email }),
    }
  )
}

export async function createAgent(
  req: AhsCreateAgentRequest
): Promise<AhsCreateAgentResponse> {
  return ahsFetch<AhsCreateAgentResponse>(
    '/agent/create',
    ahsCreateAgentResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function createSchedule(
  req: AhsCreateScheduleRequest
): Promise<AhsCreateScheduleResponse> {
  return ahsFetch<AhsCreateScheduleResponse>(
    '/schedules/create',
    ahsCreateScheduleResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function createRecurringSchedule(
  req: AhsCreateRecurringScheduleRequest
): Promise<AhsCreateScheduleResponse> {
  return ahsFetch<AhsCreateScheduleResponse>(
    '/schedules/create-recurring',
    ahsCreateScheduleResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function editAgent(
  req: AhsEditAgentRequest
): Promise<AhsEditAgentResponse> {
  return ahsFetch<AhsEditAgentResponse>(
    '/agent/edit',
    ahsEditAgentResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function listSchedules(
  params?: AhsListSchedulesParams
): Promise<AhsListSchedulesResponse> {
  return ahsFetch<AhsListSchedulesResponse>(
    `/schedules${buildQueryString({
      agent_name: params?.agent_name,
      status: params?.status,
      schedule_type: params?.schedule_type,
      created_by: params?.created_by,
      limit: params?.limit,
    })}`,
    ahsListSchedulesResponseSchema
  )
}

export async function getSchedule(
  agentScheduleId: string
): Promise<AhsScheduleDetailResponse> {
  return ahsFetch<AhsScheduleDetailResponse>(
    `/schedule/${encodeURIComponent(agentScheduleId)}`,
    ahsScheduleDetailResponseSchema
  )
}

export async function editSchedule(
  req: AhsEditScheduleRequest
): Promise<AhsEditScheduleResponse> {
  return ahsFetch<AhsEditScheduleResponse>(
    '/schedule/edit',
    ahsEditScheduleResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function triggerSchedule(
  agentScheduleId: string,
  req: AhsTriggerScheduleRequest
): Promise<AhsTriggerScheduleResponse> {
  return ahsFetch<AhsTriggerScheduleResponse>(
    `/schedule/${encodeURIComponent(agentScheduleId)}/trigger`,
    ahsTriggerScheduleResponseSchema,
    {
      method: 'POST',
      body: JSON.stringify(req),
    }
  )
}

export async function cancelSchedule(
  agentScheduleId: string,
  userId: string
): Promise<AhsDeleteScheduleResponse> {
  return ahsFetch<AhsDeleteScheduleResponse>(
    `/schedule/${encodeURIComponent(agentScheduleId)}${buildQueryString({
      user_id: userId,
    })}`,
    ahsDeleteScheduleResponseSchema,
    {
      method: 'DELETE',
    }
  )
}
