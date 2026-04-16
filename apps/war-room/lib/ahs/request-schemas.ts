import { z } from 'zod'

export const ahsCreateScheduleRequestSchema = z.object({
  agent_name: z.string(),
  message: z.string(),
  // One-time schedules come from <input type="datetime-local">, so we accept
  // ISO-8601 local datetimes without a timezone offset here.
  execute_at: z.iso.datetime({ local: true }),
  timezone: z.string().optional(),
  user_id: z.string(),
  context: z.record(z.string(), z.unknown()).optional(),
  name: z.string().optional(),
  description: z.string().optional(),
})

export const ahsEditScheduleRequestSchema = z.object({
  agent_schedule_id: z.string(),
  user_id: z.string(),
  message: z.string().optional(),
  cron_expression: z.string().optional(),
  timezone: z.string().optional(),
  name: z.string().optional(),
  description: z.string().optional(),
  context: z.record(z.string(), z.unknown()).optional(),
  max_runs: z.number().optional(),
})

export type AhsCreateScheduleRequest = z.infer<
  typeof ahsCreateScheduleRequestSchema
>
export type AhsEditScheduleRequest = z.infer<
  typeof ahsEditScheduleRequestSchema
>
