import SwiftUI

struct ActivityTimelineView: View {
    let activities: [ToolActivity]

    var body: some View {
        if activities.isEmpty {
            ContentUnavailableView("No Activity", systemImage: "clock", description: Text("Agent activity will appear here"))
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(activities) { activity in
                        HStack(alignment: .top, spacing: 8) {
                            // Timeline dot + line
                            VStack(spacing: 0) {
                                Circle()
                                    .fill(colorForStatus(activity.status))
                                    .frame(width: 8, height: 8)
                                    .padding(.top, 4)
                                Rectangle()
                                    .fill(Color(nsColor: .separatorColor))
                                    .frame(width: 1)
                            }
                            .frame(width: 8)

                            // Content
                            VStack(alignment: .leading, spacing: 2) {
                                HStack {
                                    Text(activity.name)
                                        .font(.caption)
                                        .fontWeight(.medium)
                                    if let server = activity.server {
                                        Text("(\(server))")
                                            .font(.caption2)
                                            .foregroundStyle(.tertiary)
                                    }
                                    Spacer()
                                    Text(activity.startTime, style: .time)
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                }

                                if let args = activity.arguments, !args.isEmpty {
                                    Text(args)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                }

                                HStack(spacing: 4) {
                                    Image(systemName: activity.status.systemImage)
                                        .font(.caption2)
                                        .foregroundStyle(colorForStatus(activity.status))
                                    if let duration = activity.duration {
                                        Text(String(format: "%.1fs", duration))
                                            .font(.caption2)
                                            .foregroundStyle(.tertiary)
                                            .monospacedDigit()
                                    } else if activity.status == .running {
                                        Text("running...")
                                            .font(.caption2)
                                            .foregroundStyle(.orange)
                                    }
                                }
                            }
                            .padding(.vertical, 6)
                        }
                        .padding(.horizontal, 12)
                    }
                }
            }
        }
    }

    private func colorForStatus(_ status: ToolActivityStatus) -> Color {
        switch status {
        case .running: .orange
        case .completed: .green
        case .failed: .red
        }
    }
}
