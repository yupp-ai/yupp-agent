import SwiftUI

@MainActor
final class SearchViewModel: ObservableObject {
    @Published var query = ""
    @Published var results: [SearchResultItem] = []
    @Published var isSearching = false
    @Published var error: String?

    private weak var appState: AppState?

    init(appState: AppState) {
        self.appState = appState
    }

    var groupedResults: [(SearchResultType, [SearchResultItem])] {
        let grouped = Dictionary(grouping: results) { $0.type }
        let order: [SearchResultType] = [.session, .message, .project, .task, .schedule, .pullRequest, .document]
        return order.compactMap { type in
            guard let items = grouped[type], !items.isEmpty else { return nil }
            return (type, items)
        }
    }

    func search() async {
        guard let appState, !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        isSearching = true
        defer { isSearching = false }
        do {
            results = try await appState.apiClient.search(text: query, userId: appState.authService.userId)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func selectResult(_ item: SearchResultItem) {
        guard let appState else { return }
        switch item.type {
        case .session, .message:
            if let sid = item.sessionId ?? (item.type == .session ? item.id : nil) {
                appState.selectSession(sid)
                appState.showSearchOverlay = false
            }
        case .project, .task:
            appState.currentView = .projects
            appState.showSearchOverlay = false
        case .schedule:
            appState.currentView = .schedules
            appState.showSearchOverlay = false
        default:
            if let urlStr = item.url, let url = URL(string: urlStr) {
                NSWorkspace.shared.open(url)
            }
            appState.showSearchOverlay = false
        }
    }

    func clear() {
        query = ""
        results = []
        error = nil
    }
}
