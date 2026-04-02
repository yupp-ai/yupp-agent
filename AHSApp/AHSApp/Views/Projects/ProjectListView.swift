import SwiftUI

struct ProjectListView: View {
    @ObservedObject var viewModel: ProjectsViewModel

    var body: some View {
        List(viewModel.projects, selection: Binding(
            get: { viewModel.selectedProject?.agentProjectId },
            set: { id in
                if let id, let project = viewModel.projects.first(where: { $0.agentProjectId == id }) {
                    viewModel.selectProject(project)
                }
            }
        )) { project in
            ProjectRow(project: project)
                .tag(project.agentProjectId)
        }
        .listStyle(.sidebar)
        .overlay {
            if viewModel.projects.isEmpty && !viewModel.isLoading {
                ContentUnavailableView("No Projects", systemImage: "folder", description: Text("No projects found"))
            }
            if viewModel.isLoading {
                ProgressView()
            }
        }
    }
}

struct ProjectRow: View {
    let project: AgentProject

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: project.status?.systemImage ?? "circle")
                    .font(.caption)
                    .foregroundStyle(statusColor)
                Text(project.name)
                    .fontWeight(.medium)
                    .lineLimit(1)
            }

            HStack(spacing: 8) {
                // Progress bar
                ProgressBar(summary: project.taskSummary)
                    .frame(height: 6)

                // Budget
                if let spent = project.budgetSpentUsd {
                    if let limit = project.budgetUsd {
                        Text(String(format: "$%.0f/$%.0f", spent.value, limit.value))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    } else {
                        Text(String(format: "$%.0f", spent.value))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
            }

            if let creator = project.creatorUserName ?? project.creatorUserId {
                Text(creator)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }

    private var statusColor: Color {
        switch project.status {
        case .active: .green
        case .paused: .orange
        case .completed: .gray
        case .archived: .gray
        case .none: .gray
        }
    }
}

// MARK: - Progress Bar

struct ProgressBar: View {
    let summary: TaskSummary?

    var body: some View {
        GeometryReader { geo in
            let total = max(summary?.total ?? 1, 1)
            let w = geo.size.width

            HStack(spacing: 0) {
                segment(count: summary?.completed ?? 0, total: total, width: w, color: .green)
                segment(count: summary?.inProgress ?? 0, total: total, width: w, color: .blue)
                segment(count: summary?.ready ?? 0, total: total, width: w, color: .cyan)
                segment(count: summary?.inReview ?? 0, total: total, width: w, color: .orange)
                segment(count: summary?.failed ?? 0, total: total, width: w, color: .red)
                segment(count: summary?.blocked ?? 0, total: total, width: w, color: .gray)
                Spacer(minLength: 0)
            }
            .frame(height: geo.size.height)
            .background(Color(nsColor: .separatorColor).opacity(0.3))
            .cornerRadius(3)
        }
    }

    @ViewBuilder
    private func segment(count: Int, total: Int, width: CGFloat, color: Color) -> some View {
        if count > 0 {
            Rectangle()
                .fill(color)
                .frame(width: width * CGFloat(count) / CGFloat(total))
        }
    }
}
