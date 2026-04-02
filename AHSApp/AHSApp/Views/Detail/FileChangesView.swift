import SwiftUI

struct FileChangesView: View {
    let activities: [ToolActivity]

    // Extract file-related activities
    var fileActivities: [ToolActivity] {
        activities.filter { activity in
            let name = activity.name.lowercased()
            return name.contains("file") || name.contains("edit") || name.contains("write") || name.contains("create")
        }
    }

    var body: some View {
        if fileActivities.isEmpty {
            ContentUnavailableView("No File Changes", systemImage: "doc", description: Text("File modifications will appear here"))
        } else {
            List {
                ForEach(fileActivities) { activity in
                    HStack(spacing: 8) {
                        Image(systemName: fileIcon(for: activity))
                            .font(.caption)
                            .foregroundStyle(fileColor(for: activity))

                        VStack(alignment: .leading, spacing: 1) {
                            Text(activity.displayName)
                                .font(.caption)
                                .fontWeight(.medium)
                            if let args = activity.arguments {
                                Text(args)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }

                        Spacer()

                        Image(systemName: activity.status.systemImage)
                            .font(.caption2)
                            .foregroundStyle(statusColor(activity.status))
                    }
                }
            }
            .listStyle(.plain)
        }
    }

    private func fileIcon(for activity: ToolActivity) -> String {
        let name = activity.name.lowercased()
        if name.contains("create") { return "doc.badge.plus" }
        if name.contains("edit") || name.contains("write") { return "pencil.line" }
        if name.contains("delete") { return "trash" }
        return "doc"
    }

    private func fileColor(for activity: ToolActivity) -> Color {
        let name = activity.name.lowercased()
        if name.contains("create") { return .green }
        if name.contains("delete") { return .red }
        return .blue
    }

    private func statusColor(_ status: ToolActivityStatus) -> Color {
        switch status {
        case .running: .orange
        case .completed: .green
        case .failed: .red
        }
    }
}

// MARK: - Terminal Output View

struct TerminalOutputView: View {
    let activities: [ToolActivity]

    var commandActivities: [ToolActivity] {
        activities.filter { activity in
            let name = activity.name.lowercased()
            return name.contains("command") || name.contains("bash") || name.contains("exec") || name.contains("run")
        }
    }

    var body: some View {
        if commandActivities.isEmpty {
            ContentUnavailableView("No Terminal Output", systemImage: "terminal", description: Text("Command executions will appear here"))
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(commandActivities) { activity in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 4) {
                                Text("$")
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.green)
                                Text(activity.arguments ?? activity.name)
                                    .font(.system(.caption, design: .monospaced))
                                    .lineLimit(3)
                                Spacer()
                                if let duration = activity.duration {
                                    Text(String(format: "%.1fs", duration))
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                        .monospacedDigit()
                                }
                            }

                            HStack(spacing: 4) {
                                Image(systemName: activity.status.systemImage)
                                    .font(.caption2)
                                Text(activity.status == .completed ? "done" : activity.status == .failed ? "failed" : "running")
                                    .font(.caption2)
                            }
                            .foregroundStyle(activity.status == .failed ? .red : .secondary)
                        }
                        .padding(8)
                        .background(Color.black.opacity(0.05))
                        .cornerRadius(6)
                    }
                }
                .padding(12)
            }
        }
    }
}
