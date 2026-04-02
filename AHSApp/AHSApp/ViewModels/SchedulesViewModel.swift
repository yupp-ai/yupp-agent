import SwiftUI

@MainActor
final class SchedulesViewModel: ObservableObject {
    @Published var schedules: [AgentSchedule] = []
    @Published var selectedSchedule: AgentSchedule?
    @Published var runs: [ScheduleRun] = []
    @Published var isLoading = false
    @Published var showOnlyMine = false
    @Published var error: String?

    private weak var appState: AppState?
    private var lastFetch: Date?
    private let cacheTTL: TimeInterval = 60

    init(appState: AppState) {
        self.appState = appState
    }

    var recurringSchedules: [AgentSchedule] {
        schedules.filter { $0.scheduleType == .recurring }
    }

    var oneTimeSchedules: [AgentSchedule] {
        schedules.filter { $0.scheduleType == .scheduled }
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
            let createdBy = showOnlyMine ? appState.authService.userId : nil
            schedules = try await appState.apiClient.listSchedules(createdBy: createdBy)
            lastFetch = Date()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    func selectSchedule(_ schedule: AgentSchedule) {
        selectedSchedule = schedule
        Task { await loadRuns(for: schedule.agentScheduleId) }
    }

    func loadRuns(for scheduleId: String) async {
        guard let appState else { return }
        do {
            runs = try await appState.apiClient.getScheduleRuns(scheduleId)
        } catch {
            self.error = error.localizedDescription
        }
    }

    func triggerNow() async {
        guard let schedule = selectedSchedule, let appState else { return }
        do {
            try await appState.apiClient.triggerSchedule(schedule.agentScheduleId, userId: appState.authService.userId)
            await loadRuns(for: schedule.agentScheduleId)
        } catch {
            self.error = error.localizedDescription
        }
    }

    func cancelSchedule() async {
        guard let schedule = selectedSchedule, let appState else { return }
        do {
            try await appState.apiClient.cancelSchedule(schedule.agentScheduleId, userId: appState.authService.userId)
            await refresh()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
