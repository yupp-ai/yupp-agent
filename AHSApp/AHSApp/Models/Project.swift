import Foundation

enum ProjectStatus: String, Codable, CaseIterable {
    case active = "ACTIVE"
    case paused = "PAUSED"
    case completed = "COMPLETED"
    case archived = "ARCHIVED"

    var label: String {
        switch self {
        case .active: "Active"
        case .paused: "Paused"
        case .completed: "Completed"
        case .archived: "Archived"
        }
    }

    var systemImage: String {
        switch self {
        case .active: "circle.fill"
        case .paused: "pause.circle.fill"
        case .completed: "checkmark.circle.fill"
        case .archived: "archivebox.fill"
        }
    }
}

enum TaskStatus: String, Codable, CaseIterable {
    case pending = "PENDING"
    case blocked = "BLOCKED"
    case ready = "READY"
    case inProgress = "IN_PROGRESS"
    case completed = "COMPLETED"
    case failed = "FAILED"
    case cancelled = "CANCELLED"
    case inReview = "IN_REVIEW"

    var label: String {
        switch self {
        case .pending: "Pending"
        case .blocked: "Blocked"
        case .ready: "Ready"
        case .inProgress: "In Progress"
        case .completed: "Completed"
        case .failed: "Failed"
        case .cancelled: "Cancelled"
        case .inReview: "In Review"
        }
    }

    var systemImage: String {
        switch self {
        case .pending: "circle.dashed"
        case .blocked: "xmark.circle"
        case .ready: "play.circle"
        case .inProgress: "circle.dotted.circle"
        case .completed: "checkmark.circle.fill"
        case .failed: "exclamationmark.circle.fill"
        case .cancelled: "minus.circle"
        case .inReview: "questionmark.circle"
        }
    }

    var colorName: String {
        switch self {
        case .pending: "textSecondary"
        case .blocked: "statusError"
        case .ready: "accent"
        case .inProgress: "statusActive"
        case .completed: "statusComplete"
        case .failed: "statusError"
        case .cancelled: "textSecondary"
        case .inReview: "statusWarning"
        }
    }
}

enum TaskPriority: String, Codable, CaseIterable {
    case urgent = "URGENT"
    case high = "HIGH"
    case normal = "NORMAL"
    case low = "LOW"

    var label: String {
        switch self {
        case .urgent: "Urgent"
        case .high: "High"
        case .normal: "Normal"
        case .low: "Low"
        }
    }
}

struct TaskSummary: Codable, Hashable {
    let total: Int?
    let completed: Int?
    let inProgress: Int?
    let ready: Int?
    let blocked: Int?
    let failed: Int?
    let pending: Int?
    let inReview: Int?
    let cancelled: Int?

    enum CodingKeys: String, CodingKey {
        case total
        case completed = "COMPLETED"
        case inProgress = "IN_PROGRESS"
        case ready = "READY"
        case blocked = "BLOCKED"
        case failed = "FAILED"
        case pending = "PENDING"
        case inReview = "IN_REVIEW"
        case cancelled = "CANCELLED"
    }
}

struct AgentProject: Codable, Identifiable, Hashable {
    let agentProjectId: String
    let name: String
    let description: String?
    let status: ProjectStatus?
    let creatorUserId: String?
    let creatorUserName: String?
    let createdAt: String?
    let slackChannel: String?
    let budgetUsd: FlexibleDouble?
    let budgetSpentUsd: FlexibleDouble?
    let taskSummary: TaskSummary?

    var id: String { agentProjectId }

    var progressFraction: Double {
        guard let summary = taskSummary, let total = summary.total, total > 0 else { return 0 }
        return Double(summary.completed ?? 0) / Double(total)
    }

    enum CodingKeys: String, CodingKey {
        case agentProjectId = "agent_project_id"
        case name, description, status
        case creatorUserId = "creator_user_id"
        case creatorUserName = "creator_user_name"
        case createdAt = "created_at"
        case slackChannel = "slack_channel"
        case budgetUsd = "budget_usd"
        case budgetSpentUsd = "budget_spent_usd"
        case taskSummary = "task_summary"
    }
}

/// Decodes a value that may be a number or a string containing a number.
struct FlexibleDouble: Codable, Hashable {
    let value: Double

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let d = try? container.decode(Double.self) {
            value = d
        } else if let s = try? container.decode(String.self), let d = Double(s) {
            value = d
        } else {
            value = 0
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(value)
    }
}

struct AgentTask: Codable, Identifiable, Hashable {
    let agentTaskId: String
    let agentProjectId: String?
    let title: String
    let description: String?
    let status: TaskStatus?
    let priority: TaskPriority?
    let agentName: String?
    let dependsOn: [String]?
    let parentTaskId: String?
    let assignedSessionIds: [String]?
    let result: TaskResult?
    let actualSpendingUsd: FlexibleDouble?
    let estimatedEffort: String?
    let createdAt: String?
    let completedAt: String?

    var id: String { agentTaskId }

    enum CodingKeys: String, CodingKey {
        case agentTaskId = "agent_task_id"
        case agentProjectId = "agent_project_id"
        case title, description, status, priority
        case agentName = "agent_name"
        case dependsOn = "depends_on"
        case parentTaskId = "parent_task_id"
        case assignedSessionIds = "assigned_session_ids"
        case result
        case actualSpendingUsd = "actual_spending_usd"
        case estimatedEffort = "estimated_effort"
        case createdAt = "created_at"
        case completedAt = "completed_at"
    }
}

struct TaskResult: Codable, Hashable {
    let prUrl: String?
    let slackThreadUrl: String?

    enum CodingKeys: String, CodingKey {
        case prUrl = "pr_url"
        case slackThreadUrl = "slack_thread_url"
    }
}
