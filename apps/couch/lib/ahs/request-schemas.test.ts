import { describe, expect, it } from 'bun:test'
import { ahsCreateScheduleRequestSchema } from './request-schemas'

describe('ahsCreateScheduleRequestSchema', () => {
  it('accepts the datetime-local value shape sent by the schedule form', () => {
    expect(
      ahsCreateScheduleRequestSchema.safeParse({
        agent_name: 'hercule-poirot',
        message: 'Run this once',
        execute_at: '2026-03-13T10:30',
        timezone: 'America/Los_Angeles',
        user_id: 'user-123',
      }).success
    ).toBe(true)
  })

  it('rejects invalid execute_at values', () => {
    expect(
      ahsCreateScheduleRequestSchema.safeParse({
        agent_name: 'hercule-poirot',
        message: 'Run this once',
        execute_at: 'not-a-date',
        user_id: 'user-123',
      }).success
    ).toBe(false)
  })
})
