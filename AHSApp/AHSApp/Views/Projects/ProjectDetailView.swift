import SwiftUI

struct ProjectDetailView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: ProjectsViewModel

    var body: some View {
        if let project = viewModel.selectedProject {
            VStack(spacing: 0) {
                // Project info bar
                projectInfoBar(project)

                Divider()

                // Task tree + detail
                VStack(spacing: 0) {
                    TaskTreeView(viewModel: viewModel)
                        .frame(maxHeight: .infinity)

                    Divider()

                    taskDetail
                        .frame(height: 250)
                }
            }
        } else {
            ContentUnavailableView("Select a Project", systemImage: "folder", description: Text("Choose a project from the list"))
        }
    }

    @ViewBuilder
    private func projectInfoBar(_ project: AgentProject) -> some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(project.name)
                    .font(.headline)
                if let desc = project.description {
                    Text(desc)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer()

            // Status picker
            Menu {
                ForEach(ProjectStatus.allCases, id: \.self) { status in
                    Button(status.label) {
                        Task { await viewModel.setProjectStatus(status) }
                    }
                }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: project.status?.systemImage ?? "circle")
                    Text(project.status?.label ?? "Unknown")
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(.quaternary)
                .cornerRadius(6)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            if let summary = project.taskSummary, let total = summary.total {
                VStack(alignment: .trailing, spacing: 1) {
                    Text("\(summary.completed ?? 0)/\(total) tasks")
                        .font(.caption)
                        .monospacedDigit()
                    Text("\(summary.inProgress ?? 0) in progress")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.3))
    }

    @ViewBuilder
    private var taskDetail: some View {
        if let task = viewModel.selectedTask {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    // Title + status
                    HStack {
                        Image(systemName: task.status?.systemImage ?? "circle")
                            .foregroundStyle(statusColor(task.status))
                        Text(task.title)
                            .font(.headline)
                        Spacer()
                        if let priority = task.priority {
                            Text(priority.label)
                                .font(.caption)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(priorityColor(priority).opacity(0.15))
                                .cornerRadius(4)
                        }
                    }

                    Divider()

                    // Metadata grid
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 8) {
                        MetadataItem(label: "Status", value: task.status?.label ?? "Unknown")
                        MetadataItem(label: "Agent", value: task.agentName ?? "None")
                        if let spending = task.actualSpendingUsd {
                            MetadataItem(label: "Spending", value: String(format: "$%.2f", spending.value))
                        }
                        if let effort = task.estimatedEffort {
                            MetadataItem(label: "Effort", value: effort)
                        }
                    }

                    // Dependencies
                    if let deps = task.dependsOn, !deps.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Dependencies")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            Text(deps.joined(separator: ", "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }

                    // Links
                    HStack(spacing: 8) {
                        if let pr = task.result?.prUrl, let url = URL(string: pr) {
                            Link(destination: url) {
                                Label("View PR", systemImage: "arrow.triangle.pull")
                                    .font(.caption)
                            }
                        }
                        if let sessions = task.assignedSessionIds, let first = sessions.first {
                            Button {
                                appState.selectSession(first)
                            } label: {
                                Label("Open Session", systemImage: "bubble.left.and.bubble.right")
                                    .font(.caption)
                            }
                        }

                        Spacer()

                        // Actions
                        if task.status == .failed {
                            Button("Resume") {
                                Task { await viewModel.resumeTask(task) }
                            }
                            .controlSize(.small)
                        }

                        Menu("Set Status") {
                            ForEach(TaskStatus.allCases, id: \.self) { status in
                                Button(status.label) {
                                    Task { await viewModel.setTaskStatus(task, status: status) }
                                }
                            }
                        }
                        .controlSize(.small)
                    }

                    // Description
                    if let desc = task.description, !desc.isEmpty {
                        Divider()
                        Text("Description")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundStyle(.secondary)
                        Text(desc)
                            .font(.body)
                            .textSelection(.enabled)
                    }
                }
                .padding(16)
            }
        } else {
            ContentUnavailableView("Select a Task", systemImage: "checklist", description: Text("Choose a task from the tree above"))
        }
    }

    private func statusColor(_ status: TaskStatus?) -> Color {
        switch status {
        case .completed: .green
        case .failed: .red
        case .inProgress: .blue
        case .ready: .cyan
        case .blocked: .gray
        case .inReview: .orange
        default: .secondary
        }
    }

    private func priorityColor(_ priority: TaskPriority) -> Color {
        switch priority {
        case .urgent: .red
        case .high: .orange
        case .normal: .blue
        case .low: .gray
        }
    }
}

struct MetadataItem: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.caption)
        }
    }
}
