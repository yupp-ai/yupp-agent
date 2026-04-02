import SwiftUI

struct ScheduleDetailView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: SchedulesViewModel
    @State private var showCancelConfirm = false

    var body: some View {
        if let schedule = viewModel.selectedSchedule {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Schedule info
                    VStack(alignment: .leading, spacing: 8) {
                        Text(schedule.name)
                            .font(.title3)
                            .fontWeight(.semibold)

                        if let desc = schedule.description {
                            Text(desc)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }

                        Divider()

                        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 10) {
                            MetadataItem(label: "Type", value: schedule.scheduleType?.label ?? "Unknown")
                            MetadataItem(label: "Status", value: schedule.status?.label ?? "Unknown")
                            MetadataItem(label: "Agent", value: schedule.agentName ?? "None")

                            if let cron = schedule.cronExpression {
                                MetadataItem(label: "Cron", value: cron)
                            }
                            if let tz = schedule.cronTimezone {
                                MetadataItem(label: "Timezone", value: tz)
                            }
                            if let count = schedule.runCount {
                                if let max = schedule.maxRuns {
                                    MetadataItem(label: "Runs", value: "\(count)/\(max)")
                                } else {
                                    MetadataItem(label: "Runs", value: "\(count)")
                                }
                            }
                        }
                    }
                    .padding(16)
                    .background(Color(nsColor: .controlBackgroundColor).opacity(0.3))
                    .cornerRadius(8)

                    // Actions
                    HStack(spacing: 8) {
                        if schedule.scheduleType == .recurring && schedule.status == .pending {
                            Button("Run Now") {
                                Task { await viewModel.triggerNow() }
                            }
                            .controlSize(.small)
                        }

                        if schedule.status != .cancelled && schedule.status != .completed {
                            Button("Cancel") {
                                showCancelConfirm = true
                            }
                            .controlSize(.small)
                            .foregroundStyle(.red)
                        }

                        Spacer()
                    }

                    // Message/prompt
                    if let message = schedule.message, !message.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Message")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            Text(message)
                                .font(.body)
                                .textSelection(.enabled)
                                .padding(12)
                                .background(Color(nsColor: .controlBackgroundColor).opacity(0.3))
                                .cornerRadius(6)
                        }
                    }

                    // Past runs
                    if !viewModel.runs.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Recent Runs")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)

                            ForEach(viewModel.runs) { run in
                                RunRow(run: run)
                            }
                        }
                    }
                }
                .padding(20)
            }
            .alert("Cancel Schedule", isPresented: $showCancelConfirm) {
                Button("Cancel Schedule", role: .destructive) {
                    Task { await viewModel.cancelSchedule() }
                }
                Button("Keep", role: .cancel) {}
            } message: {
                Text("Are you sure you want to cancel \"\(schedule.name)\"?")
            }
        } else {
            ContentUnavailableView("Select a Schedule", systemImage: "clock", description: Text("Choose a schedule from the list"))
        }
    }
}

struct RunRow: View {
    let run: ScheduleRun

    var body: some View {
        HStack(spacing: 8) {
            Text("#\(run.runNumber)")
                .font(.caption)
                .fontWeight(.medium)
                .monospacedDigit()

            Image(systemName: runIcon)
                .font(.caption)
                .foregroundStyle(runColor)

            if let started = run.startedAt {
                let date = ISO8601DateFormatter.flexible.date(from: started)
                if let date {
                    Text(date, style: .relative)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if let duration = run.duration {
                Text(duration)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
            }

            Spacer()

            if let error = run.error {
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color(nsColor: .controlBackgroundColor).opacity(0.3))
        .cornerRadius(6)
    }

    private var runIcon: String {
        switch run.status {
        case "COMPLETED": "checkmark.circle.fill"
        case "FAILED": "exclamationmark.circle.fill"
        case "IN_PROGRESS": "circle.dotted.circle"
        default: "circle"
        }
    }

    private var runColor: Color {
        switch run.status {
        case "COMPLETED": .green
        case "FAILED": .red
        case "IN_PROGRESS": .blue
        default: .gray
        }
    }
}
