import Foundation

/// Persistent local cache at ~/.ahsapp/ for instant startup and remembered preferences.
enum LocalCache {
    private static let cacheDir: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ahsapp")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    // MARK: - Generic Read/Write

    static func save<T: Encodable>(_ value: T, to filename: String) {
        let url = cacheDir.appendingPathComponent(filename)
        guard let data = try? JSONEncoder().encode(value) else { return }
        try? data.write(to: url, options: .atomic)
    }

    static func load<T: Decodable>(_ type: T.Type, from filename: String) -> T? {
        let url = cacheDir.appendingPathComponent(filename)
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    static func saveString(_ value: String, to filename: String) {
        let url = cacheDir.appendingPathComponent(filename)
        try? value.write(to: url, atomically: true, encoding: .utf8)
    }

    static func loadString(from filename: String) -> String? {
        let url = cacheDir.appendingPathComponent(filename)
        return try? String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Specific Keys

    /// Cached session list for instant sidebar on startup.
    static var sessions: [Session] {
        get { load([Session].self, from: "sessions.json") ?? [] }
        set { save(newValue, to: "sessions.json") }
    }

    /// Last selected server environment.
    static var selectedServer: String? {
        get { loadString(from: "server.txt") }
        set { if let v = newValue { saveString(v, to: "server.txt") } }
    }

    /// Last used agent name for quick "New Session" button.
    static var lastAgentName: String? {
        get { loadString(from: "last_agent.txt") }
        set { if let v = newValue { saveString(v, to: "last_agent.txt") } }
    }

    /// Agent usage counts for sorting the agent picker.
    static var agentUsageCounts: [String: Int] {
        get { load([String: Int].self, from: "agent_usage.json") ?? [:] }
        set { save(newValue, to: "agent_usage.json") }
    }

    /// Cached agent list for instant agent picker.
    static var agents: [Agent] {
        get { load([Agent].self, from: "agents.json") ?? [] }
        set { save(newValue, to: "agents.json") }
    }

    // MARK: - Credentials (stored separately for clarity)

    /// API key — stored at ~/.ahsapp/credentials
    static var apiKey: String? {
        get { loadString(from: "credentials") }
        set { if let v = newValue { saveString(v, to: "credentials") } }
    }
}
