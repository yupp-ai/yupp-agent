import SwiftUI

@MainActor
final class ProjectsViewModel: ObservableObject {
    @Published var projects: [AgentProject] = []
    @Published var selectedProject: AgentProject?
    @Published var tasks: [AgentTask] = []
    @Published var selectedTask: AgentTask?
    @Published var isLoading = false
    @Published var showCompleted = true
    @Published var error: String?

    private weak var appState: AppState?
    private var lastFetch: Date?
    private let cacheTTL: TimeInterval = 60

    init(appState: AppState) {
        self.appState = appState
    }

    var filteredTasks: [AgentTask] {
        var result = tasks
        if !showCompleted {
            result = result.filter { $0.status != .completed && $0.status != .cancelled }
        }
        return result
    }

    // Build hierarchical tree: root tasks first, then children nested
    var taskTree: [TaskTreeNode] {
        let rootTasks = filteredTasks.filter { $0.parentTaskId == nil }
        return rootTasks.map { buildNode($0) }
    }

    private func buildNode(_ task: AgentTask) -> TaskTreeNode {
        let children = filteredTasks
            .filter { $0.parentTaskId == task.agentTaskId }
            .map { buildNode($0) }
        return TaskTreeNode(task: task, children: children)
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
            projects = try await appState.apiClient.listProjects()
            lastFetch = Date()
            error = nil
            if let selected = selectedProject {
                await loadTasks(for: selected.agentProjectId)
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func selectProject(_ project: AgentProject) {
        selectedProject = project
        selectedTask = nil
        Task { await loadTasks(for: project.agentProjectId) }
    }

    func loadTasks(for projectId: String) async {
        guard let appState else { return }
        do {
            tasks = try await appState.apiClient.getProjectTasks(projectId)
        } catch {
            self.error = error.localizedDescription
        }
    }

    func setProjectStatus(_ status: ProjectStatus) async {
        guard let project = selectedProject, let appState else { return }
        do {
            try await appState.apiClient.setProjectStatus(project.agentProjectId, status: status.rawValue)
            await refresh()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func setTaskStatus(_ task: AgentTask, status: TaskStatus) async {
        guard let project = selectedProject, let appState else { return }
        do {
            try await appState.apiClient.setTaskStatus(project.agentProjectId, taskId: task.agentTaskId, status: status.rawValue)
            await loadTasks(for: project.agentProjectId)
        } catch {
            self.error = error.localizedDescription
        }
    }

    func resumeTask(_ task: AgentTask) async {
        guard let project = selectedProject, let appState else { return }
        do {
            try await appState.apiClient.resumeTask(project.agentProjectId, taskId: task.agentTaskId)
            await loadTasks(for: project.agentProjectId)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct TaskTreeNode: Identifiable {
    let task: AgentTask
    let children: [TaskTreeNode]
    var id: String { task.agentTaskId }
}
