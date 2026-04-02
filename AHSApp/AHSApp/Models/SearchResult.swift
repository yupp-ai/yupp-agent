import Foundation

enum SearchResultType: String, Codable {
    case session
    case message
    case project
    case task
    case schedule
    case pullRequest = "pull_request"
    case document

    var label: String {
        switch self {
        case .session: "Sessions"
        case .message: "Messages"
        case .project: "Projects"
        case .task: "Tasks"
        case .schedule: "Schedules"
        case .pullRequest: "Pull Requests"
        case .document: "Documents"
        }
    }

    var systemImage: String {
        switch self {
        case .session: "bubble.left.and.bubble.right"
        case .message: "text.bubble"
        case .project: "folder"
        case .task: "checklist"
        case .schedule: "clock"
        case .pullRequest: "arrow.triangle.pull"
        case .document: "doc.text"
        }
    }
}

struct SearchResponse: Codable {
    let results: [SearchResultItem]
}

struct SearchResultItem: Codable, Identifiable, Hashable {
    let id: String
    let type: SearchResultType
    let title: String
    let subtitle: String?
    let score: Double?
    let sessionId: String?
    let projectId: String?
    let url: String?

    enum CodingKeys: String, CodingKey {
        case id, type, title, subtitle, score
        case sessionId = "session_id"
        case projectId = "project_id"
        case url
    }
}
