import * as bunTest from 'bun:test'

const { describe, expect, it } = bunTest

;(
  bunTest as unknown as {
    mock: { module(path: string, factory: () => unknown): void }
  }
).mock.module('server-only', () => ({}))

describe('AHS history schemas', () => {
  it('parses list sessions responses that include TASK triggers', async () => {
    const { ahsListSessionsResponseSchema } = await import('./schemas')

    const parsed = ahsListSessionsResponseSchema.parse({
      sessions: [
        {
          session_id: 'session-1',
          title: 'Investigate failing deploy',
          agent_name: 'sre',
          status: 'COMPLETED',
          trigger: 'TASK',
          model: null,
          created_at: '2026-03-13T09:40:04.295582Z',
          slack_channel_name: null,
          slack_user_id: null,
          tool_permissions: {},
          has_full_tool_access: true,
          parent_session_id: null,
          message_count: 2,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    })

    expect(parsed.sessions[0]?.trigger).toBe('TASK')
    expect(parsed.sessions[0]?.title).toBe('Investigate failing deploy')
  })

  it('normalizes legacy and alternate history roles to the UI roles', async () => {
    const { ahsGetSessionHistoryResponseSchema } = await import('./schemas')

    const parsed = ahsGetSessionHistoryResponseSchema.parse({
      session_id: 'session-1',
      agent_id: 'sre',
      status: 'ACTIVE',
      messages: [
        {
          message_id: 'msg-1',
          turn_number: 1,
          role: 'USER',
          content: 'Hello',
          tool_uses: [],
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:00Z',
        },
        {
          message_id: 'msg-2',
          turn_number: 1,
          role: 'AGENT',
          content: 'Done',
          tool_uses: [],
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:01Z',
        },
        {
          message_id: 'msg-3',
          turn_number: 2,
          role: 'assistant',
          content: 'Still fine',
          tool_uses: [],
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:02Z',
        },
        {
          message_id: 'msg-4',
          turn_number: 2,
          role: 'ASSISTANT',
          content: 'Uppercase assistant',
          tool_uses: [],
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:03Z',
        },
        {
          message_id: 'msg-5',
          turn_number: 3,
          role: 'system',
          content: 'System prompt',
          tool_uses: [],
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:04Z',
        },
      ],
      total_messages: 5,
      limit: 50,
      offset: 0,
    })

    expect(parsed.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'assistant',
      'assistant',
      'assistant',
    ])
  })

  it('normalizes null tool_uses to an empty array', async () => {
    const { ahsGetSessionHistoryResponseSchema } = await import('./schemas')

    const parsed = ahsGetSessionHistoryResponseSchema.parse({
      session_id: 'session-1',
      agent_id: 'sre',
      status: 'ACTIVE',
      messages: [
        {
          message_id: 'msg-1',
          turn_number: 1,
          role: 'assistant',
          content: 'Done',
          tool_uses: null,
          cost_usd: null,
          duration_ms: null,
          num_agent_turns: null,
          slack_ts: null,
          created_at: '2026-03-06T12:00:00Z',
        },
      ],
      total_messages: 1,
      limit: 50,
      offset: 0,
    })

    expect(parsed.messages[0]?.tool_uses).toEqual([])
  })

  it('parses schedule responses with nullable optional fields', async () => {
    const { ahsListSchedulesResponseSchema } = await import('./schemas')

    const parsed = ahsListSchedulesResponseSchema.parse({
      schedules: [
        {
          agent_schedule_id: 'schedule-1',
          agent_name: 'ops-agent',
          schedule_type: 'RECURRING',
          status: 'PENDING',
          message: 'Check production status',
          name: 'Morning check',
          description: null,
          execute_at: null,
          cron_expression: '0 9 * * 1-5',
          cron_timezone: 'America/Los_Angeles',
          next_run_at: '2026-03-13T16:00:00Z',
          last_run_at: null,
          run_count: 2,
          max_runs: null,
          context: null,
          created_by_user: 'user-1',
          created_by_agent: null,
          created_at: '2026-03-10T12:00:00Z',
        },
      ],
      count: 1,
    })

    expect(parsed.schedules[0]?.schedule_type).toBe('RECURRING')
    expect(parsed.schedules[0]?.cron_expression).toBe('0 9 * * 1-5')
    expect(parsed.count).toBe(1)
  })

  it('parses schedule create responses', async () => {
    const { ahsCreateScheduleResponseSchema } = await import('./schemas')

    const parsed = ahsCreateScheduleResponseSchema.parse({
      agent_schedule_id: 'schedule-1',
      agent_name: 'ops-agent',
      schedule_type: 'SCHEDULED',
      status: 'PENDING',
    })

    expect(parsed.agent_schedule_id).toBe('schedule-1')
    expect(parsed.schedule_type).toBe('SCHEDULED')
    expect(parsed.status).toBe('PENDING')
  })

  it('defaults missing project summaries and optional project fields', async () => {
    const { ahsProjectListResponseSchema } = await import('./schemas')

    const parsed = ahsProjectListResponseSchema.parse({
      items: [
        {
          agent_project_id: 'project-1',
          name: 'Launch checklist',
          status: 'ACTIVE',
          budget_spent_usd: null,
          task_summary: {
            IN_PROGRESS: 2,
            total: 2,
          },
        },
      ],
      total: 1,
      offset: 0,
      limit: 20,
    })

    expect(parsed.items[0]?.budget_spent_usd).toBe(0)
    expect(parsed.items[0]?.task_summary.total).toBe(2)
    expect(parsed.items[0]?.task_summary.PENDING).toBe(0)
    expect(parsed.items[0]?.task_summary.IN_PROGRESS).toBe(2)
    expect(parsed.items[0]?.description).toBe(null)
  })

  it('parses task responses when optional fields are omitted', async () => {
    const { ahsTaskListResponseSchema } = await import('./schemas')

    const parsed = ahsTaskListResponseSchema.parse({
      items: [
        {
          agent_task_id: 'task-1',
          agent_project_id: 'project-1',
          title: 'Review plan',
          status: 'IN_PROGRESS',
          priority: 'HIGH',
        },
      ],
      total: 1,
      offset: 0,
      limit: 100,
      task_summary: {
        IN_PROGRESS: 1,
        total: 1,
      },
    })

    expect(parsed.items[0]?.depends_on).toBe(null)
    expect(parsed.items[0]?.task_data).toBe(null)
    expect(parsed.task_summary.total).toBe(1)
    expect(parsed.task_summary.BLOCKED).toBe(0)
  })

  it('parses task status responses with default newly ready tasks', async () => {
    const { ahsTaskStatusResponseSchema } = await import('./schemas')

    const parsed = ahsTaskStatusResponseSchema.parse({
      agent_task_id: 'task-1',
      status: 'COMPLETED',
    })

    expect(parsed.agent_task_id).toBe('task-1')
    expect(parsed.status).toBe('COMPLETED')
    expect(parsed.newly_ready_tasks).toEqual([])
  })

  it('parses task resume responses with nullable session ids', async () => {
    const { ahsTaskResumeResponseSchema } = await import('./schemas')

    const parsed = ahsTaskResumeResponseSchema.parse({
      agent_task_id: 'task-1',
      status: 'READY',
      session_to_resume: null,
    })

    expect(parsed.agent_task_id).toBe('task-1')
    expect(parsed.status).toBe('READY')
    expect(parsed.session_to_resume).toBe(null)
  })
})
