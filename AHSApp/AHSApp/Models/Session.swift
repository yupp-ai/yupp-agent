import Foundation

enum SessionStatus: String, Codable, CaseIterable {
    case active = "ACTIVE"
    case completed = "COMPLETED"
    case stale = "STALE"

    var label: String {
        switch self {
        case .active: "Active"
        case .completed: "Completed"
        case .stale: "Stale"
        }
    }

    var systemImage: String {
        switch self {
        case .active: "circle.fill"
        case .completed: "checkmark.circle.fill"
        case .stale: "circle"
        }
    }

    var colorName: String {
        switch self {
        case .active: "statusActive"
        case .completed: "statusComplete"
        case .stale: "statusWarning"
        }
    }
}

enum TriggerType: String, Codable, CaseIterable {
    case api
    case slack
    case webhook
    case cron

    // API returns uppercase ("API", "SLACK"), raw values are lowercase
    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self).lowercased()
        self = TriggerType(rawValue: value) ?? .api
    }

    var label: String { rawValue.uppercased() }

    var systemImage: String {
        switch self {
        case .api: "bolt.fill"
        case .slack: "number"
        case .webhook: "link"
        case .cron: "clock.fill"
        }
    }
}

struct Session: Codable, Identifiable, Hashable {
    let sessionId: String
    let agentId: String?
    let agentName: String?
    let status: SessionStatus?
    let trigger: TriggerType?
    let title: String?
    let messageCount: Int?
    let createdAt: String?
    let slackChannelName: String?
    let parentSessionId: String?
    let model: String?
    let userId: String?
    let executorType: String?
    let source: String?

    var id: String { sessionId }

    var displayTitle: String {
        title ?? String(sessionId.prefix(8))
    }

    var displayAgent: String {
        agentName ?? agentId ?? "unknown"
    }

    var createdDate: Date? {
        guard let createdAt else { return nil }
        return ISO8601DateFormatter.flexible.date(from: createdAt)
    }

    var timeAgo: String {
        guard let date = createdDate else { return "" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case agentId = "agent_id"
        case agentName = "agent_name"
        case status, trigger, title
        case messageCount = "message_count"
        case createdAt = "created_at"
        case slackChannelName = "slack_channel_name"
        case parentSessionId = "parent_session_id"
        case model
        case userId = "user_id"
        case executorType = "executor_type"
        case source
    }
}

extension ISO8601DateFormatter {
    nonisolated(unsafe) static let flexible: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}
