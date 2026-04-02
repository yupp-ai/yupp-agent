import SwiftUI

struct TaskTreeView: View {
    @ObservedObject var viewModel: ProjectsViewModel

    var body: some View {
        List(selection: Binding(
            get: { viewModel.selectedTask?.agentTaskId },
            set: { id in
                viewModel.selectedTask = viewModel.tasks.first { $0.agentTaskId == id }
            }
        )) {
            ForEach(viewModel.taskTree) { node in
                TaskTreeNodeView(node: node, depth: 0)
            }
        }
        .listStyle(.inset(alternatesRowBackgrounds: true))
        .overlay {
            if viewModel.filteredTasks.isEmpty && viewModel.selectedProject != nil {
                ContentUnavailableView("No Tasks", systemImage: "checklist", description: Text("This project has no tasks"))
            }
        }
    }
}

struct TaskTreeNodeView: View {
    let node: TaskTreeNode
    let depth: Int

    var body: some View {
        DisclosureGroup {
            ForEach(node.children) { child in
                TaskTreeNodeView(node: child, depth: depth + 1)
            }
        } label: {
            TaskRowView(task: node.task)
                .tag(node.task.agentTaskId)
        }
        .disclosureGroupStyle(TreeDisclosureStyle())
    }
}

struct TaskRowView: View {
    let task: AgentTask

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: task.status?.systemImage ?? "circle.dashed")
                .font(.caption)
                .foregroundStyle(statusColor)

            Text(task.title)
                .lineLimit(1)

            Spacer()

            if let deps = task.dependsOn, !deps.isEmpty {
                Text("[\u{2192}\(deps.count)]")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let agent = task.agentName {
                Text(agent)
                    .font(.caption2)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.quaternary)
                    .cornerRadius(3)
            }

            if let priority = task.priority, priority != .normal {
                Text(priority.label)
                    .font(.caption2)
                    .foregroundStyle(priorityColor)
            }
        }
    }

    private var statusColor: Color {
        switch task.status {
        case .completed: .green
        case .failed: .red
        case .inProgress: .blue
        case .ready: .cyan
        case .blocked: .gray
        case .inReview: .orange
        default: .secondary
        }
    }

    private var priorityColor: Color {
        switch task.priority {
        case .urgent: .red
        case .high: .orange
        default: .secondary
        }
    }
}

struct TreeDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                if !configuration.isExpanded {
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                } else {
                    Image(systemName: "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                configuration.label
            }
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation { configuration.isExpanded.toggle() }
            }

            if configuration.isExpanded {
                configuration.content
                    .padding(.leading, 16)
            }
        }
    }
}
