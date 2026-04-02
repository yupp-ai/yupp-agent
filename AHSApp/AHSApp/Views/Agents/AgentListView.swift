import SwiftUI

struct AgentListView: View {
    @ObservedObject var viewModel: AgentsViewModel

    var body: some View {
        List(viewModel.filteredAgents, selection: Binding(
            get: { viewModel.selectedAgent?.name },
            set: { name in
                if let name, let agent = viewModel.agents.first(where: { $0.name == name }) {
                    viewModel.selectAgent(agent)
                }
            }
        )) { agent in
            AgentRow(agent: agent)
                .tag(agent.name)
        }
        .listStyle(.sidebar)
        .overlay {
            if viewModel.filteredAgents.isEmpty && !viewModel.isLoading {
                ContentUnavailableView("No Agents", systemImage: "cpu", description: Text("No agents found"))
            }
            if viewModel.isLoading {
                ProgressView()
            }
        }
    }
}

struct AgentRow: View {
    let agent: Agent

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: agent.executorType == "harnessed" ? "cpu" : "terminal")
                    .font(.caption)
                    .foregroundStyle(agent.executorType == "harnessed" ? .green : .cyan)
                Text(agent.displayName ?? agent.name)
                    .fontWeight(.medium)
                    .lineLimit(1)
            }

            HStack(spacing: 6) {
                ModelBadge(model: agent.displayModel)

                Text(agent.executorType ?? "unknown")
                    .font(.caption2)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.quaternary)
                    .cornerRadius(3)

                Spacer()

                Text("\(agent.toolCount) tools")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let desc = agent.description {
                Text(desc)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }
}
