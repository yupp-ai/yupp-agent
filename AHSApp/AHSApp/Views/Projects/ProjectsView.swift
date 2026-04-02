import SwiftUI

struct ProjectsView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: ProjectsViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Projects")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()

                Toggle("Show Completed", isOn: $viewModel.showCompleted)
                    .toggleStyle(.switch)
                    .controlSize(.small)

                Button {
                    Task { await viewModel.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh (Cmd+R)")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)

            Divider()

            HStack(spacing: 0) {
                ProjectListView(viewModel: viewModel)
                    .frame(width: 300)

                Divider()

                ProjectDetailView(viewModel: viewModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
        .task {
            await viewModel.loadIfNeeded()
        }
    }
}
