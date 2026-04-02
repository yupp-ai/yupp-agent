import SwiftUI

enum NavigationDestination: Hashable {
    case chat
    case projects
    case schedules
    case agents
    case settings
}

enum ServerEnvironment: String, CaseIterable, Identifiable {
    case local = "localhost:8090"
    case staging = "ahs-staging.yupp.ai"
    case production = "ahs.yupp.ai"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .local: "Local (localhost:8090)"
        case .staging: "Staging (ahs-staging.yupp.ai)"
        case .production: "Production (ahs.yupp.ai)"
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var currentView: NavigationDestination = .chat
    @Published var selectedSessionId: String?
    @Published var showSearchOverlay = false
    @Published var showDetailPanel = false
    @Published var showNewSessionPicker = false
    @Published var sidebarVisible = true
    @Published var isLoading = false
    @Published var selectedServer: ServerEnvironment

    private(set) var apiClient: APIClient
    private(set) var authService: AuthService
    private(set) var webSocketClient: WebSocketClient
    let apiKey: String

    var host: String { selectedServer.rawValue }

    lazy var chatViewModel: ChatViewModel = ChatViewModel(appState: self)
    lazy var sessionsViewModel: SessionsViewModel = SessionsViewModel(appState: self)
    lazy var projectsViewModel: ProjectsViewModel = ProjectsViewModel(appState: self)
    lazy var schedulesViewModel: SchedulesViewModel = SchedulesViewModel(appState: self)
    lazy var agentsViewModel: AgentsViewModel = AgentsViewModel(appState: self)
    lazy var searchViewModel: SearchViewModel = SearchViewModel(appState: self)

    private static let serverKey = "ahs_selected_server"

    init() {
        // API key priority: env var > .env file > ~/.ahsapp/credentials
        let apiKey = ProcessInfo.processInfo.environment["AGENT_HARNESS_SERVICE_API_KEY"]
            ?? ProcessInfo.processInfo.environment["AHS_API_KEY"]
            ?? Self.loadApiKeyFromDotEnv()
            ?? LocalCache.apiKey
            ?? ""

        // Save to ~/.ahsapp/credentials for next launch
        if !apiKey.isEmpty {
            LocalCache.apiKey = apiKey
        }

        // Restore last selected server from ~/.ahsapp/
        let savedServer = LocalCache.selectedServer ?? ""
        let server = ServerEnvironment(rawValue: savedServer) ?? .production

        self.apiKey = apiKey
        self.selectedServer = server

        let api = APIClient(host: server.rawValue, apiKey: apiKey)
        self.apiClient = api
        self.authService = AuthService(apiClient: api)
        self.webSocketClient = WebSocketClient(host: server.rawValue, apiKey: apiKey)
    }

    /// Switch to a different server environment. Recreates all API clients.
    func switchServer(_ server: ServerEnvironment) {
        guard server != selectedServer else { return }
        selectedServer = server
        LocalCache.selectedServer = server.rawValue

        // Disconnect current WS
        webSocketClient.disconnect()

        // Recreate clients
        let api = APIClient(host: server.rawValue, apiKey: apiKey)
        self.apiClient = api
        self.authService = AuthService(apiClient: api)
        self.webSocketClient = WebSocketClient(host: server.rawValue, apiKey: apiKey)

        // Reset state
        selectedSessionId = nil
        currentView = .chat

        // Reload all data for the new server
        Task {
            await sessionsViewModel.refresh()
            await projectsViewModel.refresh()
            await schedulesViewModel.refresh()
            await agentsViewModel.refresh()
        }
    }

    func selectSession(_ sessionId: String) {
        selectedSessionId = sessionId
        currentView = .chat
        chatViewModel.attachSession(sessionId)
    }

    func createNewSession(agentId: String) async {
        guard authService.isAuthenticated else { return }
        do {
            let session = try await apiClient.createSession(agentId: agentId, userId: authService.userId)
            LocalCache.lastAgentName = agentId
            // Update usage counts
            var counts = LocalCache.agentUsageCounts
            counts[agentId, default: 0] += 1
            LocalCache.agentUsageCounts = counts
            selectSession(session.sessionId)
            await sessionsViewModel.refresh()
        } catch {
            // Error handled by individual VMs
        }
    }

    /// Reads AGENT_HARNESS_SERVICE_API_KEY from the project's .env file.
    private static func loadApiKeyFromDotEnv() -> String? {
        let candidates = [
            FileManager.default.currentDirectoryPath + "/.env",
            FileManager.default.currentDirectoryPath + "/../.env",
            NSString("~/workspace/yupp-agent/.env").expandingTildeInPath,
        ]

        for path in candidates {
            guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
            for line in contents.components(separatedBy: .newlines) {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("AGENT_HARNESS_SERVICE_API_KEY") {
                    if let eqIndex = trimmed.firstIndex(of: "=") {
                        var value = String(trimmed[trimmed.index(after: eqIndex)...])
                            .trimmingCharacters(in: .whitespaces)
                        if value.hasPrefix("\"") && value.hasSuffix("\"") && value.count >= 2 {
                            value = String(value.dropFirst().dropLast())
                        }
                        if !value.isEmpty { return value }
                    }
                }
            }
        }
        return nil
    }
}
