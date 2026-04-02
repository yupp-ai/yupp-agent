import SwiftUI

struct SchedulesView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: SchedulesViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Schedules")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()

                Toggle("My Schedules", isOn: Binding(
                    get: { viewModel.showOnlyMine },
                    set: { newVal in
                        viewModel.showOnlyMine = newVal
                        Task { await viewModel.refresh() }
                    }
                ))
                .toggleStyle(.switch)
                .controlSize(.small)

                Button {
                    Task { await viewModel.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)

            Divider()

            HStack(spacing: 0) {
                ScheduleListView(viewModel: viewModel)
                    .frame(width: 340)

                Divider()

                ScheduleDetailView(viewModel: viewModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
        .task {
            await viewModel.loadIfNeeded()
        }
    }
}
