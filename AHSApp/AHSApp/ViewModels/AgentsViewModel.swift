import SwiftUI

@MainActor
final class AgentsViewModel: ObservableObject {
    @Published var agents: [Agent] = []
    @Published var selectedAgent: Agent?
    @Published var detailedAgent: Agent?
    @Published var filterText = ""
    @Published var isLoading = false
    @Published var error: String?

    private weak var appState: AppState?
    private var lastFetch: Date?
    private let cacheTTL: TimeInterval = 60

    init(appState: AppState) {
        self.appState = appState
    }

    var filteredAgents: [Agent] {
        if filterText.isEmpty { return agents }
        let query = filterText.lowercased()
        return agents.filter {
            $0.name.lowercased().contains(query) ||
            ($0.displayName?.lowercased().contains(query) ?? false) ||
            ($0.description?.lowercased().contains(query) ?? false)
        }
    }

    func loadIfNeeded() async {
        if let lastFetch, Date().timeIntervalSince(lastFetch) < cacheTTL { return }
        await refresh()
    }

    func refresh() async {
        guard let appState else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            agents = try await appState.apiClient.listAgents()
            lastFetch = Date()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func selectAgent(_ agent: Agent) {
        selectedAgent = agent
        Task { await loadDetail(for: agent.name) }
    }

    func loadDetail(for name: String) async {
        guard let appState else { return }
        do {
            detailedAgent = try await appState.apiClient.getAgent(name)
        } catch {
            self.error = error.localizedDescription
        }
    }
}
