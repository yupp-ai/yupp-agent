import SwiftUI

struct ContentView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        Group {
            if !appState.authService.isAuthenticated {
                SignInSheet()
                    .environmentObject(appState)
            } else {
                mainLayout
            }
        }
    }

    @ViewBuilder
    private var mainLayout: some View {
        NavigationSplitView {
            SidebarView()
                .environmentObject(appState)
                .environmentObject(appState.sessionsViewModel)
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 600)
        } detail: {
            HStack(spacing: 0) {
                // Main content area
                mainContent
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

                // Optional detail panel
                if appState.showDetailPanel && appState.currentView == .chat {
                    Divider()
                    DetailPanelView(chatViewModel: appState.chatViewModel)
                        .environmentObject(appState)
                }
            }
        }
        .overlay {
            // Search overlay
            if appState.showSearchOverlay {
                SearchOverlay(viewModel: appState.searchViewModel)
                    .environmentObject(appState)
            }
        }
        .sheet(isPresented: $appState.showNewSessionPicker) {
            AgentPickerSheet()
                .environmentObject(appState)
        }
    }

    @ViewBuilder
    private var mainContent: some View {
        switch appState.currentView {
        case .chat:
            ChatView(viewModel: appState.chatViewModel)
                .environmentObject(appState)
        case .projects:
            ProjectsView(viewModel: appState.projectsViewModel)
                .environmentObject(appState)
        case .schedules:
            SchedulesView(viewModel: appState.schedulesViewModel)
                .environmentObject(appState)
        case .agents:
            AgentsView(viewModel: appState.agentsViewModel)
                .environmentObject(appState)
        case .settings:
            SettingsView()
                .environmentObject(appState)
        }
    }
}

// MARK: - Agent Picker Sheet

struct AgentPickerSheet: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) var dismiss
    @State private var searchText = ""
    @State private var agents: [Agent] = []
    @State private var agentUsageCounts: [String: Int] = [:]
    @State private var isLoading = true
    @State private var loadError: String?

    var filteredAgents: [Agent] {
        var result = agents
        if !searchText.isEmpty {
            let query = searchText.lowercased()
            result = result.filter {
                $0.name.lowercased().contains(query) ||
                ($0.displayName?.lowercased().contains(query) ?? false)
            }
        }
        // Sort by usage frequency (most used first), then alphabetically
        result.sort { a, b in
            let countA = agentUsageCounts[a.name] ?? 0
            let countB = agentUsageCounts[b.name] ?? 0
            if countA != countB { return countA > countB }
            return a.name < b.name
        }
        return result
    }

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("New Session")
                    .font(.headline)

                Spacer()

                // Server indicator
                Text(appState.selectedServer.rawValue)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.quaternary)
                    .cornerRadius(4)

                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(16)

            // Search
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Search agents...", text: $searchText)
                    .textFieldStyle(.plain)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.quaternary)
            .cornerRadius(8)
            .padding(.horizontal, 16)

            Divider()
                .padding(.top, 12)

            // Agent list
            if isLoading {
                Spacer()
                ProgressView("Loading agents from \(appState.host)...")
                    .font(.caption)
                Spacer()
            } else if let error = loadError {
                Spacer()
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.title)
                        .foregroundStyle(.orange)
                    Text("Failed to load agents")
                        .font(.headline)
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 350)
                    Button("Retry") { Task { await loadAgents() } }
                        .controlSize(.small)
                }
                Spacer()
            } else if filteredAgents.isEmpty {
                Spacer()
                Text("No agents found")
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(filteredAgents) { agent in
                            HStack(spacing: 10) {
                                Image(systemName: agent.executorType == "harnessed" ? "cpu" : "terminal")
                                    .font(.title3)
                                    .foregroundStyle(agent.executorType == "harnessed" ? .green : .cyan)
                                    .frame(width: 24)

                                VStack(alignment: .leading, spacing: 2) {
                                    Text(agent.displayName ?? agent.name)
                                        .fontWeight(.medium)
                                    if let desc = agent.description {
                                        Text(desc)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                    }
                                }

                                Spacer()

                                if let count = agentUsageCounts[agent.name], count > 0 {
                                    Text("\(count)")
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                        .monospacedDigit()
                                }

                                ModelBadge(model: agent.displayModel)
                            }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                Task {
                                    await appState.createNewSession(agentId: agent.name)
                                    dismiss()
                                }
                            }
                            .onHover { hovering in
                                if hovering {
                                    NSCursor.pointingHand.push()
                                } else {
                                    NSCursor.pop()
                                }
                            }

                            Divider().padding(.leading, 50)
                        }
                    }
                }
            }
        }
        .frame(width: 500, height: 450)
        .task { await loadAgents() }
    }

    private func loadAgents() async {
        isLoading = true
        loadError = nil
        do {
            agents = try await appState.apiClient.listAgents()
            // Build usage counts from sessions already loaded in sidebar
            let sessions = appState.sessionsViewModel.sessions
            var counts: [String: Int] = [:]
            for session in sessions {
                let name = session.agentName ?? session.agentId ?? ""
                if !name.isEmpty { counts[name, default: 0] += 1 }
            }
            // If sidebar hasn't loaded yet, fetch sessions for counts
            if sessions.isEmpty {
                let uid = appState.authService.userId.isEmpty ? nil : appState.authService.userId
                if let fetched = try? await appState.apiClient.listSessions(limit: 100, userId: uid) {
                    for session in fetched {
                        let name = session.agentName ?? session.agentId ?? ""
                        if !name.isEmpty { counts[name, default: 0] += 1 }
                    }
                }
            }
            agentUsageCounts = counts
        } catch {
            loadError = error.localizedDescription
        }
        isLoading = false
    }
}
