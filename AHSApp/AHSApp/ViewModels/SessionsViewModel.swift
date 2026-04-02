import SwiftUI

@MainActor
final class SessionsViewModel: ObservableObject {
    @Published var sessions: [Session] = []
    @Published var isLoading = false
    @Published var filterTrigger: TriggerType?
    @Published var showOnlyActive = false
    @Published var error: String?

    private weak var appState: AppState?
    private var lastFetch: Date?
    private let cacheTTL: TimeInterval = 60

    init(appState: AppState) {
        self.appState = appState
        // Immediately load from disk cache for instant startup
        sessions = LocalCache.sessions
    }

    var filteredSessions: [Session] {
        var result = sessions
        if let trigger = filterTrigger {
            result = result.filter { $0.trigger == trigger }
        }
        if showOnlyActive {
            result = result.filter { $0.status == .active }
        }
        return result
    }

    var groupedSessions: [(String, [Session])] {
        let sorted = filteredSessions.sorted { s1, s2 in
            (s1.createdDate ?? .distantPast) > (s2.createdDate ?? .distantPast)
        }

        var groups: [(String, [Session])] = []
        var today: [Session] = []
        var yesterday: [Session] = []
        var thisWeek: [Session] = []
        var older: [Session] = []

        let calendar = Calendar.current
        let now = Date()

        for session in sorted {
            guard let date = session.createdDate else {
                older.append(session)
                continue
            }
            if calendar.isDateInToday(date) {
                today.append(session)
            } else if calendar.isDateInYesterday(date) {
                yesterday.append(session)
            } else if let weekAgo = calendar.date(byAdding: .day, value: -7, to: now), date > weekAgo {
                thisWeek.append(session)
            } else {
                older.append(session)
            }
        }

        if !today.isEmpty { groups.append(("Today", today)) }
        if !yesterday.isEmpty { groups.append(("Yesterday", yesterday)) }
        if !thisWeek.isEmpty { groups.append(("This Week", thisWeek)) }
        if !older.isEmpty { groups.append(("Older", older)) }

        return groups
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
            let uid = appState.authService.userId.isEmpty ? nil : appState.authService.userId
            let fresh = try await appState.apiClient.listSessions(limit: 100, trigger: filterTrigger?.rawValue, userId: uid)
            sessions = fresh
            lastFetch = Date()
            error = nil
            LocalCache.sessions = fresh
        } catch {
            self.error = error.localizedDescription
        }
    }

}
