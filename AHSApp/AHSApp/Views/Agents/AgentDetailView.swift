import SwiftUI

struct AgentDetailView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: AgentsViewModel
    @State private var expandedPrompt: String?

    var body: some View {
        if let agent = viewModel.detailedAgent ?? viewModel.selectedAgent {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // Agent header
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Image(systemName: agent.executorType == "harnessed" ? "cpu" : "terminal")
                                .font(.title3)
                                .foregroundStyle(agent.executorType == "harnessed" ? .green : .cyan)
                            Text(agent.displayName ?? agent.name)
                                .font(.title3)
                                .fontWeight(.semibold)
                            Spacer()

                            Button("New Session") {
                                Task { await appState.createNewSession(agentId: agent.name) }
                            }
                            .controlSize(.small)
                        }

                        if let desc = agent.description {
                            Text(desc)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }

                    Divider()

                    // Configuration
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 10) {
                        MetadataItem(label: "Executor", value: agent.executorType ?? "unknown")
                        MetadataItem(label: "Model", value: agent.displayModel)
                        if let repo = agent.defaultRepo {
                            MetadataItem(label: "Repo", value: repo)
                        }
                        if let turns = agent.maxTurns {
                            MetadataItem(label: "Max Turns", value: "\(turns)")
                        }
                        if let budget = agent.maxBudgetUsd {
                            MetadataItem(label: "Budget", value: String(format: "$%.0f", budget))
                        }
                        if let timeout = agent.timeoutS {
                            MetadataItem(label: "Timeout", value: "\(timeout)s")
                        }
                        MetadataItem(label: "Sandbox", value: agent.sandboxEnabled == true ? "Enabled" : "Disabled")
                        MetadataItem(label: "Tools", value: agent.toolCount)
                        MetadataItem(label: "Subagents", value: agent.subagentCount)
                    }

                    // Tool permissions
                    if let perms = agent.toolPermissions, !perms.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Tool Permissions")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)

                            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 4) {
                                ForEach(perms.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                                    HStack(spacing: 4) {
                                        Image(systemName: value == "allow" ? "checkmark.circle.fill" : "xmark.circle")
                                            .font(.caption2)
                                            .foregroundStyle(value == "allow" ? .green : .red)
                                        Text(key)
                                            .font(.caption)
                                            .lineLimit(1)
                                    }
                                }
                            }
                        }
                    }

                    // Allowed subagents
                    if let subs = agent.allowedSubagents, !subs.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Allowed Subagents")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            Text(subs.joined(separator: ", "))
                                .font(.caption)
                        }
                    }

                    // System prompts
                    if let prompts = agent.systemPrompts, !prompts.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("System Prompts (\(prompts.count))")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)

                            ForEach(prompts.sorted(by: { $0.key < $1.key }), id: \.key) { name, content in
                                DisclosureGroup(
                                    isExpanded: Binding(
                                        get: { expandedPrompt == name },
                                        set: { if $0 { expandedPrompt = name } else { expandedPrompt = nil } }
                                    )
                                ) {
                                    Text(content)
                                        .font(.system(.caption, design: .monospaced))
                                        .textSelection(.enabled)
                                        .padding(8)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .background(Color(nsColor: .controlBackgroundColor))
                                        .cornerRadius(6)
                                } label: {
                                    HStack {
                                        Image(systemName: "doc.text")
                                            .font(.caption)
                                        Text(name)
                                            .font(.caption)
                                            .fontWeight(.medium)
                                        Spacer()
                                        Text("\(content.components(separatedBy: "\n").count) lines")
                                            .font(.caption2)
                                            .foregroundStyle(.tertiary)
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(20)
            }
        } else {
            ContentUnavailableView("Select an Agent", systemImage: "cpu", description: Text("Choose an agent from the list"))
        }
    }
}
