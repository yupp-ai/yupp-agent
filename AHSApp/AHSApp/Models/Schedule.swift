import Foundation

enum ScheduleType: String, Codable {
    case recurring = "RECURRING"
    case scheduled = "SCHEDULED"

    var label: String {
        switch self {
        case .recurring: "Recurring"
        case .scheduled: "One-time"
        }
    }
}

enum ScheduleStatus: String, Codable, CaseIterable {
    case pending = "PENDING"
    case paused = "PAUSED"
    case inProgress = "IN_PROGRESS"
    case completed = "COMPLETED"
    case failed = "FAILED"
    case cancelled = "CANCELLED"

    var label: String {
        switch self {
        case .pending: "Pending"
        case .paused: "Paused"
        case .inProgress: "In Progress"
        case .completed: "Completed"
        case .failed: "Failed"
        case .cancelled: "Cancelled"
        }
    }

    var systemImage: String {
        switch self {
        case .pending: "circle.fill"
        case .paused: "pause.circle.fill"
        case .inProgress: "circle.dotted.circle"
        case .completed: "checkmark.circle.fill"
        case .failed: "exclamationmark.circle.fill"
        case .cancelled: "circle"
        }
    }
}

struct AgentSchedule: Codable, Identifiable, Hashable {
    let agentScheduleId: String
    let name: String
    let description: String?
    let scheduleType: ScheduleType?
    let status: ScheduleStatus?
    let agentName: String?
    let cronExpression: String?
    let cronTimezone: String?
    let executeAt: String?
    let nextRunAt: String?
    let lastRunAt: String?
    let runCount: Int?
    let maxRuns: Int?
    let createdAt: String?
    let createdByUser: String?
    let createdByAgent: String?
    let message: String?

    var id: String { agentScheduleId }

    var nextRunDate: Date? {
        guard let s = nextRunAt else { return nil }
        return ISO8601DateFormatter.flexible.date(from: s)
    }

    enum CodingKeys: String, CodingKey {
        case agentScheduleId = "agent_schedule_id"
        case name, description
        case scheduleType = "schedule_type"
        case status
        case agentName = "agent_name"
        case cronExpression = "cron_expression"
        case cronTimezone = "cron_timezone"
        case executeAt = "execute_at"
        case nextRunAt = "next_run_at"
        case lastRunAt = "last_run_at"
        case runCount = "run_count"
        case maxRuns = "max_runs"
        case createdAt = "created_at"
        case createdByUser = "created_by_user"
        case createdByAgent = "created_by_agent"
        case message
    }
}

struct ScheduleRun: Codable, Identifiable, Hashable {
    let runNumber: Int
    let status: String?
    let startedAt: String?
    let completedAt: String?
    let sessionId: String?
    let error: String?

    var id: Int { runNumber }

    var duration: String? {
        guard let startStr = startedAt,
              let endStr = completedAt,
              let start = ISO8601DateFormatter.flexible.date(from: startStr),
              let end = ISO8601DateFormatter.flexible.date(from: endStr) else { return nil }
        let seconds = end.timeIntervalSince(start)
        if seconds < 60 { return String(format: "%.0fs", seconds) }
        return String(format: "%.0fm %.0fs", seconds / 60, seconds.truncatingRemainder(dividingBy: 60))
    }

    enum CodingKeys: String, CodingKey {
        case runNumber = "run_number"
        case status
        case startedAt = "started_at"
        case completedAt = "completed_at"
        case sessionId = "session_id"
        case error
    }
}
