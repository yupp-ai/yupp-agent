import Foundation

actor APIClient {
    let baseURL: URL
    let apiKey: String
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        return d
    }()

    init(host: String = "ahs.yupp.ai", apiKey: String = "") {
        let isLocal = host.hasPrefix("localhost") || host.hasPrefix("127.0.0.1") || host.hasPrefix("0.0.0.0")
        let scheme = isLocal ? "http" : "https"
        let hostWithPort: String
        if isLocal && !host.contains(":") {
            hostWithPort = "\(host):8090"
        } else {
            hostWithPort = host
        }
        self.baseURL = URL(string: "\(scheme)://\(hostWithPort)/ahs")!
        self.apiKey = apiKey
    }

    // MARK: - Generic Request

    private func request<T: Decodable>(_ method: String, path: String, body: [String: Any]? = nil, query: [String: String]? = nil) async throws -> T {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if let query {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }

        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(apiKey, forHTTPHeaderField: "X-API-Key")

        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let (data, response) = try await URLSession.shared.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw APIError.httpError(statusCode: httpResponse.statusCode, body: body)
        }

        return try decoder.decode(T.self, from: data)
    }

    private func requestRaw(_ method: String, path: String, body: [String: Any]? = nil, query: [String: String]? = nil) async throws -> Data {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if let query {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }

        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(apiKey, forHTTPHeaderField: "X-API-Key")

        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let (data, response) = try await URLSession.shared.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse, (200...299).contains(httpResponse.statusCode) else {
            let bodyStr = String(data: data, encoding: .utf8) ?? ""
            throw APIError.httpError(statusCode: (response as? HTTPURLResponse)?.statusCode ?? 0, body: bodyStr)
        }

        return data
    }

    // MARK: - Auth

    // Response: {"user_id": "...", "email": "..."}
    func resolveUser(email: String) async throws -> String {
        struct Resp: Decodable {
            let userId: String
            enum CodingKeys: String, CodingKey { case userId = "user_id" }
        }
        let resp: Resp = try await request("POST", path: "/resolve_user", body: ["email": email])
        return resp.userId
    }

    // MARK: - Sessions

    // Response: {"sessions": [...], "total", "limit", "offset"}
    func listSessions(limit: Int = 50, trigger: String? = nil, userId: String? = nil) async throws -> [Session] {
        struct Resp: Decodable { let sessions: [Session] }
        var query: [String: String] = ["limit": "\(limit)", "root_sessions_only": "true"]
        if let trigger { query["trigger"] = trigger }
        if let userId { query["user_id"] = userId }
        let resp: Resp = try await request("GET", path: "/sessions", query: query)
        return resp.sessions
    }

    // Response: {"session": {...}, "subsessions": [...]}
    func getSession(_ sessionId: String) async throws -> Session {
        struct Resp: Decodable { let session: Session }
        let resp: Resp = try await request("GET", path: "/session/\(sessionId)")
        return resp.session
    }

    // Response: {"session_id": "...", "status": "..."}  — NOT a full Session
    func createSession(agentId: String, userId: String, message: String? = nil) async throws -> Session {
        struct Resp: Decodable {
            let sessionId: String
            let status: String?
            enum CodingKeys: String, CodingKey {
                case sessionId = "session_id"
                case status
            }
        }
        var body: [String: Any] = [
            "agent_id": agentId,
            "trigger": "api",
            "source": "mac_app",
            "user_id": userId,
        ]
        if let message { body["message"] = message }
        let resp: Resp = try await request("POST", path: "/session/create", body: body)
        // Return a minimal Session with just the ID — caller should fetch full details
        return Session(
            sessionId: resp.sessionId,
            agentId: agentId,
            agentName: agentId,
            status: SessionStatus(rawValue: resp.status ?? "ACTIVE"),
            trigger: .api,
            title: nil,
            messageCount: 0,
            createdAt: nil,
            slackChannelName: nil,
            parentSessionId: nil,
            model: nil,
            userId: userId,
            executorType: nil,
            source: "mac_app"
        )
    }

    func sendMessage(sessionId: String, message: String, userId: String) async throws {
        _ = try await requestRaw("POST", path: "/session/message", body: [
            "session_id": sessionId,
            "message": message,
            "user_id": userId,
            "source": "mac_app",
        ])
    }

    // Response: {"messages": [...], "session_id", "total_messages", ...}
    func getHistory(sessionId: String, limit: Int = 500, offset: Int = 0) async throws -> [ChatMessage] {
        struct HistoryItem: Decodable {
            let role: String
            let content: String?
            let toolUses: [ToolUse]?
            enum CodingKeys: String, CodingKey {
                case role, content
                case toolUses = "tool_uses"
            }
        }
        struct Resp: Decodable { let messages: [HistoryItem] }
        let resp: Resp = try await request(
            "GET", path: "/session/\(sessionId)/history",
            query: ["limit": "\(limit)", "offset": "\(offset)"]
        )
        return resp.messages.compactMap { item in
            guard let role = MessageRole(rawValue: item.role.lowercased()) else { return nil }
            return ChatMessage(role: role, content: item.content ?? "", toolUses: item.toolUses)
        }
    }

    // MARK: - Agents

    // Response: {"agents": [...]}
    func listAgents() async throws -> [Agent] {
        struct Resp: Decodable { let agents: [Agent] }
        let resp: Resp = try await request("GET", path: "/agents", query: ["include_all": "true"])
        return resp.agents
    }

    // Response: {"agent": {...}, "system_prompts": {...}, "additional_system_prompt": ...}
    func getAgent(_ name: String) async throws -> Agent {
        struct Resp: Decodable {
            let agent: Agent
            let systemPrompts: [String: String]?
            let additionalSystemPrompt: String?
            enum CodingKeys: String, CodingKey {
                case agent
                case systemPrompts = "system_prompts"
                case additionalSystemPrompt = "additional_system_prompt"
            }
        }
        let resp: Resp = try await request("GET", path: "/agent/\(name)", query: ["include_system_prompts": "true"])
        // Merge system_prompts from wrapper into the agent
        var agent = resp.agent
        // Agent's own systemPrompts field might be nil since it comes from the wrapper
        // We need a mutable copy — but Agent is a struct, so let's return a combined version
        return Agent(
            name: agent.name,
            displayName: agent.displayName,
            description: agent.description,
            executorType: agent.executorType,
            executorModel: agent.executorModel,
            llmModel: agent.llmModel,
            defaultRepo: agent.defaultRepo,
            maxTurns: agent.maxTurns,
            maxBudgetUsd: agent.maxBudgetUsd,
            timeoutS: agent.timeoutS,
            sandboxEnabled: agent.sandboxEnabled,
            toolPermissions: agent.toolPermissions,
            allowedSubagents: agent.allowedSubagents,
            allowedGateways: agent.allowedGateways,
            creatorUserId: agent.creatorUserId,
            systemPrompts: resp.systemPrompts ?? agent.systemPrompts,
            additionalSystemPrompt: resp.additionalSystemPrompt ?? agent.additionalSystemPrompt
        )
    }

    // MARK: - Projects

    // Response: {"items": [...], "total", "offset", "limit"}
    func listProjects(limit: Int = 50) async throws -> [AgentProject] {
        struct Resp: Decodable { let items: [AgentProject] }
        let resp: Resp = try await request("GET", path: "/projects", query: ["limit": "\(limit)"])
        return resp.items
    }

    // Response: {"items": [...], "total", "offset", "limit", "task_summary"}
    func getProjectTasks(_ projectId: String) async throws -> [AgentTask] {
        struct Resp: Decodable { let items: [AgentTask] }
        let resp: Resp = try await request("GET", path: "/projects/\(projectId)/tasks")
        return resp.items
    }

    func setProjectStatus(_ projectId: String, status: String) async throws {
        _ = try await requestRaw("POST", path: "/projects/\(projectId)/status", body: ["status": status])
    }

    func setTaskStatus(_ projectId: String, taskId: String, status: String) async throws {
        _ = try await requestRaw("POST", path: "/projects/\(projectId)/tasks/\(taskId)/status", body: ["status": status])
    }

    func resumeTask(_ projectId: String, taskId: String) async throws {
        _ = try await requestRaw("POST", path: "/projects/\(projectId)/tasks/\(taskId)/resume")
    }

    // MARK: - Schedules

    // Response: {"schedules": [...], "count": N}
    func listSchedules(limit: Int = 100, createdBy: String? = nil) async throws -> [AgentSchedule] {
        struct Resp: Decodable { let schedules: [AgentSchedule] }
        var query: [String: String] = ["limit": "\(limit)"]
        if let createdBy { query["created_by"] = createdBy }
        let resp: Resp = try await request("GET", path: "/schedules", query: query)
        return resp.schedules
    }

    // Response: {"runs": [...], "count": N}
    func getScheduleRuns(_ scheduleId: String, limit: Int = 20) async throws -> [ScheduleRun] {
        struct Resp: Decodable { let runs: [ScheduleRun] }
        let resp: Resp = try await request("GET", path: "/schedule/\(scheduleId)/runs", query: ["limit": "\(limit)"])
        return resp.runs
    }

    func triggerSchedule(_ scheduleId: String, userId: String) async throws {
        _ = try await requestRaw("POST", path: "/schedule/\(scheduleId)/trigger", body: ["user_id": userId])
    }

    func cancelSchedule(_ scheduleId: String, userId: String) async throws {
        _ = try await requestRaw("DELETE", path: "/schedule/\(scheduleId)", query: ["user_id": userId])
    }

    // MARK: - Search

    // Response: {"query": "...", "results": [...], "total_count": N}
    func search(text: String, userId: String) async throws -> [SearchResultItem] {
        let resp: SearchResponse = try await request("POST", path: "/search", body: ["text": text, "user_id": userId])
        return resp.results
    }
}

enum APIError: LocalizedError {
    case invalidResponse
    case httpError(statusCode: Int, body: String)
    case decodingError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Invalid server response"
        case .httpError(let code, let body):
            return "HTTP \(code): \(body)"
        case .decodingError(let error):
            return "Decoding error: \(error.localizedDescription)"
        }
    }
}
