import Foundation

enum MessageRole: String, Codable {
    case user
    case assistant
    case agent
    case system

    // API returns uppercase ("USER", "AGENT"), raw values are lowercase
    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self).lowercased()
        self = MessageRole(rawValue: value) ?? .assistant
    }
}

struct ToolUse: Codable, Identifiable, Hashable, Sendable {
    let id = UUID()
    let name: String?
    let status: String?
    let result: String?
    let server: String?
    let arguments: [String: AnyCodable]?

    var displayName: String {
        if let server, !server.isEmpty {
            return "\(server):\(name ?? "unknown")"
        }
        return name ?? "unknown"
    }

    enum CodingKeys: String, CodingKey {
        case name, status, result, server, arguments
    }
}

struct ChatMessage: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let role: MessageRole
    var content: String
    let toolUses: [ToolUse]?
    let timestamp: Date

    init(role: MessageRole, content: String, toolUses: [ToolUse]? = nil, timestamp: Date = Date()) {
        self.id = UUID()
        self.role = role
        self.content = content
        self.toolUses = toolUses
        self.timestamp = timestamp
    }

    static func == (lhs: ChatMessage, rhs: ChatMessage) -> Bool {
        lhs.id == rhs.id
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
}

// MARK: - WebSocket Events

enum WSEventType: String, Codable {
    case threadStarted = "thread/started"
    case turnStarted = "turn/started"
    case itemStarted = "item/started"
    case itemDelta = "item/agentMessage/delta"
    case itemCompleted = "item/completed"
    case turnCompleted = "turn/completed"
    case error
    case stopAck = "stop_ack"
}

struct WSEvent: Codable {
    let type: String
    let item: WSItem?
    let delta: String?
    let status: String?
    let usage: WSUsage?
    let error: AnyCodable?
}

struct WSItem: Codable {
    let type: String?
    let text: String?
    let command: String?
    let exitCode: Int?
    let status: String?
    let tool: String?
    let server: String?
    let arguments: [String: AnyCodable]?
    let changes: [WSFileChange]?

    enum CodingKeys: String, CodingKey {
        case type, text, command
        case exitCode = "exit_code"
        case status, tool, server, arguments, changes
    }
}

struct WSFileChange: Codable {
    let kind: String?
    let path: String?
}

struct WSUsage: Codable {
    let inputTokens: Int?
    let outputTokens: Int?
    let costUsd: Double?
    let durationMs: Int?

    enum CodingKeys: String, CodingKey {
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case costUsd = "cost_usd"
        case durationMs = "duration_ms"
    }
}

// MARK: - Turn Summary

struct TurnSummary: Identifiable, Hashable {
    let id = UUID()
    let inputTokens: Int
    let outputTokens: Int
    let costUsd: Double
    let durationMs: Int

    var formattedCost: String {
        String(format: "$%.2f", costUsd)
    }

    var formattedDuration: String {
        let seconds = Double(durationMs) / 1000.0
        return String(format: "%.1fs", seconds)
    }

    var formattedTokens: String {
        "\(formatTokenCount(inputTokens)) in / \(formatTokenCount(outputTokens)) out"
    }

    private func formatTokenCount(_ count: Int) -> String {
        if count >= 1_000_000 { return String(format: "%.1fM", Double(count) / 1_000_000) }
        if count >= 1_000 { return String(format: "%.1fk", Double(count) / 1_000) }
        return "\(count)"
    }
}

// MARK: - Tool Activity

struct ToolActivity: Identifiable, Hashable {
    let id = UUID()
    let name: String
    let server: String?
    let arguments: String?
    var status: ToolActivityStatus
    var duration: TimeInterval?
    let startTime: Date

    var displayName: String {
        if let server, !server.isEmpty {
            return "\(name) (\(server))"
        }
        return name
    }
}

enum ToolActivityStatus: Hashable {
    case running
    case completed
    case failed

    var systemImage: String {
        switch self {
        case .running: "circle.dotted.circle"
        case .completed: "checkmark.circle.fill"
        case .failed: "xmark.circle.fill"
        }
    }
}

// MARK: - AnyCodable

struct AnyCodable: Codable, Hashable, @unchecked Sendable {
    let value: Any

    init(_ value: Any) {
        self.value = value
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            value = NSNull()
        } else if let bool = try? container.decode(Bool.self) {
            value = bool
        } else if let int = try? container.decode(Int.self) {
            value = int
        } else if let double = try? container.decode(Double.self) {
            value = double
        } else if let string = try? container.decode(String.self) {
            value = string
        } else if let array = try? container.decode([AnyCodable].self) {
            value = array.map(\.value)
        } else if let dict = try? container.decode([String: AnyCodable].self) {
            value = dict.mapValues(\.value)
        } else {
            value = NSNull()
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch value {
        case is NSNull:
            try container.encodeNil()
        case let bool as Bool:
            try container.encode(bool)
        case let int as Int:
            try container.encode(int)
        case let double as Double:
            try container.encode(double)
        case let string as String:
            try container.encode(string)
        default:
            try container.encodeNil()
        }
    }

    static func == (lhs: AnyCodable, rhs: AnyCodable) -> Bool {
        String(describing: lhs.value) == String(describing: rhs.value)
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(String(describing: value))
    }

    var stringValue: String? { value as? String }
}
