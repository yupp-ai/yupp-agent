import SwiftUI

struct ScheduleListView: View {
    @ObservedObject var viewModel: SchedulesViewModel

    var body: some View {
        List(selection: Binding(
            get: { viewModel.selectedSchedule?.agentScheduleId },
            set: { id in
                if let id, let schedule = viewModel.schedules.first(where: { $0.agentScheduleId == id }) {
                    viewModel.selectSchedule(schedule)
                }
            }
        )) {
            if !viewModel.recurringSchedules.isEmpty {
                Section("Recurring") {
                    ForEach(viewModel.recurringSchedules) { schedule in
                        ScheduleRow(schedule: schedule)
                            .tag(schedule.agentScheduleId)
                    }
                }
            }

            if !viewModel.oneTimeSchedules.isEmpty {
                Section("One-time") {
                    ForEach(viewModel.oneTimeSchedules) { schedule in
                        ScheduleRow(schedule: schedule)
                            .tag(schedule.agentScheduleId)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if viewModel.schedules.isEmpty && !viewModel.isLoading {
                ContentUnavailableView("No Schedules", systemImage: "clock", description: Text("No schedules found"))
            }
            if viewModel.isLoading {
                ProgressView()
            }
        }
    }
}

struct ScheduleRow: View {
    let schedule: AgentSchedule

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: schedule.status?.systemImage ?? "circle")
                    .font(.caption)
                    .foregroundStyle(statusColor)
                Text(schedule.name)
                    .fontWeight(.medium)
                    .lineLimit(1)
                Spacer()
                if let agent = schedule.agentName {
                    Text(agent)
                        .font(.caption2)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(.quaternary)
                        .cornerRadius(3)
                }
            }

            HStack(spacing: 8) {
                if let cron = schedule.cronExpression {
                    Label(cron, systemImage: "clock")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                if let nextRun = schedule.nextRunDate {
                    let formatter = RelativeDateTimeFormatter()
                    Text("Next: \(formatter.localizedString(for: nextRun, relativeTo: Date()))")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                if let count = schedule.runCount, count > 0 {
                    Text("\(count) runs")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private var statusColor: Color {
        switch schedule.status {
        case .pending: .blue
        case .paused: .orange
        case .inProgress: .green
        case .completed: .gray
        case .failed: .red
        case .cancelled: .gray
        case .none: .gray
        }
    }
}
