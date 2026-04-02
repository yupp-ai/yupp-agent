import SwiftUI

struct AgentsView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: AgentsViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Agents")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()

                // Filter
                HStack(spacing: 4) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Filter agents...", text: $viewModel.filterText)
                        .textFieldStyle(.plain)
                        .frame(width: 150)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(.quaternary)
                .cornerRadius(6)

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
                AgentListView(viewModel: viewModel)
                    .frame(width: 300)

                Divider()

                AgentDetailView(viewModel: viewModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
        .task {
            await viewModel.loadIfNeeded()
        }
    }
}
