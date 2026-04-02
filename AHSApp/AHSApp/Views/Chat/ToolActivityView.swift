import SwiftUI

struct ToolActivityView: View {
    let activities: [ToolActivity]
    @State private var isExpanded = true

    var completedCount: Int { activities.filter { $0.status == .completed }.count }
    var runningCount: Int { activities.filter { $0.status == .running }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header (clickable to expand/collapse)
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "wrench.and.screwdriver")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text("Tool Activity")
                        .font(.caption)
                        .fontWeight(.medium)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("\(completedCount) done\(runningCount > 0 ? ", \(runningCount) running" : "")")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
            }
            .buttonStyle(.plain)

            if isExpanded {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(activities) { activity in
                        HStack(spacing: 6) {
                            Image(systemName: activity.status.systemImage)
                                .font(.caption2)
                                .foregroundStyle(colorForStatus(activity.status))

                            Text(activity.displayName)
                                .font(.caption)
                                .fontWeight(.medium)
                                .lineLimit(1)

                            if let args = activity.arguments {
                                Text(args)
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                            }

                            Spacer()

                            if let duration = activity.duration {
                                Text(String(format: "%.1fs", duration))
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                    .monospacedDigit()
                            } else if activity.status == .running {
                                ProgressView()
                                    .scaleEffect(0.5)
                                    .frame(width: 12, height: 12)
                            }
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 2)
                    }
                }
                .padding(.bottom, 6)
            }
        }
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.5))
        .cornerRadius(8)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color(nsColor: .separatorColor), lineWidth: 0.5)
        )
    }

    private func colorForStatus(_ status: ToolActivityStatus) -> Color {
        switch status {
        case .running: .orange
        case .completed: .green
        case .failed: .red
        }
    }
}
