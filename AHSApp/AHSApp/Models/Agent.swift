import Foundation

struct Agent: Codable, Identifiable, Hashable {
    let name: String
    let displayName: String?
    let description: String?
    let executorType: String?
    let executorModel: String?
    let llmModel: String?
    let defaultRepo: String?
    let maxTurns: Int?
    let maxBudgetUsd: Double?
    let timeoutS: Int?
    let sandboxEnabled: Bool?
    let toolPermissions: [String: String]?
    let allowedSubagents: [String]?
    let allowedGateways: [String]?
    let creatorUserId: String?
    let systemPrompts: [String: String]?
    let additionalSystemPrompt: String?

    var id: String { name }

    var displayModel: String {
        executorModel ?? llmModel ?? "unknown"
    }

    var toolCount: String {
        guard let perms = toolPermissions else { return "none" }
        if perms["*"] == "allow" { return "all" }
        let allowed = perms.filter { $0.value == "allow" && $0.key != "*" }
        return allowed.isEmpty ? "none" : "\(allowed.count)"
    }

    var subagentCount: String {
        guard let subs = allowedSubagents else { return "none" }
        if subs.contains("*") { return "all" }
        return subs.isEmpty ? "none" : "\(subs.count)"
    }

    enum CodingKeys: String, CodingKey {
        case name
        case displayName = "display_name"
        case description
        case executorType = "executor_type"
        case executorModel = "executor_model"
        case llmModel = "llm_model"
        case defaultRepo = "default_repo"
        case maxTurns = "max_turns"
        case maxBudgetUsd = "max_budget_usd"
        case timeoutS = "timeout_s"
        case sandboxEnabled = "sandbox_enabled"
        case toolPermissions = "tool_permissions"
        case allowedSubagents = "allowed_subagents"
        case allowedGateways = "allowed_gateways"
        case creatorUserId = "creator_user_id"
        case systemPrompts = "system_prompts"
        case additionalSystemPrompt = "additional_system_prompt"
    }
}
